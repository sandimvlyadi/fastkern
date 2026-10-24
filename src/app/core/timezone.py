"""Convention: store and transmit datetimes in UTC everywhere (DB columns,
JSON responses). This helper exists for the few places that must render a
LOCAL time for a human (PDF/email templates, CSV exports, admin UI copy
generated server-side) — call it explicitly at that edge, never in a
repository or use case.
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.context import get_request_context


def parse_timezone(key: str) -> ZoneInfo | None:
    """`ZoneInfo(key)`, or None when `key` is not a usable IANA zone name.

    For input that isn't ours to trust (the `X-Timezone` header, a stored preference). An
    unknown name raises `ZoneInfoNotFoundError`, but a path-shaped key (`../../etc/passwd`) or one
    with a NUL raises a bare `ValueError`, and an over-long one can raise `OSError`: catching only
    the first turned an unauthenticated header into a 500 on every endpoint."""
    try:
        return ZoneInfo(key)
    except ZoneInfoNotFoundError, ValueError, OSError:
        return None


def to_user_timezone(dt: datetime) -> datetime:
    tz = get_request_context().timezone
    if dt.tzinfo is None:
        from datetime import UTC

        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz)
