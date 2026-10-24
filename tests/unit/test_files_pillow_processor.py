from pathlib import Path

import pytest
from PIL import Image

from app.modules.files.domain.exceptions import FileRejectedError
from app.modules.files.domain.ports import ImageOptions
from app.modules.files.infrastructure.image.pillow_processor import PillowImageProcessor

_OPTIONS = ImageOptions(
    variant_sizes=(128, 256, 512), max_dimension=4096, quality=80, max_pixels=40_000_000
)
_EXIF_MAKE_TAG = 0x010F
_EXIF_ORIENTATION_TAG = 0x0112
_SECRET_MAKE = "SecretCam-9000"


def _png_with_exif(path: Path, size: tuple[int, int]) -> Path:
    img = Image.new("RGB", size, "red")
    exif = img.getexif()
    exif[_EXIF_MAKE_TAG] = _SECRET_MAKE
    img.save(path, "PNG", exif=exif)
    return path


def _run(source: Path, out: Path, image_format: str = "PNG", options: ImageOptions = _OPTIONS):
    return PillowImageProcessor().process(source, out, options, image_format)


def test_output_is_webp_and_carries_no_exif(tmp_path: Path) -> None:
    source = _png_with_exif(tmp_path / "in.png", (300, 200))
    result = _run(source, tmp_path / "out")

    original = next(o for o in result.outputs if o.name == "original")
    assert original.mime_type == "image/webp"
    data = original.path.read_bytes()
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    assert _SECRET_MAKE.encode() not in data  # metadata is dropped by construction
    assert "exif" not in Image.open(original.path).info


def test_variants_are_sized_and_never_upscaled(tmp_path: Path) -> None:
    source = _png_with_exif(tmp_path / "in.png", (300, 200))
    result = _run(source, tmp_path / "out")

    names = [o.name for o in result.outputs]
    assert names == ["original", "128", "256"]  # 512 > 300px source: skipped
    variant_128 = next(o for o in result.outputs if o.name == "128")
    assert max(variant_128.width, variant_128.height) == 128


def test_max_dimension_caps_the_master(tmp_path: Path) -> None:
    source = _png_with_exif(tmp_path / "wide.png", (3000, 100))
    options = ImageOptions(
        variant_sizes=(128,), max_dimension=1000, quality=80, max_pixels=40_000_000
    )
    result = _run(source, tmp_path / "out", options=options)
    assert result.width == 1000


def test_exif_orientation_is_applied_before_stripping(tmp_path: Path) -> None:
    source = tmp_path / "rotated.jpg"
    img = Image.new("RGB", (200, 100), "blue")
    exif = img.getexif()
    exif[_EXIF_ORIENTATION_TAG] = 6  # stored sideways; should display as 100x200
    img.save(source, "JPEG", exif=exif)

    result = _run(source, tmp_path / "out", image_format="JPEG")
    assert (result.width, result.height) == (100, 200)


def test_pixel_budget_rejects_decompression_bombs(tmp_path: Path) -> None:
    source = _png_with_exif(tmp_path / "in.png", (300, 200))
    tight = ImageOptions(variant_sizes=(128,), max_dimension=4096, quality=80, max_pixels=1000)
    with pytest.raises(FileRejectedError) as excinfo:
        _run(source, tmp_path / "out", options=tight)
    assert excinfo.value.reason == "IMAGE_TOO_LARGE"


def test_declared_format_must_match_decoded_format(tmp_path: Path) -> None:
    source = _png_with_exif(tmp_path / "in.png", (50, 50))
    with pytest.raises(FileRejectedError) as excinfo:
        _run(source, tmp_path / "out", image_format="JPEG")
    assert excinfo.value.reason == "TYPE_MISMATCH"


def test_undecodable_image_is_rejected_as_invalid(tmp_path: Path) -> None:
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\xff" * 40)
    with pytest.raises(FileRejectedError) as excinfo:
        _run(broken, tmp_path / "out")
    assert excinfo.value.reason == "INVALID_IMAGE"
