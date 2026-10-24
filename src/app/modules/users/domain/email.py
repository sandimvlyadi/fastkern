"""Canonical form of an email address, shared by every place that stores or looks one up."""


def normalize_email(email: str) -> str:
    """Trimmed and lowercased.

    `EmailStr` only normalises the *domain*; the local part is technically case-sensitive but no
    mainstream provider treats it so, and treating `Jane@x.io` / `jane@x.io` as two accounts is a
    bug for a login identity. `sys_users` enforces the result with `ck_sys_users_email_lower`.
    """
    return email.strip().lower()
