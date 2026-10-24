"""Send notifications from a Celery worker so an API request never blocks
on (or fails because of) a slow/unreliable third-party email provider."""

import asyncio

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.logging import get_logger
from app.modules.notifications.application.use_cases import SendWelcomeEmailUseCase
from app.modules.notifications.domain.exceptions import NotificationDeliveryError
from app.modules.notifications.infrastructure.factory import build_notification_sender
from app.modules.notifications.infrastructure.masking import mask_email

logger = get_logger(__name__)


async def _send_welcome_email(*, to: str, full_name: str, locale: str) -> None:
    # Built per invocation: this task runs under its own `asyncio.run()` (a plain sync Celery
    # task), and the adapters hold nothing that may outlive that loop.
    sender = build_notification_sender(get_settings())
    await SendWelcomeEmailUseCase(sender).execute(to=to, full_name=full_name, locale=locale)
    logger.info("welcome_email_sent", to=mask_email(to))


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="notifications.send_welcome_email",
    # Retry only *transient* delivery failures (see NotificationDeliveryError); a permanent one
    # (NotificationRejectedError) or a bug fails the task at once instead of being retried.
    autoretry_for=(NotificationDeliveryError,),
    retry_backoff=True,  # 1s, 2s, 4s, ... between attempts
    retry_backoff_max=300,
    retry_jitter=True,  # spread retries so a mail-server blip doesn't get a thundering herd
    max_retries=3,
    # Ack after the task finishes: a worker that dies mid-send gets the message redelivered.
    acks_late=True,
)
def send_welcome_email_task(self: object, *, to: str, full_name: str, locale: str = "en") -> None:
    asyncio.run(_send_welcome_email(to=to, full_name=full_name, locale=locale))
