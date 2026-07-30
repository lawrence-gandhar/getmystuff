import re
import asyncio
import uuid
from datetime import datetime
from pathlib import Path

from litestar import post, get, delete, Controller
from litestar.response import Response, Template
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, exists, func
from litestar.connection import Request
from litestar.exceptions import HTTPException

from app.models.user import User
from app.models.datasource import DataSource, DatasourceFile
from app.services.datasource.datasource_service import (
    test_connection,
    create_datasource,
    update_datasource_name,
    get_datasource_objects,
    get_datasource_table_schema,
    get_user_datasources,
    delete_datasource,
    toggle_datasource_active,
    toggle_column_status_service,
    toggle_table_status_service,
    search_sort_tables,
    datasource_crud,
)
from app.services.datasource.file_service import check_file_exists, upload_datasource_files
from app.utils.file_utils import FILE_BASED_TYPES, ACCEPT_ATTRS, ALLOWED_EXTENSIONS
from app.db.auth import require_auth
from app.db.db_utils import fetch_file_schema

# Compiled once at import time — reused on every validation request.
_NAME_RE = re.compile(r'^[a-z0-9_]+$')

# ─────────────────────────────────────────────────────────────
# File preview helpers — each runs inside asyncio.to_thread so
# they never block the event loop. Pandas is imported lazily to
# keep startup fast when the library is not installed.
# ─────────────────────────────────────────────────────────────

_PREVIEW_PAGE_SIZE = 50


def _read_csv_chunk(file_path: str, page: int) -> tuple | None:
    """Return one page of CSV data without loading the whole file.

    Uses pd.read_csv(chunksize=…) so only two chunks are ever in memory:
    the target chunk and one extra row used to detect has_next.
    """
    import pandas as pd  # lazy import

    chunks = pd.read_csv(
        file_path, chunksize=_PREVIEW_PAGE_SIZE, dtype=str, low_memory=False
    )
    target = None
    has_next = False
    for i, chunk in enumerate(chunks):
        if i == page - 1:
            target = chunk
        elif i == page:
            has_next = True
            break
    if target is None:
        return None
    columns = target.columns.tolist()
    rows = target.fillna("").values.tolist()
    return columns, rows, has_next


def _read_excel_chunk(file_path: str, page: int) -> tuple | None:
    """Return one page of XLS/XLSX data using skiprows + nrows.

    Row 0 (header) is always kept; data rows from previous pages are
    skipped by index. Reads _PREVIEW_PAGE_SIZE + 1 rows to probe has_next
    without loading the entire file into memory.
    """
    import pandas as pd  # lazy import

    skip_data = (page - 1) * _PREVIEW_PAGE_SIZE
    # Build list of 1-based row indices to skip (keeps row 0 as header).
    skip_rows = list(range(1, skip_data + 1)) if skip_data > 0 else None
    df = pd.read_excel(
        file_path,
        skiprows=skip_rows,
        nrows=_PREVIEW_PAGE_SIZE + 1,  # +1 to detect whether a next page exists
        dtype=str,
    )
    if df.empty:
        return None
    has_next = len(df) > _PREVIEW_PAGE_SIZE
    if has_next:
        df = df.iloc[:_PREVIEW_PAGE_SIZE]
    columns = df.columns.tolist()
    rows = df.fillna("").values.tolist()
    return columns, rows, has_next


def _read_parquet_chunk(file_path: str, page: int) -> tuple | None:
    """Return one page of Parquet data using pyarrow's iter_batches.

    Only two batches are materialised at most (target + has_next probe),
    regardless of total file size.
    """
    import pyarrow.parquet as pq  # lazy import

    pf = pq.ParquetFile(file_path)
    target_idx = page - 1
    target_batch = None
    has_next = False
    for i, batch in enumerate(pf.iter_batches(batch_size=_PREVIEW_PAGE_SIZE)):
        if i == target_idx:
            target_batch = batch
        elif i == target_idx + 1:
            has_next = True
            break
    if target_batch is None or len(target_batch) == 0:
        return None
    df = target_batch.to_pandas().fillna("").astype(str)
    columns = df.columns.tolist()
    rows = df.values.tolist()
    return columns, rows, has_next


