# 14. File storage and resumable uploads

## Status
Accepted

## Context
The project had no file storage. Uploads need to survive large files (GB scale), dropped
connections, concurrent chunk retries, and hostile input, without adding infrastructure that small
deployments do not need (no S3/MinIO requirement).

## Decision
- **New `files` module** (`src/app/modules/files/`) following the four-layer layout. Business
  logic depends only on ports (`ObjectStoragePort`, `MalwareScannerPort`, `ImageProcessorPort`).
- **Storage**: local filesystem under `STORAGE_ROOT` (default `data/`, git-ignored), split into
  `tmp/` (chunks, never served), `private/` and `public/`. Only UUIDs and fixed names form paths;
  the client filename is display metadata only. `LocalFileSystemStorage` is the only code that maps
  keys to disk, and it checks every resolved path stays inside its region.
- **Chunked, resumable upload** as a session: `POST /files/uploads` → `PUT .../chunks/{i}` (raw body,
  streamed) → `POST .../complete` (202) with processing in the background. The session's status
  (`INITIATED → UPLOADING → COMPLETED`, or `ABORTED`/`EXPIRED`) is the TEMPORARY stage.
- **Size limits are enforced on bytes read, not on headers.** `Content-Length` is only a fast-reject
  hint. Each chunk stops at `expected + 1` bytes; the assembled file must equal the declared size.
  Chunk size (`UPLOAD_CHUNK_MAX_BYTES`, default 20 MiB) and total size (`UPLOAD_MAX_FILE_BYTES`,
  default 1 GiB) are separate settings.
- **Race-safe chunks.** Insert the `(upload, index)` row first (a unique index decides the winner),
  then rename the staged temp file into place. A retry with identical bytes returns
  `duplicate: true`; different bytes at the same index return 409. Partial files are never visible
  because every write is temp + `os.replace`.
- **Type detection by content, never by client claims.** Magic bytes decide the kind:
  IMAGE (JPEG/PNG/GIF/WebP), DOCUMENT (PDF), GENERIC (any other type, with a deny-list of executables
  and active markup). See `domain/signatures.py`.
- **Malware scanning is optional** (`MALWARE_SCAN_BACKEND`, default `none`). The ClamAV adapter speaks
  clamd's INSTREAM protocol directly, so no pip dependency is added. Without a scanner nothing is
  marked clean by mistake: `NoopScanner` is only selected when the backend is `none`, and a scanner
  that cannot answer fails the file closed (`FAILED`).
- **Lifecycle** for files: `UPLOADED → SCANNING → PROCESSING → AVAILABLE` (images), or
  `UPLOADED → SCANNING → AVAILABLE` (documents and generic files). `REJECTED` covers unacceptable
  content, `FAILED` covers system problems, `DELETED` is a soft delete. Every transition is a
  conditional UPDATE, so a re-delivered task cannot repeat or skip work.
- **Background processing** through the existing Celery app: `files.process_upload` (assembly,
  detection, scan, image pipeline) and `files.cleanup`, scheduled by a `beat` compose service.
  Cleanup is idempotent. Dispatch happens exactly once per upload (the first `complete` call).
- **Rate limiting**: chunk PUTs use a dedicated tier `UPLOAD` (`RATE_LIMIT_UPLOAD`, default
  600/minute). See the amendment in ADR 0007.
- **Authorization**: the owner of an upload or file can always act on it. Other users need the
  matching `files:*` permission in the same tenant. Everyone else gets 404, not 403 (anti-IDOR).

## Consequences
- Positive: no new infrastructure for small and medium deployments. The port makes an S3/MinIO
  adapter a drop-in change. Hostile input is contained at each stage.
- Negative: the filesystem adapter is single-host. `api`, `worker` and `beat` must share the
  `files_data` volume. Horizontal scaling needs a shared volume or the object-storage adapter.
- Cleanup scans terminal sessions on every run. This is acceptable at the current scale and is the
  first thing to revisit if the session table grows large.
- Deleted public files may remain in browser or CDN caches for up to the `max-age` (one day).
- Image processing and public serving are described in ADR 0015.
