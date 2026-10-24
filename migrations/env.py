import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings

# Import every module's ORM models so their tables register on Base.metadata
# before autogenerate diffs it against the database. Add new modules here.
from app.modules.api_clients.infrastructure import models as api_clients_models  # noqa: F401
from app.modules.audit_log.infrastructure import models as audit_log_models  # noqa: F401
from app.modules.examples.infrastructure import models as examples_models  # noqa: F401
from app.modules.files.infrastructure import models as files_models  # noqa: F401
from app.modules.master_examples.infrastructure import (
    models as master_examples_models,  # noqa: F401
)
from app.modules.roles.infrastructure import models as roles_models  # noqa: F401
from app.modules.tenants.infrastructure import models as tenants_models  # noqa: F401
from app.modules.users.infrastructure import models as users_models  # noqa: F401
from app.shared.infrastructure.orm_base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
