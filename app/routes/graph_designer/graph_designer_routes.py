"""
HTTP layer for the Graph Designer. Accepts the request, hands it to a service, returns
the response — no business rules live here.

Two response styles, and the split is not arbitrary:

* **The library** is HTMX. It is a list of things the user owns with buttons that change
  it, which is what every other list page in this application is, so it returns partials
  and refreshes its rows out of band exactly as ``FlowBuilderController`` does.
* **The canvas** exchanges JSON. A renderer that positions nodes and repaints their status
  needs data, not markup, and a server-templated SVG could not be dragged or re-laid-out
  without a round trip. That is the same call ``FlowBuilderController`` and
  ``ToolGraphController`` both made.

**The run endpoints never raise for a state the page can show.** A save that is refused, a
run that cannot start, an answer that is not valid — each comes back as a payload the
canvas renders beside the toolbar, because the alternative is replacing a page that holds
unsaved work. A missing or someone else's graph is still a 404: there is nothing to render
it into.

``GET /runs/{id}/events`` is a **GET** because ``EventSource`` cannot issue anything else
— the same constraint ``/deep-agents/{id}/ask-stream`` documents — and ownership is
resolved *before* the stream opens, because the response status is committed with the
first byte.
"""

import json
import logging
import uuid as uuid_pkg
from typing import AsyncIterator

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Response, ServerSentEvent, Template
from litestar.response.sse import ServerSentEventMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.graph_designer import (
    BINDING_MODES,
    MAX_NODE_VARIABLES,
    NODE_TYPES,
    TIMER_ACTIONS,
    VALUE_KINDS,
)
from app.models.tool_configs import SQL_PARAM_TYPES
from app.models.user import User
from app.schemas.graph_designer import (
    GraphAttachRequest,
    GraphCreateRequest,
    GraphNodeOptionsResponse,
    GraphRenameRequest,
    GraphResumeRequest,
    GraphRunQuery,
    GraphRunRequest,
    GraphRunStartedResponse,
    GraphRunView,
    GraphSaveRequest,
    GraphSaveResponse,
    GraphSetActiveRequest,
    GraphShareRequest,
    GraphUpdateRequest,
    GraphView,
)
from app.services.graph_designer import (
    graph_run_service,
    graph_service,
    node_variables,
    timers,
)
from app.services.graph_designer.graph_service import (
    CONDITION_OPERATORS,
    DEFAULT_MAX_ITERATIONS,
)

logger = logging.getLogger(__name__)

_JSON = "application/json"
_LIST_TEMPLATE = "graph_designer/list.htm"
_CANVAS_TEMPLATE = "graph_designer/canvas.htm"
_HELP_TEMPLATE = "graph_designer/help.htm"
_ROWS_TEMPLATE = "graph_designer/partials/graph_rows.htm"
_SAVE_TEMPLATE = "graph_designer/partials/save_result.htm"
_EDIT_FORM_TEMPLATE = "graph_designer/partials/graph_form.htm"
_MODAL_ERROR_TEMPLATE = "graph_designer/partials/modal_error.htm"


