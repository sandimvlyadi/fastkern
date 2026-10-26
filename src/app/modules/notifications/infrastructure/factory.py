"""Picks the `NotificationSenderPort` implementation for the configured `EMAIL_BACKEND`."""

from app.core.config import Settings
from app.core.logging import get_logger
from app.modules.notifications.domain.ports import NotificationSenderPort
from app.modules.notifications.infrastructure.console_adapter import ConsoleEmailAdapter
from app.modules.notifications.infrastructure.email_adapter import HttpEmailAdapter
from app.modules.notifications.infrastructure.smtp_adapter import SmtpEmailAdapter

logger = get_logger(__name__)


def warn_if_email_is_log_only(settings: Settings) -> None:
    """Production may run the console backend when EMAIL_CONSOLE_IN_PRODUCTION accepts it. No
    mail reaches anyone then, so that must be visible in the logs."""
    if settings.is_production and settings.email_backend == "console":
        logger.warning(
            "email_not_delivered",
            hint="set EMAIL_BACKEND=smtp or http with the provider's credentials",
        )


def build_notification_sender(settings: Settings) -> NotificationSenderPort:
    if settings.email_backend == "smtp":
        return SmtpEmailAdapter(
            host=settings.smtp_host,
            port=settings.smtp_port,
            sender=settings.email_from,
            username=settings.smtp_username,
            password=settings.smtp_password.get_secret_value() if settings.smtp_password else None,
            starttls=settings.smtp_starttls,
            use_ssl=settings.smtp_ssl,
            timeout_seconds=settings.smtp_timeout_seconds,
        )
    if settings.email_backend == "http":
        if not (settings.email_http_base_url and settings.email_http_api_key):
            raise ValueError("EMAIL_BACKEND=http needs EMAIL_HTTP_BASE_URL and EMAIL_HTTP_API_KEY")
        return HttpEmailAdapter(
            base_url=settings.email_http_base_url,
            api_key=settings.email_http_api_key.get_secret_value(),
        )
    return ConsoleEmailAdapter()
