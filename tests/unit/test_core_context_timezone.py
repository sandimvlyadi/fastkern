from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.core.context import (
    RequestContext,
    get_request_context,
    reset_request_context,
    set_request_context,
    update_request_context,
)
from app.core.timezone import to_user_timezone


def test_default_context() -> None:
    ctx = get_request_context()
    assert ctx.request_id == "-"
    assert ctx.tenant_id is None
    assert ctx.user_id is None
    assert ctx.active_role is None
    assert ctx.locale == "en"
    assert ctx.timezone == ZoneInfo("UTC")


def test_set_then_get_context() -> None:
    tenant_id = uuid4()
    context = RequestContext(
        request_id="req-1",
        tenant_id=tenant_id,
        user_id=None,
        active_role=None,
        locale="id",
        timezone=ZoneInfo("UTC"),
    )
    set_request_context(context)
    assert get_request_context() == context


def test_update_request_context_merges_changes() -> None:
    user_id = uuid4()
    updated = update_request_context(user_id=user_id, active_role="admin")
    assert updated.user_id == user_id
    assert updated.active_role == "admin"
    assert updated.request_id == "-"  # untouched fields carried over
    assert get_request_context() == updated


def test_reset_request_context_restores_default() -> None:
    update_request_context(user_id=uuid4())
    reset_request_context()
    assert get_request_context() == get_request_context()
    assert get_request_context().user_id is None


def test_to_user_timezone_naive_datetime_treated_as_utc() -> None:
    naive = datetime(2024, 1, 1, 12, 0, 0)
    converted = to_user_timezone(naive)
    assert converted == datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_to_user_timezone_aware_datetime_converted_to_context_timezone() -> None:
    update_request_context(timezone=ZoneInfo("Asia/Jakarta"))
    aware = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    converted = to_user_timezone(aware)
    assert converted.utcoffset() is not None
    assert converted.utcoffset().total_seconds() == 7 * 3600
    assert converted.hour == 19


def test_to_user_timezone_default_context_is_utc() -> None:
    naive = datetime(2024, 6, 1, 0, 0, 0)
    converted = to_user_timezone(naive)
    assert converted.tzinfo == ZoneInfo("UTC")
