"""
Everything that happens to a workflow when nobody is running it.

Creating one, drawing on it, publishing it, putting it on a schedule and pressing Run.
The engine underneath this file executes a *version*; this file is what produces one.

Four decisions shape the module.

**Validation is one function called from three places.** ``flow_rules.validate_flow`` runs
on save, on publish and at the start of a run, with the same rules and the same sentences.
Graph Designer settled this question for the same reason: a run that validated more
loosely than the save would be a run of a workflow its author could not have stored, and
one that validated more strictly would be a Save button that lied.

**Publishing takes a snapshot, and the snapshot is what runs.** ``integration_flows`` is
the drawing somebody is editing; ``integration_flow_versions`` is the frozen copy a
schedule fires and a replay repeats. Without the split, editing a workflow at 2pm silently
changes what the 3am sync does, and the run record from last Tuesday describes a topology
that no longer exists. This is a deliberate departure from Graph Designer, which
recompiles from the live drawing — survivable for a query tool, not for something that
writes into a CRM unattended.

**Publishing is stricter than saving, by exactly one rule.** A draft with a required
destination field nobody has mapped is a workflow halfway through being built, and
refusing to save it would make the canvas unusable. A published one is about to run with
nobody watching, so :func:`publish_flow` resolves each write step's real input list, stamps
it onto the snapshot, and refuses. Without that stamp the mapping panel's red warning would
be decorative.

**A manual run goes through the queue, not around it.** :func:`start_run` writes a run row
and a job row and returns; a worker picks it up. The alternative — running it inline
because somebody is watching — means the run tested at 11am takes a different path from
the one that fires at 3am, and the interesting failures are all in the path nobody tested.
"""

import copy
import json
import logging
import uuid as uuid_pkg
from typing import Any, Dict, List, Mapping, Optional, Sequence

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.db.integrations.queries import (
    archive_published_versions,
    flow_named,
    latest_runs_for_flows,
    next_version_number,
    published_version,
    published_versions_for_flows,
)
from app.models.integrations import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    MIN_INTERVAL_SECONDS,
    NODE_CONNECTOR_WRITE,
    NODE_TRIGGER,
    OVERLAP_POLICY_VALUES,
    OVERLAP_SKIP,
    RUN_MODE_DRY_RUN,
    RUN_MODE_LIVE,
    TERMINAL_RUN_STATUSES,
    TRIGGER_KIND_VALUES,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULE,
    VERSION_PUBLISHED,
    IntegrationFlow,
    IntegrationFlowVersion,
    IntegrationRun,
    IntegrationTrigger,
)
from app.services.integrations.engine import (
    flow_rules,
    idempotency,
    queue,
    run_service,
    scheduler,
)
from app.services.integrations.errors import FlowValidationError, IntegrationFailure
from app.services.integrations.nodes import connector_nodes

logger = logging.getLogger(__name__)

flow_crud = CRUDQueryBuilder(IntegrationFlow)
version_crud = CRUDQueryBuilder(IntegrationFlowVersion)
trigger_crud = CRUDQueryBuilder(IntegrationTrigger)
run_crud = CRUDQueryBuilder(IntegrationRun)

#: The longest a workflow's name may be. The column's own limit, stated here so the
#: refusal is a sentence rather than a database error about a varchar.
MAX_NAME_LENGTH = 255

#: What somebody is told when a run they named is not theirs, and what they are told when
#: it does not exist at all. **The same sentence deliberately.** Distinguishing the two
#: would confirm the existence of another user's run to anybody willing to guess uuids,
#: and there is nothing the owner of a real run gains from the distinction.
#:
#: Taken from ``run_service`` rather than written again here, because the progress stream
#: yields it as a frame and this raises it as a 404 — two paths to the same page, and a
#: user who reloads must not be told two different things about one run.
NO_SUCH_RUN = run_service.NO_SUCH_RUN

