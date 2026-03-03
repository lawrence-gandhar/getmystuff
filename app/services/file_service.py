"""
File-upload service for file-based datasources (CSV, XLS, JSON, Parquet, Avro).

Public API
----------
check_file_exists     — look up the latest active record for a logical filename.
upload_datasource_files — save files to disk, record in datasource_files.
"""

import uuid
import asyncio
from pathlib import Path
from typing import List

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.datasource import DataSource, DatasourceFile
from app.utils.file_utils import (
    normalize_filename,
    versioned_filename,
    compute_checksum,
    ensure_upload_dir,
    ALLOWED_EXTENSIONS,
)


# ─────────────────────────────────────────────────────────────
# Public helpers
# ─────────────────────────────────────────────────────────────

async def check_file_exists(
    db: AsyncSession,
    datasource_id: uuid.UUID,
    user_id: uuid.UUID,
    filename: str,
) -> dict:
    """
    Check whether a file already exists for this datasource.

    Returns a dict with keys:
      exists             bool
      version            int   (0 when exists=False)
      normalized_base_name  str
      stored_filename    str | None
    """
    datasource = await db.get(DataSource, datasource_id)
    if not datasource or datasource.user_id != user_id:
        return {"exists": False, "version": 0, "normalized_base_name": normalize_filename(filename)}

    normalized = normalize_filename(filename)

    stmt = (
        select(DatasourceFile)
        .where(
            DatasourceFile.datasource_id == datasource_id,
            DatasourceFile.normalized_base_name == normalized,
            DatasourceFile.is_active == True,  # noqa: E712
        )
        .order_by(DatasourceFile.version.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        return {
            "exists": True,
            "version": existing.version,
            "normalized_base_name": normalized,
            "stored_filename": existing.stored_filename,
        }

    return {"exists": False, "version": 0, "normalized_base_name": normalized}


async def upload_datasource_files(
    db: AsyncSession,
    datasource_id: uuid.UUID,
    user_id: uuid.UUID,
    file_payloads: List[dict],
    override: bool = False,
) -> List[dict]:
    """
    Save uploaded files to disk and create ``DatasourceFile`` records.

    Parameters
    ----------
    file_payloads
        List of ``{"filename": str, "content": bytes}`` dicts — the route
        handler reads file content from the ``UploadFile`` objects before
        calling this function so this service stays framework-agnostic.
    override
        When *True*, the existing active record for a logical file is
        deactivated and the new file is stored as v1 (overwrite semantics).
        When *False* (default), a new version is appended (v2, v3, …) and
        the old record is left active.

    Returns
    -------
    List of per-file result dicts with keys:
      original_filename, stored_filename, version, status ("ok" | "error"),
      message (only present on error).
    """
    datasource = await db.get(DataSource, datasource_id)
    if not datasource or datasource.user_id != user_id:
        raise ValueError("Datasource not found or access denied")

    upload_dir = ensure_upload_dir(str(datasource_id))
    results: List[dict] = []

    for payload in file_payloads:
        original_name: str = payload["filename"]
        content: bytes = payload["content"]
        normalized = normalize_filename(original_name)

        # ── Extension validation ──────────────────────────────────────────
        ext = Path(normalized).suffix.lstrip(".")
        allowed = ALLOWED_EXTENSIONS.get(datasource.db_type, frozenset())
        if allowed and ext not in allowed:
            results.append({
                "original_filename": original_name,
                "status": "error",
                "message": (
                    f"Extension '.{ext}' is not allowed for a '{datasource.db_type}' "
                    f"datasource. Allowed: {', '.join(sorted(allowed))}"
                ),
            })
            continue

        # ── Resolve version ───────────────────────────────────────────────
        stmt = (
            select(DatasourceFile)
            .where(
                DatasourceFile.datasource_id == datasource_id,
                DatasourceFile.normalized_base_name == normalized,
                DatasourceFile.is_active == True,  # noqa: E712
            )
            .order_by(DatasourceFile.version.desc())
            .limit(1)
        )
        row = await db.execute(stmt)
        existing = row.scalar_one_or_none()

        if existing and override:
            # Deactivate old record; new file takes v1 slot.
            existing.is_active = False
            new_version = 1
        elif existing:
            # Append a new version alongside the existing file.
            new_version = existing.version + 1
        else:
            new_version = 1

        stored_name = versioned_filename(normalized, new_version)
        file_path = upload_dir / stored_name

        # ── Write file to disk (non-blocking) ─────────────────────────────
        await asyncio.to_thread(file_path.write_bytes, content)

        # ── Compute checksum (non-blocking) ───────────────────────────────
        checksum = await compute_checksum(file_path)

        # ── Persist DB record ─────────────────────────────────────────────
        db.add(DatasourceFile(
            datasource_id=datasource_id,
            uploaded_by=user_id,
            original_filename=original_name,
            stored_filename=stored_name,
            normalized_base_name=normalized,
            file_path=str(file_path),
            checksum=checksum,
            version=new_version,
            is_active=True,
        ))

        results.append({
            "original_filename": original_name,
            "stored_filename": stored_name,
            "version": new_version,
            "status": "ok",
        })

    await db.commit()
    return results
