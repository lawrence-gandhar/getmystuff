"""
A sentence becomes a workflow — and then has to survive being checked.

**The model is the thing that changes a workflow, never the thing that runs one.** That is
the honest reading of "always act as Agent AI" for a deterministic engine, and it is stated
at length in ``documentations/INTEGRATIONS_AI.md``. What this module produces is a *draft*
a person publishes.

Two layers of validation, structured like ``aggregate_planner.validate_plan``:

1. **Pydantic bounds the shape** — twelve steps, twenty mappings, five assumptions, every
   field a string of bounded length. That happens in ``workflow_draft_schemas`` before
   anything here sees the draft.
2. **:func:`validate_draft` bounds the meaning** — every connection name resolved against
   the user's real rows, every operation against that connection's real operations, every
   mapping target against that operation's real inputs, every ``source_ref`` against what
   exists earlier in order. Then ids and layout are assigned, and then the whole thing goes
   through **the same ``validate_flow`` a hand-drawn workflow goes through**, which is what
   makes a generated workflow exactly as trustworthy as one somebody drew.

Four decisions worth knowing about.

**Resolution replaces the spelling.** A resolved connection name becomes the real
``connection_uuid`` in the saved node. Not "tolerated", not "recorded alongside" —
replaced, so nothing downstream can ever act on a name a model chose.

**No fuzzy matching.** ``catalogue.find_connection`` is exact, then case-insensitive, then
stops. "Shopify Prod" silently becoming "Shopify EU" writes customers into the wrong store.

**One repair, then refuse.** A flawed SQL string is readable and its author fixes it;
a canvas rendered with a step pointed at a nonexistent connection invites somebody to press
Publish. So this departs from ``sql_assist._regrouped``'s degrade-to-a-warning. Only
resolvable faults are retried — ``unsupported=True`` is never retried, because the model has
already answered.

**An unmapped required input is a warning, not an error.** Refusing throws away a draft that
is ninety percent right. Publishing refuses it later, which is the correct moment: the
canvas shows it in red and a person maps it.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    NODE_BATCH,
    NODE_CONNECTOR_READ,
    NODE_CONNECTOR_WRITE,
    NODE_SUCCESS,
    NODE_TRIGGER,
    OPERATION_READ,
    OPERATION_WRITE,
    PORT_BODY,
    PORT_DEFAULT,
    PORT_DONE,
    TRIGGER_MANUAL,
)
from app.schemas.integrations.workflow_draft_schemas import WorkflowDraft
from app.services.integrations.ai import catalogue as catalogue_builder, draft_prompts
from app.services.integrations.engine import flow_rules
from app.services.integrations.errors import FlowValidationError, IntegrationFailure

logger = logging.getLogger(__name__)

#: Where the first step lands and how far apart they sit. Assigned here rather than asked
#: for: a model spending tokens on coordinates is a model returning malformed output, and
#: the layout is a property of the number of steps rather than of what they do.
LAYOUT_ORIGIN_X = 120
LAYOUT_ORIGIN_Y = 200
LAYOUT_STEP_X = 260

#: How far a step inside a batch body is dropped, so the loop-back edge is visible as a
#: loop rather than passing behind the steps it returns over.
LAYOUT_BODY_DROP = 130

#: How many times the model is asked again. One. See the module docstring.
MAX_REPAIRS = 1


class DraftRefused(IntegrationFailure):
    """
    The draft cannot be turned into a workflow, and asking again will not help.

    Carries the problems separately from the sentence so a partial can list them, and
    ``alternatives`` so the page can say what does exist — a refusal naming the three
    connections somebody has is one they can act on, and "that connection does not exist"
    is not.
    """

    def __init__(
        self,
        message: str,
        *,
        problems: Optional[Sequence[str]] = None,
        alternatives: Optional[Sequence[str]] = None,
    ) -> None:
        super().__init__(message)
        self.problems = list(problems or [])
        self.alternatives = list(alternatives or [])


@dataclass
class ResolvedDraft:
    """A draft that survived resolution — the drawing, and what to tell the person."""

    name: str
    description: str
    graph_data: Dict[str, Any]
    assumptions: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def node_count(self) -> int:
        return len(self.graph_data.get("nodes") or [])


# ---------------------------------------------------------------------------
# Generating
# ---------------------------------------------------------------------------


async def draft_workflow(
    db: AsyncSession,
    user_id: int,
    instruction: str,
    *,
    use_inbuilt_llm: bool = False,
) -> ResolvedDraft:
    """
    One sentence in, one checked draft out — or a refusal that says what is missing.

    Goes through ``ai_analytics_service.answer_structured`` rather than
    ``build_chat_model``: it is single-shot, it covers all three providers **including the
    in-built Ollama one** — which ``build_chat_model`` refuses outright via its tool-calling
    denylist — and it is the path whose provider resolution, retries and error mapping are
    already the ones every other AI surface here uses.

    **Nothing is saved.** This returns a drawing; ``flow_service.create_flow`` is what
    stores it, from a separate request a person makes by pressing Save. That separation is
    what the AI-hallucination tests assert on: a refusal must leave zero rows behind, and
    asserting the refusal alone would pass an implementation that saved first and validated
    after.
    """
    cleaned = str(instruction or "").strip()
    if not cleaned:
        raise DraftRefused("Say what you want the workflow to do.")

    built = await catalogue_builder.build(db, user_id)

    if not built.get("connections"):
        raise DraftRefused(
            "There are no usable connections yet, so there is nothing to build a workflow "
            "out of. Add a connection first, then describe what you want moved."
        )

    # Sized to the model that will read it. The in-built one has a 1536-token prompt budget
    # and Ollama truncates from the *end* — which is where the user's own sentence goes — so
    # an over-long prompt does not crowd the request out, it deletes it.
    #
    # This bounds the variable part only. Measured, the schema `_json_only_instruction`
    # appends costs more than everything else combined, which is why the local path needs a
    # raised `OLLAMA_NUM_CTX` rather than a smaller catalogue — see MAX_CHARS_INBUILT.
    rendered = catalogue_builder.render(
        built,
        max_chars=(
            catalogue_builder.MAX_CHARS_INBUILT if use_inbuilt_llm
            else catalogue_builder.MAX_CHARS
        ),
    )
    return await _drafted_with_repairs(
        db,
        user_id,
        draft_prompts.user_content(cleaned, rendered),
        built,
        fallback_name=cleaned,
        use_inbuilt_llm=use_inbuilt_llm,
    )


async def _drafted_with_repairs(
    db: AsyncSession,
    user_id: int,
    content: str,
    built: Mapping[str, Any],
    *,
    fallback_name: str,
    use_inbuilt_llm: bool,
) -> ResolvedDraft:
    """
    Ask, check, and ask once more if what came back can be corrected.

    **Only resolvable faults are retried.** A model that set ``unsupported`` has answered the
    question, and asking it the same thing again to get a different answer is precisely how
    a decline turns into a hallucination.
    """
    problems: List[str] = []

    for attempt in range(MAX_REPAIRS + 1):
        note = draft_prompts.repair_note(problems) if problems else ""
        draft = await _ask(db, user_id, content + note, use_inbuilt_llm=use_inbuilt_llm)

        if draft.unsupported:
            raise DraftRefused(
                draft.reason.strip()
                or "That cannot be built from the connections you have.",
                alternatives=catalogue_builder.connection_names(built),
            )

        try:
            return validate_draft(draft, built, fallback_name=fallback_name)
        except DraftRefused as refusal:
            if attempt >= MAX_REPAIRS:
                raise
            problems = refusal.problems or [str(refusal)]
            logger.info(
                "Workflow draft rejected, asking once more. Problems: %s", problems
            )

    # Unreachable: the loop either returns or raises on its last pass. Present so a future
    # change to MAX_REPAIRS cannot fall off the end returning None.
    raise DraftRefused("That draft could not be turned into a workflow.")


async def _ask(
    db: AsyncSession, user_id: int, content: str, *, use_inbuilt_llm: bool
) -> WorkflowDraft:
    """
    One call to whichever provider this user has configured.

    Imported inside the function, the same call ``graph_run_service`` makes for its
    compiler: the analytics service pulls in three provider SDKs, and this module is
    imported by the routes, which should not pay for that at import time.
    """
    from app.services.ai_analytics import ai_analytics_service

    return await ai_analytics_service.answer_structured(
        db,
        user_id,
        draft_prompts.system_prompt(),
        content,
        WorkflowDraft,
        use_inbuilt_llm=use_inbuilt_llm,
    )


# ---------------------------------------------------------------------------
# Validating the meaning
# ---------------------------------------------------------------------------


def validate_draft(
    draft: WorkflowDraft,
    built: Mapping[str, Any],
    *,
    fallback_name: str = "",
) -> ResolvedDraft:
    """
    Turn a model's proposal into a drawing, refusing anything that does not resolve.

    Pure — no database, no network. Everything it checks against is in ``built``, which is
    the same structure the model was shown, so what was offered and what is enforced cannot
    disagree. That also makes every one of these refusals a table-driven unit test.

    The order is the order a reader would want: is there anything here, does every step
    name a type we can wire, does every name resolve, does every reference point backwards,
    and finally — after ids and edges are assigned — does the whole thing pass the validator
    a hand-drawn workflow passes.
    """
    if not draft.steps:
        raise DraftRefused("That draft has no steps in it.")

    problems: List[str] = []
    warnings: List[str] = []
    resolved: List[Dict[str, Any]] = []
    seen_refs: Dict[str, str] = {}

    for index, step in enumerate(draft.steps):
        node_id = f"n{index + 1}_{step.type or 'step'}"
        node = _resolve_step(step, built, seen_refs, node_id, problems, warnings)
        if node is not None:
            resolved.append(node)
            seen_refs[step.ref] = node_id

    if problems:
        raise DraftRefused(
            "That draft named things that do not exist.",
            problems=problems,
            alternatives=catalogue_builder.connection_names(built),
        )

    graph_data = _wire(resolved)

    try:
        flow_rules.validate_flow(graph_data)
    except FlowValidationError as exc:
        # The same validator a hand-drawn workflow goes through. A draft that reaches here
        # and fails is a wiring bug in this module rather than a model's mistake, which is
        # why the message says so — nobody should spend an afternoon rewording a prompt to
        # fix it.
        raise DraftRefused(
            "That draft did not come out as a runnable workflow: " + str(exc),
            problems=[str(exc)],
        ) from exc

    return ResolvedDraft(
        name=_name_for(draft, fallback_name),
        description=draft.description.strip(),
        graph_data=graph_data,
        assumptions=[a.strip() for a in draft.assumptions if a.strip()],
        warnings=warnings,
    )


def _resolve_step(
    step: Any,
    built: Mapping[str, Any],
    seen_refs: Mapping[str, str],
    node_id: str,
    problems: List[str],
    warnings: List[str],
) -> Optional[Dict[str, Any]]:
    """One step, resolved — or ``None`` with a problem recorded."""
    if step.type not in draft_prompts.DRAFTABLE_STEP_TYPES:
        problems.append(
            f"'{step.label or step.ref}' is a {step.type or 'nameless'} step, which a draft "
            "cannot include. Add it on the canvas after saving: "
            + ", ".join(sorted(draft_prompts.DRAFTABLE_STEP_TYPES))
            + " are the ones a draft may use."
        )
        return None

    data: Dict[str, Any] = {"label": step.label.strip() or step.ref}

    if step.source_ref:
        # **Backwards only.** A forward reference is a step reading records that do not
        # exist yet; left alone it would produce a workflow reading an empty set and
        # reporting success.
        if step.source_ref not in seen_refs:
            problems.append(
                f"'{data['label']}' reads from '{step.source_ref}', which is not a step "
                "that comes before it."
            )
            return None
        data["source_node"] = seen_refs[step.source_ref]

    if step.type == NODE_BATCH:
        data["batch_size"] = _batch_size(step.batch_size)

    if step.type in (NODE_CONNECTOR_READ, NODE_CONNECTOR_WRITE):
        if not _resolve_connector(step, built, data, problems, warnings):
            return None

    return {
        "id": node_id,
        "type": step.type,
        "position": {"x": 0, "y": 0},   # assigned by _wire
        "data": data,
    }


def _resolve_connector(
    step: Any,
    built: Mapping[str, Any],
    data: Dict[str, Any],
    problems: List[str],
    warnings: List[str],
) -> bool:
    """
    Resolve a read or write step's connection, operation and mappings.

    **Every one of the four lies this catches is silent if it is not.** A nonexistent
    connection, an operation the connector does not have, a mapping target the operation
    does not accept, and a required field with nothing in it — the first two fail loudly at
    run time, and the third and fourth do not fail at all: the sync runs green and the field
    is simply not there.
    """
    label = data["label"]

    connection = catalogue_builder.find_connection(built, step.connection)
    if connection is None:
        names = catalogue_builder.connection_names(built)
        problems.append(
            f"'{label}' uses a connection called '{step.connection}', which does not "
            "exist. You have: " + (", ".join(names) if names else "none") + "."
        )
        return False

    # **The spelling is replaced, not kept.** Nothing downstream can act on a name the
    # model chose — see the module docstring.
    data["connection_uuid"] = connection["uuid"]

    operation = catalogue_builder.find_operation(connection, step.operation)
    if operation is None:
        wanted = OPERATION_READ if step.type == NODE_CONNECTOR_READ else OPERATION_WRITE
        available = catalogue_builder.operation_ids(connection, wanted)
        problems.append(
            f"'{label}' uses an operation called '{step.operation}', which "
            f"'{connection['label']}' does not have. It has: "
            + (", ".join(available) if available else "none of that kind") + "."
        )
        return False

    expected = OPERATION_READ if step.type == NODE_CONNECTOR_READ else OPERATION_WRITE
    if operation["kind"] != expected:
        problems.append(
            f"'{label}' is a {expected} step but '{operation['id']}' is a "
            f"{operation['kind']} operation."
        )
        return False

    data["operation_id"] = operation["id"]

    if step.type == NODE_CONNECTOR_WRITE:
        data["mappings"] = _resolve_mappings(step, operation, label, problems, warnings)
        # Stamped so the canvas can mark the red fields immediately, before anybody
        # presses Publish. Publishing re-derives it from the live operation rather than
        # trusting this — see `flow_service._stamped_for_publish`.
        data["required_inputs"] = catalogue_builder.required_inputs(operation)

    return True


def _resolve_mappings(
    step: Any,
    operation: Mapping[str, Any],
    label: str,
    problems: List[str],
    warnings: List[str],
) -> List[Dict[str, Any]]:
    """
    Each mapping's destination, resolved against the operation's real input list.

    **This is the hallucination that matters most.** A model writing ``customer_email``
    where the operation takes ``email`` produces a workflow that runs, reports success, and
    does not carry the address — and nothing in the run record says so, because as far as
    the engine is concerned nobody asked for that field.
    """
    known = {name.lower(): name for name in catalogue_builder.input_names(operation)}
    mappings: List[Dict[str, Any]] = []

    for mapping in step.mappings:
        target = known.get(mapping.target.lower())

        if target is None:
            problems.append(
                f"'{label}' maps something into '{mapping.target}', which "
                f"'{operation['id']}' does not accept. It accepts: "
                + ", ".join(catalogue_builder.input_names(operation)) + "."
            )
            continue

        entry: Dict[str, Any] = {"target": target}
        if mapping.const is not None:
            entry["const"] = mapping.const
        else:
            entry["source"] = mapping.source
        if mapping.transform:
            entry["transform"] = mapping.transform

        mappings.append(entry)

    _warn_about_unmapped(operation, mappings, label, warnings)

    return mappings


def _warn_about_unmapped(
    operation: Mapping[str, Any],
    mappings: Sequence[Mapping[str, Any]],
    label: str,
    warnings: List[str],
) -> None:
    """
    A required field with nothing in it is a **warning**, not a refusal.

    Refusing would throw away a draft that is ninety percent right over a field somebody
    can fill in in five seconds. Publishing refuses it, which is the correct moment: by
    then a person has looked at it, and the canvas has been showing it in red the whole
    time.
    """
    filled = {str(entry.get("target")) for entry in mappings}
    missing = [
        name for name in catalogue_builder.required_inputs(operation) if name not in filled
    ]

    if missing:
        warnings.append(
            f"'{label}' has required fields with nothing mapped to them: "
            + ", ".join(missing)
            + ". Map them on the canvas — this workflow cannot be published until you do."
        )


def _batch_size(value: Any) -> int:
    """Clamped rather than refused. A model that wrote 100000 meant "a lot", and refusing
    a whole draft over a number with an obvious right answer is not worth the exchange."""
    try:
        size = int(value)
    except (TypeError, ValueError):
        return DEFAULT_BATCH_SIZE

    return max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, size))


def _name_for(draft: WorkflowDraft, fallback: str) -> str:
    """
    What the workflow is called.

    The model's name if it gave one; otherwise the first line of what was asked for, so a
    draft always arrives with something recognisable rather than "Untitled". Uniqueness is
    ``flow_service``'s to enforce — it has the database and this does not.
    """
    named = draft.name.strip()
    if named:
        return named[:255]

    first_line = fallback.strip().splitlines()[0] if fallback.strip() else ""
    return (first_line[:255] or "Generated workflow")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _wire(steps: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Turn an ordered list of steps into a drawing, edges and positions included.

    **The model draws none of this**, and that is the decision the whole module rests on.
    A model-drawn batch whose body never returns is one batch of a hundred reported as a
    success — and the drawing looks entirely reasonable, so nobody reviewing it has a reason
    to doubt it. Computing the wiring means that failure cannot be expressed.

    The shape, which is the shape every Phase 1 draft has:

        trigger → [before the batch] → batch ─body→ [the body] ─┐
                                         ↑                       │
                                         └───────────────────────┘
                                       ─done→ success

    With no batch it is a straight line into ``success``.
    """
    trigger = {
        "id": "trigger_1",
        "type": NODE_TRIGGER,
        "position": {"x": LAYOUT_ORIGIN_X, "y": LAYOUT_ORIGIN_Y},
        "data": {"label": "Trigger", "kind": TRIGGER_MANUAL},
    }
    success = {
        "id": "success_1",
        "type": NODE_SUCCESS,
        "position": {"x": 0, "y": LAYOUT_ORIGIN_Y},
        "data": {"label": "Done"},
    }

    chain = [trigger] + list(steps)
    batch_index = _index_of_batch(chain)

    _lay_out(chain, batch_index)
    success["position"] = {
        "x": LAYOUT_ORIGIN_X + LAYOUT_STEP_X * len(chain),
        "y": LAYOUT_ORIGIN_Y,
    }

    edges = (
        _loop_edges(chain, batch_index, success)
        if batch_index is not None
        else _straight_edges(chain, success)
    )

    return {"nodes": chain + [success], "edges": edges}


