"""Background DB writes retry a dropped connection, never a bad statement (a bug)."""

from typing import Any

import pytest

from app.core.db import TRANSIENT_DB_ERRORS
from app.modules.api_clients.infrastructure.tasks import record_request_log_task
from app.modules.audit_log.infrastructure.tasks import record_audit_event_task


@pytest.mark.parametrize("task", [record_request_log_task, record_audit_event_task])
def test_background_db_writes_retry_only_on_transient_db_errors(task: Any) -> None:
    assert tuple(task.autoretry_for) == TRANSIENT_DB_ERRORS
    assert task.max_retries == 3
