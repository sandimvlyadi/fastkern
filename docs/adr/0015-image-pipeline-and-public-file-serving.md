# 15. Image pipeline and public file serving

## Status
Accepted

## Context
Uploaded images must be decoded safely, stripped of privacy-sensitive metadata, resized, and
published as optimized variants. Public files need to be cacheable, and a public URL must never
expose private content, temporary chunks, or the original filename.

## Decision
- **Pipeline** (`files.process_upload`, run in the worker): verify → scan → decode with Pillow →
  apply EXIF orientation → cap the longest side at `IMAGE_MAX_DIMENSION` → re-encode from pixel data
  as WebP → generate variants for `IMAGE_VARIANT_SIZES` (default 128, 256, 512).
- **Pillow** is the image library (new dependency, approved). It is used only in
  `infrastructure/image/pillow_processor.py`, behind `ImageProcessorPort`.
- **Metadata policy**: outputs are encoded without `exif=`, `xmp=` or any other metadata argument, so
  EXIF, XMP and GPS data cannot survive. The ICC color profile is kept so colors render correctly.
  The original upload's raw bytes are not retained: the sanitized master (`original` variant) replaces
  them.
- **Formats**: WebP only for now (AVIF is a future extension). Lossless WebP for PNG, GIF and WebP
  sources; lossy at `IMAGE_QUALITY` for JPEG.
- **No upscaling**: a variant larger than the source's longest side is skipped.
- **Decompression-bomb guard**: `IMAGE_MAX_PIXELS` is checked before decoding. Pillow's own bomb error
  is mapped to a rejection too.
- **Documents and generic files** skip the image pipeline. Their bytes are stored as the single
  `original` variant, served as attachments only (never inline), so a hostile file cannot run in the
  app's origin.
- **Public serving**: `GET /api/v1/public/files/{file_id}/{variant}` is anonymous and IP-rate-limited.
  It serves only PUBLIC and AVAILABLE files, reads only from `public/`, and looks up the variant name
  from the database (a fixed set, never a path from the URL). Responses use
  `Cache-Control: public, max-age=86400` and an `ETag` equal to the variant's SHA-256.
- **Private serving**: `GET /files/{id}/content` requires ownership or `files:read`, always sends
  `Cache-Control: private, no-store`, and reads from `private/` (or `public/` for PUBLIC files, which
  keeps one code path for both).
- **Reverse proxy** (static serving of `public/`) is a later option. The paths are chosen so that it
  can be added without changing the API.

## Consequences
- Positive: every served byte comes from a re-encoded or verified source. Public access cannot reach
  temporary or private data.
- Negative: CPU-bound work runs in a worker thread. Large images should use a dedicated Celery queue
  with low concurrency, and the per-task time limit must be raised for them.
- Deleted public files may stay cached for up to one day (see ADR 0014).