#: Refused on three paths — switching a workflow on, saving an enabled trigger, and
#: enabling an existing one — because all three amount to asking the scheduler to run
#: something that has no published version to run.
PUBLISH_FIRST = (
    "Publish this workflow before switching on its schedule. A schedule runs the "
    "published version, and this one does not have one yet."
)

#: What a new canvas opens holding. One trigger, because an empty canvas gives somebody
#: nothing to drag from and ``validate_flow`` refuses a workflow with no trigger anyway —
#: so the first thing a blank flow would do is fail to save.
DEFAULT_GRAPH: Dict[str, Any] = {
    "nodes": [
        {
            "id": "trigger_1",
            "type": NODE_TRIGGER,
            "position": {"x": 120, "y": 200},
            "data": {"label": "Trigger", "kind": TRIGGER_MANUAL},
        }
    ],
    "edges": [],
}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def list_flows(db: AsyncSession, user_id: int) -> List[IntegrationFlow]:
    """This user's workflows, newest first."""
    return await flow_crud.get_many(
        db, filters={"user_id": user_id}, order_by="created_at", desc=True
    )


async def get_flow(
    db: AsyncSession, user_id: int, flow_id: uuid_pkg.UUID
) -> IntegrationFlow:
    """
    One workflow, scoped to its owner **in the query**.

    Loaded with ``user_id`` in the filter rather than fetched and then checked, so there
    is no window in which another user's row exists in this function's scope. Every read
    path in this module goes through here for that reason.
    """
    flow = await flow_crud.get_by_uuid(db, flow_id, extra_filters={"user_id": user_id})
    if flow is None:
        raise HTTPException(status_code=404, detail="That workflow does not exist.")
    return flow


async def build_flow_views(
    db: AsyncSession, flows: Sequence[IntegrationFlow]
) -> List[dict]:
    """
    Workflows shaped for the list page.

    The last run of every flow comes back in **one** query rather than one per row — see
    :func:`latest_runs_for_flows`. "Last run: failed, 20 minutes ago" is the column people
    actually scan, and forty flows is forty queries the moment it is done the obvious way.

    Public ``uuid`` only. The bigint ``id`` never leaves this layer.
    """
    flow_ids = [flow.id for flow in flows]
    latest = await latest_runs_for_flows(db, flow_ids)
    published = await published_versions_for_flows(db, flow_ids)

    views = []
    for flow in flows:
        run = latest.get(flow.id)
        version = published.get(flow.id)
        views.append(
            {
                "uuid": str(flow.uuid),
                "name": flow.name,
                "description": flow.description or "",
                "is_active": flow.is_active,
                "is_published": version is not None,
                "version_number": version.version_number if version else None,
                "created_by_ai": flow.created_by_ai,
                "node_count": len(flow_rules.nodes_of(flow.graph_data)),
                "created_at": flow.created_at,
                "updated_at": flow.updated_at,
                "last_run_status": run.status if run else "",
                "last_run_at": run.started_at if run else None,
                "last_run_uuid": str(run.uuid) if run else "",
            }
        )
    return views


async def get_published_version(
    db: AsyncSession, flow: IntegrationFlow
) -> Optional[IntegrationFlowVersion]:
    """The version a schedule or a Run press would execute, or ``None`` for a draft."""
    return await published_version(db, flow.id)


async def list_versions(
    db: AsyncSession, user_id: int, flow_id: uuid_pkg.UUID
) -> List[IntegrationFlowVersion]:
    """Every version of one workflow, newest first."""
    flow = await get_flow(db, user_id, flow_id)
    return await version_crud.get_many(
        db, filters={"flow_id": flow.id}, order_by="version_number", desc=True
    )


def build_version_views(versions: Sequence[IntegrationFlowVersion]) -> List[dict]:
    """Versions shaped for the history panel. The hash is shown short — it exists to be
    compared by eye between a run and a version, not to be read."""
    return [
        {
            "uuid": str(version.uuid),
            "version_number": version.version_number,
            "status": version.status,
            "is_published": version.status == VERSION_PUBLISHED,
            "graph_hash": version.graph_hash,
            "short_hash": (version.graph_hash or "")[:12],
            "published_at": version.published_at,
        }
        for version in versions
    ]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


