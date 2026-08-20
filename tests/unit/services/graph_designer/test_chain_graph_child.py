"""
Tests for a **graph** embedded in a tool config: the third thing a nested child can be,
and the only one that can stop mid-run to ask a person a question.

The properties this file holds still, most important first:

* **The values are the whole result, never the preview.** ``GraphOutcome.rows`` is a
  twenty-row sample with the real total beside it — right for describing a result,
  catastrophic for building a filter out of. A parent restricted to the first twenty of
  five hundred ids answers a different question than the one asked and looks exactly like
  an answer. This is the test that would have caught the row caps being reintroduced one
  layer up.
* **A pause is the tool's output.** The chain stops, the question comes back verbatim with
  a run id, and answering it finishes the *tool* — not just the graph. The answer path
  supplies the graph's values rather than re-running it, because re-running would ask the
  same question of somebody who has already answered it.
* **A graph that reads the parent is refused.** A graph's ``tool_config`` node runs that
  tool *including its chain*, so tool → graph → tool is unbounded recursion across
  separate LangGraph runs, where neither run's limits apply to the other. Nothing would
  report it; the turn would never end. This is the one refusal here that prevents a hang
  rather than a wrong answer.
* **A graph cannot be deleted or unpublished under a parent.** Either would drop that
  parent's filter and widen its results silently — the failure the whole nesting feature
  is designed against.

Against the real database, the real chain runner and a real SQLite datasource, like its
neighbours. Everything here is about two subsystems agreeing, and a stub of either would
only prove this file calls what it calls.

**It lives beside the Graph Designer tests rather than the tool-config ones**, although
its subject is a tool config's chain. Every test here runs a real graph, which needs the
three autouse fixtures in this package's ``conftest.py`` — the per-test session factory,
the in-memory checkpointer and the run cleanup. Without them a test does not fail
cleanly: it either writes to the development database or trips the network guard with an
error about sockets.
"""

from __future__ import annotations

import sqlite3
import uuid as uuid_pkg

import pytest

pytest.importorskip("langgraph", reason="LangGraph is installed in the container only")

from litestar.exceptions import HTTPException  # noqa: E402

from app.models.data_agents import DataAgent  # noqa: E402
from app.models.datasource import DataSource  # noqa: E402
from app.models.tool_configs import ToolConfig  # noqa: E402
from app.services.graph_designer import graph_service  # noqa: E402
from app.services.tool_configs import tool_chain_service  # noqa: E402
from app.services.tool_configs.tool_chain_graph import (  # noqa: E402
    describe_question,
    graph_values,
    run_chain,
)

#: Departments in the fixture. Past the twenty-row preview cap on purpose — that is the
#: whole point of ``TestTheValuesAreTheWholeResult``.
DEPARTMENTS = 60


