from app.core.exceptions import ExternalServiceError, NotFoundError


class PostNotFoundError(NotFoundError):
    code = "post_not_found"
    key = "post.not_found"
    message = "Post not found."


class PostsServiceError(ExternalServiceError):
    code = "posts_service_unavailable"
    key = "posts.service_unavailable"
    message = "The posts service is temporarily unavailable."