async def create_flow(
    db: AsyncSession,
    user_id: int,
    name: str,
    *,
    description: Optional[str] = None,
    graph_data: Optional[Mapping[str, Any]] = None,
    created_by_ai: bool = False,
) -> IntegrationFlow:
    """
    A new workflow, as a draft.

    ``is_active`` is not a parameter. A workflow that arrived switched on would run on
    whatever schedule it was created with before anybody had looked at it, and the one
    caller most likely to want that — the AI generator — is exactly the one that must not
    have it. Turning it on is :func:`set_flow_active`, which refuses an unpublished flow.

    A supplied ``graph_data`` is validated like any other save. The generator's output
    goes through the same door as the canvas's.
    """
    await _require_unused_name(db, user_id, name)

    drawing = _validated_graph(graph_data) if graph_data is not None else _default_graph()

    return await flow_crud.create(
        db,
        {
            "user_id": user_id,
            "name": _validated_name(name),
            "description": (description or "").strip() or None,
            "graph_data": drawing,
            "is_active": False,
            "default_batch_size": DEFAULT_BATCH_SIZE,
            "created_by_ai": bool(created_by_ai),
        },
    )


async def save_flow(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid_pkg.UUID,
    graph_data: Any,
) -> IntegrationFlow:
    """
    Store the drawing, having refused it if it is not a workflow.

    **Nothing is written when validation fails**, which is what lets the canvas keep the
    user's unsaved work and show them the offending node. A partial save would leave the
    stored drawing and the one on screen disagreeing, and the next Save would be against a
    baseline nobody chose.

    Saving does not touch the published version. Editing a live workflow is allowed and
    changes nothing about what runs until Publish is pressed — that is the whole point of
    the two tables.
    """
    flow = await get_flow(db, user_id, flow_id)

    return await flow_crud.update(db, flow.id, {"graph_data": _validated_graph(graph_data)})


async def update_flow_settings(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid_pkg.UUID,
    *,
    name: str,
    description: Optional[str] = None,
    default_batch_size: Optional[int] = None,
    redacted_fields: Optional[Sequence[str]] = None,
) -> IntegrationFlow:
    """
    Everything the settings form can change.

    ``redacted_fields`` is added to the deny-list ``preview_of`` applies before anything is
    stored — a workflow whose source carries a field the generic list does not know about
    (``ssn``, ``iban``) can name it here, and it stops appearing in previews from the next
    run onward. It does not retroactively clean what is already written; say so in the UI.
    """
    flow = await get_flow(db, user_id, flow_id)
    await _require_unused_name(db, user_id, name, exclude_id=flow.id)

    values: Dict[str, Any] = {
        "name": _validated_name(name),
        "description": (description or "").strip() or None,
    }

    if default_batch_size is not None:
        values["default_batch_size"] = _validated_batch_size(default_batch_size)

    if redacted_fields is not None:
        values["redacted_fields"] = _validated_redacted_fields(redacted_fields)

    return await flow_crud.update(db, flow.id, values)


async def set_flow_active(
    db: AsyncSession, user_id: int, flow_id: uuid_pkg.UUID, is_active: bool
) -> IntegrationFlow:
    """
    Switch a workflow on or off.

    **Switching on requires a published version.** An active flow with nothing published
    is not idle — the scheduler claims its trigger every interval, finds no version and
    writes a ``skipped`` run saying so, which fills somebody's run list with rows about a
    button they pressed too early. Refusing here says the real thing instead.
    """
    flow = await get_flow(db, user_id, flow_id)

    if is_active and await published_version(db, flow.id) is None:
        raise HTTPException(status_code=400, detail=PUBLISH_FIRST)

    return await flow_crud.update(db, flow.id, {"is_active": bool(is_active)})


