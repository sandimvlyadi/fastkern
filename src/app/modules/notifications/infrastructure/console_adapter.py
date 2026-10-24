"""Development/test sender: logs that a message *would* have been sent, sends nothing.

Production refuses this backend (see `Settings._validate_email_settings`). It logs the masked
recipient and the subject only — never the body, which can carry links or tokens."""

from app.core.logging import get_logger
from app.modules.notifications.domain.ports import NotificationSenderPort
from app.modules.notifications.infrastructure.masking import mask_email

logger = get_logger(__name__)


class ConsoleEmailAdapter(NotificationSenderPort):
    async def send_email(self, *, to: str, subject: str, body: str) -> None:
        logger.info("email_console_delivery", to=mask_email(to), subject=subject)
