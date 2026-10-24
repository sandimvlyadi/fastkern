"""Identifier generation for entities and ORM primary keys.

Primary keys are UUIDv7 (RFC 9562), not v4: the leading 48 bits are a
millisecond timestamp, so new rows land at the right-hand edge of the PK
b-tree instead of at random pages. Random v4 keys fragment the index and
evict hot pages as a table grows. The type stays `uuid.UUID`, so nothing
downstream (schemas, JWT claims, FKs) changes.

Stdlib only (`uuid.uuid7()`, Python 3.14+), so the domain layer may use it.
Call sites import this wrapper rather than `uuid.uuid7` directly, so the
choice of generator stays in one place.
"""

import uuid


def uuid7() -> uuid.UUID:
    """Time-ordered UUID: 48-bit unix-ms timestamp, version 7, RFC 9562 variant.

    The stdlib generator also keeps ids minted within the same millisecond
    monotonic within this process (RFC 9562 method 1, a 42-bit counter).
    """
    return uuid.uuid7()