class GraphDesignerController(Controller):
    """The graph library, the canvas, and the runs a canvas starts and watches."""

    path = "/graph-designer"
    dependencies = {"user": require_auth}

    # --------------------------
    # LIBRARY
    # --------------------------
    @get("/")
    async def index(self, db: AsyncSession, user: User) -> Template:
        """Every graph this user owns."""
        graphs = await graph_service.get_graph_views(db, user.id)

        return Template(
            template_name=_LIST_TEMPLATE,
            context={
                "user": user,
                "graphs": GraphView.payload_for_many(graphs),
                # No option lists here: the two pickers live in the edit dialog, whose
                # body is fetched per open, so the agent and workspace lists are read
                # once when somebody opens it rather than on every render of the table.
                "active": "graph_designer",
            },
        )

    # --------------------------
    # HELP
    # --------------------------
    @get("/help")
    async def help_page(self, user: User) -> Template:
        """
        The Pipelines help page — the browsable form of
        documentations/GRAPH_DESIGNER.md, opened in its own tab by the Help button on
        the library page.

        Static: it reads nothing and takes no query parameters, so there is no service
        call and no schema to parse. It is a route rather than a link to the markdown
        file because a help page has to arrive inside the application's own layout,
        behind the same auth as the page it explains — the same call
        ``tool_config_routes.help_page`` makes.

        A literal path, so it cannot be confused with ``/{graph_id:uuid}/…``.
        """
        return Template(
            template_name=_HELP_TEMPLATE,
            context={"user": user, "active": "graph_designer"},
        )

    @post("/create")
    async def create(self, request: Request, db: AsyncSession, user: User) -> Template:
        """Create a draft graph. Its canvas opens holding one Start node."""
        error = None

        try:
            payload = await GraphCreateRequest.from_form(request)
            await graph_service.create_graph(
                db, user.id, payload.name, payload.description,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @get("/{graph_id:uuid}/edit-form")
    async def edit_form(
        self,
        graph_id: uuid_pkg.UUID,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        The edit dialog's body, fetched per open rather than rendered once per row.

        A row carries a name, a description and two pickers; rendering that markup for
        every graph in the library would repeat both option lists on every row for a
        dialog that is opened one at a time. Fetching it also guarantees the form opens
        on what the database holds — the pattern ``data_agent_routes.edit_form`` uses.

        A refusal renders into the dialog rather than raising, because there is a modal
        already on screen waiting for a body.
        """
        try:
            graph = await graph_service.get_graph_view(db, user.id, graph_id)
            choices = await graph_service.get_attachment_choices(db, user.id)
        except HTTPException as exc:
            return Template(
                template_name=_MODAL_ERROR_TEMPLATE,
                context={"error": str(exc.detail)},
            )

        return Template(
            template_name=_EDIT_FORM_TEMPLATE,
            context={
                "graph": GraphView.build(graph).payload(),
                "choices": choices,
                "form_action": f"/graph-designer/{graph_id}/update",
            },
        )

    @post("/{graph_id:uuid}/update")
    async def update(
        self,
        graph_id: uuid_pkg.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        Save the edit dialog: name, description and who may call this graph.

        One endpoint for all four fields because they are one form. The two attachments
        keep their own endpoints as well — they are the write paths this one calls — so
        nothing here decides anything ``graph_service`` does not.
        """
        error = None

        try:
            payload = await GraphUpdateRequest.from_form(request)
            await graph_service.update_graph(
                db,
                user.id,
                graph_id,
                payload.name,
                payload.description,
                payload.data_agent_id,
                payload.workspace_id,
                payload.allow_recursive_aggregate,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{graph_id:uuid}/rename")
    async def rename(
        self,
        graph_id: uuid_pkg.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """Rename, and edit the description a model reads when the graph is attached."""
        error = None

        try:
            payload = await GraphRenameRequest.from_form(request)
            await graph_service.rename_graph(
                db, user.id, graph_id, payload.name, payload.description,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{graph_id:uuid}/set-active")
    async def set_active(
        self,
        graph_id: uuid_pkg.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        Publish or unpublish.

        Publishing validates the drawing, so this is the one toggle that can be refused —
        an active graph attached to an agent is callable by a model, and one that cannot
        compile would fail inside somebody's conversation.
        """
        error = None

        try:
            payload = await GraphSetActiveRequest.from_form(request)
            await graph_service.set_graph_active(
                db, user.id, graph_id, payload.is_active,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{graph_id:uuid}/attach")
    async def attach(
        self,
        graph_id: uuid_pkg.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """Point a data agent at this graph, or detach it by submitting nothing."""
        error = None

        try:
            payload = await GraphAttachRequest.from_form(request)
            await graph_service.attach_graph(
                db, user.id, graph_id, payload.data_agent_id,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{graph_id:uuid}/share")
    async def share(
        self,
        graph_id: uuid_pkg.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        Share this graph with a workspace, or stop sharing by submitting nothing.

        A separate endpoint from ``attach`` because the two are separate answers to "who
        may call this" and setting either clears the other — one endpoint taking both
        would let a client submit both and have one silently dropped.
        """
        error = None

        try:
            payload = await GraphShareRequest.from_form(request)
            await graph_service.share_graph(
                db, user.id, graph_id, payload.workspace_id,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{graph_id:uuid}/delete")
    async def delete(
        self,
        graph_id: uuid_pkg.UUID,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """Delete a graph, and its runs with it."""
        error = None

        try:
            await graph_service.delete_graph(db, user.id, graph_id)
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    # --------------------------
    # CANVAS
    # --------------------------
    @get("/{graph_id:uuid}/edit")
    async def edit(
        self,
        graph_id: uuid_pkg.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        The canvas, the palette and the run dock.

        The graph is injected as JSON rather than fetched after load, so the canvas draws
        on first paint instead of flashing empty. A ``?run=`` in the query string is
        passed through so a link to a run someone is watching reopens on it.
        """
        graph = await graph_service.get_graph(db, user.id, graph_id)
        view = await graph_service.get_graph_view(db, user.id, graph_id)
        selection = GraphRunQuery.from_query(request)

        return Template(
            template_name=_CANVAS_TEMPLATE,
            context={
                "user": user,
                "graph": GraphView.build(view).payload(),
                "graph_data_json": json.dumps(graph.graph_data or {}),
                "vocabulary_json": json.dumps(_vocabulary()),
                "selected_run": str(selection.run or ""),
                "active": "graph_designer",
            },
        )

    @get("/{graph_id:uuid}/graph")
    async def graph(
        self,
        graph_id: uuid_pkg.UUID,
        db: AsyncSession,
        user: User,
    ) -> Response:
        """The stored drawing, for the canvas's Reload button."""
        graph = await graph_service.get_graph(db, user.id, graph_id)
        return Response(graph.graph_data or {}, media_type=_JSON, status_code=200)

    @post("/{graph_id:uuid}/save")
    async def save(
        self,
        graph_id: uuid_pkg.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        Replace the drawing, whole.

        A refusal renders into the banner under the toolbar rather than replacing the
        page: the canvas is holding work that has not been stored anywhere else, and
        navigating away from it would lose that. The reason is passed to a **template**
        rather than interpolated into an HTML string, so a message quoting something the
        user typed cannot inject markup.
        """
        result = GraphSaveResponse(saved=False)

        try:
            payload = await GraphSaveRequest.from_json(request)
            graph = await graph_service.save_graph(
                db, user.id, graph_id, payload.graph_data(),
            )
            result = GraphSaveResponse(
                saved=True,
                message="Graph saved.",
                node_count=len((graph.graph_data or {}).get("nodes") or []),
                edge_count=len((graph.graph_data or {}).get("edges") or []),
            )
        except HTTPException as exc:
            result = GraphSaveResponse.failure(str(exc.detail))

        return Template(
            template_name=_SAVE_TEMPLATE,
            context={"result": result.payload()},
        )

    @get("/{graph_id:uuid}/node-options")
    async def node_options(
        self, graph_id: uuid_pkg.UUID, db: AsyncSession, user: User,
    ) -> dict:
        """
        What the properties panel's pickers offer, for one graph.

        Scoped to a graph rather than to the user because the email template list is: a
        graph shared into a workspace picks from that workspace's templates. There is
        deliberately no unscoped version — it would be a documented way to fetch the
        unfiltered list, which is the thing the scoping exists to prevent.

        Answered as a 200 with ``error`` set when it cannot be built, the contract
        ``GET /tool-configs/child-options`` established: a picker that cannot be filled
        should put one sentence next to itself, not replace the canvas. That covers the
        404 for somebody else's graph too, which reaches the browser as a sentence beside
        an empty picker rather than as a broken page.
        """
        try:
            options = await graph_service.node_options(db, user.id, graph_id)
        except HTTPException as exc:
            return GraphNodeOptionsResponse.failure(str(exc.detail)).payload()

        return GraphNodeOptionsResponse.build(options).payload()

    # --------------------------
    # RUNS
    # --------------------------
    @post("/{graph_id:uuid}/runs")
    async def start_run(
        self,
        graph_id: uuid_pkg.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Response:
        """
        Start a run — the whole graph, or a tested selection of it.

        Returns a handle, not a result. The run is driven by a background task and watched
        through the events stream, because a graph can take minutes and can stop to ask a
        question whose answer arrives in a different request.

        A refusal comes back as a 200 carrying ``error``, for the same reason the save
        does: the canvas is the page the user is working in.
        """
        try:
            payload = await GraphRunRequest.from_json(request)
            run_uuid = await graph_run_service.start_run(
                db,
                user.id,
                graph_id,
                scope=payload.scope,
                node_ids=payload.node_ids,
                inputs=payload.inputs,
            )
        except HTTPException as exc:
            return Response(
                {"error": str(exc.detail)}, media_type=_JSON, status_code=200,
            )

        return Response(
            GraphRunStartedResponse.for_run(run_uuid).payload(),
            media_type=_JSON,
            status_code=200,
        )

    @get("/runs/{run_id:uuid}")
    async def run_status(
        self,
        run_id: uuid_pkg.UUID,
        db: AsyncSession,
        user: User,
    ) -> dict:
        """
        One run's whole state — the dock's polling fallback, and what a link opens on.

        The same shape the stream sends, so a client whose connection dropped does not
        have to understand a second payload.
        """
        view = await graph_run_service.get_run(db, user.id, run_id)
        return GraphRunView.build(view).payload()

    @get("/runs/{run_id:uuid}/events")
    async def run_events(
        self,
        run_id: uuid_pkg.UUID,
        db: AsyncSession,
        user: User,
    ) -> ServerSentEvent:
        """
        The run as a stream of frames, until it ends.

        A **GET**, because ``EventSource`` cannot issue anything else. Ownership is
        resolved here, before the generator is handed over: the status code is committed
        with the first byte, so a 404 has to be decided while there is still a response to
        put it in.
        """
        # Raises 404 for a missing run or one belonging to somebody else — deliberately
        # before the stream opens.
        await graph_run_service.get_run(db, user.id, run_id)

        async def messages() -> AsyncIterator[ServerSentEventMessage]:
            async for frame in graph_run_service.stream_run(user.id, run_id):
                yield ServerSentEventMessage(
                    data=json.dumps(frame, default=str),
                    event=_event_name(frame),
                )

        return ServerSentEvent(messages())

    @post("/runs/{run_id:uuid}/resume")
    async def resume_run(
        self,
        run_id: uuid_pkg.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Response:
        """
        Answer a paused run's question.

        The answer is checked against what the node asked for, so a bad one is refused
        while the person is still looking at the prompt rather than failing a node three
        steps later.
        """
        try:
            payload = await GraphResumeRequest.from_json(request)
            view = await graph_run_service.resume_run(
                db, user.id, run_id, payload.answer,
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            return Response(
                {"error": str(exc.detail)}, media_type=_JSON, status_code=200,
            )

        return Response(
            GraphRunView.build(view).payload(), media_type=_JSON, status_code=200,
        )

    @post("/runs/{run_id:uuid}/cancel")
    async def cancel_run(
        self,
        run_id: uuid_pkg.UUID,
        db: AsyncSession,
        user: User,
    ) -> dict:
        """Stop a run. Its steps are kept — how far it got is the useful part."""
        view = await graph_run_service.cancel_run(db, user.id, run_id)
        return GraphRunView.build(view).payload()

    # --------------------------
    # HELPERS
    # --------------------------
    async def _rows(
        self,
        db: AsyncSession,
        user: User,
        error: str | None = None,
    ) -> Template:
        """
        The library's rows, plus a marker and any error.

        Every mutation answers with this, so the list a user is looking at is always the
        list the database holds — the pattern ``flow_builder._rows`` and
        ``tool_config_routes._rows`` both follow.
        """
        graphs = await graph_service.get_graph_views(db, user.id)

        return Template(
            template_name=_ROWS_TEMPLATE,
            context={
                "user": user,
                "graphs": GraphView.payload_for_many(graphs),
                "error": error,
            },
        )


def _event_name(frame: dict) -> str:
    """
    The SSE event name for one frame.

    Derived from the run's own status so a browser can switch on ``event.type`` or read
    the payload and the two cannot disagree — they come from the same value. That is the
    rule ``_streamed_progress`` states for the export feed.
    """
    status = str(frame.get("status") or "")

    if status in ("succeeded", "failed", "cancelled"):
        return status

    if status == "awaiting_input":
        return "awaiting"

    return "progress"


def _vocabulary() -> dict:
    """
    Everything the canvas needs to build its palette and its property forms.

    Sent from the server rather than duplicated in JavaScript, because these lists decide
    what the validator will accept: a palette offering a node type the service refuses, or
    an operator it does not know, is a form that can only be filled in wrongly. The
    labels come from the model's own tuples, so adding a node type updates the palette
    without anyone touching the canvas.
    """
    return {
        "node_types": [
            {"type": value, "label": label} for value, label in NODE_TYPES
        ],
        "value_kinds": [
            {"value": value, "label": label} for value, label in VALUE_KINDS
        ],
        "operators": [
            {"value": value, "label": label} for value, label in CONDITION_OPERATORS
        ],
        # What a wired parameter takes: one value, or the whole list as an expanding IN.
        # Sent for the same reason as the rest — a picker offering a mode the validator
        # refuses is a form that can only be filled in wrongly.
        "binding_modes": [
            {"value": value, "label": label} for value, label in BINDING_MODES
        ],
        # And what a declared parameter holds. The tool config form's own list, because a
        # graph's parameters are validated by that form's validator.
        "param_types": [
            {"value": value, "label": label} for value, label in SQL_PARAM_TYPES
        ],
        # What a Timer node does. Four actions on one node type, so the picker and the
        # validator read the same tuple.
        "timer_actions": [
            {"value": value, "label": label} for value, label in TIMER_ACTIONS
        ],
        # Which fields on each node type take a `{{VARIABLE}}`, as the labels the panel
        # shows. Sent rather than mirrored in JavaScript for the reason this whole
        # function exists: the server's table is what the validator enforces, and a panel
        # offering a variable in a field the validator ignores would substitute nothing
        # and say nothing about it.
        "variable_fields": {
            node_type: [spec.label for spec in specs]
            for node_type, specs in node_variables.VARIABLE_FIELDS.items()
        },
        "max_node_variables": MAX_NODE_VARIABLES,
        "max_wait_seconds": timers.MAX_WAIT_SECONDS,
        "default_wait_seconds": timers.DEFAULT_WAIT_SECONDS,
        "default_max_iterations": DEFAULT_MAX_ITERATIONS,
    }
