"""Port for refresh-token session/blacklist state (implemented with Redis
in infrastructure, but the application layer only knows this interface)."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID


class TokenSessionStorePort(ABC):
    @abstractmethod
    async def store_refresh_token(
        self, *, user_id: UUID, jti: str, expires_at: datetime
    ) -> None: ...

    @abstractmethod
    async def is_refresh_token_valid(self, *, user_id: UUID, jti: str) -> bool: ...

    @abstractmethod
    async def revoke_refresh_token(self, *, user_id: UUID, jti: str) -> None: ...

    @abstractmethod
    async def consume_refresh_token(self, *, user_id: UUID, jti: str) -> bool:
        """Atomically checks validity and revokes in one step — use this
        (not `is_refresh_token_valid` + `revoke_refresh_token`) for token
        *rotation*, where a separate check-then-revoke leaves a window for
        two concurrent requests presenting the same refresh token to both
        pass the check before either revokes it. Returns whether the token
        was valid (and has now been consumed)."""
        ...

    @abstractmethod
    async def revoke_all_for_user(self, *, user_id: UUID) -> None: ...

    @abstractmethod
    async def revoke_session(self, *, session_id: str, ttl_seconds: int) -> None:
        """Denies every token (access and refresh) carrying this `sid` claim for `ttl_seconds`
        -- pass at least the longest token lifetime, after which the tokens are expired anyway."""
        ...

    @abstractmethod
    async def is_session_revoked(self, *, session_id: str) -> bool:
        """Whether `revoke_session` was called for this session. Implementations decide what an
        unreachable backend means; the Redis one fails OPEN (returns False, logs, counts) so a
        Redis outage degrades logout-takes-effect-immediately instead of taking the API down."""
        ...
