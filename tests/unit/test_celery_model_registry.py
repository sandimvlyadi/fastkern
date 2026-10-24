import subprocess
import sys


def test_celery_app_registers_every_table_it_may_write_to() -> None:
    """Regression: a worker imports only celery_app and its task modules. Every foreign key
    target (sys_tenants, for example) must be registered by then, or the first insert that
    references it fails in the worker. A fresh interpreter is required, because pytest has
    already imported every model."""
    code = (
        "import app.core.celery_app\n"
        "from app.shared.infrastructure.orm_base import Base\n"
        "required = {'sys_tenants', 'sys_users', 'trans_files', 'trans_file_variants'}\n"
        "print(sorted(required - set(Base.metadata.tables)))\n"
    )
    # The code string is a constant in this file, not external input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
