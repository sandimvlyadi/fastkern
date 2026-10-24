import pytest
from pydantic import ValidationError

from app.modules.roles.domain.baseline import MAX_PERMISSIONS_PER_ROLE
from app.modules.roles.presentation.schemas import CreateRoleRequest, UpdateRoleRequest


def test_valid_permissions_are_accepted() -> None:
    request = CreateRoleRequest(name="r", permissions=["*", "users:read", "audit_log:read"])
    assert request.permissions == ["*", "users:read", "audit_log:read"]


def test_per_resource_wildcard_is_rejected() -> None:
    # `Role.has_permission` never honored `resource:*`; storing it granted nothing.
    with pytest.raises(ValidationError, match="each permission must be"):
        CreateRoleRequest(name="r", permissions=["audit_log:*"])


def test_permissions_default_to_empty() -> None:
    assert CreateRoleRequest(name="r").permissions == []


@pytest.mark.parametrize("bad", ["not a perm!!", "::", "Users:read", "users:", "users:read\n"])
def test_malformed_permission_is_rejected_without_echoing_it(bad: str) -> None:
    with pytest.raises(ValidationError) as exc:
        CreateRoleRequest(name="r", permissions=["users:read", bad])
    assert "each permission must be" in str(exc.value)


def test_duplicates_are_dropped_keeping_order() -> None:
    request = CreateRoleRequest(name="r", permissions=["b:x", "a:y", "b:x", "a:y", "c:z"])
    assert request.permissions == ["b:x", "a:y", "c:z"]


def test_permission_count_is_capped() -> None:
    at_limit = [f"res{i}:read" for i in range(MAX_PERMISSIONS_PER_ROLE)]
    assert len(CreateRoleRequest(name="r", permissions=at_limit).permissions) == 100
    with pytest.raises(ValidationError, match="at most 100"):
        CreateRoleRequest(name="r", permissions=[*at_limit, "one:more"])


def test_update_request_validates_permissions_only_when_sent() -> None:
    assert UpdateRoleRequest(name="new").permissions is None
    assert UpdateRoleRequest(permissions=[]).permissions == []
    with pytest.raises(ValidationError):
        UpdateRoleRequest(permissions=["nope!"])
