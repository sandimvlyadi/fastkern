from datetime import timedelta
from pathlib import Path

import pytest

from app.core.config import Settings
from app.modules.files.infrastructure.factory import (
    build_image_options,
    build_malware_scanner,
    build_object_storage,
    build_upload_policy,
    work_dir,
)
from app.modules.files.infrastructure.scanner import ClamAVScanner, NoopScanner
from app.modules.files.infrastructure.storage.local_fs import LocalFileSystemStorage


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "secret_key": "x" * 40,
        "database_url": "sqlite+aiosqlite:///:memory:",
        "storage_root": str(tmp_path / "data"),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_scanner_is_noop_unless_clamav_is_selected(tmp_path: Path) -> None:
    assert isinstance(build_malware_scanner(_settings(tmp_path)), NoopScanner)
    enabled = _settings(tmp_path, malware_scan_backend="clamav", clamav_host="h", clamav_port=1234)
    assert isinstance(build_malware_scanner(enabled), ClamAVScanner)


def test_storage_uses_the_configured_root(tmp_path: Path) -> None:
    assert isinstance(build_object_storage(_settings(tmp_path)), LocalFileSystemStorage)


def test_upload_policy_and_image_options_follow_settings(tmp_path: Path) -> None:
    settings = _settings(tmp_path, upload_session_ttl_seconds=600, image_quality=70)
    policy = build_upload_policy(settings)
    assert policy.session_ttl == timedelta(seconds=600)
    assert policy.max_active_sessions == settings.upload_max_active_sessions_per_user
    options = build_image_options(settings)
    assert options.quality == 70
    assert options.variant_sizes == (128, 256, 512)


def test_work_dir_is_created_under_the_storage_root(tmp_path: Path) -> None:
    path = work_dir(_settings(tmp_path))
    assert path.is_dir()
    assert path.is_relative_to(tmp_path / "data" / "tmp")


def test_production_without_malware_scan_logs_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from app.modules.files.infrastructure import factory

    events: list[str] = []

    class _Capture:
        def warning(self, event: str, **_: object) -> None:
            events.append(event)

    monkeypatch.setattr(factory, "logger", _Capture())
    factory.warn_if_uploads_are_unscanned(
        SimpleNamespace(is_production=True, malware_scan_backend="none")  # type: ignore[arg-type]
    )
    factory.warn_if_uploads_are_unscanned(
        SimpleNamespace(is_production=False, malware_scan_backend="none")  # type: ignore[arg-type]
    )
    factory.warn_if_uploads_are_unscanned(
        SimpleNamespace(is_production=True, malware_scan_backend="clamav")  # type: ignore[arg-type]
    )
    assert events == ["uploads_not_malware_scanned"]
