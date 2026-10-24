from app.modules.posts.domain.entities import Post


def test_post_audit_snapshot() -> None:
    post = Post(id=1, user_id=7, title="Hello", body="World")

    assert post.audit_snapshot() == {"userId": 7, "title": "Hello", "body": "World"}
