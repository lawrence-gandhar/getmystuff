"""
Running a published graph on somebody else's behalf, and what came of it.

Four things can now own a published graph and want it run: a **data agent** calling it as a
tool, a **tool config** embedding it the way it embeds another tool, a **flow builder** node
running it mid-conversation, and a **workspace** sharing it across its agents. All four need
the same three questions answered — did it finish, did it stop to ask something, or did it
fail — and only the wording differs between them.

So the answering happens here, once, as a :class:`GraphOutcome`, and each owner phrases it for
its own audience. ``graph_tool_factory`` is the first caller and the reason this module exists
at all: its ``_run_tool`` and ``_answer_tool`` had the classification and the model's sentences
tangled together, which meant a second owner had to either import a model-facing string or
write the polling, the pause detection and the failure handling again. Both are ways for two
callers to disagree about what a paused run *is*.

## A pause is an outcome, not an error

That is the decision the shape rests on. A graph with an ``Ask a human`` node stops mid-run and
waits, and every owner has to be able to carry that: the agent relays the question and calls an
answering tool, a flow node ends the turn awaiting a reply, an embedding tool config returns the
question instead of rows. None of them can treat it as a failure, because nothing failed — and
none of them can ignore it, because the rows they wanted do not exist yet.

The run is parked on a persisted ``thread_id``, so a pause is resumable from a different request
by :func:`answer_graph_run`. Nothing about it lives in memory.

## What this module does not do

It does not phrase anything for a model, a visitor or an operator, and it does not decide how
long a caller should wait. It runs, watches, classifies, and hands back.
"""

import logging
import uuid as uuid_pkg
from typing import Any, Dict, List, Mapping, Optional

from app.models.graph_designer import (
    RUN_AWAITING_INPUT,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_SUCCEEDED,
)

logger = logging.getLogger(__name__)


# How long a caller waits for a run before being told it is still going, and how often it
# looks. Both are this module's rather than each owner's: a flow node and an agent tool
# waiting different lengths for the same graph would be two answers to one question.
WAIT_SECONDS = 90.0
POLL_SECONDS = 0.4


#: A run that finished and produced something.
OUTCOME_FINISHED = "finished"
#: A run that stopped to ask a question and is waiting for an answer.
OUTCOME_QUESTION = "question"
#: A run that failed, or was cancelled, or could not be started.
OUTCOME_FAILED = "failed"
#: A run still going when the caller's patience ran out. **Not** cancelled — see
#: :func:`_await_outcome`.
OUTCOME_RUNNING = "running"


class GraphOutcome:
    """
    What came of running a graph, in the four shapes an owner has to tell apart.

    ``view`` is the whole run view when there is one, carried rather than picked apart so a
    caller that already knows how to read one — ``graph_tool_factory`` does — keeps working
    off exactly what it read before. The named fields are for callers that should not have
    to learn that shape.
    """

    __slots__ = ("kind", "run_id", "view", "question", "reason")

    def __init__(
        self,
        kind: str,
        run_id: str = "",
        view: Optional[dict] = None,
        question: Optional[dict] = None,
        reason: str = "",
    ) -> None:
        self.kind = kind
        self.run_id = run_id
        self.view = view
        self.question = question
        self.reason = reason

    @property
    def finished(self) -> bool:
        return self.kind == OUTCOME_FINISHED

    @property
    def asks(self) -> bool:
        return self.kind == OUTCOME_QUESTION

    @property
    def rows(self) -> List[dict]:
        """
        The rows the run produced, or an empty list.

        Off ``result_preview``, which is the run's own record of what its last
        data-producing node returned — already capped by ``graph_state.preview_of`` with the
        real total in ``count`` beside it. A caller wanting the count wants
        ``self.total_rows``, not ``len(self.rows)``: they are different numbers whenever the
        result was larger than the preview, and confusing them is how a sample gets reported
        as a total.
        """
        output = self._output()

        return list(output.get("rows") or []) if output.get("kind") == "rows" else []

    @property
    def total_rows(self) -> int:
        """How many rows there really were, which is not how many :attr:`rows` holds."""
        return int(self._output().get("count") or 0)

    def _output(self) -> Mapping[str, Any]:
        return ((self.view or {}).get("result_preview") or {}).get("output") or {}


