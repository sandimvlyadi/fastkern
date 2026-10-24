"""SMTP delivery with the standard library (no new dependency).

`smtplib` is blocking, so the whole conversation runs in a worker thread via
`asyncio.to_thread`. Failures are sorted into two classes the delivery task treats differently:

* transient -> `NotificationDeliveryError` (retried): connection refused / reset / DNS failure,
  timeouts, the server hanging up, or a 4xx reply ("try again later");
* permanent -> `NotificationRejectedError` (not retried): 5xx replies, bad credentials, every
  recipient refused, or a server that lacks a required capability.

Nothing sensitive is logged: no body, no password, and the recipient is masked.
"""

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from app.core.logging import get_logger
from app.modules.notifications.domain.exceptions import (
    NotificationDeliveryError,
    NotificationRejectedError,
)
from app.modules.notifications.domain.ports import NotificationSenderPort
from app.modules.notifications.infrastructure.masking import mask_email

logger = get_logger(__name__)

_TRANSIENT_SMTP_CODES = range(400, 500)


class SmtpEmailAdapter(NotificationSenderPort):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = False,
        use_ssl: bool = False,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._username = username
        self._password = password
        self._starttls = starttls
        self._use_ssl = use_ssl
        self._timeout = timeout_seconds

    async def send_email(self, *, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)

        try:
            await asyncio.to_thread(self._deliver, message)
        except smtplib.SMTPRecipientsRefused as exc:
            codes = [code for code, _ in exc.recipients.values()]
            self._fail(to, transient=all(code in _TRANSIENT_SMTP_CODES for code in codes), exc=exc)
        except smtplib.SMTPResponseException as exc:
            self._fail(to, transient=exc.smtp_code in _TRANSIENT_SMTP_CODES, exc=exc)
        except smtplib.SMTPServerDisconnected as exc:
            self._fail(to, transient=True, exc=exc)
        except smtplib.SMTPException as exc:
            # e.g. the server doesn't offer STARTTLS/AUTH we were told to use: a config problem.
            self._fail(to, transient=False, exc=exc)
        except (TimeoutError, OSError) as exc:
            # After the SMTP-specific clauses (SMTPException is itself an OSError): what remains
            # is the network — refused, reset, unreachable, DNS.
            self._fail(to, transient=True, exc=exc)

    def _deliver(self, message: EmailMessage) -> None:
        if self._use_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                self._host,
                self._port,
                timeout=self._timeout,
                context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        with server:
            if self._starttls:
                server.starttls(context=ssl.create_default_context())
            if self._username and self._password:
                server.login(self._username, self._password)
            server.send_message(message)

    def _fail(self, to: str, *, transient: bool, exc: Exception) -> None:
        logger.warning(
            "email_delivery_failed",
            to=mask_email(to),
            transient=transient,
            error_type=type(exc).__name__,
        )
        if transient:
            raise NotificationDeliveryError() from exc
        raise NotificationRejectedError() from exc
