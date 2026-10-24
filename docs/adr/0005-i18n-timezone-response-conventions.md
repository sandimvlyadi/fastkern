# 5. Mandatory i18n and timezone handling for every response

## Status
Accepted

## Context
The base project shipped the *plumbing* for multi-language and
multi-timezone support early: locale negotiation from `Accept-Language`
(`app/core/i18n.negotiate_locale`), a request-scoped `RequestContext`
carrying `locale`/`timezone` (`app/core/context.py`), a middleware that
populates it from headers (`app/core/middleware/request_context.py`), a
fallback to the authenticated user's saved `locale`/`timezone` preference
(`app/core/di.get_current_user`), JSON locale catalogs
(`app/locales/en.json`, `id.json`), a `translate()` lookup function, and a
`to_user_timezone()` conversion helper.

None of it was actually wired into a response. Every `AppError` message was
a hardcoded English string; `app/core/errors.py`'s four exception handlers
emitted that raw English straight into the JSON error envelope, ignoring
`get_request_context().locale` even though it was already in scope; the
welcome-email notification had no locale parameter at all; and DB timestamp
columns were timezone-naive at the schema level (UTC by convention only,
not enforced). This meant "multi-language and multi-timezone support" was
documented (CLAUDE.md section 3) but not real — a request with
`Accept-Language: id` got English error messages regardless, and a bug
existed as a direct symptom: `RoleNotAssignedError(role=...)` never
actually substituted the role name into its message, because nothing ever
called `translate()` to apply `str.format(**context)`.

## Decision

**i18n is mandatory for every user-facing string, enforced through one
choke point — never scattered `translate()` calls.**

- Every `app.core.exceptions.AppError` subclass carries a `key: str` class
  attribute (a translation lookup), in addition to the existing `code`
  (used for the machine-readable error code) and `message` (the English
  default/fallback). Adding a domain exception without a `key` — and a
  matching entry in **both** `app/locales/en.json` and `id.json` — is a
  bug, not a style choice, exactly like skipping `record_audit_event` on a
  mutating endpoint.
- `app/core/errors.py` is the **only** place that calls `translate()` for
  error responses: `translate(exc.key, ctx.locale, default=exc.message,
  **exc.context)`. Routers and use cases must never call `translate()`
  themselves for an error message — raise the `AppError` and let the
  central handler translate it. This is the same "one choke point" shape
  as `record_audit_event` for audit events.
- This also fixes the `role.not_assigned` interpolation bug: `exc.context`
  now flows into `translate()`'s `**params`, so a locale catalog entry with
  a `{role}`/`{permission}` placeholder is actually filled in.
- Notification content (e.g. the welcome email) is translated the same
  way, but since Celery tasks run outside any HTTP request and have no
  ambient `RequestContext`, the locale must be passed explicitly as a task
  argument — sourced from the **recipient's own saved preference**
  (`user.locale`), not the caller's `Accept-Language` header, since the
  recipient's language governs, not the sender's.
- A raw `StarletteHTTPException.detail` (from a directly-raised
  `HTTPException`, or a Starlette built-in like the 404 for an unmatched
  route) is deliberately **not** run through `translate()`. It's arbitrary
  developer-supplied text, not a controlled catalog string — the same
  treatment CLAUDE.md already gives internal log messages. Raising
  `HTTPException` outside `presentation/` is already discouraged in favor
  of `AppError` subclasses, which do get a translated `message`.
- **`success_envelope()` stays data-only — no message field.** Most
  success responses are structured data, not prose (a `UserResponse` isn't
  a translatable string), and there's no single choke point analogous to
  `errors.py` that could enforce a message on every mutation without
  inviting inconsistent, half-adopted usage. If a specific feature needs a
  human-facing success message, it goes through its own translated
  notification channel (email/webhook/etc.), not the envelope.
- Enforcement is automated, not review-only:
  `tests/unit/test_i18n_key_coverage.py` walks every `AppError` subclass
  and fails if any lacks a `key`, or if any `key` is missing from either
  locale file. This runs under `make test` (a required CI gate) the same
  way `make arch-check` catches layering violations.

**Timezone stays UTC-over-the-wire; "mandatory" means timezone-*safety*,
not per-response local-time conversion.**

- JSON responses continue to carry UTC datetimes (ISO-8601), never
  converted to the requester's local time. This is unchanged from the
  existing convention and is standard REST practice: the client knows its
  own device timezone and renders locally; a server-side "helpful"
  conversion baked into the JSON body would be wrong the moment the value
  is displayed on a different device or in a different timezone than the
  one resolved at response time.
- What "mandatory" adds: UTC-ness is now a **structural** guarantee, not
  just convention. `app/shared/infrastructure/orm_base.py`'s
  `TimestampMixin`/`SoftDeleteMixin`, and `audit_log`'s own `created_at`
  column, all use `DateTime(timezone=True)` (Postgres `TIMESTAMPTZ`) —
  every stored datetime is unambiguous.
- `app.core.timezone.to_user_timezone()` remains reserved for genuine
  human-rendering edges (PDF/email/CSV) — never a repository, use case, or
  response DTO. `SendWelcomeEmailUseCase` documents this call site inline
  so the next render-edge feature has an obvious, copyable pattern (the
  same role `record_audit_event`'s router comment plays for audit).
- Locale/timezone resolution itself (header → user preference → UTC/`en`
  default) must always be available via `get_request_context()` on every
  authenticated request — that part was already correct and is unchanged.

## Consequences
- A new domain exception that skips the `key` attribute, or a locale entry
  missing from `en.json`/`id.json`, now fails `make test` automatically
  instead of silently shipping English-only errors.
- Adding a new module's exceptions requires one extra line per class (the
  `key`) and two catalog entries — see the updated `docs/module-guide.md`.
- The repository's first real Alembic migration
  (`migrations/versions/672031a0e07c_baseline_schema.py`) was generated as
  part of this change and already reflects `DateTime(timezone=True)` on
  every table; there is no existing deployed data to backfill since this
  was previously an empty scaffold.
- If a future feature needs a translated *success* message (not just
  errors/notifications), don't bolt an ad-hoc `message` field onto one
  router's response — open a new ADR describing the mechanism (e.g. an
  optional field on `success_envelope` with an equivalent enforcement
  story), so it doesn't become a second, undocumented convention.
