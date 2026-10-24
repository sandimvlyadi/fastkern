"""Port for outbound notification delivery.

This is the anti-corruption layer boundary: the application layer only
ever talks to `NotificationSenderPort`. Swapping the email provider (or
adding SMS/push) means writing one new infrastructure adapter — no
application-layer code changes."""

from abc import ABC, abstractmethod


class NotificationSenderPort(ABC):
    @abstractmethod
    async def send_email(self, *, to: str, subject: str, body: str) -> None: ...