async def full_result(user_id: int, run_uuid: str) -> Any:
    """
    A finished run's whole last output, not the preview — every row, every value.

    :attr:`GraphOutcome.rows` is a **sample**: it comes off ``result_preview``, which is
    capped at twenty rows with the real total beside it. That is right for an owner
    *describing* a result to somebody, which is what every caller did until now, and wrong
    for one that is going to **use** the values. A tool config embedding a graph builds its
    filter from them, and a filter made of the first twenty of five hundred ids answers a
    different question than the one asked, with nothing in the result saying so.

    So a caller has to choose, and the choice is which of two failures it would rather
    have: a described result that says "20 of 500", or a used result that is silently 20.
    Anything that filters, joins or counts on these values wants this function.

    Read from the checkpointer by ``graph_run_service.full_result`` — nothing re-runs.
    Returns ``None`` when there is nothing to read.
    """
    from app.services.graph_designer import graph_run_service, run_store

    try:
        async with run_store.open_session() as db:
            return await graph_run_service.full_result(
                db, user_id, uuid_pkg.UUID(str(run_uuid)),
            )
    except Exception:  # noqa: BLE001 — see run_graph: an answer, never an exception
        logger.exception("Could not read the full result of graph run %s", run_uuid)
        return None


async def run_graph(
    user_id: int,
    graph_uuid: str,
    inputs: Optional[Mapping[str, Any]] = None,
) -> GraphOutcome:
    """
    Start a published graph, wait for it, and say what happened.

    Every failure is **returned rather than raised**, including one from starting the run at
    all. That is the rule ``tool_factory._build_tool`` states and the reason it exists holds
    for every owner here: raising aborts whatever was in progress — a conversation turn, a
    parent tool's query, a flow — and hands somebody a 500 for a state that could have been
    explained. The one thing this module owes its callers is an answer.
    """
    from app.services.graph_designer import graph_run_service, run_store

    try:
        async with run_store.open_session() as db:
            run_uuid = await graph_run_service.start_run(
                db,
                user_id,
                uuid_pkg.UUID(str(graph_uuid)),
                inputs=dict(inputs or {}),
            )
    except Exception as exc:  # noqa: BLE001 — a failure is an outcome, not a 500
        logger.exception("Could not start a run of graph %s", graph_uuid)
        return GraphOutcome(OUTCOME_FAILED, reason=readable(exc))

    return await _await_outcome(user_id, run_uuid)


async def answer_graph_run(
    user_id: int,
    run_uuid: str,
    answer: Any,
) -> GraphOutcome:
    """
    Hand a paused run its answer and say what happened next.

    Two failures are told apart here because the callers have to tell them apart, and only
    one of them is the answerer's to fix:

    * a **400** is the answer not fitting the question — "maybe" to a yes/no. Ordinary input,
      not a fault: it comes back as :data:`OUTCOME_QUESTION` with the same run still waiting,
      so the caller asks again rather than reporting that something is broken. It is
      deliberately not logged as an exception either — a stack trace per typo is noise.
    * anything else is a failure.
    """
    from litestar.exceptions import HTTPException

    from app.services.graph_designer import graph_run_service, run_store

    cleaned = str(run_uuid or "").strip()

    try:
        parsed = uuid_pkg.UUID(cleaned)
    except ValueError:
        return GraphOutcome(
            OUTCOME_FAILED,
            run_id=cleaned,
            reason="That is not a run id.",
        )

    try:
        async with run_store.open_session() as db:
            await graph_run_service.resume_run(db, user_id, parsed, answer)
    except HTTPException as exc:
        if exc.status_code == 400:
            return GraphOutcome(
                OUTCOME_QUESTION,
                run_id=cleaned,
                reason=str(exc.detail),
            )

        logger.warning("Could not resume graph run %s: %s", cleaned, exc.detail)
        return GraphOutcome(OUTCOME_FAILED, run_id=cleaned, reason=readable(exc))
    except Exception as exc:  # noqa: BLE001 — see run_graph
        logger.exception("Could not resume graph run %s", cleaned)
        return GraphOutcome(OUTCOME_FAILED, run_id=cleaned, reason=readable(exc))

    return await _await_outcome(user_id, cleaned)


