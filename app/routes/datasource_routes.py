import re
import uuid
from datetime import datetime

from litestar import post, get, Controller
from litestar.response import Response, Template
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, exists, func
from litestar.connection import Request
from litestar.exceptions import HTTPException

from app.models.user import User
from app.models.datasource import DataSource
from app.services.datasource_service import (
    test_connection,
    create_datasource,
    update_datasource_name,
    get_datasource_objects,
    get_user_datasources,
    toggle_column_status_service,
    toggle_table_status_service,
    search_sort_tables
)
from app.services.file_service import check_file_exists, upload_datasource_files
from app.utils.file_utils import FILE_BASED_TYPES, ACCEPT_ATTRS, ALLOWED_EXTENSIONS
from app.db.auth import require_auth

# Compiled once at import time — reused on every validation request.
_NAME_RE = re.compile(r'^[a-z0-9_]+$')


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
                            datasource_id=datasource.id,
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
                        "datasource_id": str(datasource.id),
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

        schema = datasource.get("configuration_data", {})

        return Template(
            template_name="datasources/column_view.htm",
            context={
                "table_name": table_name,
                "schema": schema[table_name]["column_data"],
                "datasource_id": str(datasource_id),
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

        return Template(
            template_name="datasources/column_row.htm",
            context={
                "col": updated_column,
                "datasource_id": str(datasource_id),
                "table_name": table_name,
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
