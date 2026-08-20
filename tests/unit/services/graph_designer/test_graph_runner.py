"""
Tests for app/services/graph_designer/graph_runner.py — running a published graph for
somebody, and classifying what came of it.

This module exists because four owners now want the same three answers: a data agent calling
a graph as a tool, a tool config embedding one, a flow node running one, and a workspace
sharing one. Only the wording differs between them, so the classifying happens once here and
these tests are what hold that contract still while the other owners are built on it.

The property that carries the suite: **a pause is an outcome, not an error.** Every owner has
to be able to carry "it stopped to ask something" — the agent relays the question, a flow ends
its turn, an embedding tool returns the question instead of rows — and none of them can treat
it as a failure, because nothing failed, or ignore it, because the rows do not exist yet.

Against a real SQLite datasource and the real run service, like its neighbours: whether a
paused run is actually resumable from a different session is the interesting question, and a
mock would only prove this module calls what it calls.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

pytest.importorskip("langgraph", reason="LangGraph is installed in the container only")

from app.models.datasource import DataSource  # noqa: E402
from app.services.graph_designer import graph_runner, graph_service  # noqa: E402


@pytest.fixture
async def datasource(db, user, tmp_path):  # noqa: ANN001, ANN201
    import sqlite3

    path = tmp_path / "runner.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE departments (id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO departments VALUES (1, 'Eng'), (2, 'Sales'), (3, 'Ops');
        """
    )
    connection.commit()
    connection.close()

    row = DataSource(
        user_id=user.id,
        datasource_name=f"runner-{uuid_pkg.uuid4().hex[:6]}",
        db_type="sqlite",
        database_name=str(path),
        is_active=True,
        password_encrypted="",
        configuration_data={},
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _nodes(datasource, *, asks: bool, sql: str = "SELECT id, name FROM departments"):  # noqa: ANN001
    nodes = [
        {"id": "s", "type": "start", "position": {}, "data": {"label": "Start"}},
        {
            "id": "q", "type": "sql", "position": {},
            "data": {
                "label": "departments",
                "datasource_id": str(datasource.uuid),
                "table_names": ["departments"],
                "sql_query": sql,
            },
        },
        {"id": "ok", "type": "success", "position": {}, "data": {"label": "Done"}},
    ]
    edges = [{"id": "e1", "source": "s", "source_port": "default", "target": "q"}]

    if asks:
        nodes.append({
            "id": "ask", "type": "human", "position": {},
            "data": {
                "label": "Confirm", "prompt": "Shall I include archived ones?",
                "expects": "confirm",
            },
        })
        edges += [
            {"id": "e2", "source": "q", "source_port": "default", "target": "ask"},
            {"id": "e3", "source": "ask", "source_port": "default", "target": "ok"},
        ]
    else:
        edges.append({"id": "e2", "source": "q", "source_port": "default", "target": "ok"})

    return {"nodes": nodes, "edges": edges}


@pytest.fixture
def published(db, user):  # noqa: ANN001, ANN201
    """A saved, published graph — the state in which anything may run one."""
    async def _publish(data: dict):  # noqa: ANN202
        graph = await graph_service.create_graph(
            db, user.id, f"runner {uuid_pkg.uuid4().hex[:6]}", "For the runner tests.",
        )
        await graph_service.save_graph(db, user.id, graph.uuid, data)
        await graph_service.set_graph_active(db, user.id, graph.uuid, True)
        return graph

    return _publish


class TestAFinishedRun:
    async def test_reports_the_rows_and_the_real_total(
        self, db, user, datasource, published, background_sessions,
    ) -> None:  # noqa: ANN001
        graph = await published(_nodes(datasource, asks=False))

        outcome = await graph_runner.run_graph(user.id, str(graph.uuid))

        assert outcome.finished
        assert outcome.kind == graph_runner.OUTCOME_FINISHED
        assert outcome.total_rows == 3
        assert {row["name"] for row in outcome.rows} == {"Eng", "Sales", "Ops"}

    async def test_the_rows_come_from_the_last_data_node_not_the_success_node(
        self, db, user, datasource, published, background_sessions,
    ) -> None:  # noqa: ANN001
        """
        A Success node's output is ``{"succeeded": true}``, so "the last output" would report
        a graph that read three rows as having returned nothing. Asserted here as well as in
        the compiler's suite because every new owner reads its result through this property.
        """
        graph = await published(_nodes(datasource, asks=False))

        outcome = await graph_runner.run_graph(user.id, str(graph.uuid))

        assert outcome.rows, "the SQL node's rows, not the Success node's flag"

    async def test_total_rows_is_not_the_length_of_the_sample(
        self, db, user, datasource, published, background_sessions,
    ) -> None:
        """
        The two numbers differ whenever a result was larger than the preview, and confusing
        them is how a sample is reported as a total. 81 rows, 20 in the sample.
        """
        graph = await published(_nodes(
            datasource, asks=False,
            sql=(
                "SELECT a.id AS a, b.id AS b, c.id AS c, d.id AS d "
                "FROM departments a, departments b, departments c, departments d"
            ),
        ))

        outcome = await graph_runner.run_graph(user.id, str(graph.uuid))

        assert outcome.total_rows == 81
        assert len(outcome.rows) == 20


class TestAPauseIsAnOutcome:
    """The property the next three owners are built on."""

    async def test_a_paused_run_is_a_question_with_a_resumable_id(
        self, db, user, datasource, published, background_sessions,
    ) -> None:  # noqa: ANN001
        graph = await published(_nodes(datasource, asks=True))

        outcome = await graph_runner.run_graph(user.id, str(graph.uuid))

        assert outcome.asks
        assert outcome.kind == graph_runner.OUTCOME_QUESTION
        assert outcome.question["prompt"] == "Shall I include archived ones?"
        assert outcome.run_id

    async def test_a_pause_is_not_reported_as_a_failure(
        self, db, user, datasource, published, background_sessions,
    ) -> None:  # noqa: ANN001
        """
        Stated as its own test because it is the mistake each owner would otherwise make
        independently: nothing failed, so a failure would tell a visitor the tool is broken
        when it is waiting for them.
        """
        graph = await published(_nodes(datasource, asks=True))

        outcome = await graph_runner.run_graph(user.id, str(graph.uuid))

        assert outcome.kind != graph_runner.OUTCOME_FAILED
        assert not outcome.reason

    async def test_answering_finishes_the_run_from_a_different_call(
        self, db, user, datasource, published, background_sessions,
    ) -> None:  # noqa: ANN001
        """
        The interrupt fires in one call and the answer arrives in another, so the pause is
        parked on a persisted thread rather than in memory. This is the test that proves it.
        """
        graph = await published(_nodes(datasource, asks=True))

        asked = await graph_runner.run_graph(user.id, str(graph.uuid))
        answered = await graph_runner.answer_graph_run(user.id, asked.run_id, "yes")

        assert answered.finished, answered.reason
        assert answered.total_rows == 3

    async def test_an_answer_that_does_not_fit_leaves_the_question_waiting(
        self, db, user, datasource, published, background_sessions,
    ) -> None:  # noqa: ANN001
        """
        "maybe" to a yes/no is ordinary input, not a fault — so it comes back as a question
        with the validator's sentence, and the same run is still there to answer. Reported as
        a failure, every owner would tell somebody the thing is broken when they only need to
        answer again.
        """
        graph = await published(_nodes(datasource, asks=True))

        asked = await graph_runner.run_graph(user.id, str(graph.uuid))
        rejected = await graph_runner.answer_graph_run(user.id, asked.run_id, "maybe")

        assert rejected.asks
        assert rejected.reason
        assert rejected.run_id == asked.run_id

        # And the run really is still answerable afterwards.
        answered = await graph_runner.answer_graph_run(user.id, asked.run_id, "yes")
        assert answered.finished, answered.reason


class TestFailuresAreReturnedNeverRaised:
    """
    Every owner is mid-something when it calls this — a conversation turn, a parent tool's
    query, a flow — so raising would hand somebody a 500 for a state that could have been
    explained. The one thing this module owes a caller is an answer.
    """

    async def test_a_graph_that_is_not_there_is_a_failure_not_an_exception(
        self, db, user, background_sessions,
    ) -> None:  # noqa: ANN001
        outcome = await graph_runner.run_graph(user.id, str(uuid_pkg.uuid4()))

        assert outcome.kind == graph_runner.OUTCOME_FAILED
        assert outcome.reason

    async def test_another_users_graph_is_a_failure_not_an_exception(
        self, db, user, make_user, datasource, published, background_sessions,
    ) -> None:  # noqa: ANN001
        """Ownership is a 404 here as everywhere: indistinguishable from one not there."""
        graph = await published(_nodes(datasource, asks=False))
        intruder = await make_user("intruder@example.com")

        outcome = await graph_runner.run_graph(intruder.id, str(graph.uuid))

        assert outcome.kind == graph_runner.OUTCOME_FAILED

    async def test_a_failing_node_is_a_failure_carrying_the_reason(
        self, db, user, datasource, published, background_sessions,
    ) -> None:  # noqa: ANN001
        graph = await published(_nodes(
            datasource, asks=False, sql="SELECT nope FROM departments",
        ))

        outcome = await graph_runner.run_graph(user.id, str(graph.uuid))

        assert outcome.kind == graph_runner.OUTCOME_FAILED
        assert "departments" in outcome.reason or outcome.reason

    async def test_a_run_id_that_is_not_one_is_refused_readably(
        self, db, user, background_sessions,
    ) -> None:  # noqa: ANN001
        outcome = await graph_runner.answer_graph_run(user.id, "not-a-uuid", "yes")

        assert outcome.kind == graph_runner.OUTCOME_FAILED
        assert "not a run id" in outcome.reason.lower()