async def delete_flow(db: AsyncSession, user_id: int, flow_id: uuid_pkg.UUID) -> None:
    """
    Delete a workflow, its versions, triggers, runs and logs.

    The cascade is the model's, not this function's. What this function refuses is
    deleting a workflow that is still running: the worker driving it would carry on
    writing into somebody's system with no row left to record what it did, and the first
    sign of it would be a foreign-key error in a log nobody reads.
    """
    flow = await get_flow(db, user_id, flow_id)

    live = await run_crud.get_many(db, filters={"flow_id": flow.id}, limit=200)
    if any(run.status not in TERMINAL_RUN_STATUSES for run in live):
        raise HTTPException(
            status_code=409,
            detail=(
                "This workflow is running. Stop the run, then delete it — deleting it "
                "now would leave the run with nothing to write to."
            ),
        )

    await flow_crud.delete(db, flow.id)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


async def publish_flow(
    db: AsyncSession, user_id: int, flow_id: uuid_pkg.UUID
) -> IntegrationFlowVersion:
    """
    Freeze the current drawing as the version that runs.

    Four things happen, in this order, and the order is the design:

    1. **The write steps' real input lists are resolved and stamped onto a copy** of the
       drawing. ``flow_rules`` deliberately has no database, so it cannot look up what
       fields an operation takes; without this stamp its unmapped-required rule has
       nothing to check and publishing would accept a workflow that fails on its first
       record.
    2. **The stamped copy is validated** by ``validate_for_publish`` — everything Save
       refuses, plus that rule.
    3. **Every published version is archived**, in this transaction.
    4. **The new row is inserted** with the next version number.

    Steps 3 and 4 are one transaction ending in a partial unique index, which is what makes
    "one published version per flow" true rather than merely usual. The index alone would
    turn a double-click into an ``IntegrityError``; this function alone would lose to a
    concurrent request. Both, and the failure is a sentence.

    Re-publishing an unchanged drawing still makes a version. Tempting to refuse it as a
    no-op, and wrong: the drawing is not the only input — an operation's definition may
    have changed underneath it — and a version number people can point at is cheaper than
    a Publish button that sometimes does nothing.
    """
    flow = await get_flow(db, user_id, flow_id)

    snapshot = await _stamped_for_publish(db, user_id, flow)
    flow_rules.validate_for_publish(snapshot)

    await archive_published_versions(db, flow.id)

    version = await version_crud.create_pending(
        db,
        {
            "flow_id": flow.id,
            "version_number": await next_version_number(db, flow.id),
            "graph_data": snapshot,
            "graph_hash": idempotency.graph_hash(snapshot),
            "status": VERSION_PUBLISHED,
        },
    )
    await db.commit()
    await db.refresh(version)

    return version


async def unpublish_flow(
    db: AsyncSession, user_id: int, flow_id: uuid_pkg.UUID
) -> IntegrationFlow:
    """
    Withdraw the published version and switch the workflow off.

    Both, together. Archiving the version while leaving the flow active produces a
    schedule that claims its trigger every interval and records a skip because there is
    nothing to run — noise that says "misconfigured" when the truth is "somebody withdrew
    it deliberately". Runs already in flight are left alone: they are pinned to the version
    by id, and stopping them is :func:`stop_run`'s job, not this one's.
    """
    flow = await get_flow(db, user_id, flow_id)

    await archive_published_versions(db, flow.id)
    updated = await flow_crud.update(db, flow.id, {"is_active": False})

    return updated