@pytest.fixture
async def datasource(db, user, tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "chain_graph.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        f"""
        CREATE TABLE departments (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE projects (id INTEGER PRIMARY KEY, department_id INTEGER,
                               name TEXT);
        WITH RECURSIVE counter(n) AS (
            SELECT 1 UNION ALL SELECT n + 1 FROM counter WHERE n < {DEPARTMENTS}
        )
        INSERT INTO departments (id, name) SELECT n, 'dept ' || n FROM counter;
        WITH RECURSIVE counter(n) AS (
            SELECT 1 UNION ALL SELECT n + 1 FROM counter WHERE n < {DEPARTMENTS}
        )
        INSERT INTO projects (id, department_id, name)
            SELECT n, n, 'project ' || n FROM counter;
        """
    )
    connection.commit()
    connection.close()

    row = DataSource(
        user_id=user.id,
        datasource_name=f"cg-{uuid_pkg.uuid4().hex[:6]}",
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


@pytest.fixture
async def agent(db, user):  # noqa: ANN001, ANN201
    row = DataAgent(
        user_id=user.id, name=f"agent-{uuid_pkg.uuid4().hex[:6]}", is_active=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
async def parent(db, user, agent, datasource):  # noqa: ANN001, ANN201
    """A SQL-mode tool whose statement has a placeholder for the graph's values."""
    # No `user_id`: ownership on a tool config runs tool → agent → user.
    row = ToolConfig(
        data_agent_id=agent.id,
        datasource_id=datasource.id,
        tool_name="projects_in_departments",
        table_name="projects",
        extra_tables=[],
        description="Projects in the departments a graph picks.",
        query_mode="sql",
        sql_query="SELECT id, name FROM projects WHERE department_id IN :dept_ids",
        config={},
        sql_params=[],
        is_enabled=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def graph_data(datasource, *, asks: bool = False, tool_uuid: str = ""):  # noqa: ANN001
    """A graph that reads department ids, optionally asking something on the way."""
    nodes = [
        {"id": "s", "type": "start", "position": {}, "data": {"label": "Start"}},
        {
            "id": "q", "type": "sql", "position": {},
            "data": {
                "label": "departments",
                "datasource_id": str(datasource.uuid),
                "table_names": ["departments"],
                "sql_query": "SELECT id FROM departments",
            },
        },
        {"id": "ok", "type": "success", "position": {}, "data": {}},
    ]
    edges = [{"id": "e1", "source": "s", "source_port": "default", "target": "q"}]

    if tool_uuid:
        nodes.insert(2, {
            "id": "t", "type": "tool_config", "position": {},
            "data": {"label": "the parent", "tool_config_id": tool_uuid},
        })
        edges += [
            {"id": "e2", "source": "q", "source_port": "default", "target": "t"},
            {"id": "e3", "source": "t", "source_port": "default", "target": "ok"},
        ]
    elif asks:
        nodes.insert(2, {
            "id": "ask", "type": "human", "position": {},
            "data": {
                "label": "Confirm",
                "prompt": "Shall I include archived departments?",
                "expects": "confirm",
            },
        })
        edges += [
            {"id": "e2", "source": "q", "source_port": "default", "target": "ask"},
            {"id": "e3", "source": "ask", "source_port": "default", "target": "ok"},
        ]
    else:
        edges.append({
            "id": "e2", "source": "q", "source_port": "default", "target": "ok",
        })

    return {"nodes": nodes, "edges": edges}


@pytest.fixture
def published(db, user, datasource):  # noqa: ANN001, ANN201
    async def _publish(**kwargs):  # noqa: ANN003
        graph = await graph_service.create_graph(
            db, user.id, f"Departments {uuid_pkg.uuid4().hex[:4]}", "Picks departments.",
        )
        await graph_service.save_graph(
            db, user.id, graph.uuid, graph_data(datasource, **kwargs),
        )

        if kwargs.pop("publish", True):
            await graph_service.set_graph_active(db, user.id, graph.uuid, True)

        return graph

    return _publish


@pytest.fixture
def embed(db, user):  # noqa: ANN001, ANN201
    """Embed a graph in a tool as its only child, and return the resolved chain."""
    async def _embed(parent_row, graph, column: str = "id", **overrides):  # noqa: ANN001
        entry = {
            "child_graph_id": str(graph.uuid),
            "child_column": column,
            "parent_reference": "dept_ids",
            "binding_mode": "in_list",
            "value_alias": "",
        }
        entry.update(overrides)

        links = await tool_chain_service.validated_children(
            db, user.id, parent_row, [entry],
        )
        await tool_chain_service.replace_child_links(db, parent_row.id, links)
        await db.commit()

        from app.models.datasource import DataSource as _DataSource
        from app.db.db_utils import CRUDQueryBuilder

        datasource_row = await CRUDQueryBuilder(_DataSource).get_one(
            db, filters={"id": parent_row.datasource_id},
        )

        return await tool_chain_service.chain_for_tool(db, parent_row, datasource_row)

    return _embed


class TestTheValuesAreTheWholeResult:
    """
    The property this whole file exists for.

    A graph's ``result_preview`` is capped at twenty rows. These values become the
    parent's ``IN`` filter, so reading the preview would restrict sixty departments to
    twenty — a query that runs, returns rows, and answers a different question, with
    nothing about the result saying so.
    """

    async def test_a_graph_returning_more_than_the_preview_supplies_every_value(
        self, db, user, parent, published, embed, background_sessions,
    ) -> None:  # noqa: ANN001
        chain = await embed(parent, await published())

        result = await run_chain(chain)

        assert len(result.rows) == DEPARTMENTS, (
            "one project per department — a preview-sized filter would give 20"
        )

    async def test_the_preview_really_is_smaller_than_the_answer(
        self, db, user, published, background_sessions,
    ) -> None:  # noqa: ANN001
        """
        States the trap outright, so the test above cannot quietly stop testing anything
        if the preview cap is ever raised past the fixture size.
        """
        from app.services.graph_designer import graph_runner

        graph = await published()
        outcome = await graph_runner.run_graph(user.id, str(graph.uuid))

        assert outcome.total_rows == DEPARTMENTS
        assert len(outcome.rows) < DEPARTMENTS, "the preview is a sample"

        whole = await graph_runner.full_result(user.id, outcome.run_id)

        assert len(graph_values(whole, "id")) == DEPARTMENTS


class TestAGraphChildBehavesLikeAToolChild:
    async def test_an_empty_graph_result_stops_the_chain_by_name(
        self, db, user, parent, datasource, published, embed, background_sessions,
    ) -> None:  # noqa: ANN001
        """
        A graph that matched nothing is an answer, not a failure — the same short circuit
        a tool-config child produces, named so the model can say *which* step found
        nothing.
        """
        graph = await graph_service.create_graph(db, user.id, "Nothing", "Finds none.")
        data = graph_data(datasource)
        data["nodes"][1]["data"]["sql_query"] = (
            "SELECT id FROM departments WHERE id < 0"
        )
        await graph_service.save_graph(db, user.id, graph.uuid, data)
        await graph_service.set_graph_active(db, user.id, graph.uuid, True)

        chain = await embed(parent, graph)
        result = await run_chain(chain)

        assert result.rows == []
        assert result.stopped_by == graph.name
        assert not result.waiting, "nothing matched is not the same as waiting"

    async def test_a_column_the_graph_does_not_return_stops_the_chain(
        self, db, user, parent, published, embed, background_sessions,
    ) -> None:  # noqa: ANN001
        """
        Nothing knows a graph's output columns until it runs, so a wrong name cannot be
        refused on save — it comes back as "no values", which is what it is.
        """
        chain = await embed(parent, await published(), column="nope")

        result = await run_chain(chain)

        assert result.rows == []
        assert result.stopped_by

    async def test_the_chain_resolves_the_graph_as_a_leaf(
        self, db, user, parent, published, embed,
    ) -> None:  # noqa: ANN001
        graph = await published()
        chain = await embed(parent, graph)

        assert len(chain.children) == 1

        child = chain.children[0]

        assert child.is_graph
        assert child.children == [], "a graph's composition is drawn, not nested here"
        assert child.label == graph.name
        assert child.key == str(graph.uuid)


class TestAPauseIsTheToolsOutput:
    async def test_a_graph_that_asks_stops_the_chain_and_carries_the_question(
        self, db, user, parent, published, embed, background_sessions,
    ) -> None:  # noqa: ANN001
        chain = await embed(parent, await published(asks=True))

        result = await run_chain(chain)

        assert result.waiting
        assert result.rows == []
        assert not result.short_circuited, "waiting is not nothing-matched"
        assert result.asked["question"] == "Shall I include archived departments?"
        assert result.asked["run_id"]

    async def test_the_question_is_relayed_verbatim_with_a_way_back(
        self, db, user, parent, published, embed, background_sessions,
    ) -> None:  # noqa: ANN001
        """
        Word for word, because a model rewording a question asks the user the wrong
        thing — and with the run id, because a question that cannot be resumed is a
        conversation that cannot continue.
        """
        chain = await embed(parent, await published(asks=True))
        result = await run_chain(chain)

        told = describe_question(result, "projects_in_departments")

        assert "Shall I include archived departments?" in told
        assert "word for word" in told
        assert result.asked["run_id"] in told
        assert "answer_projects_in_departments" in told
        assert "not a failure" in told

    async def test_answering_finishes_the_tool_without_asking_again(
        self, db, user, parent, published, embed, background_sessions,
    ) -> None:  # noqa: ANN001
        """
        The half that is easy to get wrong: resuming the graph is not the answer the user
        asked for. The chain has to be re-run with the graph's values *supplied*, or the
        graph runs again and asks the same question of somebody who just answered it.
        """
        from app.services.graph_designer import graph_runner
        from app.services.tool_configs.tool_chain_graph import chain_node_name

        chain = await embed(parent, await published(asks=True))
        asked = await run_chain(chain)

        answered = await graph_runner.answer_graph_run(
            user.id, asked.asked["run_id"], "yes",
        )
        assert answered.finished, answered.reason

        values = graph_values(
            await graph_runner.full_result(user.id, answered.run_id), "id",
        )
        node_key = chain_node_name(chain.children[0])

        finished = await run_chain(chain, None, {}, resolved={node_key: values})

        assert not finished.waiting, "the question is not asked a second time"
        assert len(finished.rows) == DEPARTMENTS

    async def test_describe_question_is_none_when_nothing_asked(
        self, db, user, parent, published, embed, background_sessions,
    ) -> None:  # noqa: ANN001
        chain = await embed(parent, await published())

        assert describe_question(await run_chain(chain), "x") is None


class TestWhatIsRefusedWhenTheLinkIsSaved:
    async def test_a_draft_graph_cannot_be_embedded(
        self, db, user, parent, datasource,
    ) -> None:  # noqa: ANN001
        graph = await graph_service.create_graph(db, user.id, "Draft", "Not ready.")
        await graph_service.save_graph(
            db, user.id, graph.uuid, graph_data(datasource),
        )

        with pytest.raises(HTTPException, match="still a draft"):
            await tool_chain_service.validated_children(db, user.id, parent, [{
                "child_graph_id": str(graph.uuid),
                "child_column": "id",
                "parent_reference": "dept_ids",
            }])

    async def test_a_graph_that_reads_the_parent_is_refused(
        self, db, user, parent, datasource,
    ) -> None:  # noqa: ANN001
        """
        The refusal that prevents a **hang** rather than a wrong answer. A graph's
        ``tool_config`` node runs that tool including its chain, so parent → graph →
        parent recurses across separate LangGraph runs with neither one's limits applying
        to the other.
        """
        graph = await graph_service.create_graph(db, user.id, "Loops", "Reads parent.")
        await graph_service.save_graph(
            db, user.id, graph.uuid,
            graph_data(datasource, tool_uuid=str(parent.uuid)),
        )
        await graph_service.set_graph_active(db, user.id, graph.uuid, True)

        with pytest.raises(HTTPException) as caught:
            await tool_chain_service.validated_children(db, user.id, parent, [{
                "child_graph_id": str(graph.uuid),
                "child_column": "id",
                "parent_reference": "dept_ids",
            }])

        assert "without end" in caught.value.detail

    async def test_such_a_graph_is_not_even_offered(
        self, db, user, parent, datasource,
    ) -> None:  # noqa: ANN001
        """The cycle rule applied before the operator can build one, not after."""
        looping = await graph_service.create_graph(db, user.id, "Loops", "Reads it.")
        await graph_service.save_graph(
            db, user.id, looping.uuid,
            graph_data(datasource, tool_uuid=str(parent.uuid)),
        )
        await graph_service.set_graph_active(db, user.id, looping.uuid, True)

        fine = await graph_service.create_graph(db, user.id, "Fine", "Reads depts.")
        await graph_service.save_graph(
            db, user.id, fine.uuid, graph_data(datasource),
        )
        await graph_service.set_graph_active(db, user.id, fine.uuid, True)

        offered = await tool_chain_service.embeddable_graphs(
            db, user.id, str(parent.uuid),
        )

        names = [entry["tool_name"] for entry in offered]

        assert fine.name in names
        assert looping.name not in names

    async def test_a_draft_graph_is_not_offered(
        self, db, user, parent, datasource,
    ) -> None:  # noqa: ANN001
        graph = await graph_service.create_graph(db, user.id, "Draft", "Not ready.")
        await graph_service.save_graph(
            db, user.id, graph.uuid, graph_data(datasource),
        )

        offered = await tool_chain_service.embeddable_graphs(
            db, user.id, str(parent.uuid),
        )

        assert graph.name not in [entry["tool_name"] for entry in offered]

    async def test_another_users_graph_cannot_be_embedded(
        self, db, user, make_user, parent, datasource,
    ) -> None:  # noqa: ANN001
        intruder = await make_user("intruder@example.com")
        theirs = await graph_service.create_graph(db, intruder.id, "Theirs", "Mine.")

        with pytest.raises(HTTPException, match="could not be found"):
            await tool_chain_service.validated_children(db, user.id, parent, [{
                "child_graph_id": str(theirs.uuid),
                "child_column": "id",
                "parent_reference": "dept_ids",
            }])

    async def test_a_placeholder_the_statement_does_not_use_is_refused(
        self, db, user, parent, published,
    ) -> None:  # noqa: ANN001
        """The shared rule, reached through the graph branch: a name nothing binds."""
        graph = await published()

        with pytest.raises(HTTPException, match="does not use"):
            await tool_chain_service.validated_children(db, user.id, parent, [{
                "child_graph_id": str(graph.uuid),
                "child_column": "id",
                "parent_reference": "nowhere",
            }])


class TestAnEmbeddedGraphIsProtected:
    async def test_it_cannot_be_deleted(
        self, db, user, parent, published, embed,
    ) -> None:  # noqa: ANN001
        graph = await published()
        await embed(parent, graph)

        with pytest.raises(HTTPException) as caught:
            await graph_service.delete_graph(db, user.id, graph.uuid)

        assert "projects_in_departments" in caught.value.detail
        assert "more rows than they should" in caught.value.detail

    async def test_it_cannot_be_made_a_draft(
        self, db, user, parent, published, embed,
    ) -> None:  # noqa: ANN001
        """
        Unpublishing would drop the parent's filter as surely as deleting would, and the
        parent would carry on running and return more rows than it should.
        """
        graph = await published()
        await embed(parent, graph)

        with pytest.raises(HTTPException, match="cannot be made a draft"):
            await graph_service.set_graph_active(db, user.id, graph.uuid, False)

    async def test_an_unembedded_graph_is_unaffected(
        self, db, user, published,
    ) -> None:  # noqa: ANN001
        graph = await published()

        await graph_service.set_graph_active(db, user.id, graph.uuid, False)
        await graph_service.delete_graph(db, user.id, graph.uuid)


class TestGraphValues:
    """
    Reading one named value out of whatever shape a graph's last node produced.

    Three shapes, because a graph's last data node may be a query, a Value node holding a
    list, or one holding a single value — and reporting a list as "no values" would refuse
    a perfectly ordinary graph.
    """

    def test_rows_are_read_by_name(self) -> None:
        rows = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]

        assert graph_values(rows, "id") == [1, 2]

    def test_a_bare_list_is_the_answer_whatever_the_name(self) -> None:
        """A list has no columns, so requiring the name to match would refuse it."""
        assert graph_values([7, 8, 9], "anything") == [7, 8, 9]

    def test_a_single_value_is_a_list_of_one(self) -> None:
        assert graph_values(42, "id") == [42]

    def test_a_dict_is_read_by_name(self) -> None:
        assert graph_values({"id": 5, "other": 6}, "id") == [5]

    def test_nulls_are_dropped(self) -> None:
        """A NULL never matches an IN, so carrying it forward only inflates the list."""
        assert graph_values([{"id": 1}, {"id": None}, {"id": 2}], "id") == [1, 2]

    def test_duplicates_collapse(self) -> None:
        """A value repeated restricts a query exactly once."""
        assert graph_values([{"id": 1}, {"id": 1}, {"id": 2}], "id") == [1, 2]

    def test_nothing_is_an_empty_list_not_a_failure(self) -> None:
        assert graph_values(None, "id") == []
        assert graph_values([], "id") == []
