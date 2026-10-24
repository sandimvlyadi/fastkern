# 11. Notification (email) delivery

## Status
Accepted.

## Context
The first version of `notifications.send_welcome_email` was a placeholder that could not work
and was unsafe:

- it posted to a hard-coded provider URL that does not exist (every task failed with
  `httpx.ConnectError`);
- it sent **`settings.secret_key` — the JWT signing key — as the Bearer token** to that host, so
  pointing it at a real provider would have handed that provider the key that forges admin
  tokens;
- `max_retries=3` was declared but nothing ever retried, and the adapter only handled
  `httpx.TimeoutException`, so a `ConnectError` escaped as an unhandled task failure.

## Decision
- **Backend by configuration**: `EMAIL_BACKEND` = `console` (default; logs only, for
  local/test) | `smtp` | `http`. `notifications/infrastructure/factory.py`
  (`build_notification_sender`) picks the adapter; the application layer still only knows
  `NotificationSenderPort`. Settings: `EMAIL_FROM`, `SMTP_HOST/PORT/USERNAME/PASSWORD`,
  `SMTP_STARTTLS`, `SMTP_SSL` (mutually exclusive), `SMTP_TIMEOUT_SECONDS`,
  `EMAIL_HTTP_BASE_URL`, `EMAIL_HTTP_API_KEY`; secrets are `SecretStr`.
  **No code path may use `secret_key` for anything but signing tokens.**
- **Production refuses `console`** and requires the credentials of the chosen backend
  (`Settings._validate_email_settings`), like the `SECRET_KEY` guard.
- **SMTP uses the standard library** (`smtplib` + `email.message.EmailMessage`) run through
  `asyncio.to_thread`, so it adds **no new dependency**. (`aiosmtplib` would be reasonable but
  needs its own ADR first, per CLAUDE.md §6.) TLS via STARTTLS or implicit SSL with a verifying
  `ssl.create_default_context()`.
- **Two failure classes, one retry policy.**
  - `NotificationDeliveryError` = *transient*: connection refused/reset, DNS, timeouts, the
    server hanging up, a 4xx SMTP reply, an HTTP 5xx/429. The task retries exactly this class.
  - `NotificationRejectedError` = *permanent*: 5xx SMTP replies, bad credentials, every
    recipient refused, a server lacking a required capability, an HTTP 4xx. Retrying cannot
    help, so it deliberately is **not** a subclass of the above and is not retried.
- **Task**: `@celery_app.task(bind=True, autoretry_for=(NotificationDeliveryError,),
  retry_backoff=True, retry_backoff_max=300, retry_jitter=True, max_retries=3,
  acks_late=True)`. `acks_late` means a worker that dies mid-send gets the message redelivered.
  Callers still enqueue by task name (`send_task("notifications.send_welcome_email", ...)`), so
  modules stay decoupled.
- **No sensitive data in logs**: the recipient is masked (`j***@example.com`), and neither the
  body, the subject of an SMTP failure, credentials, nor an HTTP provider's response body is
  logged (a provider's error text can echo the message it rejected).
- **Localisation** stays as in ADR 0005: the task receives the recipient's saved `locale` as an
  explicit argument (a Celery task has no request context) and translates at that edge.
- **Local development**: docker-compose points the api and worker at an SMTP catcher on the
  *host* (`host.docker.internal:1025`, e.g. mailpit, UI/API on `:8025`). No mailpit service is
  defined in `docker-compose.yml` because those ports are usually already taken; see the README
  for a one-line `docker run` if you need one.

## Consequences
- Delivery survives a mail-server blip (bounded, jittered retries) without retrying hopeless
  messages.
- The `HttpEmailAdapter` keeps a provider-shaped payload (`POST {base}/v1/email`); adapt it to
  the provider you actually choose.
- The retry count is per message and not persisted beyond the broker: if the broker is lost, so
  are pending retries. That is the usual Celery trade-off; introduce an outbox if email becomes
  business-critical.
