"""
HTTP layer for workflows: the library, the canvas, the schedule and the runs.

Accepts the request, hands it to a service, returns the response. No business rules here —
which for this feature means specifically that ``validate_flow`` is not called from a
handler, ``publish_flow``'s one-published-version rule is not restated, and the
administrator check for private hosts lives in ``connection_service`` where a second route
cannot skip it.

**Two response styles, and the split is not arbitrary.** The library is HTMX, because it is
a list of things somebody owns with buttons that change it — the same shape as every other
list page here. The canvas exchanges JSON, because a renderer that positions steps and
repaints their status needs data rather than markup. Both calls are the ones
``GraphDesignerController`` made, for the same reasons.

**The canvas never gets a status code it cannot render.** A refusal from Save, Publish or
Run comes back as a **200** carrying ``error`` — and, where the service named one, the
``node_id`` to highlight. The alternative is replacing a page that holds unsaved work with
an error page, which loses the drawing somebody was in the middle of. A missing workflow
is still a 404: there is nothing to render it into.

``FlowValidationError`` is caught here rather than converted in the service, because it is
the only exception in the module carrying the *node* at fault, and that is exactly what the
canvas needs. Flattening it to a sentence one layer down would leave the drawing with a red
banner and nothing highlighted.

``GET /runs/{id}/events`` is a **GET** because ``EventSource`` cannot issue anything else,
and ownership is resolved *before* the stream opens: the status code is committed with the
first byte, so a 404 has to be decided while there is still a response to put it in.
"""

import json
import logging
import uuid as uuid_pkg
from typing import Any, AsyncIterator, Dict, Optional

from litestar import Controller, delete, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Response, ServerSentEvent, Template
from litestar.response.sse import ServerSentEventMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.integrations import (
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_SUCCEEDED,
    RUN_CANCELLED,
    RUN_AWAITING_INPUT,
)
from app.models.user import User
from app.schemas.integrations import (
    FlowCreateRequest,
    FlowGraphRequest,
    FlowSettingsRequest,
    FlowVersionView,
    FlowView,
    RunFrameView,
    RunListQuery,
    RunRecordView,
    RunStartRequest,
    RunStepView,
    StepPageQuery,
    TriggerRequest,
    TriggerView,
)
from app.services.integrations import connection_service, flow_service
from app.services.integrations.engine import flow_rules, record_log, run_service, run_store
from app.services.integrations.errors import FlowValidationError

logger = logging.getLogger(__name__)

_JSON = "application/json"
_LIST_TEMPLATE = "integrations/list.htm"
_CANVAS_TEMPLATE = "integrations/canvas.htm"
_RUNS_TEMPLATE = "integrations/runs.htm"
_ROWS_TEMPLATE = "integrations/partials/flow_rows.htm"