async def _stamped_for_publish(
    db: AsyncSession, user_id: int, flow: IntegrationFlow
) -> Dict[str, Any]:
    """
    A deep copy of the drawing with each write step's required inputs written onto it.

    Deep-copied because the stamp must not reach the flow row: ``required_inputs`` is
    derived from an operation definition that can change, and a stale copy on the drawing
    would make the canvas's warning describe last month's API. The version is a snapshot
    and stale is exactly what a snapshot is for; the drawing is not.

    A step whose connection or operation cannot be resolved is refused here, naming the
    step. Publishing is the last moment anybody is watching — leaving it for the run means
    discovering it at 3am.
    """
    snapshot = copy.deepcopy(_validated_graph(flow.graph_data))

    for node in flow_rules.nodes_of(snapshot):
        if flow_rules.node_type_of(node) != NODE_CONNECTOR_WRITE:
            continue

        data = flow_rules.data_of(node)
        data["required_inputs"] = await _required_inputs(db, user_id, node)
        node["data"] = data

    return snapshot


async def _required_inputs(
    db: AsyncSession, user_id: int, node: Mapping[str, Any]
) -> List[str]:
    """
    The destination fields this step's operation will not accept a record without.

    Resolved through ``connector_nodes.resolve_target`` — the **same** function the write
    node itself calls at run time — rather than through a second lookup written for this
    file. A publish that resolved an operation differently from the run would produce a
    required-input list the run does not agree with, which is the exact failure this check
    exists to prevent.
    """
    try:
        target = await connector_nodes.resolve_target(
            db, flow_rules.data_of(node), user_id=user_id
        )
    except IntegrationFailure as exc:
        raise FlowValidationError(
            f"'{flow_rules.label_of(node)}': {exc}",
            node_id=flow_rules.node_id_of(node),
        ) from exc

    return [field.name for field in target.operation.inputs if field.required]


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


async def list_triggers(
    db: AsyncSession, flow: IntegrationFlow
) -> List[IntegrationTrigger]:
    """Every trigger on one workflow."""
    return await trigger_crud.get_many(db, filters={"flow_id": flow.id})


def build_trigger_views(triggers: Sequence[IntegrationTrigger]) -> List[dict]:
    """Triggers shaped for the schedule panel."""
    return [
        {
            "uuid": str(trigger.uuid),
            "node_id": trigger.node_id,
            "kind": trigger.kind,
            "is_enabled": trigger.is_enabled,
            "interval_seconds": trigger.interval_seconds,
            "timezone": trigger.timezone,
            "overlap_policy": trigger.overlap_policy,
            "next_run_at": trigger.next_run_at,
            "last_fired_at": trigger.last_fired_at,
        }
        for trigger in triggers
    ]


async def save_trigger(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid_pkg.UUID,
    *,
    node_id: str,
    kind: str,
    is_enabled: bool = False,
    interval_seconds: Optional[int] = None,
    timezone_name: str = "UTC",
    overlap_policy: str = OVERLAP_SKIP,
) -> IntegrationTrigger:
    """
    Create or update the trigger for one node of one workflow.

    Keyed on ``(flow, node_id)`` and upserted rather than inserted, because the canvas has
    one trigger node and editing its schedule is the ordinary case. An insert-only version
    would accumulate a row per edit and the scheduler would fire all of them.

    **``next_run_at`` is recomputed on every write**, by
    :func:`scheduler.backfill_next_run_at`. That column is the entire schedule — the
    scheduler holds nothing in memory and a fresh process fires a due row on its first
    tick — so a path that changes the interval without recomputing it leaves the workflow
    running on the old one until it next fires, which is the kind of bug that looks like
    the scheduler is broken.

    Enabling a schedule requires a published version, for the reason
    :func:`set_flow_active` gives.
    """
    flow = await get_flow(db, user_id, flow_id)

    kind = _validated_trigger_kind(kind)
    overlap_policy = _validated_overlap_policy(overlap_policy)

    if kind == TRIGGER_SCHEDULE:
        interval_seconds = _validated_interval(interval_seconds)
    else:
        interval_seconds = None
        is_enabled = bool(is_enabled) and kind != TRIGGER_MANUAL

    if is_enabled and await published_version(db, flow.id) is None:
        raise HTTPException(status_code=400, detail=PUBLISH_FIRST)

    existing = await trigger_crud.get_many(
        db, filters={"flow_id": flow.id, "node_id": str(node_id)}, limit=1
    )

    values = {
        "flow_id": flow.id,
        "node_id": str(node_id),
        "kind": kind,
        "is_enabled": bool(is_enabled),
        "interval_seconds": interval_seconds,
        "timezone": (timezone_name or "UTC").strip() or "UTC",
        "overlap_policy": overlap_policy,
    }

    # **One transaction, and ``next_run_at`` is written inside it.** The obvious shape —
    # write the row through the CRUD helper, which commits, then backfill and commit again
    # — leaves a window where an enabled schedule has no next slot, and a trigger in that
    # state never fires. Nothing about it looks wrong on the page.
    if existing:
        trigger = existing[0]
        for name, value in values.items():
            setattr(trigger, name, value)
    else:
        trigger = await trigger_crud.create_pending(db, values)

    scheduler.backfill_next_run_at(trigger)
    await db.commit()
    await db.refresh(trigger)

    return trigger


