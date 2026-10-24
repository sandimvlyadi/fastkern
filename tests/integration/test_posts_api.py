from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.di import get_posts_api_port
from app.main import app
from app.modules.posts.domain.entities import Post
from tests.fakes import InMemoryPostsApiAdapter
from tests.helpers import audit_entries, auth_header, grant_role, register_and_login


def _override_posts_api(fake: InMemoryPostsApiAdapter) -> None:
    app.dependency_overrides[get_posts_api_port] = lambda: fake


async def _admin(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
) -> tuple[str, str]:
    user_id, token, _ = await register_and_login(client, tenant_id, email)
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="admin",
        permissions=["posts:read", "posts:write", "posts:delete", "audit_log:read"],
    )
    return user_id, token


async def test_list_requires_permission(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _override_posts_api(InMemoryPostsApiAdapter())
    _, no_perm_token, _ = await register_and_login(client, tenant_id, "noperm@example.com")
    forbidden = await client.get("/api/v1/posts", headers=auth_header(no_perm_token))
    assert forbidden.status_code == 403

    _, admin_token = await _admin(client, tenant_id, session_factory, "admin@example.com")
    allowed = await client.get("/api/v1/posts", headers=auth_header(admin_token))
    assert allowed.status_code == 200


async def test_list_posts_returns_pagination_meta(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    fake = InMemoryPostsApiAdapter(
        seed=[Post(id=i, user_id=1, title=f"t{i}", body="b") for i in range(1, 6)]
    )
    _override_posts_api(fake)
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin2@example.com")

    response = await client.get(
        "/api/v1/posts", params={"page": 1, "page_size": 2}, headers=auth_header(admin_token)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["data"]) == 2
    assert body["meta"]["pagination"]["total"] == 5


async def test_get_unknown_post_returns_404(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _override_posts_api(InMemoryPostsApiAdapter())
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin3@example.com")

    response = await client.get("/api/v1/posts/999", headers=auth_header(admin_token))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "post_not_found"


async def test_get_post_by_id_returns_it(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    fake = InMemoryPostsApiAdapter(seed=[Post(id=1, user_id=1, title="Hello", body="World")])
    _override_posts_api(fake)
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-get@example.com")

    response = await client.get("/api/v1/posts/1", headers=auth_header(admin_token))

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"id": 1, "userId": 1, "title": "Hello", "body": "World"}


async def test_create_post_returns_201_and_audits(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _override_posts_api(InMemoryPostsApiAdapter())
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin4@example.com")

    response = await client.post(
        "/api/v1/posts",
        json={"userId": 1, "title": "Hello", "body": "World"},
        headers=auth_header(admin_token),
    )
    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["title"] == "Hello"
    assert body["userId"] == 1

    entries = await audit_entries(client, admin_token, "post.created")
    assert len(entries) == 1


async def test_create_post_forbidden_without_write_permission(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _override_posts_api(InMemoryPostsApiAdapter())
    user_id, token, _ = await register_and_login(client, tenant_id, "readonly@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="viewer",
        permissions=["posts:read"],
    )

    response = await client.post(
        "/api/v1/posts",
        json={"userId": 1, "title": "Hello", "body": "World"},
        headers=auth_header(token),
    )
    assert response.status_code == 403


async def test_update_then_delete_post(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    fake = InMemoryPostsApiAdapter(seed=[Post(id=1, user_id=1, title="Old", body="Body")])
    _override_posts_api(fake)
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin5@example.com")

    update = await client.patch(
        "/api/v1/posts/1", json={"title": "New"}, headers=auth_header(admin_token)
    )
    assert update.status_code == 200, update.text
    assert update.json()["data"]["title"] == "New"

    update_entries = await audit_entries(client, admin_token, "post.updated")
    assert len(update_entries) == 1

    delete = await client.delete("/api/v1/posts/1", headers=auth_header(admin_token))
    assert delete.status_code == 204

    delete_entries = await audit_entries(client, admin_token, "post.deleted")
    assert len(delete_entries) == 1

    get_after_delete = await client.get("/api/v1/posts/1", headers=auth_header(admin_token))
    assert get_after_delete.status_code == 404


async def test_update_unknown_post_returns_404(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _override_posts_api(InMemoryPostsApiAdapter())
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin6@example.com")

    response = await client.patch(
        "/api/v1/posts/999", json={"title": "New"}, headers=auth_header(admin_token)
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "post_not_found"
