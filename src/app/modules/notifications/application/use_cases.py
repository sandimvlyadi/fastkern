from app.core.i18n import translate
from app.modules.notifications.domain.ports import NotificationSenderPort


class SendWelcomeEmailUseCase:
    def __init__(self, sender: NotificationSenderPort) -> None:
        self._sender = sender

    async def execute(self, *, to: str, full_name: str, locale: str = "en") -> None:
        # Runs outside any HTTP request, so the caller must pass the recipient's own
        # locale explicitly instead of reading it from request context.
        subject = translate("notification.welcome.subject", locale, default="Welcome!")
        body = translate(
            "notification.welcome.body",
            locale,
            default=f"Hi {full_name}, your account has been created.",
            full_name=full_name,
        )
        await self._sender.send_email(to=to, subject=subject, body=body)
