"""Celery entry points of the files module, called directly.

Plain (non-async) tests on purpose: the tasks run their own `asyncio.run()`, which cannot nest
inside the pytest-asyncio loop. They use the test database and a temporary storage root.
"""

from pathlib import Path
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.modules.files.application.pipeline import FileNotYetVisibleError
from app.modules.files.infrastructure import tasks


def _temp_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = get_settings().model_copy(update={"storage_root": str(tmp_path / "data")})
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)


def test_cleanup_task_reports_counts_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _temp_settings(monkeypatch, tmp_path)
    first = tasks.cleanup_files_task()
    assert set(first) == {
        "expired_sessions",
        "purged_upload_dirs",
        "purged_file_trees",
        "failed_stale_files",
    }
    second = tasks.cleanup_files_task()
    assert second["expired_sessions"] == 0  # nothing new to expire on the second run


def test_process_task_asks_for_a_retry_while_the_file_is_not_committed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _temp_settings(monkeypatch, tmp_path)
    # Outside a worker, Celery re-raises the original exception instead of scheduling a retry.
    with pytest.raises(FileNotYetVisibleError):
        tasks.process_upload_task(upload_id=str(uuid4()), file_id=str(uuid4()))