async def _await_outcome(user_id: int, run_uuid: str) -> GraphOutcome:
    """
    Wait until the run finishes, pauses, or the caller's patience runs out.

    **Pausing counts as an outcome** — it is *the* outcome for a graph that asks something —
    so this waits for a terminal status **or** ``awaiting_input`` rather than only for the end.

    :data:`OUTCOME_RUNNING` means it is still going after :data:`WAIT_SECONDS`. The run is
    **not** cancelled in that case: it is doing real work and somebody may still want the
    result, so the caller says it is being worked out. Killing a run because one caller got
    bored would throw away the work and the answer with it.

    A short session per poll, because the task driving the run writes through its own and a
    session held open here would keep serving the rows it first read.
    """
    import asyncio

    from app.services.graph_designer import graph_run_service, run_store

    waited = 0.0

    while waited <= WAIT_SECONDS:
        async with run_store.open_session() as db:
            try:
                view = await graph_run_service.get_run(
                    db, user_id, uuid_pkg.UUID(run_uuid),
                )
            except Exception:  # noqa: BLE001 — a lookup failure is "still unknown"
                logger.exception("Could not read graph run %s", run_uuid)
                return GraphOutcome(OUTCOME_RUNNING, run_id=run_uuid)

        outcome = _classified(view, run_uuid)

        if outcome is not None:
            return outcome

        await asyncio.sleep(POLL_SECONDS)
        waited += POLL_SECONDS

    return GraphOutcome(OUTCOME_RUNNING, run_id=run_uuid)


def _classified(view: dict, run_uuid: str) -> Optional[GraphOutcome]:
    """
    One run view as an outcome, or ``None`` while it is still going.

    A cancelled run is a **failure** rather than a fourth kind. Every owner does the same
    thing with it — say the work did not complete and do not present a partial result as an
    answer — so a separate kind would be a distinction none of them acted on.
    """
    status = str(view.get("status") or "")

    if status == RUN_AWAITING_INPUT:
        return GraphOutcome(
            OUTCOME_QUESTION,
            run_id=run_uuid,
            view=view,
            question=dict(view.get("interrupt_payload") or {}),
        )

    if status == RUN_SUCCEEDED:
        return GraphOutcome(OUTCOME_FINISHED, run_id=run_uuid, view=view)

    if status == RUN_FAILED:
        return GraphOutcome(
            OUTCOME_FAILED,
            run_id=run_uuid,
            view=view,
            reason=str(view.get("error_message") or "The run could not be completed."),
        )

    if status == RUN_CANCELLED:
        return GraphOutcome(
            OUTCOME_FAILED,
            run_id=run_uuid,
            view=view,
            reason="The run was stopped before it finished.",
        )

    return None


def readable(exc: Exception) -> str:
    """
    One sentence out of an exception, for an owner to put in front of somebody.

    A Litestar ``HTTPException``'s ``detail`` is already written for a person — it is what a
    validator says to an operator filling in a form — so it is used as it is. Anything else
    gets a fixed sentence, because the alternative is a driver's or a library's words in
    front of a visitor.
    """
    detail = getattr(exc, "detail", None)

    if detail:
        return str(detail)

    return "The graph could not be run."
