from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.modules.api_clients.presentation.schemas import CreateApiKeyRequest


def test_expires_at_in_the_future_is_accepted() -> None:
    expires_at = datetime.now(UTC) + timedelta(days=30)
    assert CreateApiKeyRequest(name="Acme", expires_at=expires_at).expires_at == expires_at


def test_expires_at_defaults_to_never() -> None:
    assert CreateApiKeyRequest(name="Acme").expires_at is None


def test_expires_at_in_the_past_is_rejected() -> None:
    """It would create a key that is refused on its very first use."""
    with pytest.raises(ValidationError, match="must be in the future"):
        CreateApiKeyRequest(name="Acme", expires_at=datetime.now(UTC) - timedelta(minutes=1))


def test_a_naive_expires_at_is_rejected() -> None:
    """A naive value would be read in whatever timezone the database session uses."""
    with pytest.raises(ValidationError, match="timezone"):
        CreateApiKeyRequest.model_validate({"name": "Acme", "expiresAt": "2099-01-01T00:00:00"})