async def set_trigger_enabled(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid_pkg.UUID,
    trigger_id: uuid_pkg.UUID,
    is_enabled: bool,
) -> IntegrationTrigger:
    """
    Switch one schedule on or off.

    Goes through ``backfill_next_run_at`` like every other trigger write. Switching a
    schedule back on after a week must not fire the week's worth of missed slots — and it
    does not, because the backfill computes the *next* slot from now rather than
    continuing from ``last_fired_at``. That is ``catch_up = false`` expressed as
    arithmetic.
    """
    flow = await get_flow(db, user_id, flow_id)

    trigger = await trigger_crud.get_by_uuid(
        db, trigger_id, extra_filters={"flow_id": flow.id}
    )
    if trigger is None:
        raise HTTPException(status_code=404, detail="That trigger does not exist.")

    if is_enabled and await published_version(db, flow.id) is None:
        raise HTTPException(status_code=400, detail=PUBLISH_FIRST)

    trigger.is_enabled = bool(is_enabled)
    scheduler.backfill_next_run_at(trigger)
    await db.commit()
    await db.refresh(trigger)

    return trigger


async def delete_trigger(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid_pkg.UUID,
    trigger_id: uuid_pkg.UUID,
) -> None:
    """Remove a trigger. The scheduler stops seeing it the moment the row is gone."""
    flow = await get_flow(db, user_id, flow_id)

    trigger = await trigger_crud.get_by_uuid(
        db, trigger_id, extra_filters={"flow_id": flow.id}
    )
    if trigger is None:
        raise HTTPException(status_code=404, detail="That trigger does not exist.")

    await trigger_crud.delete(db, trigger.id)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


async def get_run(
    db: AsyncSession, user_id: int, run_id: uuid_pkg.UUID
) -> IntegrationRun:
    """
    One run, if it belongs to a workflow this user owns.

    Two statements rather than a join, because ``integration_runs`` has no ``user_id`` and
    putting one there would be a second copy of a fact the flow already holds — the kind
    that goes stale when a workflow changes hands.

    The run page, the progress poll, Stop and Replay all come through here, so ownership is
    decided once. Every failure says :data:`NO_SUCH_RUN` whether the run is somebody else's
    or nobody's; distinguishing them would confirm the existence of another user's run to
    anybody willing to guess uuids.
    """
    run = await run_crud.get_by_uuid(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=NO_SUCH_RUN)

    owner = await flow_crud.get_one(db, filters={"id": run.flow_id, "user_id": user_id})
    if owner is None:
        raise HTTPException(status_code=404, detail=NO_SUCH_RUN)

    return run


async def list_runs(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid_pkg.UUID,
    *,
    limit: int = 50,
) -> List[IntegrationRun]:
    """The history of one workflow, newest first."""
    flow = await get_flow(db, user_id, flow_id)
    return await run_crud.get_many(
        db, filters={"flow_id": flow.id}, order_by="started_at", desc=True, limit=limit
    )


