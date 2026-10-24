from app.core.exceptions import (
    AlreadyExistsError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationAppError,
)


class UserNotFoundError(NotFoundError):
    code = "user_not_found"
    key = "user.not_found"
    message = "User not found."


class IncorrectCurrentPasswordError(ForbiddenError):
    """403, not 401: the bearer token is valid and authenticated — it's the current-password
    proof (a business rule, not an auth check) that failed. A 401 here would get conflated with
    an invalid/expired token and could trigger a client's forced-logout handling."""

    code = "incorrect_current_password"
    key = "user.incorrect_current_password"
    message = "The current password you entered is incorrect."


class CannotModifySelfError(ConflictError):
    """Deactivating or deleting your own account would lock you out of the system."""

    code = "cannot_modify_self"
    key = "user.cannot_modify_self"
    message = "You cannot deactivate or delete your own account."


class EmailAlreadyRegisteredError(AlreadyExistsError):
    code = "email_already_registered"
    key = "user.email_already_registered"
    message = "This email is already registered."


class UserHasNoAvatarError(NotFoundError):
    code = "user_has_no_avatar"
    key = "user.no_avatar"
    message = "This user has no avatar to remove."


class AvatarMustBeImageError(ValidationAppError):
    code = "avatar_must_be_image"
    key = "user.avatar_must_be_image"
    message = "The avatar must be an image file (JPEG, PNG, WEBP or GIF)."


class AvatarTooLargeError(PayloadTooLargeError):
    code = "avatar_too_large"
    key = "user.avatar_too_large"
    message = "The avatar image is larger than the maximum allowed size."


class AvatarChangeInProgressError(ConflictError):
    code = "avatar_change_in_progress"
    key = "user.avatar_change_in_progress"
    message = "The previous avatar is still being processed. Try again shortly."
