"""Response builders for a stored file, shared by every router that serves one (this module's own
and the partner-facing one), so both return the same payload and download headers."""

from typing import Any
from urllib.parse import quote

from fastapi.responses import StreamingResponse

from app.core.config import get_settings
from app.modules.files.domain.entities import FileAsset, FileVariant
from app.modules.files.domain.ports import ObjectStoragePort
from app.modules.files.domain.value_objects import FileKind, Region, Visibility, file_key
from app.modules.files.presentation.schemas import FileAssetResponse, FileVariantResponse

_PRIVATE_CACHE_CONTROL = "private, no-store"


def content_disposition(asset: FileAsset) -> str:
    """Images render inline. Documents and generic files always download, so a hostile file
    cannot run in the app's origin."""
    disposition = "inline" if asset.kind is FileKind.IMAGE else "attachment"
    return f"{disposition}; filename*=UTF-8''{quote(asset.original_name)}"


def file_payload(asset: FileAsset, variants: list[FileVariant]) -> dict[str, Any]:
    prefix = get_settings().api_v1_prefix
    items = [
        FileVariantResponse(
            name=variant.name,
            width=variant.width,
            height=variant.height,
            mime_type=variant.mime_type,
            url=(
                f"{prefix}/public/files/{asset.id}/{variant.name}"
                if asset.is_public_available
                else None
            ),
        )
        for variant in variants
    ]
    return FileAssetResponse(
        id=asset.id,
        status=asset.status.value,
        visibility=asset.visibility,
        kind=asset.kind.value,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        sha256=asset.sha256,
        width=asset.width,
        height=asset.height,
        original_name=asset.original_name,
        failure_reason=asset.failure_reason,
        variants=items,
    ).model_dump(by_alias=True)


def private_variant_response(
    storage: ObjectStoragePort, asset: FileAsset, variant: FileVariant
) -> StreamingResponse:
    """Streams a variant to an authorized caller. Never cached by a shared cache: access was
    decided for this caller, not for everyone."""
    region = Region.PUBLIC if asset.visibility is Visibility.PUBLIC else Region.PRIVATE
    return StreamingResponse(
        storage.read(file_key(region, asset.id, "variants", variant.name)),
        media_type=variant.mime_type,
        headers={
            "Content-Disposition": content_disposition(asset),
            "Content-Length": str(variant.size_bytes),
            "Cache-Control": _PRIVATE_CACHE_CONTROL,
            "ETag": f'"{variant.sha256}"',
        },
    )
