import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "secret_key": "x" * 40,
        "database_url": "sqlite+aiosqlite:///:memory:",
        "environment": "local",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


def test_upload_defaults_match_the_plan() -> None:
    settings = _settings()
    assert settings.upload_chunk_max_bytes == 20 * 1024 * 1024
    assert settings.upload_max_file_bytes == 1024**3
    assert settings.image_variant_sizes == [128, 256, 512]
    assert settings.malware_scan_backend == "none"  # optional, off by default
    assert settings.rate_limit_upload == "600/minute"


def test_chunk_size_is_capped_independently_of_file_size() -> None:
    with pytest.raises(ValidationError):
        _settings(upload_chunk_max_bytes=200 * 1024 * 1024)


def test_max_file_size_must_fit_at_least_one_chunk() -> None:
    with pytest.raises(ValidationError):
        _settings(upload_chunk_max_bytes=10 * 1024 * 1024, upload_max_file_bytes=1024)


@pytest.mark.parametrize("sizes", [[512, 128], [128, 128], [], [0], [5000]])
def test_variant_sizes_must_be_ascending_unique_and_bounded(sizes: list[int]) -> None:
    with pytest.raises(ValidationError):
        _settings(image_variant_sizes=sizes)


def test_clamav_is_opt_in_and_takes_host_and_port() -> None:
    enabled = _settings(malware_scan_backend="clamav", clamav_host="clamav", clamav_port=3310)
    assert enabled.malware_scan_backend == "clamav"
    with pytest.raises(ValidationError):
        _settings(malware_scan_backend="defender")  # only "none" and "clamav" exist


def test_clamav_backend_requires_a_host() -> None:
    with pytest.raises(ValidationError):
        _settings(malware_scan_backend="clamav", clamav_host="")


def test_avatar_max_bytes_must_not_exceed_the_chunk_size() -> None:
    with pytest.raises(ValidationError):
        _settings(upload_chunk_max_bytes=1024, avatar_max_bytes=2048)


def test_cleanup_interval_defaults_to_15_minutes_and_is_configurable() -> None:
    assert _settings().files_cleanup_interval_seconds == 900.0
    assert _settings(files_cleanup_interval_seconds=60).files_cleanup_interval_seconds == 60
    with pytest.raises(ValidationError):
        _settings(files_cleanup_interval_seconds=1)
