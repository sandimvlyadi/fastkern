"""Async SQLAlchemy engine/session management."""

import socket
from collections.abc import AsyncGenerator

from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings

# Failures to *reach* the database, as opposed to it rejecting a statement: what a background
# write retries on (`autoretry_for=`). asyncpg raises the socket-level errors itself, hence the
# builtins. A bad statement (IntegrityError, DataError, ...) is a bug and is never retried.
TRANSIENT_DB_ERRORS: tuple[type[Exception], ...] = (
    ConnectionError,
    TimeoutError,
    socket.gaierror,
    sa_exc.OperationalError,
    sa_exc.InterfaceError,
    sa_exc.TimeoutError,
)


def create_engine(settings: Settings, *, pooled: bool = True) -> AsyncEngine:
    """`pooled=False` is for a short-lived engine (e.g. one per Celery task
    invocation): no pool is kept, so each connection is opened and closed with
    the work that used it and nothing outlives the event loop it ran on."""
    connect_args: dict[str, object] = {}
    engine_kwargs: dict[str, object] = {"echo": settings.database_echo, "future": True}

    if settings.database_url.startswith("sqlite"):
        # SQLite (used only in tests) has no real pool and needs check_same_thread off.
        connect_args["check_same_thread"] = False
    elif pooled:
        engine_kwargs["pool_size"] = settings.database_pool_size
        engine_kwargs["max_overflow"] = settings.database_max_overflow
        engine_kwargs["pool_pre_ping"] = True
    else:
        engine_kwargs["poolclass"] = NullPool

    return create_async_engine(settings.database_url, connect_args=connect_args, **engine_kwargs)


_engine: AsyncEngine = create_engine(get_settings())
_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=_engine, expire_on_commit=False, autoflush=False
)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return _session_factory


def get_engine() -> AsyncEngine:
    return _engine


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency yielding a session scoped to a single request.

    Always depend on it as `Depends(get_db_session, scope="function")`. The default scope runs
    the code after `yield` (the commit) only once the response has been sent, so a client could
    get a 201 for a write that is not committed yet, or never is if the commit fails. Mixing the
    two forms also gives one request two separate sessions. Enforced by
    `tests/unit/test_db_session_scope.py`."""
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
