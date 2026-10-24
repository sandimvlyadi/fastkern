"""Celery task that reconciles a just-uploaded avatar once `files.process_upload` finishes:
undoes the swap if the upload didn't turn out to be a usable image. Registered in
`core/celery_app.py` via `include=`.

Uses its own unpooled DB engine (see `files/infrastructure/tasks.py`): a task runs under its own
`asyncio.run()`, so it cannot share the shared engine in `app.core.db`, whose asyncpg pool is
bound to one event loop.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.db import create_engine
from app.core.logging import get_logger
from app.modules.files.domain.state import STALE_FILE_STATUSES, FileStatus
from app.modules.files.domain.value_objects import FileKind
from app.modules.files.infrastructure.repository import FileRepository
from app.modules.users.infrastructure.repository import UserRepository

logger = get_logger(__name__)


class _AvatarNotYetTerminalError(Exception):
    """The linked file is still UPLOADED/SCANNING/PROCESSING. Retry shortly."""


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="users.finalize_avatar",
    max_retries=30,
    acks_late=True,
)
def finalize_avatar_task(self: Any, *, user_id: str, file_id: str) -> str:
    try:
        return asyncio.run(_finalize(UUID(user_id), UUID(file_id)))
    except _AvatarNotYetTerminalError as exc:
        raise self.retry(exc=exc, countdown=5) from exc


async def _finalize(user_id: UUID, file_id: UUID) -> str:
    settings = get_settings()
    engine = create_engine(settings, pooled=False)
    try:
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        async with session_factory() as session:
            files = FileRepository(session)
            asset = await files.get_file_unscoped(file_id)
            if asset is None or asset.status in STALE_FILE_STATUSES:
                raise _AvatarNotYetTerminalError()
            if asset.status is FileStatus.AVAILABLE and asset.kind is FileKind.IMAGE:
                logger.info("avatar_finalized_ok", file_id=str(file_id))
                return "ok"

            users = UserRepository(session)
            user = await users.get_by_id(user_id, tenant_scoped=False)
            if user is not None and user.avatar_file_id == file_id:
                # Only clear the field if it still points at THIS upload: a second, newer avatar
                # change may already be in flight for the same user.
                user.avatar_file_id = None
                await users.save(user, tenant_scoped=False)
            deleted = True
            if asset.status is FileStatus.AVAILABLE:  # valid file, just not an image
                deleted = await files.transition_file(
                    file_id,
                    FileStatus.AVAILABLE,
                    FileStatus.DELETED,
                    deleted_at=datetime.now(UTC),
                )
            await session.commit()
            if not deleted:
                # Lost a race against a concurrent transition (e.g. the user/an admin already
                # removed this avatar another way) — the file is already gone one way or
                # another, so this is informational, not an error.
                logger.info(
                    "avatar_already_transitioned", file_id=str(file_id), status=asset.status.value
                )
            logger.warning(
                "avatar_rejected",
                file_id=str(file_id),
                status=asset.status.value,
                kind=asset.kind.value,
            )
            return f"rejected:{asset.status.value}:{asset.kind.value}"
    finally:
        await engine.dispose()
