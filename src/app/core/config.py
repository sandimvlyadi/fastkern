"""Application configuration.

Single source of truth for runtime configuration. Fails fast at startup if a
required variable is missing or malformed — never silently falls back to a
guessed default for anything security- or environment-sensitive.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from limits import parse as parse_rate_limit
from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Words that mark a copy-pasted placeholder key (the public `.env.example` value included).
_PLACEHOLDER_SECRET_MARKERS = ("change-me", "changeme", "example", "secret")
_MIN_PRODUCTION_SECRET_LENGTH = 64
_MIN_DISTINCT_SECRET_CHARS = 8


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    environment: Environment = Environment.LOCAL
    app_name: str = "fastkern"
    app_version: str = "0.2.0"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    # None = follow the environment (on unless production). Set explicitly to expose the docs on
    # an internal staging/production deployment, or to hide them elsewhere.
    docs_enabled: bool | None = None

    # --- Security ---
    secret_key: str = Field(min_length=32)
    access_token_expire_minutes: int = 15
    refresh_token_expire_minutes: int = 60 * 24 * 7
    jwt_algorithm: str = "HS256"

    # --- CORS ---
    cors_origins: list[str] = Field(default_factory=list)

    # --- Database ---
    database_url: str
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_echo: bool = False

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"
    redis_socket_timeout_seconds: float = Field(default=2.0, gt=0)

    # --- Celery ---
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # --- Observability ---
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"
    otel_service_name: str = "fastkern"
    enable_tracing: bool = False
    enable_metrics: bool = True
    # When set, GET /metrics requires `Authorization: Bearer <token>`; unset (the default) leaves
    # it open, which suits a local Prometheus but not a public listener.
    metrics_auth_token: SecretStr | None = None

    # --- i18n / timezone ---
    default_locale: str = "en"
    supported_locales: list[str] = Field(default_factory=lambda: ["en", "id"])
    default_timezone: str = "UTC"

    # --- Rate limiting ---
    # Limit strings use the `limits` syntax ("100/minute", "10/second", ...).
    # See `app/core/rate_limit.py` for which endpoints use which tier.
    rate_limit_enabled: bool = True
    rate_limit_ip: str = "300/minute"
    rate_limit_default: str = "100/minute"
    rate_limit_auth: str = "10/minute"
    rate_limit_write: str = "30/minute"
    # Chunk uploads are PUTs, so they get their own tier: at WRITE's 30/minute a multi-GB file
    # (dozens of chunks) would take minutes just in throttling.
    rate_limit_upload: str = "600/minute"
    # Partner (API-key-authenticated) routes, keyed by api_client_id, not by user — a partner's
    # office may NAT many staff through one IP, so per-user DEFAULT limits would be too strict.
    rate_limit_partner: str = "60/minute"

    # --- Multi-tenancy ---
    multi_tenancy_enabled: bool = True
    default_tenant_id: str = "00000000-0000-0000-0000-000000000000"

    # --- Health checks ---
    readiness_check_timeout_seconds: float = 3.0
    # How often beat schedules `ops.heartbeat`. Status keys expire after 3 beats.
    ops_heartbeat_interval_seconds: float = Field(default=60.0, ge=10)

    # --- Email delivery (notifications module) ---
    # "console" only logs (local/test); "smtp" uses the stdlib client; "http" posts to a provider
    # API. Production must use smtp or http with real credentials. Nothing here ever reuses
    # `secret_key`: that key signs JWTs and must never be sent anywhere.
    email_backend: Literal["smtp", "http", "console"] = "console"
    email_from: str = "no-reply@example.com"
    smtp_host: str = "localhost"
    smtp_port: int = Field(default=1025, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False
    smtp_ssl: bool = False
    smtp_timeout_seconds: float = Field(default=10.0, gt=0)
    email_http_base_url: str | None = None
    email_http_api_key: SecretStr | None = None

    # --- Posts module (external API demo) ---
    posts_api_base_url: str = "https://jsonplaceholder.typicode.com"
    posts_api_timeout_seconds: float = Field(default=5.0, gt=0)
    posts_api_auth_header: str = "Authorization"
    # Demo-only placeholder: JSONPlaceholder ignores this. Stands in for a real partner API's
    # bearer token — swap for a real SecretStr-backed credential when pointing this adapter at
    # an API that actually checks it.
    posts_api_auth_token: SecretStr = SecretStr("dummy-posts-api-token")

    # --- File storage & uploads (files module) ---
    # Local filesystem root; holds tmp/, private/ and public/. Must be
    # shared by the api, worker and beat containers.
    storage_root: str = "data"
    upload_chunk_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    upload_max_file_bytes: int = Field(default=1024**3, ge=1)
    upload_session_ttl_seconds: int = Field(default=86400, ge=300)
    upload_max_active_sessions_per_user: int = Field(default=5, ge=1)
    avatar_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1)
    image_variant_sizes: list[int] = Field(default_factory=lambda: [128, 256, 512])
    image_max_dimension: int = Field(default=4096, ge=1, le=16384)
    image_quality: int = Field(default=80, ge=1, le=100)
    image_max_pixels: int = Field(default=40_000_000, ge=1)
    # Optional malware scan (ClamAV). Off by default so small deployments carry no extra container.
    malware_scan_backend: Literal["none", "clamav"] = "none"
    clamav_host: str = "clamav"
    clamav_port: int = Field(default=3310, ge=1, le=65535)
    clamav_timeout_seconds: float = Field(default=60.0, gt=0)
    # How often beat runs the files cleanup. 900 s is for deployments; set a small value (e.g. 60)
    # locally to watch the schedule without waiting.
    files_cleanup_interval_seconds: float = Field(default=900.0, ge=10)

    # --- Partner API keys (api_clients module) ---
    # Hours a rotated-out key's previous hash stays valid, so a partner's own rotation isn't a
    # hard cutover: in-flight calls signed with the old key keep working until this window
    # closes instead of failing the instant a new key is issued.
    api_key_rotation_overlap_hours: int = Field(default=48, ge=1, le=168)
    # Retention window for sys_api_request_logs, purged by a Celery beat job — deliberately
    # NOT the same (unbounded, today) retention as sys_audit_logs: this table is high-volume
    # observability/billing-support data, not the compliance-grade audit trail.
    api_request_log_retention_days: int = Field(default=60, ge=30, le=90)
    api_request_log_purge_interval_seconds: float = Field(default=3600.0, ge=60)

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, v: str) -> str:
        # Allow sqlite for tests, otherwise enforce a real DSN shape.
        if v.startswith("sqlite"):
            return v
        PostgresDsn(v)  # raises if malformed
        return v

    @field_validator("redis_url", "celery_broker_url", "celery_result_backend")
    @classmethod
    def _validate_redis_url(cls, v: str) -> str:
        RedisDsn(v)
        return v

    @field_validator("image_variant_sizes")
    @classmethod
    def _validate_variant_sizes(cls, v: list[int]) -> list[int]:
        if not v or any(not 1 <= size <= 4096 for size in v):
            raise ValueError("IMAGE_VARIANT_SIZES must be non-empty and each size in 1..4096")
        if v != sorted(set(v)):
            raise ValueError("IMAGE_VARIANT_SIZES must be strictly ascending without duplicates")
        return v

    @model_validator(mode="after")
    def _validate_upload_limits(self) -> Self:
        if self.upload_max_file_bytes < self.upload_chunk_max_bytes:
            raise ValueError("UPLOAD_MAX_FILE_BYTES must be at least UPLOAD_CHUNK_MAX_BYTES")
        if self.avatar_max_bytes > self.upload_chunk_max_bytes:
            raise ValueError("AVATAR_MAX_BYTES must not exceed UPLOAD_CHUNK_MAX_BYTES")
        if self.malware_scan_backend == "clamav" and not (self.clamav_host and self.clamav_port):
            raise ValueError("MALWARE_SCAN_BACKEND=clamav needs CLAMAV_HOST and CLAMAV_PORT")
        return self

    @field_validator(
        "rate_limit_ip",
        "rate_limit_default",
        "rate_limit_auth",
        "rate_limit_write",
        "rate_limit_upload",
        "rate_limit_partner",
    )
    @classmethod
    def _validate_rate_limit(cls, v: str) -> str:
        item = parse_rate_limit(v)  # raises ValueError on a malformed limit string
        if item.amount < 1:
            # "0/minute" parses fine but rejects every request, locking out the whole API.
            raise ValueError(f"rate limit must allow at least 1 request per window, got {v!r}")
        return v

    @model_validator(mode="after")
    def _validate_email_settings(self) -> Self:
        if self.smtp_starttls and self.smtp_ssl:
            raise ValueError("SMTP_STARTTLS and SMTP_SSL are mutually exclusive (pick one)")
        if not self.is_production:
            return self

        problems: list[str] = []
        if self.email_backend == "console":
            problems.append("EMAIL_BACKEND=console only logs mail; use smtp or http")
        elif self.email_backend == "smtp":
            if not (self.smtp_username and self.smtp_password):
                problems.append("EMAIL_BACKEND=smtp needs SMTP_USERNAME and SMTP_PASSWORD")
        elif not (self.email_http_base_url and self.email_http_api_key):
            problems.append("EMAIL_BACKEND=http needs EMAIL_HTTP_BASE_URL and EMAIL_HTTP_API_KEY")
        if problems:
            raise ValueError("Unsafe email configuration for production: " + "; ".join(problems))
        return self

    @model_validator(mode="after")
    def _enforce_deploy_guards(self) -> Self:
        # LOCAL/TEST stay frictionless (the public `.env.example` placeholder must keep working
        # there); staging/production refuse to start on anything that would make the deployment
        # trivially forgeable.
        if self.environment not in (Environment.STAGING, Environment.PRODUCTION):
            return self

        problems: list[str] = []
        key_problems = 0
        key = self.secret_key
        lowered = key.lower()
        if any(marker in lowered for marker in _PLACEHOLDER_SECRET_MARKERS):
            problems.append("SECRET_KEY looks like a placeholder (it contains a well-known word)")
            key_problems += 1
        if len(set(key)) < _MIN_DISTINCT_SECRET_CHARS:
            problems.append("SECRET_KEY has too few distinct characters (repeated/low-entropy)")
            key_problems += 1
        if self.is_production and len(key) < _MIN_PRODUCTION_SECRET_LENGTH:
            problems.append(f"SECRET_KEY needs at least {_MIN_PRODUCTION_SECRET_LENGTH} characters")
            key_problems += 1
        if self.is_production and self.debug:
            problems.append("DEBUG must be false (tracebacks would leak internals)")
        if self.is_production and "*" in self.cors_origins:
            problems.append("CORS_ORIGINS must not contain '*' (list the real origins)")

        if problems:
            hint = (
                ' Generate a key with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
                if key_problems
                else ""
            )
            raise ValueError(
                f"Unsafe configuration for ENVIRONMENT={self.environment.value}: "
                + "; ".join(problems)
                + "."
                + hint
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def api_docs_enabled(self) -> bool:
        """Whether /docs, /redoc and /openapi.json are served (they go together: the schema
        alone is the whole API surface)."""
        return self.docs_enabled if self.docs_enabled is not None else not self.is_production

    @property
    def is_test(self) -> bool:
        return self.environment is Environment.TEST


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton. Import this, never instantiate Settings directly."""
    return Settings()
