"""Builds the files module's adapters from settings: the only place that picks an implementation."""

from datetime import timedelta
from pathlib import Path

from app.core.config import Settings
from app.core.logging import get_logger
from app.modules.files.application.use_cases import UploadPolicy
from app.modules.files.domain.ports import ImageOptions, MalwareScannerPort, ObjectStoragePort
from app.modules.files.infrastructure.image.pillow_processor import PillowImageProcessor
from app.modules.files.infrastructure.scanner import ClamAVScanner, NoopScanner
from app.modules.files.infrastructure.storage.local_fs import LocalFileSystemStorage

logger = get_logger(__name__)


def warn_if_uploads_are_unscanned(settings: Settings) -> None:
    """Production accepts uploads without a malware scan when MALWARE_SCAN_BACKEND is `none`
    (the default). That is a supported choice, but it must be visible in the logs."""
    if settings.is_production and settings.malware_scan_backend == "none":
        logger.warning(
            "uploads_not_malware_scanned",
            hint="set MALWARE_SCAN_BACKEND=clamav and enable the clamav compose profile",
        )


def build_object_storage(settings: Settings) -> ObjectStoragePort:
    return LocalFileSystemStorage(Path(settings.storage_root))


def build_malware_scanner(settings: Settings) -> MalwareScannerPort:
    if settings.malware_scan_backend == "clamav":
        return ClamAVScanner(
            settings.clamav_host, settings.clamav_port, settings.clamav_timeout_seconds
        )
    return NoopScanner()


def build_image_processor() -> PillowImageProcessor:
    return PillowImageProcessor()


def build_image_options(settings: Settings) -> ImageOptions:
    return ImageOptions(
        variant_sizes=tuple(settings.image_variant_sizes),
        max_dimension=settings.image_max_dimension,
        quality=settings.image_quality,
        max_pixels=settings.image_max_pixels,
    )


def build_upload_policy(settings: Settings) -> UploadPolicy:
    return UploadPolicy(
        chunk_max_bytes=settings.upload_chunk_max_bytes,
        max_file_bytes=settings.upload_max_file_bytes,
        session_ttl=timedelta(seconds=settings.upload_session_ttl_seconds),
        max_active_sessions=settings.upload_max_active_sessions_per_user,
    )


def work_dir(settings: Settings) -> Path:
    """Scratch space for Pillow outputs, under the storage root (same disk, never served)."""
    path = Path(settings.storage_root) / "tmp" / "work"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o750)  # mkdir's mode does not apply to an existing directory or to parents
    return path
