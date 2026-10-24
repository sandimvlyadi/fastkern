# Internationalization and timezone

## Language

Error messages and notification content are translated from flat JSON catalogs in
`src/app/locales/` (`en.json`, `id.json`). The language comes from, in order:

1. the `Accept-Language` header, negotiated against `SUPPORTED_LOCALES`;
2. the authenticated user's saved `locale`;
3. `DEFAULT_LOCALE` (`en`).

```http
Accept-Language: id
```

Every `AppError` subclass carries a translation `key` with an entry in each catalog, and
`app/core/errors.py` is the only place that translates them. A test fails the suite when a key or a
catalog entry is missing. Successful responses carry data only, no translated message.

A Celery task has no request, so a task that produces text (such as the welcome email) receives the
recipient's saved locale as an argument.

## Timezone

**Datetimes are stored and returned in UTC**, always: every timestamp column is
`timestamptz`, and API responses are never converted to the caller's timezone. Clients localize
for display with their own device timezone, as with any REST API.

The request's timezone comes from the `X-Timezone` header (an IANA name), then the user's saved
`timezone`, then `DEFAULT_TIMEZONE` (`UTC`):

```http
X-Timezone: Asia/Jakarta
```

It is used only where the server renders a time for a person to read (an email, a PDF, a CSV
export), through `app.core.timezone.to_user_timezone()`, never in a repository, use case or
response schema. An unknown or malformed value falls back to the default instead of failing the
request.

See [ADR 0005](./adr/0005-i18n-timezone-response-conventions.md).
