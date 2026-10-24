from abc import ABC, abstractmethod
from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams
from app.modules.roles.domain.entities import Role, UserRoleAssignment


class RoleRepositoryPort(ABC):
    @abstractmethod
    async def get_by_id(self, role_id: UUID) -> Role | None: ...

    @abstractmethod
    async def get_by_name(self, name: str, *, tenant_id: UUID | None = None) -> Role | None: ...

    @abstractmethod
    async def list_all(self) -> list[Role]: ...

    @abstractmethod
    async def paginate(self, params: OffsetParams) -> OffsetPage[Role]: ...

    @abstractmethod
    async def add(self, role: Role) -> Role: ...

    @abstractmethod
    async def save(self, role: Role) -> Role: ...

    @abstractmethod
    async def delete(self, role_id: UUID) -> None:
        """Soft-deletes the role. Existing user assignments are left in place
        (they no longer resolve to a role — see `UserRoleRepositoryPort.revoke`)."""
        ...


class UserRoleRepositoryPort(ABC):
    @abstractmethod
    async def assign(
        self,
        user_id: UUID,
        role_id: UUID,
        *,
        tenant_id: UUID | None = None,
        is_default: bool = False,
    ) -> bool:
        """Returns True if a new assignment was created, False if the user
        already had the role (assignment is idempotent). `is_default` is only
        applied to a fresh assignment; it's ignored when the row already
        existed."""
        ...

    @abstractmethod
    async def revoke(self, user_id: UUID, role_id: UUID, *, tenant_id: UUID | None = None) -> bool:
        """Returns True if an assignment was removed, False if there was
        none. Removes the row regardless of the role's own soft-delete state,
        so a dangling assignment to a deleted role can still be cleaned up."""
        ...

    @abstractmethod
    async def list_roles_for_user(
        self, user_id: UUID, *, tenant_id: UUID | None = None
    ) -> list[Role]: ...

    @abstractmethod
    async def paginate_roles_for_user(
        self, user_id: UUID, params: OffsetParams, *, tenant_id: UUID | None = None
    ) -> OffsetPage[UserRoleAssignment]:
        """Paginated variant of `list_roles_for_user`, for the API list
        endpoints — each item also carries whether that role is the user's
        default. Internal callers (login's active-role pick, switch-role
        validation) need the complete membership set and should keep using
        `list_roles_for_user` — only a human paging through the list needs
        this."""
        ...

    @abstractmethod
    async def get_default_role_for_user(
        self, user_id: UUID, *, tenant_id: UUID | None = None
    ) -> Role | None:
        """The user's current default role (used as `active_role` at
        login/refresh), or None if they have no roles or none is marked
        default."""
        ...

    @abstractmethod
    async def set_default_role(
        self, user_id: UUID, role_id: UUID, *, tenant_id: UUID | None = None
    ) -> bool:
        """Returns True if `role_id` is one of the user's assigned roles
        (and is now their sole default); False if the user doesn't hold that
        role at all (nothing changed)."""
        ...

    @abstractmethod
    async def user_has_role(
        self, user_id: UUID, role_name: str, *, tenant_id: UUID | None = None
    ) -> bool: ...
