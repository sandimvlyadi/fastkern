"""Adapter implementing `NotificationSenderPort` against an external HTTP
email API (e.g. Postmark/SendGrid/SES-over-HTTP — swap the URL/payload
shape for your provider).

Retries transient failures (timeouts, 5xx) with exponential backoff via
tenacity; a non-retryable 4xx fails fast. This is the pattern every other
"call an external system" adapter in this codebase should follow.
"""

import contextlib
from collections.abc import AsyncIterator

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.logging import get_logger
from app.modules.notifications.domain.exceptions import (
    NotificationDeliveryError,
    NotificationRejectedError,
)
from app.modules.notifications.domain.ports import NotificationSenderPort
from app.modules.notifications.infrastructure.masking import mask_email

logger = get_logger(__name__)


class _TransientDeliveryError(Exception):
    """Raised for retryable failures (timeout / 5xx) only."""


class HttpEmailAdapter(NotificationSenderPort):
    def __init__(
        self, *, base_url: str, api_key: str, client: httpx.AsyncClient | None = None
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._injected_client = client

    @contextlib.asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        """The injected client (left open: its owner closes it), else a short-lived one. Not
        cached on the instance: a Celery task runs each send under its own `asyncio.run()`, and an
        `httpx` connection pool is bound to the loop it was created in."""
        if self._injected_client is not None:
            yield self._injected_client
            return
        async with httpx.AsyncClient(timeout=5.0) as client:
            yield client

    @retry(
        retry=retry_if_exception_type(_TransientDeliveryError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    async def _post(self, payload: dict[str, str]) -> None:
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{self._base_url}/v1/email",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.TransportError as exc:
            # Timeouts, refused/reset connections, DNS failures, broken reads: every failure to
            # get an answer at all, not only timeouts.
            raise _TransientDeliveryError() from exc

        if response.status_code >= 500 or response.status_code == 429:
            raise _TransientDeliveryError()
        if response.status_code >= 400:
            # Status only: a provider's error body can echo the message it refused.
            logger.error("email_delivery_rejected", status=response.status_code)
            raise NotificationRejectedError(status_code=response.status_code)

    async def send_email(self, *, to: str, subject: str, body: str) -> None:
        try:
            await self._post({"to": to, "subject": subject, "body": body})
        except _TransientDeliveryError as exc:
            logger.error("email_delivery_failed_after_retries", to=mask_email(to))
            raise NotificationDeliveryError() from exc