def _read_json_chunk(file_path: str, page: int) -> dict:
    """Read JSON / JSONL files.

    JSONL and JSON arrays of flat dicts → table view (paginated).
    Nested structures → formatted text view.
    Returns a dict with a 'type' key: 'table' or 'json'.
    """
    import json as _json

    ext = Path(file_path).suffix.lower().lstrip(".")

    if ext == "jsonl":
        records = []
        with open(file_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(_json.loads(line))
        start = (page - 1) * _PREVIEW_PAGE_SIZE
        end = start + _PREVIEW_PAGE_SIZE
        page_records = records[start:end]
        has_next = len(records) > end
        if not page_records:
            return {"type": "json", "content": "[]", "page": page, "has_next": False}
        if all(
            isinstance(r, dict) and all(not isinstance(v, (dict, list)) for v in r.values())
            for r in page_records
        ):
            columns = list(dict.fromkeys(k for r in page_records for k in r.keys()))
            rows = [[str(r.get(c, "")) for c in columns] for r in page_records]
            return {"type": "table", "columns": columns, "rows": rows, "page": page, "has_next": has_next}
        return {
            "type": "json",
            "content": _json.dumps(page_records, indent=2, ensure_ascii=False),
            "page": page,
            "has_next": has_next,
        }

    # Regular .json
    with open(file_path, "r", encoding="utf-8") as fh:
        data = _json.load(fh)

    if isinstance(data, list):
        start = (page - 1) * _PREVIEW_PAGE_SIZE
        end = start + _PREVIEW_PAGE_SIZE
        has_next = len(data) > end
        page_data = data[start:end]
        if page_data and all(
            isinstance(r, dict) and all(not isinstance(v, (dict, list)) for v in r.values())
            for r in page_data
        ):
            columns = list(dict.fromkeys(k for r in page_data for k in r.keys()))
            rows = [[str(r.get(c, "")) for c in columns] for r in page_data]
            return {"type": "table", "columns": columns, "rows": rows, "page": page, "has_next": has_next}
        return {
            "type": "json",
            "content": _json.dumps(page_data, indent=2, ensure_ascii=False),
            "page": page,
            "has_next": has_next,
        }

    # Object / scalar — render the whole thing formatted
    return {"type": "json", "content": _json.dumps(data, indent=2, ensure_ascii=False), "page": 1, "has_next": False}


def _read_xml_chunk(file_path: str, page: int) -> dict:
    """Read XML file and return pretty-printed content."""
    import xml.dom.minidom

    with open(file_path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    try:
        dom = xml.dom.minidom.parseString(raw.encode("utf-8"))
        pretty = dom.toprettyxml(indent="  ")
        # minidom adds a spurious blank first line — remove it
        lines = [ln for ln in pretty.splitlines() if ln.strip()]
        raw = "\n".join(lines)
    except Exception:
        pass  # Fall back to raw content if parsing fails
    return {"type": "xml", "content": raw, "page": 1, "has_next": False}


def _read_file_chunk(file_path: str, page: int) -> tuple | dict | None:
    """Dispatch to the correct reader based on file extension.

    Returns:
      - (columns, rows, has_next) tuple for CSV / XLS / Parquet
      - dict with 'type' key for JSON / JSONL / XML
      - None when the page is empty
    Raises FileNotFoundError / ValueError for unrecoverable conditions.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    ext = path.suffix.lower().lstrip(".")
    if ext == "csv":
        return _read_csv_chunk(file_path, page)
    if ext in ("xls", "xlsx"):
        return _read_excel_chunk(file_path, page)
    if ext == "parquet":
        return _read_parquet_chunk(file_path, page)
    if ext in ("json", "jsonl"):
        return _read_json_chunk(file_path, page)
    if ext == "xml":
        return _read_xml_chunk(file_path, page)
    raise ValueError(f"Unsupported file format: .{ext}")


class DataSourceController(Controller):
    path = "/datasource"
    dependencies = {"user": require_auth}

    # --------------------------
    # CREATE DATASOURCE
    # --------------------------
    @post("/create")
    async def create(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Response | Template:

        form = await request.form()

        datasource_name = form.get("datasource_name", "")
        db_type = form.get("db_type")
        host = form.get("host")
        port = form.get("port")
        database_name = form.get("database_name")
        username = form.get("username")
        password = form.get("password")

        try:
            datasource = await create_datasource(
                db=db,
                user_id=user.id,
                datasource_name=datasource_name,
                db_type=db_type,
                host=host,
                port=port,
                database_name=database_name,
                username=username,
                password=password,
                connection_tester=test_connection,
            )

            # For file-based datasources, process any files uploaded with
            # the creation form, then return the upload widget so the user
            # can add more files without leaving the offcanvas.
            if db_type in FILE_BASED_TYPES:
                raw_files = form.getall("files")
                file_payloads = [
                    {"filename": uf.filename, "content": await uf.read()}
                    for uf in raw_files
                    if hasattr(uf, "filename") and uf.filename
                ]
                upload_results = []
                if file_payloads:
                    try:
                        upload_results = await upload_datasource_files(
                            db=db,
                            datasource_id=datasource.uuid,
                            user_id=user.id,
                            file_payloads=file_payloads,
                        )
                    except Exception as exc:
                        # Roll back the datasource so the user can retry
                        # with the same name rather than hitting 409.
                        await db.delete(datasource)
                        await db.commit()
                        return Response(
                            f"<div class='alert alert-danger'>File upload failed: {exc}. "
                            f"The datasource was not saved. Please try again.</div>",
                            status_code=500,
                            media_type="text/html",
                        )

                return Template(
                    template_name="datasources/file_upload_widget.htm",
                    context={
                        "datasource_id": str(datasource.uuid),
                        "db_type": db_type,
                        "accept_attr": ACCEPT_ATTRS.get(db_type, ""),
                        "allowed_ext": ", ".join(
                            f".{e}" for e in sorted(ALLOWED_EXTENSIONS.get(db_type, set()))
                        ),
                        "initial_results": upload_results,
                    },
                )

            return Response(
                "<div class='alert alert-success'>Datasource Added Successfully</div>",
                media_type="text/html",
            )
        except HTTPException as e:
            # Preserve the original HTTP status code (400 / 409 / 422) so
            # HTMX / the browser can distinguish conflict from bad input.
            return Response(
                f"<div class='alert alert-danger'>{e.detail}</div>",
                status_code=e.status_code,
                media_type="text/html",
            )

    # --------------------------
    # UPDATE DATASOURCE NAME
    # --------------------------
    @post("/{datasource_id:uuid}/rename")
    async def rename(
        self,
        datasource_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Response:
        """Rename an existing datasource.  Returns an HTMX-friendly HTML fragment."""

        form = await request.form()
        datasource_name = form.get("datasource_name", "")

        try:
            await update_datasource_name(
                db=db,
                datasource_id=datasource_id,
                user_id=user.id,
                datasource_name=datasource_name,
            )
            return Response(
                "<div class='alert alert-success'>Datasource renamed successfully</div>",
                media_type="text/html",
            )
        except HTTPException as e:
            return Response(
                f"<div class='alert alert-danger'>{e.detail}</div>",
                status_code=e.status_code,
                media_type="text/html",
            )

    # --------------------------
    # VALIDATE DATASOURCE NAME
    # (called on blur via HTMX — returns an HTML fragment, never JSON)
    # --------------------------
    @post("/validate-name")
    async def validate_datasource_name(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Response:
        """
        Real-time name validation endpoint consumed by the HTMX blur trigger
        on the datasource_name input.

        Checks (in order):
          1. Non-empty after stripping whitespace.
          2. Length ≤ 255 characters.
          3. Regex: only lowercase letters, digits, underscores.
          4. Case-insensitive uniqueness via a PostgreSQL EXISTS query.

        Always returns an HTML snippet so HTMX can swap it directly into
        the ``#datasource-name-feedback`` div — no JSON, no page reload.
        """
        form = await request.form()
        raw = form.get("datasource_name", "")
        name = raw.strip().lower()

        # ── 1. Empty ────────────────────────────────────────────────────────
        if not name:
            return Response(
                "<div class='text-danger small mt-1'>"
                "<i class='las la-times-circle'></i> Name is required."
                "</div>",
                media_type="text/html",
            )

        # ── 2. Length ────────────────────────────────────────────────────────
        if len(name) > 255:
            return Response(
                "<div class='text-danger small mt-1'>"
                "<i class='las la-times-circle'></i> "
                "Name must not exceed 255 characters."
                "</div>",
                media_type="text/html",
            )

        # ── 3. Character set ─────────────────────────────────────────────────
        if not _NAME_RE.match(name):
            return Response(
                "<div class='text-danger small mt-1'>"
                "<i class='las la-times-circle'></i> "
                "Only lowercase letters (a–z), digits (0–9), "
                "and underscores (_) are allowed."
                "</div>",
                media_type="text/html",
            )

        # ── 4. Case-insensitive uniqueness check ─────────────────────────────
        # Uses an EXISTS subquery so PostgreSQL stops scanning as soon as it
        # finds the first match — no full table scan, no row data returned.
        stmt = select(
            exists().where(
                func.lower(DataSource.datasource_name) == name
            )
        )
        result = await db.execute(stmt)
        already_taken = result.scalar()

        if already_taken:
            return Response(
                "<div class='text-danger small mt-1'>"
                "<i class='las la-times-circle'></i> "
                "This name is already taken. Please choose a different one."
                "</div>",
                media_type="text/html",
            )

        # ── All checks passed ────────────────────────────────────────────────
        return Response(
            "<div class='text-success small mt-1'>"
            "<i class='las la-check-circle'></i> Name is available."
            "</div>",
            media_type="text/html",
        )

    # --------------------------
    # GET TABLES / COLLECTIONS
    # --------------------------
    @get("/{datasource_id:uuid}")
    async def get_objects(
        self,
        datasource_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> Template:

        data = await get_datasource_objects(
            db=db,
            datasource_id=datasource_id,
            user_id=user.id,
        )

        return Template(
            template_name="datasources/schema_preview.htm",
            context={
                "objects": data["objects"],
                "datasource_id": datasource_id,
                "datasource_name": data["datasource_name"],
                "configuration_data": data["configuration_data"],
            },
        )

    # --------------------------
    # LIST ALL DATASOURCES
    # --------------------------
    @get("/")
    async def list_datasources(
        self,
        db: AsyncSession,
        user: User,
    ) -> Template:

        datasources = await get_user_datasources(
            db=db,
            user_id=user.id,
        )

        return Template(
            template_name="datasources/index.htm",
            context={
                "user": user,
                "datasources": datasources,
                "active": "datasource"
            },
        )

    @get("/{datasource_id:uuid}/table/{table_name:str}/view")
    async def view_table_schema(
        self,
        datasource_id: uuid.UUID,
        table_name: str,
        db: AsyncSession,
        user: User,
    ) -> Template:

        datasource = await get_datasource_objects(
            db=db,
            datasource_id=datasource_id,
            user_id=user.id,
        )

        configuration_data = datasource.get("configuration_data") or {}
        table_schema = configuration_data.get(table_name, {})
        column_data = table_schema.get("column_data") or {}
        table_status = table_schema.get("status", "active")

        if not column_data:
            # configuration_data was never populated (e.g. existing datasources
            # created before the collect_datasource_metadata return bug was fixed).
            # Fall back to a live schema fetch from the actual database.
            live = await get_datasource_table_schema(
                db=db,
                datasource_id=datasource_id,
                user_id=user.id,
                table_name=table_name,
            )
            column_data = {
                col["column"]: {"column_name": col["column"], "status": "active"}
                for col in live.get("schema", [])
            }

        return Template(
            template_name="datasources/column_view.htm",
            context={
                "table_name": table_name,
                "schema": column_data,
                "datasource_id": str(datasource_id),
                "table_status": table_status,
            },
        )

    @get("/{datasource_id:uuid}/file/{file_id:uuid}/view")
    async def view_file_schema(
        self,
        datasource_id: uuid.UUID,
        file_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> Template:
        datasource = await datasource_crud.get_by_uuid(db, datasource_id)
        if not datasource or datasource.user_id != user.id:
            raise HTTPException(status_code=404, detail="Datasource not found")

        result = await db.execute(
            select(DatasourceFile).where(
                DatasourceFile.uuid == file_id,
                DatasourceFile.datasource_id == datasource.id,
                DatasourceFile.is_active == True,  # noqa: E712
            )
        )
        file = result.scalar_one_or_none()
        if not file:
            raise HTTPException(status_code=404, detail="File not found")

        columns = await fetch_file_schema(file.file_path, datasource.db_type)
        schema = {
            col["column"]: {"column_name": col["column"], "status": "active"}
            for col in columns
        }

        return Template(
            template_name="datasources/column_view.htm",
            context={
                "table_name": file.original_filename,
                "schema": schema,
                "datasource_id": str(datasource_id),
                "table_status": "active",
            },
        )

    @post("/{datasource_id:uuid}/table/{table_name:str}/column/{column_name:str}/toggle")
    async def toggle_column_status(
        self,
        datasource_id: uuid.UUID,
        table_name: str,
        column_name: str,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:

        form = await request.form()
        new_status = form.get("status")

        if new_status not in {"active", "inactive"}:
            raise HTTPException(status_code=400)

        updated_column = await toggle_column_status_service(
            db=db,
            datasource_id=datasource_id,
            user_id=user.id,
            table_name=table_name,
            column_name=column_name,
            new_status=new_status,
        )

        if not updated_column:
            raise HTTPException(status_code=404)

        # Re-read the datasource to get the current table status for rendering
        datasource_obj = await datasource_crud.get_by_uuid(db, datasource_id)
        config = (datasource_obj.configuration_data or {}) if datasource_obj else {}
        table_status = config.get(table_name, {}).get("status", "active")

        return Template(
            template_name="datasources/column_row.htm",
            context={
                "col": updated_column,
                "datasource_id": str(datasource_id),
                "table_name": table_name,
                "table_status": table_status,
            },
        )

    @post("/{datasource_id:uuid}/table/{table_name:str}/toggle-status")
    async def toggle_table_status(
        self,
        datasource_id: uuid.UUID,
        table_name: str,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:

        form = await request.form()
        new_status = form.get("status")

        if new_status not in {"active", "inactive"}:
            raise HTTPException(status_code=400)

        datasource = await toggle_table_status_service(
            db=db,
            datasource_id=datasource_id,
            user_id=user.id,
            table_name=table_name,
            new_status=new_status,
        )

        if not datasource:
            raise HTTPException(status_code=404)

        return Template(
            template_name="datasources/table_row.htm",
            context={
                "datasource": datasource,
                "datasource_id": str(datasource_id),
                "table_name": table_name
            },
        )

    @get("/{datasource_id:uuid}/tables")
    async def list_tables(
        self,
        datasource_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        search = request.query_params.get("search", "").lower()
        status_filter = request.query_params.get("status_filter", "all")
        sort_by = request.query_params.get("sort_by", "az")

        tables = await search_sort_tables(
            db=db,
            datasource_id=datasource_id,
            user_id=user.id,
            search=search,
            status_filter=status_filter,
            sort_by=sort_by
        )

        return Template(
            template_name="datasources/table_list.htm",
            context={
                "objects": tables,
                "datasource_id": datasource_id,
            }
        )

    # --------------------------
    # DELETE DATASOURCE
    # --------------------------
    @post("/{datasource_id:uuid}/delete")
    async def delete_ds(
        self,
        datasource_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> Response:
        try:
            await delete_datasource(db=db, datasource_id=datasource_id, user_id=user.id)
            return Response("", media_type="text/html")
        except HTTPException as e:
            return Response(
                f"<div class='alert alert-danger'>{e.detail}</div>",
                status_code=e.status_code,
                media_type="text/html",
            )

    # --------------------------
    # TOGGLE DATASOURCE ACTIVE / INACTIVE
    # --------------------------
    @post("/{datasource_id:uuid}/toggle-active")
    async def toggle_active(
        self,
        datasource_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> Template:
        try:
            datasource = await toggle_datasource_active(
                db=db, datasource_id=datasource_id, user_id=user.id
            )
            return Template(
                template_name="datasources/ds_row.htm",
                context={
                    "datasource": datasource,
                    "file_types": list(FILE_BASED_TYPES),
                    "nosql_types": ["mongodb"],
                },
            )
        except HTTPException as e:
            return Response(
                f"<div class='alert alert-danger'>{e.detail}</div>",
                status_code=e.status_code,
                media_type="text/html",
            )

    # --------------------------
    # CHECK FILE EXISTS
    # (called per-file by JS before the upload — returns an HTML badge)
    # --------------------------
    @post("/{datasource_id:uuid}/check-file")
    async def check_file(
        self,
        datasource_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Response:
        """
        Return an HTML badge for a single filename indicating whether a
        previous version already exists for this datasource.
        """
        form = await request.form()
        filename = form.get("filename", "").strip()

        if not filename:
            return Response(
                "<div class='text-danger small'>No filename provided.</div>",
                media_type="text/html",
            )

        info = await check_file_exists(
            db=db,
            datasource_id=datasource_id,
            user_id=user.id,
            filename=filename,
        )

        if info["exists"]:
            next_v = info["version"] + 1
            html = (
                f"<div class='d-flex align-items-center gap-2 mb-1 file-check-row'>"
                f"<i class='las la-file-alt text-warning'></i>"
                f"<span class='small text-truncate'>{filename}</span>"
                f"<span class='badge bg-warning text-dark ms-auto' data-conflict='1'>"
                f"v{info['version']} exists &rarr; will create v{next_v}"
                f"</span>"
                f"</div>"
            )
        else:
            html = (
                f"<div class='d-flex align-items-center gap-2 mb-1 file-check-row'>"
                f"<i class='las la-file-alt text-success'></i>"
                f"<span class='small text-truncate'>{filename}</span>"
                f"<span class='badge bg-success ms-auto'>New file</span>"
                f"</div>"
            )

        return Response(html, media_type="text/html")

    # --------------------------
    # UPLOAD FILES
    # --------------------------
    @post("/{datasource_id:uuid}/upload-files")
    async def upload_files(
        self,
        datasource_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Response:
        """
        Accept multipart file uploads and persist them under
        ``app/uploads/<datasource_id>/``.  Returns an HTML fragment
        listing per-file outcomes.
        """
        form = await request.form()
        raw_files = form.getall("files")
        override = form.get("override", "no") == "yes"

        if not raw_files:
            return Response(
                "<div class='alert alert-warning'>No files were submitted.</div>",
                media_type="text/html",
            )

        # Read file content here (framework-specific) before passing plain
        # dicts to the framework-agnostic service layer.
        file_payloads = []
        for uf in raw_files:
            if not hasattr(uf, "filename"):
                continue
            content = await uf.read()
            file_payloads.append({"filename": uf.filename, "content": content})

        if not file_payloads:
            return Response(
                "<div class='alert alert-warning'>No valid files were submitted.</div>",
                media_type="text/html",
            )

        try:
            results = await upload_datasource_files(
                db=db,
                datasource_id=datasource_id,
                user_id=user.id,
                file_payloads=file_payloads,
                override=override,
            )
        except ValueError as exc:
            return Response(
                f"<div class='alert alert-danger'>{exc}</div>",
                status_code=403,
                media_type="text/html",
            )

        # Build an HTML summary of all per-file outcomes.
        rows = []
        for r in results:
            if r["status"] == "ok":
                rows.append(
                    f"<li class='list-group-item list-group-item-success d-flex align-items-center gap-2'>"
                    f"<i class='las la-check-circle'></i>"
                    f"<span class='text-truncate'><strong>{r['original_filename']}</strong>"
                    f" saved as <code>{r['stored_filename']}</code> (v{r['version']})</span>"
                    f"</li>"
                )
            else:
                rows.append(
                    f"<li class='list-group-item list-group-item-danger d-flex align-items-center gap-2'>"
                    f"<i class='las la-times-circle'></i>"
                    f"<span class='text-truncate'><strong>{r['original_filename']}</strong>"
                    f" — {r['message']}</span>"
                    f"</li>"
                )

        html = (
            "<ul class='list-group list-group-flush mt-2'>"
            + "".join(rows)
            + "</ul>"
        )
        return Response(html, media_type="text/html")

    # --------------------------
    # FILE DATA PREVIEW (paginated, memory-safe)
    # --------------------------
    @get("/{datasource_id:uuid}/file-preview")
    async def file_preview(
        self,
        datasource_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> dict:
        """Return one page of data from an uploaded file datasource as JSON.

        Never loads the full file — uses chunk iterators / skiprows so that
        only _PREVIEW_PAGE_SIZE rows are in memory at any time.

        Query params
        ------------
        page     : int   (default 1)
        file_id  : uuid  public id of a specific DatasourceFile record (optional)
        """
        try:
            page = max(1, int(request.query_params.get("page", "1")))
        except (ValueError, TypeError):
            page = 1

        file_id_str = request.query_params.get("file_id")

        # Verify ownership
        datasource = await datasource_crud.get_by_uuid(db, datasource_id)
        if not datasource or datasource.user_id != user.id:
            return {"error": "Datasource not found"}

        # Resolve the target file
        if file_id_str:
            try:
                file_uuid = uuid.UUID(file_id_str)
            except ValueError:
                return {"error": "Invalid file ID"}
            stmt = select(DatasourceFile).where(
                DatasourceFile.uuid == file_uuid,
                DatasourceFile.datasource_id == datasource.id,
                DatasourceFile.is_active == True,  # noqa: E712
            )
        else:
            # Default: most recently uploaded active file
            stmt = (
                select(DatasourceFile)
                .where(
                    DatasourceFile.datasource_id == datasource.id,
                    DatasourceFile.is_active == True,  # noqa: E712
                )
                .order_by(DatasourceFile.uploaded_at.desc())
                .limit(1)
            )

        result = await db.execute(stmt)
        target_file = result.scalar_one_or_none()

        if not target_file:
            return {
                "error": (
                    "No uploaded files found for this datasource. "
                    "Please upload a file first."
                )
            }

        # All active files — sent to the frontend for the file selector
        all_stmt = (
            select(DatasourceFile)
            .where(
                DatasourceFile.datasource_id == datasource.id,
                DatasourceFile.is_active == True,  # noqa: E712
            )
            .order_by(DatasourceFile.uploaded_at.desc())
        )
        all_result = await db.execute(all_stmt)
        files_list = [
            {"id": str(f.uuid), "filename": f.original_filename}
            for f in all_result.scalars().all()
        ]

        # Read the chunk in a thread — never blocks the event loop
        try:
            chunk = await asyncio.to_thread(
                _read_file_chunk, target_file.file_path, page
            )
        except FileNotFoundError:
            return {
                "error": "File not found on disk. It may have been moved or deleted."
            }
        except ValueError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Failed to read file: {exc}"}

        if chunk is None:
            return {
                "error": (
                    "No data found at this page. "
                    "The file may be empty or the page number is out of range."
                )
            }

        base = {
            "file_id": str(target_file.uuid),
            "filename": target_file.original_filename,
            "files": files_list,
        }

        # JSON / XML readers return a dict; tabular readers return a tuple
        if isinstance(chunk, dict):
            return {**chunk, **base}

        columns, rows, has_next = chunk
        return {
            "type": "table",
            "columns": columns,
            "rows": rows,
            "page": page,
            "has_next": has_next,
            **base,
        }

    # --------------------------
    # FILE LIST (for file-based datasource Preview offcanvas)
    # --------------------------
    @get("/{datasource_id:uuid}/file-list")
    async def file_list(
        self,
        datasource_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """Return an HTML partial listing all active uploaded files for a
        file-based datasource. No action buttons — read-only view."""
        datasource = await datasource_crud.get_by_uuid(db, datasource_id)
        if not datasource or datasource.user_id != user.id:
            raise HTTPException(status_code=404, detail="Datasource not found")

        stmt = (
            select(DatasourceFile)
            .where(
                DatasourceFile.datasource_id == datasource.id,
                DatasourceFile.is_active == True,  # noqa: E712
            )
            .order_by(DatasourceFile.uploaded_at.desc())
        )
        result = await db.execute(stmt)
        files = result.scalars().all()

        return Template(
            template_name="datasources/file_list.htm",
            context={
                "files": files,
                "datasource_name": datasource.datasource_name,
            },
        )