def _index_of_batch(chain: Sequence[Mapping[str, Any]]) -> Optional[int]:
    for index, node in enumerate(chain):
        if node["type"] == NODE_BATCH:
            return index
    return None


def _lay_out(chain: Sequence[Dict[str, Any]], batch_index: Optional[int]) -> None:
    """Left to right, with the batch body dropped so the loop-back reads as a loop rather
    than as a line passing behind the steps it returns over."""
    for index, node in enumerate(chain):
        inside_body = batch_index is not None and index > batch_index
        node["position"] = {
            "x": LAYOUT_ORIGIN_X + LAYOUT_STEP_X * index,
            "y": LAYOUT_ORIGIN_Y + (LAYOUT_BODY_DROP if inside_body else 0),
        }


def _straight_edges(
    chain: Sequence[Mapping[str, Any]], success: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    edges = []
    for index in range(len(chain) - 1):
        edges.append(_edge(chain[index], chain[index + 1]))
    edges.append(_edge(chain[-1], success))
    return edges


def _loop_edges(
    chain: Sequence[Mapping[str, Any]],
    batch_index: int,
    success: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """
    The batch and everything after it.

    ``done`` goes to success and ``body`` goes to the first step after the batch; the last
    step of the body returns to the batch. A body that did not return is the failure the
    validator refuses and the one this shape cannot produce.
    """
    edges = []

    for index in range(batch_index):
        edges.append(_edge(chain[index], chain[index + 1]))

    batch = chain[batch_index]
    body = list(chain[batch_index + 1:])

    edges.append(_edge(batch, success, port=PORT_DONE))

    if not body:
        # A batch with nothing in its body would be refused by the validator, and the
        # sentence it produces is about a drawing nobody drew. Sending the body straight
        # back is a loop that reads records and does nothing with them — honest, saveable,
        # and obvious on the canvas.
        edges.append(_edge(batch, batch, port=PORT_BODY))
        return edges

    edges.append(_edge(batch, body[0], port=PORT_BODY))

    for index in range(len(body) - 1):
        edges.append(_edge(body[index], body[index + 1]))

    edges.append(_edge(body[-1], batch))

    return edges


def _edge(
    source: Mapping[str, Any], target: Mapping[str, Any], *, port: str = PORT_DEFAULT
) -> Dict[str, Any]:
    """
    One connection between two steps.

    Every draftable step leaves by ``default``; the batch's two ports are passed
    explicitly. There is no port table here because there is nothing to choose between —
    the three step types with interesting ports are exactly the three a draft may not
    contain, for the reason ``draft_prompts.DRAFTABLE_STEPS`` gives.
    """
    return {
        "id": f"{source['id']}-{port}-{target['id']}",
        "source": source["id"],
        "source_port": port,
        "target": target["id"],
    }
