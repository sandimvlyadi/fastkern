"""Keeps recipient addresses out of logs in full."""


def mask_email(address: str) -> str:
    """`jane.doe@example.com` -> `j***@example.com`.

    Enough to correlate a log line with a user when debugging, not enough to harvest addresses
    from a log aggregator. Anything that isn't shaped like an address is fully masked."""
    local, at, domain = address.partition("@")
    if not at or not local or not domain:
        return "***"
    return f"{local[0]}***@{domain}"
