"""
Tests for the compiled graph — app/services/graph_designer/graph_compiler.py and the
runners and orchestrator it drives.

Against a **real SQLite database** and a real checkpointer, like the chain-graph tests
next door: the interesting questions are whether a loop actually iterates, whether a
failure actually takes the error path, and whether a paused run actually resumes — and a
mock would only prove the module calls what it calls.

The properties that carry the suite, in the order they matter:

* **A failure with an error path drawn does not fail the run.** Two state channels rather
  than one flag, and this is the pair of tests that holds them apart: the same broken node
  ends the run when nothing handles it and is recovered from when something does.
* **A loop iterates, and its ceiling refuses rather than truncating.** Rows from the first
  two of three departments are indistinguishable from rows for all three, so the ceiling
  has to stop the run rather than quietly shorten it.
* **A paused run resumes from its checkpoint.** The interrupt fires in one task and the
  answer arrives from another, so the seam is crossed for real here rather than mocked.
* **Testing a selection runs exactly the selection**, records the rest as skipped, and
  fails loudly when a chosen node reads something that was left out — a green tick on a
  test that ran nothing is the outcome worth preventing.

Requires LangGraph, so it runs in the container:
``docker compose exec app python -m pytest tests/unit/services/graph_designer``
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid as uuid_pkg
from pathlib import Path

import pytest

# Before the compiler is imported, not after: LangGraph lives in the container (see
# DOCKER_AND_LOCAL_LLM.md), and importing it outside would be a collection error rather
# than a skip. Same placement as test_tool_chain_graph.py and test_download_graph.py.
pytest.importorskip("langgraph", reason="LangGraph is installed in the container only")

from app.models.datasource import DataSource  # noqa: E402
from app.models.graph_designer import (  # noqa: E402
    RUN_AWAITING_INPUT,
    RUN_FAILED,
    RUN_SUCCEEDED,
    ToolGraph,
)
from app.services.graph_designer import (  # noqa: E402
    graph_run_service,
    run_store,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    """
    Three departments, so a loop over them has a count worth asserting.

    ``staff`` exists so a loop body can run a *different* statement per department. The
    counts are deliberately uneven — 2, 1, 3 — because a union of equal-sized passes would
    pass just as well if the same pass were collected three times.
    """
    path = tmp_path / "warehouse.db"

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE departments (id INTEGER PRIMARY KEY, name TEXT, active INTEGER);
        INSERT INTO departments VALUES (1, 'Eng', 1), (2, 'Sales', 1), (3, 'Ops', 0);

        CREATE TABLE staff (id INTEGER PRIMARY KEY, dept_id INTEGER, name TEXT);
        INSERT INTO staff VALUES
            (10, 1, 'Ann'), (11, 1, 'Bob'),
            (12, 2, 'Cid'),
            (13, 3, 'Dee'), (14, 3, 'Eve'), (15, 3, 'Fay');
        """
    )
    connection.commit()
    connection.close()

    return path


