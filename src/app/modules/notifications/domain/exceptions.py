from app.core.exceptions import ExternalServiceError


class NotificationDeliveryError(ExternalServiceError):
    """A *transient* delivery failure (connection refused, timeout, 4xx SMTP reply, 5xx from an
    HTTP provider): worth retrying. The delivery task retries exactly this class."""

    code = "notification_delivery_failed"
    key = "notification.delivery_failed"
    message = "Failed to deliver the notification."


class NotificationRejectedError(ExternalServiceError):
    """A *permanent* failure (bad recipient, rejected credentials, 4xx from the provider):
    retrying the same message cannot succeed, so it is deliberately not a
    `NotificationDeliveryError` and the task does not retry it."""

    code = "notification_rejected"
    key = "notification.rejected"
    message = "The notification was rejected and will not be retried."