async def start_run(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid_pkg.UUID,
    *,
    mode: str = RUN_MODE_LIVE,
) -> IntegrationRun:
    """
    Queue a run of the published version, now.

    **The run row and the job row are written in one transaction**, then committed, then
    the worker is woken. Committing between the two loses a run to a crash; waking before
    the commit sends a worker looking for a job that is not visible yet.

    A dry run is allowed on a draft — that is most of what a dry run is for, and it calls
    nobody. A live run is not: what would it even run? The refusal says so.
    """
    flow = await get_flow(db, user_id, flow_id)
    mode = _validated_mode(mode)

    version = await published_version(db, flow.id)
    if version is None and mode == RUN_MODE_LIVE:
        raise HTTPException(
            status_code=400,
            detail=(
                "Publish this workflow before running it for real. You can dry-run a "
                "draft — that calls nothing and shows you what it would have sent."
            ),
        )

    run = await run_service.begin_run(
        db, flow, version=version, mode=mode, trigger_kind=TRIGGER_MANUAL
    )
    await queue.enqueue(db, run, priority=1)
    await db.commit()
    await db.refresh(run)

    # After the commit, never before. See the docstring.
    queue.wake()

    return run


async def replay_run(
    db: AsyncSession, user_id: int, run_id: uuid_pkg.UUID
) -> IntegrationRun:
    """
    Run the same topology again, pinned to the **same version** as the original.

    Not "run the workflow again" — that would be :func:`start_run`, and if the drawing has
    moved on since, it would be a different workflow wearing the same name. A replay of a
    run that failed last Tuesday has to be the thing that failed last Tuesday, or the
    result answers a question nobody asked.

    ``replay_of_run_id`` links the two, so the run list can show a chain rather than two
    unrelated rows.
    """
    original = await get_run(db, user_id, run_id)
    flow = await flow_crud.get_one(db, filters={"id": original.flow_id})

    if original.status not in TERMINAL_RUN_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="That run has not finished yet. Wait for it, or stop it first.",
        )

    version = None
    if original.flow_version_id is not None:
        version = await version_crud.get_one(
            db, filters={"id": original.flow_version_id}
        )

    if version is None and original.mode == RUN_MODE_LIVE:
        raise HTTPException(
            status_code=409,
            detail=(
                "The version this run used has been deleted, so it cannot be repeated "
                "exactly. Publish the current drawing and run that instead."
            ),
        )

    run = await run_service.begin_run(
        db,
        flow,
        version=version,
        mode=original.mode,
        trigger_kind=TRIGGER_MANUAL,
        replay_of_run_id=original.id,
    )
    await queue.enqueue(db, run, priority=1)
    await db.commit()
    await db.refresh(run)

    queue.wake()

    return run


async def stop_run(db: AsyncSession, user_id: int, run_id: uuid_pkg.UUID) -> None:
    """
    Ask a run to stop.

    Delegates to ``run_service.request_stop``, which marks the row **before** cancelling
    the local task — the order matters, because cancelling first races the write and
    leaves a run that stopped with nothing on it saying why.

    Stopping is a request, not an instruction: a node already waiting on somebody else's
    server finishes that call first. The UI says so.
    """
    run = await get_run(db, user_id, run_id)
    await run_service.request_stop(db, run)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _default_graph() -> Dict[str, Any]:
    """A fresh copy of :data:`DEFAULT_GRAPH`. Round-tripped through JSON rather than
    ``deepcopy`` so the value written to a JSONB column is provably JSON — a stray tuple
    in the template would otherwise reach the database and fail there."""
    return json.loads(json.dumps(DEFAULT_GRAPH))


def _validated_graph(graph_data: Any) -> Dict[str, Any]:
    """
    The drawing, having passed the same rules a run will apply to it.

    ``validate_flow`` raises :class:`FlowValidationError` carrying the node at fault, and
    that exception is deliberately **not** converted to an ``HTTPException`` here: the
    canvas needs ``node_id`` to highlight the step, and a route that flattened it to a
    string would throw that away.
    """
    flow_rules.validate_flow(graph_data)
    return graph_data