@pytest.fixture
async def datasource(db, user, warehouse: Path):  # noqa: ANN001, ANN201
    """A real datasource row — the runner resolves it by uuid and scopes it to its owner."""
    row = DataSource(
        user_id=user.id,
        datasource_name=f"warehouse-{uuid_pkg.uuid4().hex[:6]}",
        db_type="sqlite",
        database_name=str(warehouse),
        is_active=True,
        password_encrypted="",
        configuration_data={},
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
async def make_graph(db, user):  # noqa: ANN001, ANN201
    async def _make(nodes: list, edges: list) -> ToolGraph:
        row = ToolGraph(
            user_id=user.id,
            name=f"graph-{uuid_pkg.uuid4().hex[:8]}",
            graph_data={"nodes": nodes, "edges": edges},
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def run_graph(db, user, background_sessions):  # noqa: ANN001, ANN201
    """
    Start a run and wait for it to settle.

    ``background_sessions`` is load-bearing: the run's nodes and the poll loop open their
    own sessions through ``run_store.open_session``, which in the container points at the
    *development* database unless it is redirected at the test one. The same fixture the
    downloader graph tests depend on, for the same reason.
    """
    async def _run(graph: ToolGraph, **kwargs) -> dict:
        run_uuid = await graph_run_service.start_run(db, user.id, graph.uuid, **kwargs)

        for _ in range(200):
            await asyncio.sleep(0.05)
            view = await graph_run_service.get_run(
                db, user.id, uuid_pkg.UUID(run_uuid),
            )
            if view["status"] != "running":
                return view

        return view

    return _run


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def node(node_id: str, node_type: str, **data) -> dict:
    return {"id": node_id, "type": node_type, "position": {"x": 0, "y": 0}, "data": data}


def edge(source: str, target: str, port: str = "default") -> dict:
    return {
        "id": f"{source}->{target}:{port}",
        "source": source, "source_port": port, "target": target,
    }


def sql(node_id: str, datasource, statement: str, tables=("departments",), **extra) -> dict:
    return node(
        node_id, "sql",
        label=node_id,
        datasource_id=str(datasource.uuid),
        table_names=list(tables),
        sql_query=statement,
        **extra,
    )


def steps_by_node(view: dict, node_id: str) -> list:
    return [step for step in view["steps"] if step["node_id"] == node_id]


def statuses(view: dict) -> dict:
    """The latest status per node, which is what the canvas paints."""
    latest = {}
    for step in view["steps"]:
        latest[step["node_id"]] = step["status"]
    return latest


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNothingCapsWhatANodeReturns:
    """
    A SQL node returns every row its statement matches. The only limit is the one the
    author wrote in the statement.

    ``MAX_TOOL_ROWS`` is 200 and answers a different question — what a *tool* may put into a
    language model's prompt. A designed graph is a pipeline moving rows between its own
    nodes, and capping it there returned a sample of somebody's data as though it were the
    answer, with nothing in the result saying so.

    The counts here are asserted **exactly**, never as `> 200`: a truncated result and a
    complete one are indistinguishable from an inequality, which is the whole failure this
    is about.
    """

    async def test_a_node_returns_more_rows_than_the_tool_cap_allows(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """A cross join of three departments four ways: 81 rows, where a tool would see 200
        as its ceiling and this used to stop at it."""
        graph = await make_graph(
            [
                node("s", "start"),
                sql(
                    "many", datasource,
                    "SELECT a.id AS a, b.id AS b, c.id AS c, d.id AS d "
                    "FROM departments a, departments b, departments c, departments d",
                ),
                node("ok", "success"),
            ],
            [edge("s", "many"), edge("many", "ok")],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED, view.get("error_message")
        assert steps_by_node(view, "many")[0]["output_preview"]["count"] == 81

    async def test_a_limit_in_the_statement_is_what_bounds_the_result(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        The other half of the same rule: the author's own ``LIMIT`` is honoured, and it is
        now the only thing that bounds a node. Asserted so "no cap" cannot quietly become
        "the statement is rewritten".
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql(
                    "few", datasource,
                    "SELECT a.id FROM departments a, departments b, departments c "
                    "LIMIT 7",
                ),
                node("ok", "success"),
            ],
            [edge("s", "few"), edge("few", "ok")],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED, view.get("error_message")
        assert steps_by_node(view, "few")[0]["output_preview"]["count"] == 7

    async def test_the_log_preview_is_still_capped(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        Uncapping the *query* must not uncap the *log*. The step row still carries twenty
        sample rows and the real count beside them — which is also what a graph called as an
        agent's tool reports from, so a model's side of this is unchanged.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql(
                    "many", datasource,
                    "SELECT a.id AS a, b.id AS b, c.id AS c, d.id AS d "
                    "FROM departments a, departments b, departments c, departments d",
                ),
                node("ok", "success"),
            ],
            [edge("s", "many"), edge("many", "ok")],
        )

        view = await run_graph(graph)
        preview = steps_by_node(view, "many")[0]["output_preview"]

        assert preview["count"] == 81, "the real total"
        assert len(preview["rows"]) == 20, "but only a sample in the row"
        assert preview["truncated"] is True


class TestALinearRun:
    async def test_runs_every_node_in_order_and_records_each_one(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        graph = await make_graph(
            [
                node("s", "start"),
                sql("q", datasource, "SELECT id, name FROM departments"),
                node("ok", "success", message="Done."),
            ],
            [edge("s", "q"), edge("q", "ok")],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED
        assert [step["node_id"] for step in view["steps"]] == ["s", "q", "ok"]
        assert [step["sequence"] for step in view["steps"]] == [0, 1, 2]

    async def test_the_sequence_increments_rather_than_staying_at_zero(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        Asserted on its own because the obvious implementation — ``max(sequence) or -1`` —
        reads a legitimate first sequence of 0 as "no rows yet" and gives every step the
        same position, leaving the log with no order at all.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql("a", datasource, "SELECT id FROM departments"),
                sql("b", datasource, "SELECT name FROM departments"),
                node("ok", "success"),
            ],
            [edge("s", "a"), edge("a", "b"), edge("b", "ok")],
        )

        view = await run_graph(graph)

        sequences = [step["sequence"] for step in view["steps"]]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)

    async def test_records_the_row_count_and_a_capped_preview(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        graph = await make_graph(
            [
                node("s", "start"),
                sql("q", datasource, "SELECT id, name FROM departments"),
                node("ok", "success"),
            ],
            [edge("s", "q"), edge("q", "ok")],
        )

        view = await run_graph(graph)
        step = steps_by_node(view, "q")[0]

        assert step["output_preview"]["kind"] == "rows"
        assert step["output_preview"]["count"] == 3
        assert step["duration_ms"] is not None

    async def test_the_runs_result_is_the_data_not_the_success_nodes_bookkeeping(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        A graph ends at a Success node whose output is ``{"succeeded": true}``, so taking
        "the last output" reports a graph that read three rows as having returned nothing.
        Observed doing exactly that before the result walked back to the last data node.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql("q", datasource, "SELECT id, name FROM departments"),
                node("ok", "success"),
            ],
            [edge("s", "q"), edge("q", "ok")],
        )

        view = await run_graph(graph)

        assert view["result_preview"]["node_id"] == "q"
        assert view["result_preview"]["output"]["count"] == 3


class TestFailurePaths:
    async def test_a_failure_with_no_error_path_ends_the_run(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        graph = await make_graph(
            [
                node("s", "start"),
                sql("q", datasource, "SELECT * FROM ghost", tables=("ghost",)),
                node("after", "success"),
            ],
            [edge("s", "q"), edge("q", "after")],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_FAILED
        assert statuses(view)["q"] == "failed"
        assert "after" not in statuses(view), "nothing downstream may run"

    async def test_the_same_failure_is_recovered_when_an_error_path_is_drawn(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        The other half of the two-channel design. The node still records a *failed* step —
        it did fail — but the **run succeeds**, because the author said what to do about
        it. With one flag instead of two, this run would report as failed and drawing a
        recovery path would mean nothing.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql("q", datasource, "SELECT * FROM ghost", tables=("ghost",)),
                node("after", "success", message="Not reached."),
                node("recovered", "success", message="Handled it."),
            ],
            [edge("s", "q"), edge("q", "after"), edge("q", "recovered", "error")],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED
        assert statuses(view)["q"] == "failed"
        assert statuses(view)["recovered"] == "succeeded"
        assert "after" not in statuses(view)

    async def test_a_failure_node_fails_the_run_but_is_not_itself_a_failed_step(
        self, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        A Failure node is the author saying "this is a bad outcome", not a malfunction. Its
        own step succeeded — it did its job — and the *run* is what failed.
        """
        graph = await make_graph(
            [node("s", "start"), node("bad", "failure", message="Nothing matched.")],
            [edge("s", "bad")],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_FAILED
        assert view["error_message"] == "Nothing matched."
        assert statuses(view)["bad"] == "succeeded"

    async def test_a_switched_off_datasource_stops_the_node_with_a_readable_reason(
        self, db, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        datasource.is_active = False
        await db.commit()

        graph = await make_graph(
            [node("s", "start"), sql("q", datasource, "SELECT id FROM departments")],
            [edge("s", "q")],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_FAILED
        assert "switched off" in steps_by_node(view, "q")[0]["message"]

    async def test_a_table_switched_off_in_data_sources_stops_the_node(
        self, db, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        The declared table list is what makes this possible — nothing parses tables out of
        the statement — so this is the test that the list is actually honoured rather than
        merely stored.
        """
        # The stored shape is flat — table name to entry — see
        # `app/utils/datasource_status._table_entry`.
        datasource.configuration_data = {"departments": {"status": "inactive"}}
        await db.commit()

        graph = await make_graph(
            [node("s", "start"), sql("q", datasource, "SELECT id FROM departments")],
            [edge("s", "q")],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_FAILED
        assert "inactive" in steps_by_node(view, "q")[0]["message"]


class TestLoops:
    async def test_for_each_runs_its_body_once_per_item_numbering_the_passes(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        graph = await make_graph(
            [
                node("s", "start"),
                sql("q", datasource, "SELECT id FROM departments"),
                node("loop", "for_each", source_node="q", item_name="dept", max_iterations=10),
                node("body", "value", value_kind="list", value_json="[1]"),
                node("ok", "success"),
            ],
            [
                edge("s", "q"), edge("q", "loop"),
                edge("loop", "body", "body"), edge("body", "loop"),
                edge("loop", "ok", "done"),
            ],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED
        body_steps = steps_by_node(view, "body")
        assert len(body_steps) == 3, "three departments, three passes"
        assert [step["iteration"] for step in body_steps] == [0, 1, 2]

    async def test_the_ceiling_refuses_rather_than_running_part_of_the_list(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        Rows for the first two of three departments look exactly like rows for all three,
        and a total taken over them is a plausible number that is wrong. So the run stops
        and names the node instead of quietly doing less.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql("q", datasource, "SELECT id FROM departments"),
                node("loop", "for_each", label="each dept", source_node="q", max_iterations=2),
                node("body", "value", value_kind="list", value_json="[1]"),
                node("ok", "success"),
            ],
            [
                edge("s", "q"), edge("q", "loop"),
                edge("loop", "body", "body"), edge("body", "loop"),
                edge("loop", "ok", "done"),
            ],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_FAILED
        assert "would run 3 times" in view["error_message"]
        assert "each dept" in view["error_message"]
        assert steps_by_node(view, "body") == [], "no pass may run at all"

    async def test_for_each_over_an_empty_result_runs_no_passes_and_succeeds(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """Nothing to loop over is an answer, not a failure."""
        graph = await make_graph(
            [
                node("s", "start"),
                sql("q", datasource, "SELECT id FROM departments WHERE name = 'nope'"),
                node("loop", "for_each", source_node="q", max_iterations=5),
                node("body", "value", value_kind="list", value_json="[1]"),
                node("ok", "success"),
            ],
            [
                edge("s", "q"), edge("q", "loop"),
                edge("loop", "body", "body"), edge("body", "loop"),
                edge("loop", "ok", "done"),
            ],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED
        assert steps_by_node(view, "body") == []

    async def test_do_until_stops_when_its_condition_holds(
        self, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        graph = await make_graph(
            [
                node("s", "start"),
                node(
                    "loop", "do_until", max_iterations=10,
                    condition={"source_node": "loop", "field": "index",
                               "operator": "equals", "value": 2},
                ),
                node("body", "value", value_kind="list", value_json="[1]"),
                node("ok", "success"),
            ],
            [
                edge("s", "loop"),
                edge("loop", "body", "body"), edge("body", "loop"),
                edge("loop", "ok", "done"),
            ],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED
        assert len(steps_by_node(view, "loop")) == 3

    async def test_a_do_until_that_never_satisfies_is_stopped_by_its_ceiling(
        self, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        The ceiling matters most here: this is the shape that would otherwise run until
        LangGraph raised ``GraphRecursionError`` somewhere the author cannot connect to
        their drawing. Instead it stops and names the node.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                node(
                    "loop", "do_until", label="forever", max_iterations=4,
                    condition={"source_node": "loop", "field": "index",
                               "operator": "equals", "value": 9999},
                ),
                node("body", "value", value_kind="list", value_json="[1]"),
                node("ok", "success"),
            ],
            [
                edge("s", "loop"),
                edge("loop", "body", "body"), edge("body", "loop"),
                edge("loop", "ok", "done"),
            ],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_FAILED
        assert "forever" in view["error_message"]
        assert "4 passes" in view["error_message"]


class TestBranches:
    async def test_takes_the_first_matching_condition(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        graph = await make_graph(
            [
                node("s", "start"),
                sql("q", datasource, "SELECT id FROM departments"),
                node("b", "branch", conditions=[
                    {"source_node": "q", "operator": "not_empty", "port": "found"},
                ]),
                node("ok", "success", message="Found some."),
                node("none", "failure", message="Found none."),
            ],
            [
                edge("s", "q"), edge("q", "b"),
                edge("b", "ok", "found"), edge("b", "none", "else"),
            ],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED
        assert statuses(view)["ok"] == "succeeded"
        assert "none" not in statuses(view)

    async def test_takes_else_when_nothing_matches(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        graph = await make_graph(
            [
                node("s", "start"),
                sql("q", datasource, "SELECT id FROM departments WHERE name = 'nope'"),
                node("b", "branch", conditions=[
                    {"source_node": "q", "operator": "not_empty", "port": "found"},
                ]),
                node("ok", "success"),
                node("none", "failure", message="Found none."),
            ],
            [
                edge("s", "q"), edge("q", "b"),
                edge("b", "ok", "found"), edge("b", "none", "else"),
            ],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_FAILED
        assert view["error_message"] == "Found none."

    async def test_a_count_of_zero_is_not_treated_as_empty(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        ``0`` and ``False`` are real answers. Reading a count of zero as "empty" sends a
        graph down its nothing-found path when the thing it found was zero — the reason
        ``_is_empty`` exists rather than ``not value``.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql("q", datasource, "SELECT COUNT(*) AS total FROM departments WHERE name = 'nope'"),
                node("b", "branch", conditions=[
                    {"source_node": "q", "field": "total", "operator": "not_empty",
                     "port": "answered"},
                ]),
                node("ok", "success", message="Got a figure."),
                node("none", "failure", message="No figure."),
            ],
            [
                edge("s", "q"), edge("q", "b"),
                edge("b", "ok", "answered"), edge("b", "none", "else"),
            ],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED, "zero is a figure, not an absence"


class TestHumanInTheLoop:
    async def test_the_run_pauses_with_the_question_stored(
        self, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        graph = await make_graph(
            [
                node("s", "start"),
                node("ask", "human", label="Approve", prompt="Shall I continue?",
                     expects="confirm"),
                node("ok", "success"),
            ],
            [edge("s", "ask"), edge("ask", "ok")],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_AWAITING_INPUT
        assert view["interrupt_payload"]["prompt"] == "Shall I continue?"
        assert view["interrupt_payload"]["node_id"] == "ask"

    async def test_the_human_node_writes_no_step_until_it_has_an_answer(
        self, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        ``interrupt()`` re-runs its node when the graph resumes, so anything before it
        happens twice. The step is written after the pause, which is what keeps the log
        from showing the question being asked twice.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                node("ask", "human", prompt="Shall I continue?", expects="confirm"),
                node("ok", "success"),
            ],
            [edge("s", "ask"), edge("ask", "ok")],
        )

        view = await run_graph(graph)

        assert steps_by_node(view, "ask") == []

    async def test_resuming_continues_from_the_checkpoint(
        self, db, user, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        The seam this feature rests on, crossed for real: the interrupt fires inside the
        background task and the answer arrives through a different call, connected only by
        the persisted ``thread_id``.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                node("ask", "human", prompt="Shall I continue?", expects="confirm"),
                node("b", "branch", conditions=[
                    {"source_node": "ask", "operator": "equals", "value": True,
                     "port": "yes"},
                ]),
                node("ok", "success", message="Approved."),
                node("no", "failure", message="Declined."),
            ],
            [
                edge("s", "ask"), edge("ask", "b"),
                edge("b", "ok", "yes"), edge("b", "no", "else"),
            ],
        )

        paused = await run_graph(graph)
        assert paused["status"] == RUN_AWAITING_INPUT

        await graph_run_service.resume_run(
            db, user.id, uuid_pkg.UUID(paused["uuid"]), "yes",
        )

        for _ in range(200):
            await asyncio.sleep(0.05)
            view = await graph_run_service.get_run(
                db, user.id, uuid_pkg.UUID(paused["uuid"]),
            )
            if view["status"] != "running":
                break

        assert view["status"] == RUN_SUCCEEDED
        assert len(steps_by_node(view, "ask")) == 1, "asked once, answered once"
        assert statuses(view)["ok"] == "succeeded"

    async def test_answering_no_takes_the_other_path(
        self, db, user, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        graph = await make_graph(
            [
                node("s", "start"),
                node("ask", "human", prompt="Shall I continue?", expects="confirm"),
                node("b", "branch", conditions=[
                    {"source_node": "ask", "operator": "equals", "value": True,
                     "port": "yes"},
                ]),
                node("ok", "success"),
                node("no", "failure", message="Declined."),
            ],
            [
                edge("s", "ask"), edge("ask", "b"),
                edge("b", "ok", "yes"), edge("b", "no", "else"),
            ],
        )

        paused = await run_graph(graph)
        await graph_run_service.resume_run(
            db, user.id, uuid_pkg.UUID(paused["uuid"]), "no",
        )

        for _ in range(200):
            await asyncio.sleep(0.05)
            view = await graph_run_service.get_run(
                db, user.id, uuid_pkg.UUID(paused["uuid"]),
            )
            if view["status"] != "running":
                break

        assert view["status"] == RUN_FAILED
        assert view["error_message"] == "Declined."

    async def test_an_answer_that_does_not_fit_is_refused_before_the_run_resumes(
        self, db, user, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        Refused while the person is still looking at the prompt. Resuming and failing a
        node three steps later would be technically equivalent and much less useful.
        """
        from litestar.exceptions import HTTPException

        graph = await make_graph(
            [
                node("s", "start"),
                node("ask", "human", prompt="Shall I continue?", expects="confirm"),
                node("ok", "success"),
            ],
            [edge("s", "ask"), edge("ask", "ok")],
        )

        paused = await run_graph(graph)

        with pytest.raises(HTTPException) as caught:
            await graph_run_service.resume_run(
                db, user.id, uuid_pkg.UUID(paused["uuid"]), "maybe",
            )

        assert caught.value.status_code == 400
        assert "yes or no" in caught.value.detail

        still = await graph_run_service.get_run(
            db, user.id, uuid_pkg.UUID(paused["uuid"]),
        )
        assert still["status"] == RUN_AWAITING_INPUT, "the run is still waiting"

    async def test_a_choice_outside_the_offered_list_is_refused(
        self, db, user, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        from litestar.exceptions import HTTPException

        graph = await make_graph(
            [
                node("s", "start"),
                node("ask", "human", prompt="Which?", expects="choice",
                     choices=["north", "south"]),
                node("ok", "success"),
            ],
            [edge("s", "ask"), edge("ask", "ok")],
        )

        paused = await run_graph(graph)

        with pytest.raises(HTTPException) as caught:
            await graph_run_service.resume_run(
                db, user.id, uuid_pkg.UUID(paused["uuid"]), "east",
            )

        assert "north, south" in caught.value.detail

    async def test_a_run_that_is_not_waiting_cannot_be_answered(
        self, db, user, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        from litestar.exceptions import HTTPException

        graph = await make_graph(
            [
                node("s", "start"),
                sql("q", datasource, "SELECT id FROM departments"),
                node("ok", "success"),
            ],
            [edge("s", "q"), edge("q", "ok")],
        )

        finished = await run_graph(graph)

        with pytest.raises(HTTPException) as caught:
            await graph_run_service.resume_run(
                db, user.id, uuid_pkg.UUID(finished["uuid"]), "yes",
            )

        assert "not waiting" in caught.value.detail


class TestSelections:
    async def test_runs_only_the_chosen_nodes_and_records_the_rest_as_skipped(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        A skipped node gets a row because a node missing from the log is
        indistinguishable from one the run never reached — and "I only tested this one" is
        what somebody reading a selection run needs to be told.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql("q", datasource, "SELECT id FROM departments"),
                node("loop", "for_each", source_node="q", max_iterations=5),
                node("pass", "value", value_kind="list", value_json="[1]"),
                node("ok", "success"),
            ],
            [
                edge("s", "q"), edge("q", "loop"), edge("loop", "pass", "body"),
                edge("pass", "loop"), edge("loop", "ok", "done"),
            ],
        )

        view = await run_graph(graph, scope="selection", node_ids=["q"])

        assert view["status"] == RUN_SUCCEEDED
        assert statuses(view)["q"] == "succeeded"
        assert statuses(view)["s"] == "skipped"
        assert statuses(view)["loop"] == "skipped"
        assert statuses(view)["ok"] == "skipped"

    async def test_a_chosen_node_reading_an_omitted_one_fails_naming_it(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        The test that stops a selection run being quietly meaningless: a ``for_each`` whose
        source was left out would read ``None``, loop zero times and report success — a
        green tick on a test that ran nothing.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql("q", datasource, "SELECT id FROM departments"),
                node("loop", "for_each", label="each dept", source_node="q", max_iterations=5),
                node("pass", "value", value_kind="list", value_json="[1]"),
                node("ok", "success"),
            ],
            [
                edge("s", "q"), edge("q", "loop"), edge("loop", "pass", "body"),
                edge("pass", "loop"), edge("loop", "ok", "done"),
            ],
        )

        view = await run_graph(graph, scope="selection", node_ids=["loop"])

        assert view["status"] == RUN_FAILED
        assert "each dept" in view["error_message"]
        assert "not part of this test" in view["error_message"]
        assert statuses(view)["loop"] == "failed", "the failure is in the log, not only on the run"

    async def test_two_unconnected_nodes_are_both_run(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        Choosing nodes that are not joined is an ordinary way to ask "do these two work",
        so the disconnected pieces are chained rather than one of them being dropped.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql("a", datasource, "SELECT id FROM departments"),
                node("mid", "value", value_kind="list", value_json="[1]"),
                sql("b", datasource, "SELECT name FROM departments"),
                node("ok", "success"),
            ],
            [
                edge("s", "a"), edge("a", "mid"), edge("mid", "b"), edge("b", "ok"),
            ],
        )

        view = await run_graph(graph, scope="selection", node_ids=["a", "b"])

        assert view["status"] == RUN_SUCCEEDED
        assert statuses(view)["a"] == "succeeded"
        assert statuses(view)["b"] == "succeeded"
        assert statuses(view)["mid"] == "skipped"

    async def test_a_selection_naming_nothing_real_is_refused(
        self, db, user, make_graph,
    ) -> None:  # noqa: ANN001
        """
        Refused rather than widened. "Run these three" and "run everything" must never be
        the same request.
        """
        from litestar.exceptions import HTTPException

        graph = await make_graph(
            [node("s", "start"), node("ok", "success")], [edge("s", "ok")],
        )

        with pytest.raises(HTTPException) as caught:
            await graph_run_service.start_run(
                db, user.id, graph.uuid, scope="selection", node_ids=["ghost"],
            )

        assert "no longer in this graph" in caught.value.detail

    async def test_an_empty_selection_is_refused(self, db, user, make_graph) -> None:  # noqa: ANN001
        from litestar.exceptions import HTTPException

        graph = await make_graph(
            [node("s", "start"), node("ok", "success")], [edge("s", "ok")],
        )

        with pytest.raises(HTTPException) as caught:
            await graph_run_service.start_run(
                db, user.id, graph.uuid, scope="selection", node_ids=[],
            )

        assert "at least one node" in caught.value.detail


class TestParameters:
    async def test_a_declared_parameter_is_bound_from_the_runs_inputs(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        Bound as a parameter, never concatenated — the statement is the operator's text and
        the value travels separately.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql(
                    "q", datasource,
                    "SELECT id, name FROM departments WHERE name = :wanted",
                    params=[{"param": "wanted", "type": "text", "required": True}],
                ),
                node("ok", "success"),
            ],
            [edge("s", "q"), edge("q", "ok")],
        )

        view = await run_graph(graph, inputs={"wanted": "Sales"})

        assert view["status"] == RUN_SUCCEEDED
        assert steps_by_node(view, "q")[0]["output_preview"]["count"] == 1

    async def test_a_value_shaped_like_an_injection_matches_nothing(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        Compared as a value, not spliced into the statement. The same guarantee
        ``test_query_executor`` asserts for a nested tool's values, re-asserted here because
        a graph is a new way to reach the same executor.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql(
                    "q", datasource,
                    "SELECT id FROM departments WHERE name = :wanted",
                    params=[{"param": "wanted", "type": "text", "required": True}],
                ),
                node("ok", "success"),
            ],
            [edge("s", "q"), edge("q", "ok")],
        )

        view = await run_graph(graph, inputs={"wanted": "') OR ('1'='1"})

        assert view["status"] == RUN_SUCCEEDED
        assert steps_by_node(view, "q")[0]["output_preview"]["count"] == 0


class TestTheLoopItemFillsAParameter:
    """
    The case this feature was built for, and the one the user actually hit: a statement
    inside a loop body reading the item the loop is on.

    Asserted through the *rows*, never through the SQL — the statement is bound, not
    rewritten, so what proves the item arrived is that each pass returned that
    department's staff and nobody else's.
    """

    async def test_a_parameter_named_after_the_item_is_filled_with_no_wiring(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        graph = await make_graph(
            [
                node("s", "start"),
                sql("depts", datasource, "SELECT id FROM departments ORDER BY id"),
                node(
                    "loop", "for_each",
                    source_node="depts", item_name="dept_id", max_iterations=10,
                ),
                sql(
                    "staff", datasource,
                    "SELECT name FROM staff WHERE dept_id = :dept_id ORDER BY id",
                    tables=("staff",),
                    params=[{"param": "dept_id", "type": "number", "required": True}],
                ),
                node("ok", "success"),
            ],
            [
                edge("s", "depts"), edge("depts", "loop"),
                edge("loop", "staff", "body"), edge("staff", "loop"),
                edge("loop", "ok", "done"),
            ],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED

        passes = steps_by_node(view, "staff")
        assert len(passes) == 3

        # 2, 1, 3 — the real distribution. Equal counts would pass even if the same
        # department were queried three times.
        assert [step["output_preview"]["count"] for step in passes] == [2, 1, 3]

    async def test_a_wiring_wins_over_the_item_of_the_same_name(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        The author drew that line about this parameter, so it is the more specific
        statement. Wired to a literal 3, every pass reads department 3 — which is visible
        because department 3 has a different number of staff from the others.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql("depts", datasource, "SELECT id FROM departments ORDER BY id"),
                node("three", "value", value_kind="list", value_json="[3]"),
                node(
                    "loop", "for_each",
                    source_node="depts", item_name="dept_id", max_iterations=10,
                ),
                sql(
                    "staff", datasource,
                    "SELECT name FROM staff WHERE dept_id = :dept_id ORDER BY id",
                    tables=("staff",),
                    params=[{"param": "dept_id", "type": "number", "required": True}],
                    bindings={"dept_id": {"node": "three", "mode": "one"}},
                ),
                node("ok", "success"),
            ],
            [
                edge("s", "three"), edge("three", "depts"), edge("depts", "loop"),
                edge("loop", "staff", "body"), edge("staff", "loop"),
                edge("loop", "ok", "done"),
            ],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED
        counts = [step["output_preview"]["count"] for step in steps_by_node(view, "staff")]
        assert counts == [3, 3, 3], "the wiring, not the item, on every pass"

    async def test_a_field_names_which_column_of_a_row_to_bind(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        A loop over multi-column rows has no single value per item, so the parameter is
        wired to the loop and told which column to read.

        Note the field is ``id`` — a column of the *item* — not the item's name. Wiring a
        parameter to a loop means the item it is on, so the envelope is unwrapped before
        the field is applied; binding ``{item, index, total}`` whole is never what anybody
        putting a value into a statement meant.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql("depts", datasource, "SELECT id, name FROM departments ORDER BY id"),
                node(
                    "loop", "for_each",
                    source_node="depts", item_name="dept", max_iterations=10,
                ),
                sql(
                    "staff", datasource,
                    "SELECT name FROM staff WHERE dept_id = :dept_id ORDER BY id",
                    tables=("staff",),
                    params=[{"param": "dept_id", "type": "number", "required": True}],
                    bindings={"dept_id": {"node": "loop", "field": "id", "mode": "one"}},
                ),
                node("ok", "success"),
            ],
            [
                edge("s", "depts"), edge("depts", "loop"),
                edge("loop", "staff", "body"), edge("staff", "loop"),
                edge("loop", "ok", "done"),
            ],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED, view.get("error_message")
        counts = [step["output_preview"]["count"] for step in steps_by_node(view, "staff")]
        assert counts == [2, 1, 3]

    async def test_a_multi_column_item_is_refused_rather_than_guessed_at(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        Binding an arbitrary column would filter the statement on the wrong thing and the
        result would look entirely normal. So it stops, names the loop, and says what to
        do instead.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql("depts", datasource, "SELECT id, name FROM departments ORDER BY id"),
                node(
                    "loop", "for_each", label="each dept",
                    source_node="depts", item_name="dept_id", max_iterations=10,
                ),
                sql(
                    "staff", datasource,
                    "SELECT name FROM staff WHERE dept_id = :dept_id",
                    tables=("staff",),
                    params=[{"param": "dept_id", "type": "number", "required": True}],
                ),
                node("ok", "success"),
            ],
            [
                edge("s", "depts"), edge("depts", "loop"),
                edge("loop", "staff", "body"), edge("staff", "loop"),
                edge("loop", "ok", "done"),
            ],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_FAILED
        assert "each dept" in view["error_message"]
        assert "2 columns" in view["error_message"]
        assert "name the column" in view["error_message"].lower()

    async def test_a_field_the_item_has_not_got_names_the_field_and_lists_the_real_ones(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        The setup a user actually arrived at: the loop's item was renamed, and the binding's
        *field* kept the old name. ``_field_of`` answers ``None``, the parameter is dropped,
        and what used to come back was ``query_executor``'s "this tool needs a value for
        'dept_id' and none was given" — a sentence about an input nobody supplied, when a
        line had been drawn and was reading the wrong key.

        Worse for an optional parameter: the filter would leave the statement and the run
        would succeed over every row. So it stops, and says which field and which fields
        there are.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql("depts", datasource, "SELECT id FROM departments ORDER BY id"),
                node(
                    "loop", "for_each", label="each dept",
                    source_node="depts", item_name="dept_id", max_iterations=10,
                ),
                sql(
                    "staff", datasource,
                    "SELECT name FROM staff WHERE dept_id = :dept_id",
                    tables=("staff",),
                    params=[{"param": "dept_id", "type": "number", "required": True}],
                    bindings={"dept_id": {"node": "loop", "field": "item", "mode": "one"}},
                ),
                node("ok", "success"),
            ],
            [
                edge("s", "depts"), edge("depts", "loop"),
                edge("loop", "staff", "body"), edge("staff", "loop"),
                edge("loop", "ok", "done"),
            ],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_FAILED
        assert "'item'" in view["error_message"]
        assert "each dept" in view["error_message"]
        assert "'id'" in view["error_message"], "the fields it does have are listed"

    async def test_a_field_on_a_source_that_is_one_value_says_so(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """A field names a column, and a plain value has none — so the advice is to clear
        the box rather than to find the right name."""
        graph = await make_graph(
            [
                node("s", "start"),
                node("three", "value", value_kind="list", value_json="[3]"),
                sql(
                    "staff", datasource,
                    "SELECT name FROM staff WHERE dept_id = :dept_id",
                    tables=("staff",),
                    params=[{"param": "dept_id", "type": "number", "required": True}],
                    bindings={"dept_id": {"node": "three", "field": "id", "mode": "one"}},
                ),
                node("ok", "success"),
            ],
            [edge("s", "three"), edge("three", "staff"), edge("staff", "ok")],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_FAILED
        assert "single value rather than rows" in view["error_message"]

    async def test_a_binding_stored_as_a_bare_node_id_still_works(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        The shape graphs were saved with before a binding could carry a field or a mode.
        Asserted directly: a silent change of meaning in a stored graph is the worst
        outcome available, and there are saved graphs using it.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                node("three", "value", value_kind="list", value_json="[3]"),
                sql(
                    "staff", datasource,
                    "SELECT name FROM staff WHERE dept_id = :dept_id ORDER BY id",
                    tables=("staff",),
                    params=[{"param": "dept_id", "type": "number", "required": True}],
                    bindings={"dept_id": "three"},
                ),
                node("ok", "success"),
            ],
            [edge("s", "three"), edge("three", "staff"), edge("staff", "ok")],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED, view.get("error_message")
        assert steps_by_node(view, "staff")[0]["output_preview"]["count"] == 3


class TestBindingAListInsteadOfALoop:
    async def test_in_list_mode_matches_every_value_in_one_query(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        The alternative to a loop: one statement, one round trip, every department's staff.
        One step rather than three is half the assertion — the other half is that all six
        rows came back.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql("depts", datasource, "SELECT id FROM departments"),
                sql(
                    "staff", datasource,
                    "SELECT name FROM staff WHERE dept_id IN :dept_ids",
                    tables=("staff",),
                    params=[{"param": "dept_ids", "type": "number", "required": True}],
                    bindings={"dept_ids": {"node": "depts", "mode": "in_list"}},
                ),
                node("ok", "success"),
            ],
            [edge("s", "depts"), edge("depts", "staff"), edge("staff", "ok")],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED, view.get("error_message")

        runs = steps_by_node(view, "staff")
        assert len(runs) == 1, "one query, not one per department"
        assert runs[0]["output_preview"]["count"] == 6

    async def test_an_empty_list_is_refused_rather_than_bound(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        ``IN ()`` is a syntax error in most dialects and an always-false filter in the
        rest. An empty result reading as "nothing matched" would hide that the filter was
        never built.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql("none", datasource, "SELECT id FROM departments WHERE id = 999"),
                sql(
                    "staff", datasource,
                    "SELECT name FROM staff WHERE dept_id IN :dept_ids",
                    tables=("staff",),
                    params=[{"param": "dept_ids", "type": "number", "required": True}],
                    bindings={"dept_ids": {"node": "none", "mode": "in_list"}},
                ),
                node("ok", "success"),
            ],
            [edge("s", "none"), edge("none", "staff"), edge("staff", "ok")],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_FAILED
        assert "produced none" in view["error_message"]


class TestUnioningThePasses:
    def _graph(self, datasource, **loop_data):
        return (
            [
                node("s", "start"),
                sql("depts", datasource, "SELECT id FROM departments ORDER BY id"),
                node(
                    "loop", "for_each", label="each dept",
                    source_node="depts", item_name="dept_id", max_iterations=10,
                    **loop_data,
                ),
                sql(
                    "staff", datasource,
                    "SELECT name FROM staff WHERE dept_id = :dept_id ORDER BY id",
                    tables=("staff",),
                    params=[{"param": "dept_id", "type": "number", "required": True}],
                ),
                node("ok", "success"),
            ],
            [
                edge("s", "depts"), edge("depts", "loop"),
                edge("loop", "staff", "body"), edge("staff", "loop"),
                edge("loop", "ok", "done"),
            ],
        )

    async def test_the_loops_result_is_every_passs_rows(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        Six staff over three departments. Without collection the loop's output is the last
        pass's three rows, and the difference between 6 and 3 is the whole feature.
        """
        nodes, edges = self._graph(datasource, collect_from="staff")
        graph = await make_graph(nodes, edges)

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED, view.get("error_message")

        final = steps_by_node(view, "loop")[-1]
        assert final["output_preview"]["count"] == 6
        assert [row["name"] for row in final["output_preview"]["rows"]] == [
            "Ann", "Bob", "Cid", "Dee", "Eve", "Fay",
        ], "in the order the departments were walked"

    async def test_a_loop_that_collects_nothing_still_reports_the_item(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        The default, and every graph saved before this existed. The loop's output stays the
        item envelope, so nothing that read it changes meaning.
        """
        nodes, edges = self._graph(datasource)
        graph = await make_graph(nodes, edges)

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED
        final = steps_by_node(view, "loop")[-1]
        assert final["output_preview"]["kind"] == "dict"
        assert "dept_id" in final["output_preview"]["entries"]

    async def test_each_row_can_record_the_item_that_produced_it(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        A statement that filters on a department without selecting it is ordinary SQL, and
        its rows are indistinguishable once concatenated. Two rows for department 1, one
        for 2, three for 3.
        """
        nodes, edges = self._graph(
            datasource, collect_from="staff", label_item_as="dept_id",
        )
        graph = await make_graph(nodes, edges)

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED, view.get("error_message")

        rows = steps_by_node(view, "loop")[-1]["output_preview"]["rows"]
        assert [row["dept_id"] for row in rows] == [1, 1, 2, 3, 3, 3]

    async def test_a_label_colliding_with_a_real_column_is_refused(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        Overwriting would replace a value from the database with one from the loop;
        skipping would leave rows whose label says nothing about them. Both look right and
        are not, so ``labelled_rows`` refuses and the loop reports it.
        """
        nodes, edges = self._graph(
            datasource, collect_from="staff", label_item_as="name",
        )
        graph = await make_graph(nodes, edges)

        view = await run_graph(graph)

        assert view["status"] == RUN_FAILED
        assert "each dept" in view["error_message"]
        assert "name" in view["error_message"]

    async def test_a_union_past_the_old_tool_cap_comes_back_whole(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        A graph is a pipeline moving its own rows, so nothing here stops at 200.

        81 rows a pass over three passes — 243, comfortably past the ``MAX_TOOL_ROWS`` that
        used to bound both the body's query and the union it fed. Neither is capped now, and
        the number that proves it is the exact total: a truncated union and a complete one
        are indistinguishable from a row count alone, which is why the assertion is `== 243`
        and not `> 200`.
        """
        nodes, edges = self._graph(datasource, collect_from="staff")
        nodes[3] = sql(
            "staff", datasource,
            "SELECT a.id FROM departments a, departments b, departments c, departments d "
            "WHERE a.id = a.id AND :dept_id = :dept_id",
            params=[{"param": "dept_id", "type": "number", "required": True}],
        )
        graph = await make_graph(nodes, edges)

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED, view.get("error_message")

        per_pass = [step["output_preview"]["count"] for step in steps_by_node(view, "staff")]
        assert per_pass == [81, 81, 81], "the body's own query is uncapped too"

        assert steps_by_node(view, "loop")[-1]["output_preview"]["count"] == 243


class TestBuildingOneStatementInsteadOfMany:
    """
    The union node: one copy of a statement per pass, joined, and run once at the end.

    Everything here is asserted through the **rows**, never by reading the SQL the node
    built. That is deliberate and it is the point: a test that checked the text would pass
    just as well if the values had been concatenated into it, which is the one thing this
    node must never do. The uneven 2/1/3 staff split is what makes the rows load-bearing —
    six rows can only come back if all three department ids were bound, and ``UNION``
    removes duplicates, so a fragment bound to the same value three times would return two.
    """

    def _union(self, node_id, datasource, statement, **extra) -> dict:  # noqa: ANN001
        return node(
            node_id, "sql_union",
            label=node_id,
            datasource_id=str(datasource.uuid),
            table_names=["staff"],
            sql_query=statement,
            **extra,
        )

    def _graph(self, datasource, statement=None, **loop_data):  # noqa: ANN001
        """`depts → loop → union → back`, with `execute` leaving for Success."""
        return (
            [
                node("s", "start"),
                sql("depts", datasource, "SELECT id FROM departments ORDER BY id"),
                node(
                    "loop", "for_each", label="each dept",
                    source_node="depts", item_name="dept_id", max_iterations=10,
                    **loop_data,
                ),
                self._union(
                    "rows", datasource,
                    statement or "SELECT name FROM staff WHERE dept_id = :dept_id",
                    params=[{"param": "dept_id", "type": "number", "required": True}],
                ),
                node("ok", "success"),
                node("empty", "success", message="nothing to union"),
            ],
            [
                edge("s", "depts"), edge("depts", "loop"),
                edge("loop", "rows", "body"), edge("rows", "loop"),
                edge("rows", "ok", "execute"),
                edge("loop", "empty", "done"),
            ],
        )

    async def test_one_query_returns_every_passs_rows(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        graph = await make_graph(*self._graph(datasource))

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED, view.get("error_message")

        steps = steps_by_node(view, "rows")
        assert len(steps) == 3, "one visit per department"
        assert steps[-1]["output_preview"]["count"] == 6, (
            "six staff across three departments, so every id was bound separately"
        )

    async def test_the_unions_rows_are_the_runs_result(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        A union node counts as a data-producing node, so what it ran is what the run reports.

        Worth its own test because getting this wrong is silent in the worst way: with
        ``sql_union`` missing from ``_DATA_NODE_TYPES`` the result walked back to the
        *previous* SQL node, so a graph that unioned six staff rows reported the three
        departments it looped over — a plausible number belonging to a different question.
        """
        graph = await make_graph(*self._graph(datasource))

        view = await run_graph(graph)

        assert view["result_preview"]["node_id"] == "rows"
        assert view["result_preview"]["output"]["count"] == 6

    async def test_a_value_shaped_like_an_injection_matches_nothing(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        The guarantee this node has to keep, and the only test that can prove it.

        Every other test here would pass just as well if each pass's value had been
        *concatenated* into the fragment — which is the three-lines-shorter implementation and
        the one that would make every looped statement an injection site. So one pass is given
        ``') OR ('1'='1``. Written into the text it opens the filter and all six staff come
        back; bound as a value it is a name nobody has, and the answer is nothing.

        The same assertion ``TestParameters`` makes about a plain SQL node, re-made here
        because composing a statement is a new way to get it wrong.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                node(
                    "names", "value", value_kind="list",
                    value_json='["\') OR (\'1\'=\'1", "also nobody"]',
                ),
                node(
                    "loop", "for_each", label="each name",
                    source_node="names", item_name="wanted", max_iterations=10,
                ),
                self._union(
                    "rows", datasource,
                    "SELECT name FROM staff WHERE name = :wanted",
                    params=[{"param": "wanted", "type": "text", "required": True}],
                ),
                node("ok", "success"),
                node("empty", "success", message="nothing to union"),
            ],
            [
                edge("s", "names"), edge("names", "loop"),
                edge("loop", "rows", "body"), edge("rows", "loop"),
                edge("rows", "ok", "execute"),
                edge("loop", "empty", "done"),
            ],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED, view.get("error_message")
        assert steps_by_node(view, "rows")[-1]["output_preview"]["count"] == 0

    async def test_only_the_last_visit_runs_anything(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        The property the whole node exists for. The first two visits add to the statement and
        say so; the third is the only one that goes to the database.
        """
        graph = await make_graph(*self._graph(datasource))

        view = await run_graph(graph)
        steps = steps_by_node(view, "rows")

        assert [step["message"] for step in steps[:2]] == [
            "Added pass 1 of 3 to the union.",
            "Added pass 2 of 3 to the union.",
        ]
        assert "Ran one query built from 3 pass(es)" in steps[-1]["message"]

    async def test_the_statement_under_construction_is_what_the_node_shows(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        While it accumulates, the node's output *is* the statement being written, which is
        where the author watches it — "the union node will only create the complete sql
        query" is the requirement this asserts. On the executing visit that output is
        replaced by the rows, so the node after ``execute`` reads rows like any other.
        """
        graph = await make_graph(*self._graph(datasource))

        view = await run_graph(graph)
        steps = steps_by_node(view, "rows")

        building = steps[0]["output_preview"]
        assert building["kind"] == "dict"
        assert "SELECT name FROM staff" in building["entries"]["sql"]
        assert building["entries"]["passes"] == 1

        assert steps[1]["output_preview"]["entries"]["passes"] == 2, "one more each pass"

        assert steps[-1]["output_preview"]["kind"] == "rows", (
            "the executing visit publishes rows, not the builder"
        )

    async def test_the_run_leaves_by_execute_and_not_by_done(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        Which port was taken, asserted by which Success node the run reached. ``done`` is
        left for the empty-list case, so reaching it here would mean the union never ran.
        """
        graph = await make_graph(*self._graph(datasource))

        view = await run_graph(graph)

        assert statuses(view).get("ok") == "succeeded"
        assert "empty" not in statuses(view)

    async def test_an_empty_list_builds_nothing_and_runs_nothing(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        No passes, so no fragment and no query — and the loop leaves by ``done``, which is
        the reason that port still has to be drawn.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                sql("depts", datasource, "SELECT id FROM departments WHERE id < 0"),
                node(
                    "loop", "for_each", label="each dept",
                    source_node="depts", item_name="dept_id", max_iterations=10,
                ),
                self._union(
                    "rows", datasource,
                    "SELECT name FROM staff WHERE dept_id = :dept_id",
                    params=[{"param": "dept_id", "type": "number", "required": True}],
                ),
                node("ok", "success"),
                node("empty", "success", message="nothing to union"),
            ],
            [
                edge("s", "depts"), edge("depts", "loop"),
                edge("loop", "rows", "body"), edge("rows", "loop"),
                edge("rows", "ok", "execute"),
                edge("loop", "empty", "done"),
            ],
        )

        view = await run_graph(graph)

        assert view["status"] == RUN_SUCCEEDED, view.get("error_message")
        assert statuses(view).get("empty") == "succeeded"
        assert steps_by_node(view, "rows") == [], "the body never ran"

    async def test_a_ceiling_below_the_item_count_runs_no_partial_union(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        The most important test here. A union of two departments out of three is a plausible
        number that is wrong, and there is nothing in the result to say so — so the run must
        stop rather than execute what it has.
        """
        nodes, edges = self._graph(datasource)
        nodes[2]["data"]["max_iterations"] = 2

        graph = await make_graph(nodes, edges)

        view = await run_graph(graph)

        assert view["status"] == RUN_FAILED
        assert "each dept" in view["error_message"]
        assert not [
            step for step in steps_by_node(view, "rows")
            if "Ran one query" in (step["message"] or "")
        ], "nothing was executed"

    async def test_a_statement_longer_than_the_ceiling_is_refused(
        self, datasource, make_graph, run_graph, monkeypatch,
    ) -> None:  # noqa: ANN001
        """
        Named rather than truncated, and pointed at the mechanism that has no text budget —
        a union short of its last passes is short of whole *departments*, which "200 rows"
        does not say.
        """
        from app.utils import sql_guard

        monkeypatch.setattr(sql_guard, "MAX_BUILT_SQL_LENGTH", 80)

        graph = await make_graph(*self._graph(datasource))

        view = await run_graph(graph)

        assert view["status"] == RUN_FAILED
        assert "each dept" in view["error_message"]
        assert "collect" in view["error_message"]

    async def test_a_failed_query_takes_the_error_path_not_execute(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        The router asks about the failure before it asks about the union, so a query that did
        not run cannot leave by the port that means "here are the rows".
        """
        nodes, edges = self._graph(
            datasource, statement="SELECT nope FROM staff WHERE dept_id = :dept_id",
        )
        nodes.append(node("bad", "failure", message="the union would not run"))
        edges.append(edge("rows", "bad", "error"))

        graph = await make_graph(nodes, edges)

        view = await run_graph(graph)

        assert statuses(view).get("bad") == "succeeded", "the error path was taken"
        assert "ok" not in statuses(view), "and execute was not"

    async def test_a_union_node_outside_a_loop_is_refused_before_it_runs(
        self, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """
        Refused by ``validate_graph``, which ``start_run`` calls — so a graph stored before
        the rule existed cannot run either, rather than building a statement and dropping it.
        """
        from litestar.exceptions import HTTPException

        graph = await make_graph(
            [
                node("s", "start"),
                self._union(
                    "rows", datasource, "SELECT name FROM staff WHERE dept_id = :dept_id",
                    params=[{"param": "dept_id", "type": "number", "required": True}],
                ),
                node("ok", "success"),
            ],
            [edge("s", "rows"), edge("rows", "ok")],
        )

        with pytest.raises(HTTPException) as caught:
            await run_graph(graph)

        assert "inside a For each" in caught.value.detail


class TestCancelling:
    async def test_a_cancelled_run_keeps_its_log(
        self, db, user, make_graph,
    ) -> None:  # noqa: ANN001
        """
        A cancelled run's log is the most useful thing about it: it says how far it got.
        """
        graph = await make_graph(
            [
                node("s", "start"),
                node("ask", "human", prompt="Waiting?", expects="confirm"),
                node("ok", "success"),
            ],
            [edge("s", "ask"), edge("ask", "ok")],
        )

        run_uuid = await graph_run_service.start_run(db, user.id, graph.uuid)

        for _ in range(200):
            await asyncio.sleep(0.05)
            view = await graph_run_service.get_run(db, user.id, uuid_pkg.UUID(run_uuid))
            if view["status"] == RUN_AWAITING_INPUT:
                break

        cancelled = await graph_run_service.cancel_run(
            db, user.id, uuid_pkg.UUID(run_uuid),
        )

        assert cancelled["status"] == "cancelled"
        assert len(cancelled["steps"]) >= 1

    async def test_cancelling_a_finished_run_is_not_an_error(
        self, db, user, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        """Pressing Stop on a run that has just finished is an ordinary race."""
        graph = await make_graph(
            [
                node("s", "start"),
                sql("q", datasource, "SELECT id FROM departments"),
                node("ok", "success"),
            ],
            [edge("s", "q"), edge("q", "ok")],
        )

        finished = await run_graph(graph)

        again = await graph_run_service.cancel_run(
            db, user.id, uuid_pkg.UUID(finished["uuid"]),
        )

        assert again["status"] == RUN_SUCCEEDED, "a finished run is not retroactively cancelled"


class TestOwnership:
    async def test_another_users_run_is_not_found(
        self, db, user, make_user, datasource, make_graph, run_graph,
    ) -> None:  # noqa: ANN001
        from litestar.exceptions import HTTPException

        graph = await make_graph(
            [node("s", "start"), node("ok", "success")], [edge("s", "ok")],
        )
        view = await run_graph(graph)

        intruder = await make_user("intruder@example.com")

        with pytest.raises(HTTPException) as caught:
            await graph_run_service.get_run(
                db, intruder.id, uuid_pkg.UUID(view["uuid"]),
            )

        assert caught.value.status_code == 404