class IntegrationsController(Controller):
    """Workflows: the library, the canvas, the schedule and the runs."""

    path = "/integrations"
    dependencies = {"user": require_auth}

    # --------------------------
    # LIBRARY
    # --------------------------
    @get("/")
    async def index(self, db: AsyncSession, user: User) -> Template:
        """Every workflow this user owns, with how its last run went."""
        flows = await flow_service.list_flows(db, user.id)
        views = await flow_service.build_flow_views(db, flows)

        return Template(
            template_name=_LIST_TEMPLATE,
            context={
                "user": user,
                "flows": FlowView.payload_for_many(views),
                "active": "integrations",
            },
        )

    @post("/create")
    async def create(self, request: Request, db: AsyncSession, user: User) -> Template:
        """Create a draft. Its canvas opens holding one trigger."""
        error = None

        try:
            payload = await FlowCreateRequest.from_form(request)
            await flow_service.create_flow(
                db, user.id, payload.name, description=payload.description
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{flow_id:uuid}/settings")
    async def update_settings(
        self, flow_id: uuid_pkg.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        """Rename a workflow, set its batch size, and name fields to keep out of previews."""
        error = None

        try:
            payload = await FlowSettingsRequest.from_form(request)
            await flow_service.update_flow_settings(
                db,
                user.id,
                flow_id,
                name=payload.name,
                description=payload.description,
                default_batch_size=payload.default_batch_size,
                redacted_fields=payload.redacted_fields,
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{flow_id:uuid}/active")
    async def set_active(
        self, flow_id: uuid_pkg.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        """
        Switch a workflow on or off.

        Read from the form rather than toggled from the stored value, because two people
        with the page open would otherwise each flip whatever they happened to load — and
        the one who pressed Off second would turn it back on.
        """
        error = None

        try:
            form = await request.form()
            wanted = str(form.get("is_active", "")).strip().lower() in ("true", "on", "1")
            await flow_service.set_flow_active(db, user.id, flow_id, wanted)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{flow_id:uuid}/delete")
    async def delete_flow(
        self, flow_id: uuid_pkg.UUID, db: AsyncSession, user: User
    ) -> Template:
        """Delete a workflow. Refused while it is running — see ``flow_service``."""
        error = None

        try:
            await flow_service.delete_flow(db, user.id, flow_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            error = str(exc.detail)

        return await self._rows(db, user, error)

    # --------------------------
    # CANVAS
    # --------------------------
    @get("/{flow_id:uuid}/canvas")
    async def canvas(
        self, flow_id: uuid_pkg.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        """
        The drawing board for one workflow.

        The drawing, the vocabulary and the connection list are handed over as JSON **in
        the page** rather than fetched, so the canvas draws on first paint instead of
        flashing empty — and so nothing about the node vocabulary is duplicated in
        JavaScript. ``/integrations/vocabulary`` still exists for the AI layer and for a
        client that wants it separately; both read ``flow_rules.vocabulary()``, so there is
        one answer to what the validator accepts.

        ``?run=`` opens the dock on a run somebody followed a link to.
        """
        flow = await flow_service.get_flow(db, user.id, flow_id)
        version = await flow_service.get_published_version(db, flow)
        triggers = await flow_service.list_triggers(db, flow)
        connections = await connection_service.list_connections(db, user.id)

        return Template(
            template_name=_CANVAS_TEMPLATE,
            context={
                "user": user,
                "flow": {
                    "uuid": str(flow.uuid),
                    "name": flow.name,
                    "description": flow.description or "",
                    "is_active": flow.is_active,
                    "default_batch_size": flow.default_batch_size,
                    "redacted_fields": flow.redacted_fields or [],
                },
                "is_published": version is not None,
                "version_number": version.version_number if version else None,
                "graph_data_json": json.dumps(flow.graph_data or {}, default=str),
                "vocabulary_json": json.dumps(flow_rules.vocabulary(), default=str),
                "connections_json": json.dumps(
                    connection_service.build_views(connections), default=str
                ),
                "triggers_json": json.dumps(
                    TriggerView.payload_for_many(
                        flow_service.build_trigger_views(triggers)
                    ),
                    default=str,
                ),
                "open_run": str(request.query_params.get("run") or ""),
                "active": "integrations",
            },
        )

    @get("/vocabulary")
    async def vocabulary(self, user: User) -> dict:
        """
        Every list the palette and the property panels are built from.

        **Served from the server, not hardcoded in JavaScript.** These lists decide what
        the validator accepts, so a palette offering a step type it refuses — or a port it
        does not know — is a form that can only be filled in wrongly. Adding a node type
        touches no JavaScript, which is the improvement this makes over
        ``graph_designer.js``' own ``PORTS`` table.

        No database, so no failure to report: it is built from the models' tuples and
        ``filter_algebra``'s operator table.
        """
        return flow_rules.vocabulary()

    @post("/{flow_id:uuid}/save")
    async def save(
        self, flow_id: uuid_pkg.UUID, request: Request, db: AsyncSession, user: User
    ) -> Response:
        """
        Store the drawing.

        A refusal is a 200 carrying ``error`` and the ``node_id`` to highlight — see the
        module docstring on why the canvas never gets a status code it cannot render.
        Nothing is written when validation fails, so the page keeps the unsaved work it
        already has.
        """
        try:
            payload = await FlowGraphRequest.from_json(request)
            await flow_service.save_flow(db, user.id, flow_id, payload.graph_data)
        except FlowValidationError as exc:
            return _refused(exc)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            return _refused(exc)

        return Response({"ok": True}, media_type=_JSON, status_code=200)

    @post("/{flow_id:uuid}/publish")
    async def publish(
        self, flow_id: uuid_pkg.UUID, db: AsyncSession, user: User
    ) -> Response:
        """
        Freeze the current drawing as the version that runs.

        Stricter than Save by exactly one rule — a required destination field with nothing
        mapped to it — and that refusal names both the step and the fields, so it comes
        back in the same shape Save's does.
        """
        try:
            version = await flow_service.publish_flow(db, user.id, flow_id)
        except FlowValidationError as exc:
            return _refused(exc)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            return _refused(exc)

        return Response(
            {"ok": True, "version": FlowVersionView.build(
                flow_service.build_version_views([version])[0]
            ).payload()},
            media_type=_JSON,
            status_code=200,
        )

    @post("/{flow_id:uuid}/unpublish")
    async def unpublish(
        self, flow_id: uuid_pkg.UUID, db: AsyncSession, user: User
    ) -> Response:
        """Withdraw the published version and switch the workflow off — both, together."""
        try:
            await flow_service.unpublish_flow(db, user.id, flow_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            return _refused(exc)

        return Response({"ok": True}, media_type=_JSON, status_code=200)

    @get("/{flow_id:uuid}/versions")
    async def versions(
        self, flow_id: uuid_pkg.UUID, db: AsyncSession, user: User
    ) -> dict:
        """Every version of one workflow, newest first. The drawing behind one is not
        included — see :class:`FlowVersionView`."""
        found = await flow_service.list_versions(db, user.id, flow_id)

        return {
            "versions": FlowVersionView.payload_for_many(
                flow_service.build_version_views(found)
            )
        }

    # --------------------------
    # TRIGGERS
    # --------------------------
    @get("/{flow_id:uuid}/triggers")
    async def list_triggers(
        self, flow_id: uuid_pkg.UUID, db: AsyncSession, user: User
    ) -> dict:
        """This workflow's triggers, with when each is next due."""
        flow = await flow_service.get_flow(db, user.id, flow_id)
        found = await flow_service.list_triggers(db, flow)

        return {
            "triggers": TriggerView.payload_for_many(
                flow_service.build_trigger_views(found)
            )
        }

    @post("/{flow_id:uuid}/triggers")
    async def save_trigger(
        self, flow_id: uuid_pkg.UUID, request: Request, db: AsyncSession, user: User
    ) -> Response:
        """
        Create or update the schedule on one step.

        ``next_run_at`` is recomputed by the service on every write — that column is the
        whole schedule, and a path that changed an interval without it would leave the
        workflow running on the old one.
        """
        try:
            payload = await TriggerRequest.from_form(request)
            trigger = await flow_service.save_trigger(
                db,
                user.id,
                flow_id,
                node_id=payload.node_id,
                kind=payload.kind,
                is_enabled=payload.is_enabled,
                interval_seconds=payload.interval_seconds,
                timezone_name=payload.timezone_name,
                overlap_policy=payload.overlap_policy,
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            return _refused(exc)

        return Response(
            {
                "ok": True,
                "trigger": TriggerView.build(
                    flow_service.build_trigger_views([trigger])[0]
                ).payload(),
            },
            media_type=_JSON,
            status_code=200,
        )

    @delete("/{flow_id:uuid}/triggers/{trigger_id:uuid}", status_code=200)
    async def delete_trigger(
        self,
        flow_id: uuid_pkg.UUID,
        trigger_id: uuid_pkg.UUID,
        db: AsyncSession,
        user: User,
    ) -> dict:
        """Remove a trigger. The scheduler stops seeing it the moment the row is gone."""
        await flow_service.delete_trigger(db, user.id, flow_id, trigger_id)
        return {"ok": True}

    # --------------------------
    # RUNS
    # --------------------------
    @get("/{flow_id:uuid}/runs")
    async def runs(
        self, flow_id: uuid_pkg.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        """This workflow's history."""
        query = RunListQuery.from_query(request)
        flow = await flow_service.get_flow(db, user.id, flow_id)
        found = await flow_service.list_runs(db, user.id, flow_id, limit=query.limit)

        return Template(
            template_name=_RUNS_TEMPLATE,
            context={
                "user": user,
                "flow": {"uuid": str(flow.uuid), "name": flow.name},
                "runs": [_run_row(run) for run in found],
                "active": "integrations",
            },
        )

    @post("/{flow_id:uuid}/runs")
    async def start_run(
        self, flow_id: uuid_pkg.UUID, request: Request, db: AsyncSession, user: User
    ) -> Response:
        """
        Queue a run, now.

        Returns a handle, not a result. **A manual run goes through the same queue as a
        scheduled one**, so the run somebody tests at 11am takes the path that fires at
        3am — the interesting failures are all in the path nobody tested.
        """
        try:
            payload = await RunStartRequest.from_form(request)
            run = await flow_service.start_run(
                db, user.id, flow_id, mode=payload.mode
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            return _refused(exc)

        return Response(
            {"ok": True, "run_uuid": str(run.uuid)}, media_type=_JSON, status_code=200
        )

    @get("/runs/{run_id:uuid}")
    async def run_status(
        self, run_id: uuid_pkg.UUID, db: AsyncSession, user: User
    ) -> dict:
        """
        One run's whole state — the dock's polling fallback, and what a link opens on.

        The same payload the stream sends, so a client whose connection dropped does not
        have to understand a second shape.
        """
        run = await flow_service.get_run(db, user.id, run_id)
        found = await run_store.get_run_and_flow(db, run.uuid, user.id)

        return RunFrameView.build(
            await run_store.run_view(db, found[0], found[1])
        ).payload()

    @get("/runs/{run_id:uuid}/events")
    async def run_events(
        self, run_id: uuid_pkg.UUID, db: AsyncSession, user: User
    ) -> ServerSentEvent:
        """
        The run as a stream of frames, until it ends.

        A **GET**, because ``EventSource`` cannot issue anything else. Ownership is
        resolved here, before the generator is handed over: the status code is committed
        with the first byte, so a 404 has to be decided while there is still a response to
        put it in.
        """
        # Raises 404 for a missing run or somebody else's — deliberately before the stream
        # opens.
        await flow_service.get_run(db, user.id, run_id)

        async def messages() -> AsyncIterator[ServerSentEventMessage]:
            async for frame in run_service.watch_run(user.id, run_id):
                yield ServerSentEventMessage(
                    data=json.dumps(frame, default=str),
                    event=_event_name(frame),
                )

        return ServerSentEvent(messages())

    @get("/runs/{run_id:uuid}/steps")
    async def run_steps(
        self, run_id: uuid_pkg.UUID, request: Request, db: AsyncSession, user: User
    ) -> dict:
        """
        The paginated step log behind the frame's hundred-row window.

        Paged by sequence number rather than by offset, so a page fetched while the run is
        still writing steps does not repeat or skip rows the way an offset into a growing
        table does.
        """
        query = StepPageQuery.from_query(request)
        run = await flow_service.get_run(db, user.id, run_id)

        steps = await run_store.steps_page(
            db, run.id, after_sequence=query.after, limit=query.limit
        )

        return {"steps": RunStepView.payload_for_many(steps)}

    @get("/runs/{run_id:uuid}/records")
    async def run_records(
        self, run_id: uuid_pkg.UUID, db: AsyncSession, user: User
    ) -> dict:
        """
        The records this run could not move.

        ``failed_logged`` and ``records_failed`` are **two numbers** and both are sent: the
        log keeps at most a thousand failures while the counter keeps counting, and a page
        showing only what the log kept would quietly under-report a bad sync.
        """
        run = await flow_service.get_run(db, user.id, run_id)
        rows = await record_log.failures(db, run.id)

        return {
            "records": RunRecordView.payload_for_many(
                [record_log.record_view(row) for row in rows]
            ),
            "records_failed": int(run.records_failed or 0),
            "failed_logged": len(rows),
            "records_log_truncated": bool(run.records_log_truncated),
        }

    @post("/runs/{run_id:uuid}/stop")
    async def stop_run(
        self, run_id: uuid_pkg.UUID, db: AsyncSession, user: User
    ) -> dict:
        """
        Ask a run to stop.

        A request, not an instruction: a step already waiting on somebody else's server
        finishes that call first, so this returns immediately and the page shows
        "stopping" until the run's own status catches up.
        """
        await flow_service.stop_run(db, user.id, run_id)
        return {"ok": True}

    @post("/runs/{run_id:uuid}/replay")
    async def replay_run(
        self, run_id: uuid_pkg.UUID, db: AsyncSession, user: User
    ) -> Response:
        """Run the same topology again, pinned to the **same version** as the original."""
        try:
            run = await flow_service.replay_run(db, user.id, run_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            return _refused(exc)

        return Response(
            {"ok": True, "run_uuid": str(run.uuid)}, media_type=_JSON, status_code=200
        )

    # --------------------------
    # HELPERS
    # --------------------------
    async def _rows(
        self, db: AsyncSession, user: User, error: Optional[str] = None
    ) -> Template:
        """
        The library's rows, plus any error.

        Every mutation answers with this, so the list somebody is looking at is always the
        list the database holds — the pattern ``flow_builder._rows`` and
        ``graph_designer._rows`` both follow.
        """
        flows = await flow_service.list_flows(db, user.id)
        views = await flow_service.build_flow_views(db, flows)

        return Template(
            template_name=_ROWS_TEMPLATE,
            context={
                "user": user,
                "flows": FlowView.payload_for_many(views),
                "error": error,
            },
        )


def _refused(exc: Exception) -> Response:
    """
    A refusal the canvas can render, carrying the step to highlight when there is one.

    A 200, deliberately. The alternative replaces a page holding unsaved work with an error
    page — see the module docstring. ``node_id`` and ``edge_id`` are only present on
    :class:`FlowValidationError`, which is the whole reason that exception is caught
    separately rather than being flattened into a sentence in the service.
    """
    detail = getattr(exc, "detail", None) or str(exc)

    body: Dict[str, Any] = {"ok": False, "error": str(detail)}

    node_id = getattr(exc, "node_id", "")
    edge_id = getattr(exc, "edge_id", "")

    if node_id:
        body["node_id"] = node_id
    if edge_id:
        body["edge_id"] = edge_id

    return Response(body, media_type=_JSON, status_code=200)


def _event_name(frame: dict) -> str:
    """
    The SSE event name for one frame.

    Derived from the run's own status, so a browser can switch on ``event.type`` or read
    the payload and the two cannot disagree — they come from the same value.

    **A named event never reaches ``onmessage``.** Every name here has to be registered
    with ``addEventListener`` on the client; that has bitten this codebase before, which is
    why the list is short and derived rather than free-form.
    """
    status = str(frame.get("status") or "")

    if status in (RUN_SUCCEEDED, RUN_PARTIAL, RUN_FAILED, RUN_CANCELLED):
        return status

    if status == RUN_AWAITING_INPUT:
        return "awaiting"

    return "progress"


def _run_row(run: Any) -> dict:  # noqa: ANN401
    """One run as the history table reads it. Public ``uuid`` only."""
    return {
        "uuid": str(run.uuid),
        "status": run.status,
        "mode": run.mode,
        "trigger_kind": run.trigger_kind,
        "attempt": run.attempt,
        "records_read": int(run.records_read or 0),
        "records_written": int(run.records_written or 0),
        "records_failed": int(run.records_failed or 0),
        "records_skipped": int(run.records_skipped or 0),
        "error_message": run.error_message,
        "is_replay": run.replay_of_run_id is not None,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }
