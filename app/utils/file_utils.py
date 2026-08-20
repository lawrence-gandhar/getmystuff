import re
import hashlib
import asyncio
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

# Base directory for all datasource file uploads.
# Relative to the project root (where the app is started from).
UPLOAD_BASE = Path("app/uploads")

# Base directory for chatbot widget branding assets (logo, background image,
# bot icon). Unlike datasource uploads, these live under static/ because the
# embedded widget script runs on third-party sites and must be able to fetch
# them over a public URL (see chatbot_widget_settings_service).
WIDGET_UPLOAD_BASE = Path("static/chatbot_widgets")

# Allowed image extensions for widget branding uploads (lowercase, no dot).
ALLOWED_IMAGE_EXTENSIONS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp", "svg"})

# Max size for a single widget branding image upload.
MAX_IMAGE_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB

# Base directory for AI Fallback knowledge-base document uploads (Flow
# Builder). Keyed by the owning FlowNodeKnowledgeBase's own uuid, mirroring
# ensure_upload_dir's per-owner layout below.
KNOWLEDGE_BASE_UPLOAD_BASE = Path("app/uploads/knowledge_base")

# Allowed knowledge-base document extensions (lowercase, no dot).
ALLOWED_KB_EXTENSIONS: frozenset[str] = frozenset({"pdf", "txt", "docx"})

# HTML <input accept="..."> string for knowledge-base document uploads.
KB_ACCEPT_ATTR = ".pdf,.txt,.docx"

# Max size for a single knowledge-base document upload.
MAX_KB_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

# Base directory for generated export files (Downloader Agents). Note the path:
# "uploads/exports", NOT "app/uploads/exports" like the two above.
#
# That difference is deliberate and load-bearing. docker-compose mounts the named
# `uploads` volume at /app/uploads, while UPLOAD_BASE resolves to /app/app/uploads —
# which falls inside the `.:/app` bind mount, i.e. the host's source tree. Datasource
# uploads landing there is pre-existing behaviour; generated exports must not, so this
# base points at the volume that actually survives a rebuild.
#
# Also deliberately not under static/, which main.py serves with no authentication at
# all. An export is one user's data, and it is served by
# app/routes/downloader_agents/download_routes.py after an ownership check.
EXPORT_BASE = Path("uploads/exports")

# Base directory for *finished* export artifacts, the files a user actually downloads.
# One directory per chat session inside it:
#
#     uploads/file_downloaders/<session-id>/project_details_2026-08-07.csv
#
# Separate from EXPORT_BASE, which holds the per-batch part files an export is built
# from. Those are scratch and are deleted the moment the merge succeeds; these are the
# deliverable, and grouping them by session is what lets the reaper — and a session
# ending — clear out one visitor's files without touching anybody else's.
#
# Same two constraints as EXPORT_BASE, for the same reasons: under the `uploads` volume
# so a rebuild does not take the files with it, and NOT under static/, which main.py
# serves with no authentication. The URL is /file_downloaders/<session>/<file>, but it
# is served by app/routes/downloader_agents/download_routes.py after the session,
# expiry and ownership checks — never as a static directory.
DOWNLOAD_BASE = Path("uploads/file_downloaders")

# db_type values that represent file-based (non-connection) datasources.
FILE_BASED_TYPES: frozenset[str] = frozenset({"csv", "xls", "json", "parquet", "avro"})

# Allowed file extensions per db_type (lowercase, no leading dot).
ALLOWED_EXTENSIONS: dict[str, frozenset[str]] = {
    "csv":     frozenset({"csv"}),
    "xls":     frozenset({"xls", "xlsx"}),
    "json":    frozenset({"json", "jsonl"}),
    "parquet": frozenset({"parquet"}),
    "avro":    frozenset({"avro"}),
}

# HTML <input accept="..."> string per db_type.
ACCEPT_ATTRS: dict[str, str] = {
    "csv":     ".csv",
    "xls":     ".xls,.xlsx",
    "json":    ".json,.jsonl",
    "parquet": ".parquet",
    "avro":    ".avro",
}

# ─────────────────────────────────────────────────────────────
# Private regex helpers (compiled once at import time)
# ─────────────────────────────────────────────────────────────

# Matches any character that is NOT alphanum, dot, dash, or underscore.
_UNSAFE_CHARS = re.compile(r"[^\w.\-]")
# Collapses consecutive separator characters (. _ -) into a single underscore.
_MULTI_SEP = re.compile(r"[._\-]{2,}")


# ─────────────────────────────────────────────────────────────
# Filename helpers
# ─────────────────────────────────────────────────────────────