def _validated_name(name: Optional[str]) -> str:
    """A workflow's name: present, and short enough for the column."""
    cleaned = (name or "").strip()

    if not cleaned:
        raise HTTPException(status_code=400, detail="A workflow needs a name.")

    if len(cleaned) > MAX_NAME_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"A workflow's name cannot be longer than {MAX_NAME_LENGTH} characters.",
        )

    return cleaned


async def _require_unused_name(
    db: AsyncSession,
    user_id: int,
    name: Optional[str],
    *,
    exclude_id: Optional[int] = None,
) -> None:
    """
    Refuse a name this user already used, before the unique index does.

    The index is the guarantee; this is the message. Without it the collision surfaces as
    an ``IntegrityError`` and a 500, which tells somebody nothing about what to change.
    Case-insensitive, matching ``uq_integration_flows_user_name_lower``.
    """
    cleaned = _validated_name(name)

    existing = await flow_named(db, user_id, cleaned, exclude_id=exclude_id)
    if existing is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"You already have a workflow called '{existing.name}'. Pick a different "
                "name."
            ),
        )


def _validated_batch_size(value: Any) -> int:
    """
    A default batch size inside the bounds the engine can honour.

    The ceiling is not a preference. ``record_buffer`` is process memory, so a batch is
    that many records held at once per running node — which is why ``MAX_BATCH_SIZE`` is
    enforced in validation rather than merely defaulted.
    """
    try:
        size = int(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"'{value}' is not a batch size. Give a whole number of records.",
        )

    if not MIN_BATCH_SIZE <= size <= MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A batch has to be between {MIN_BATCH_SIZE} and {MAX_BATCH_SIZE} "
                "records. A whole batch is held in memory at once, which is what the "
                "upper limit is protecting."
            ),
        )

    return size


def _validated_redacted_fields(values: Sequence[str]) -> List[str]:
    """Extra field names to strip from every preview. Deduplicated, lowercased and
    trimmed, because the deny-list is matched case-insensitively and a list holding
    ``Email`` and ``email`` twice is a list somebody will misread."""
    cleaned = []
    for value in values or []:
        name = str(value or "").strip().lower()
        if name and name not in cleaned:
            cleaned.append(name)
    return cleaned


def _validated_trigger_kind(kind: str) -> str:
    cleaned = str(kind or "").strip()
    if cleaned not in TRIGGER_KIND_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{kind}' is not a way to start a workflow. Choose one of: "
                f"{', '.join(sorted(TRIGGER_KIND_VALUES))}."
            ),
        )
    return cleaned


def _validated_overlap_policy(policy: str) -> str:
    cleaned = str(policy or "").strip() or OVERLAP_SKIP
    if cleaned not in OVERLAP_POLICY_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{policy}' is not a way to handle a run that is still going when the "
                f"next one is due. Choose one of: {', '.join(sorted(OVERLAP_POLICY_VALUES))}."
            ),
        )
    return cleaned


def _validated_interval(seconds: Any) -> int:
    """
    How often a schedule fires, with a floor.

    A minute is the floor because every fire is a run row, a queue job, a compile and a
    checkpoint stream — and because a sync that takes longer than its interval spends its
    life skipping, which is a workflow that looks scheduled and is not.
    """
    try:
        interval = int(seconds)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"'{seconds}' is not an interval. Give a whole number of seconds.",
        )

    if interval < MIN_INTERVAL_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A schedule cannot run more often than every {MIN_INTERVAL_SECONDS} "
                "seconds."
            ),
        )

    return interval


def _validated_mode(mode: str) -> str:
    cleaned = str(mode or "").strip() or RUN_MODE_LIVE
    if cleaned not in (RUN_MODE_LIVE, RUN_MODE_DRY_RUN):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{mode}' is not a way to run a workflow. Use '{RUN_MODE_LIVE}' or "
                f"'{RUN_MODE_DRY_RUN}'."
            ),
        )
    return cleaned
