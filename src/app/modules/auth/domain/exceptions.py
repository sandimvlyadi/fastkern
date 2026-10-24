from app.core.exceptions import ForbiddenError, UnauthorizedError


class InvalidCredentialsError(UnauthorizedError):
    code = "invalid_credentials"
    key = "auth.invalid_credentials"
    message = "Invalid email or password."


class AccountDisabledError(ForbiddenError):
    """403, not 401: the credentials were right, the account is what's refused — so "fix your
    password / retry" (401) is the wrong signal and "contact an administrator" the right one."""

    code = "account_disabled"
    key = "auth.account_disabled"
    message = "This account has been disabled."


class InvalidTokenError(UnauthorizedError):
    code = "invalid_token"
    key = "auth.invalid_token"
    message = "Token is invalid or has expired."


class TokenRevokedError(UnauthorizedError):
    code = "token_revoked"
    key = "auth.token_revoked"
    message = "This token has been revoked."