def normalize_filename(filename: str) -> str:
    """
    Produce a safe, lowercase filename from an arbitrary user-supplied name.

    Rules applied in order:
      1. Strip leading/trailing whitespace.
      2. Lowercase everything.
      3. Replace spaces with underscores.
      4. Remove any character that is not alphanum, dot, dash, or underscore.
      5. Collapse consecutive separators (.. __ --) to a single underscore.
      6. Strip any leading/trailing separators.
      7. Fall back to "file" if the result is empty after normalization.

    Examples::

        normalize_filename("Sales Data 2024.csv")  ->  "sales_data_2024.csv"
        normalize_filename("  My File!!.xlsx ")    ->  "my_file.xlsx"
        normalize_filename("../../etc/passwd")      ->  "etcpasswd"
    """
    name = filename.strip().lower()
    name = name.replace(" ", "_")
    name = _UNSAFE_CHARS.sub("", name)
    name = _MULTI_SEP.sub("_", name)
    name = name.strip("._-")
    return name or "file"


def versioned_filename(base_name: str, version: int) -> str:
    """
    Return the stored filename for a given version number.

    version == 1  →  <stem>.<ext>          (the base name, unchanged)
    version >= 2  →  <stem>_v<N>.<ext>

    Examples::

        versioned_filename("sales_data.csv", 1)  ->  "sales_data.csv"
        versioned_filename("sales_data.csv", 3)  ->  "sales_data_v3.csv"
    """
    if version <= 1:
        return base_name
    p = Path(base_name)
    return f"{p.stem}_v{version}{p.suffix}"


# ─────────────────────────────────────────────────────────────
# Checksum
# ─────────────────────────────────────────────────────────────

async def compute_checksum(file_path: Path) -> str:
    """Return the SHA-256 hex digest of a file (runs in a thread executor)."""

    def _hash() -> str:
        h = hashlib.sha256()
        with file_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    return await asyncio.to_thread(_hash)


# ─────────────────────────────────────────────────────────────
# Directory helpers
# ─────────────────────────────────────────────────────────────

def ensure_upload_dir(datasource_id: str) -> Path:
    """
    Return (and create if absent) the upload directory for a datasource.

    Path layout:  <UPLOAD_BASE>/<datasource_id>/
    """
    upload_dir = UPLOAD_BASE / str(datasource_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def ensure_widget_upload_dir(key_uuid: str) -> Path:
    """
    Return (and create if absent) the upload directory for one chatbot
    widget's branding assets.

    Path layout:  <WIDGET_UPLOAD_BASE>/<key_uuid>/
    """
    upload_dir = WIDGET_UPLOAD_BASE / str(key_uuid)
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def ensure_knowledge_base_upload_dir(knowledge_base_uuid: str) -> Path:
    """
    Return (and create if absent) the upload directory for one AI Fallback
    node's knowledge-base documents.

    Path layout:  <KNOWLEDGE_BASE_UPLOAD_BASE>/<knowledge_base_uuid>/
    """
    upload_dir = KNOWLEDGE_BASE_UPLOAD_BASE / str(knowledge_base_uuid)
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


# ─────────────────────────────────────────────────────────────
# Multipart upload helpers
#
# A file part is the one thing in a request body that a Pydantic schema cannot
# validate: it is a stream, not a value, and its rules (extension, size) are
# checked by the ingestion service after the bytes are in hand. So the schema
# layer drops upload parts (see app.schemas.base.form_to_dict) and the two
# helpers below are how a route reads them instead — without calling
# request.form() itself, which is what the module-schemas audit forbids.
#
# Litestar caches the parsed form on the request, so a handler that reads its
# scalar fields through a schema and its files through here parses the body once.
# ─────────────────────────────────────────────────────────────

async def read_upload_payloads(request, field: str = "files") -> list[dict]:
    """
    Every uploaded file on ``field``, as ``{"filename", "content"}`` dicts.

    Reading the bytes here rather than in the service is deliberate: an
    ``UploadFile`` is framework-specific, and the ingestion services take plain
    dicts so they can be tested without a request at all.

    Parts with no filename are skipped — an empty ``<input type="file">`` still
    posts a part, and treating that as a zero-byte upload would create an empty
    file record on every submit.
    """
    form = await request.form()
    raw_files = form.getall(field, []) if hasattr(form, "getall") else []

    payloads: list[dict] = []
    for upload in raw_files:
        if not getattr(upload, "filename", ""):
            continue
        payloads.append({"filename": upload.filename, "content": await upload.read()})

    return payloads


async def read_upload_field(request, field: str):
    """
    One optional uploaded file, or ``None`` when the input was left empty.

    Used by the chatbot widget's branding form, where each image is its own
    field and "not touched" has to be distinguishable from "cleared" — the
    clearing is a separate checkbox, so this must return ``None`` rather than a
    zero-byte part.
    """
    form = await request.form()
    upload = form.get(field)

    if upload is None or not getattr(upload, "filename", ""):
        return None

    return upload
