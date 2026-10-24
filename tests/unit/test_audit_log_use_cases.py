from uuid import uuid4

import pytest

from app.core.pagination import OffsetParams
from app.modules.audit_log.application.dto import RecordAuditEventInput
from app.modules.audit_log.application.use_cases import (
    ListAuditLogsUseCase,
    RecordAuditEventUseCase,
)
from app.modules.audit_log.infrastructure import tasks as audit_tasks
from tests.fakes import InMemoryAuditLogRepository


async def test_record_audit_event_copies_all_fields() -> None:
    audit_logs = InMemoryAuditLogRepository()
    tenant_id, actor_id = uuid4(), uuid4()
    data = RecordAuditEventInput(
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="user.updated",
        resource_type="user",
        resource_id="abc",
        before={"name": "old"},
        after={"name": "new"},
        request_id="req-1",
        ip_address="1.2.3.4",
    )

    entry = await RecordAuditEventUseCase(audit_logs).execute(data)

    assert entry.tenant_id == tenant_id
    assert entry.actor_id == actor_id
    assert entry.action == "user.updated"
    assert entry.resource_type == "user"
    assert entry.resource_id == "abc"
    assert entry.before == {"name": "old"}
    assert entry.after == {"name": "new"}
    assert entry.request_id == "req-1"
    assert entry.ip_address == "1.2.3.4"
    assert entry in audit_logs.records


async def test_list_audit_logs_passes_filters_through() -> None:
    audit_logs = InMemoryAuditLogRepository()
    actor_id = uuid4()

    await ListAuditLogsUseCase(audit_logs).execute(
        OffsetParams(), resource_type="user", actor_id=actor_id
    )

    assert audit_logs.last_paginate_kwargs == {"resource_type": "user", "actor_id": actor_id}


def test_record_audit_event_task_delegates_to_record(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_record(payload: dict[str, object]) -> None:
        captured.update(payload)

    monkeypatch.setattr(audit_tasks, "_record", fake_record)

    audit_tasks.record_audit_event_task(
        tenant_id=None,
        actor_id=None,
        action="tenant.created",
        resource_type="tenant",
        resource_id="1",
    )

    assert captured["action"] == "tenant.created"
    assert captured["resource_type"] == "tenant"
