"""Celery tasks for the files module. Registered in `core/celery_app.py` via `include=`.

Both tasks run under their own `asyncio.run()`, so they use a task-local unpooled engine instead of
the shared one in `app.core.db` (see audit_log/infrastructure/tasks.py for why).
"""

import asyncio
from dataclasses import asdict
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.db import create_engine
from app.core.logging import get_logger
from app.modules.files.application.pipeline import FileNotYetVisibleError, ProcessUploadUseCase
from app.modules.files.application.use_cases import CleanupFilesUseCase
from app.modules.files.infrastructure.factory import (
    build_image_options,
    build_image_processor,
    build_malware_scanner,
    build_object_storage,
    work_dir,
)
from app.modules.files.infrastructure.repository import (
    ChunkRepository,
    FileRepository,
    UploadRepository,
)

logger = get_logger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="files.process_upload",
    max_retries=10,
    acks_late=True,
)
def process_upload_task(self: Any, *, upload_id: str, file_id: str) -> str:
    try:
        return asyncio.run(_process(UUID(upload_id), UUID(file_id)))
    except FileNotYetVisibleError as exc:
        # Dispatched before the completing request committed; retry shortly.
        raise self.retry(exc=exc, countdown=2) from exc


async def _process(upload_id: UUID, file_id: UUID) -> str:
    settings = get_settings()
    engine = create_engine(settings, pooled=False)
    try:
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        async with session_factory() as session:
            use_case = ProcessUploadUseCase(
                UploadRepository(session),
                ChunkRepository(session),
                FileRepository(session),
                build_object_storage(settings),
                build_malware_scanner(settings),
                build_image_processor(),
                build_image_options(settings),
                work_dir(settings),
            )
            try:
                status = await use_case.execute(upload_id, file_id)
            except FileNotYetVisibleError:
                raise
            except Exception:
                await session.commit()  # keep the FAILED state the use case recorded
                raise
            await session.commit()
            logger.info("file_processed", file_id=str(file_id), status=status)
            return status
    finally:
        await engine.dispose()


@celery_app.task(name="files.cleanup")  # type: ignore[untyped-decorator]
def cleanup_files_task() -> dict[str, int]:
    return asyncio.run(_cleanup())


async def _cleanup() -> dict[str, int]:
    settings = get_settings()
    engine = create_engine(settings, pooled=False)
    try:
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        async with session_factory() as session:
            report = await CleanupFilesUseCase(
                UploadRepository(session), FileRepository(session), build_object_storage(settings)
            ).execute()
            await session.commit()
            logger.info("files_cleanup_done", **asdict(report))
            return asdict(report)
    finally:
        await engine.dispose()
