"""Pillow image step: decode, orient, cap size, re-encode without metadata, then make variants.

Metadata policy: every output is encoded from pixel data only. EXIF, XMP and GPS
are never passed to the encoder, so they cannot survive. The ICC color profile is kept so
colors render correctly.
"""

import hashlib
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from app.modules.files.domain.exceptions import FileRejectedError
from app.modules.files.domain.ports import (
    ImageOptions,
    ImageProcessorPort,
    ImageResult,
    ProcessedOutput,
)

_LOSSLESS_FORMATS = frozenset({"PNG", "GIF", "WEBP"})
_WEBP_METHOD = 4
_OUTPUT_MIME = "image/webp"


class PillowImageProcessor(ImageProcessorPort):
    def process(
        self, source: Path, out_dir: Path, options: ImageOptions, image_format: str
    ) -> ImageResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            with Image.open(source) as probe:
                probe.verify()  # structural check; invalidates this handle, so reopen below
            with Image.open(source) as img:
                if img.format != image_format:
                    raise FileRejectedError("TYPE_MISMATCH")
                if img.width * img.height > options.max_pixels:
                    raise FileRejectedError("IMAGE_TOO_LARGE")
                img.load()
                return self._render(img, out_dir, options, image_format)
        except FileRejectedError:
            raise
        except (
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
            SyntaxError,
            ValueError,
        ) as exc:
            raise FileRejectedError("INVALID_IMAGE") from exc

    def _render(
        self, img: Image.Image, out_dir: Path, options: ImageOptions, image_format: str
    ) -> ImageResult:
        icc_profile = img.info.get("icc_profile")
        lossless = image_format in _LOSSLESS_FORMATS

        oriented = ImageOps.exif_transpose(img)  # applies EXIF orientation, then drops it
        has_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
        master = oriented.convert("RGBA" if has_alpha else "RGB")
        master.thumbnail((options.max_dimension, options.max_dimension), Image.Resampling.LANCZOS)

        save_kwargs: dict[str, Any] = {
            "lossless": lossless,
            "quality": options.quality,
            "method": _WEBP_METHOD,
        }
        if icc_profile:
            save_kwargs["icc_profile"] = icc_profile

        outputs = [self._write(master, out_dir / "original", save_kwargs, "original")]
        longest = max(master.width, master.height)
        for size in options.variant_sizes:
            if size > longest:  # never upscale a variant beyond the source
                continue
            variant = master.copy()
            variant.thumbnail((size, size), Image.Resampling.LANCZOS)
            outputs.append(self._write(variant, out_dir / str(size), save_kwargs, str(size)))
        return ImageResult(width=master.width, height=master.height, outputs=outputs)

    @staticmethod
    def _write(
        img: Image.Image, path: Path, save_kwargs: dict[str, Any], name: str
    ) -> ProcessedOutput:
        # No `exif=` / `xmp=` here: metadata is dropped by construction (see module docstring).
        img.save(path, format="WEBP", **save_kwargs)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return ProcessedOutput(
            name=name,
            path=path,
            width=img.width,
            height=img.height,
            size_bytes=path.stat().st_size,
            sha256=digest,
            mime_type=_OUTPUT_MIME,
        )
