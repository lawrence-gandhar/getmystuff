"""
Tests for app/services/tool_configs/tool_chain_graph.py — running a nested tool as
a LangGraph.

Against a **real SQLite database**, like the executor it drives: the interesting
question is whether the inner query's values actually restrict the outer one, and a
mock would only prove the module calls what it calls. The dataset is built so the
answer is wrong unless every level is applied — client 1 is both paid and active,
client 2 is paid but churned, client 3 is active but unpaid — so a chain that skips
a level returns more rows and the test notices.

Three properties, in the order they matter:

* **the values propagate outward** — a three-level chain answers as the innermost
  filter dictates;
* **nothing runs above an empty node** — the conditional edge ends the run, and the
  parent's query is never executed at all;
* **inner rows do not leak** — what comes back is the root's rows, as a sub-query's
  inner rows are not part of an outer result.

Requires LangGraph, so it runs in the container: ``docker compose exec app python
-m pytest tests/unit/services/tool_configs/test_tool_chain_graph.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.deep_agents.query_executor import ToolQueryError
from app.services.tool_configs.tool_chain_service import ChainNode

# Before the graph module is imported, not after: LangGraph lives in the container
# (see DOCKER_AND_LOCAL_LLM.md), and importing it outside would be a collection
# error rather than a skip.
pytest.importorskip("langgraph", reason="LangGraph is installed in the container only")

from app.services.tool_configs import tool_chain_graph as graph_module  # noqa: E402
from app.services.tool_configs.tool_chain_graph import (  # noqa: E402
    build_chain_graph,
    describe_stop,
    run_chain,
)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    """Clients, their invoices and their projects — three tables that disagree."""
    path = tmp_path / "warehouse.db"

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT, status TEXT);
        CREATE TABLE invoices (id INTEGER PRIMARY KEY, client_id INTEGER, paid INTEGER);
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY, client_id INTEGER, name TEXT, revenue INTEGER
        );
        INSERT INTO clients VALUES
            (1, 'Acme', 'active'), (2, 'Globex', 'churned'), (3, 'Initech', 'active');
        INSERT INTO invoices VALUES (1, 1, 1), (2, 2, 1), (3, 3, 0);
        INSERT INTO projects VALUES
            (1, 1, 'apollo', 100), (2, 2, 'zeus', 50),
            (3, 3, 'hermes', 70), (4, 1, 'atlas', 30);
        """
    )
    connection.commit()
    connection.close()

    return path


@pytest.fixture
def datasource(database: Path) -> SimpleNamespace:
    """A datasource as the executor reads it — five attributes, no ORM row."""
    return SimpleNamespace(
        db_type="sqlite",
        database_name=str(database),
        datasource_name="warehouse",
        host=None,
        port=None,
        username=None,
        password_encrypted=None,
        configuration_data={},
    )


def tool(name: str, table: str, config: dict, **kwargs) -> SimpleNamespace:
    """A tool config as a chain node holds it."""
    return SimpleNamespace(
        uuid=name,
        tool_name=name,
        table_name=table,
        extra_tables=[],
        config=config,
        sql_query=kwargs.pop("sql_query", None),
        sql_params=kwargs.pop("sql_params", None),
        query_mode=kwargs.pop("query_mode", "builder"),
        is_enabled=True,
    )


@pytest.fixture
def paid_invoices(datasource) -> ChainNode:  # noqa: ANN001
    """Deepest: the clients with a paid invoice — 1 and 2."""
    return ChainNode(
        tool=tool(
            "paid_invoices",
            "invoices",
            {
                "columns": [{"column": "client_id", "alias": ""}],
                "filters": [{"column": "paid", "operator": "=", "value": "1"}],
            },
        ),
        datasource=datasource,
        child_column="client_id",
        parent_reference="id",
    )


@pytest.fixture
def active_clients(datasource, paid_invoices) -> ChainNode:  # noqa: ANN001
    """Middle: active clients, restricted to those with a paid invoice — 1."""
    return ChainNode(
        tool=tool(
            "active_clients",
            "clients",
            {
                "columns": [{"column": "id", "alias": ""}],
                "filters": [{"column": "status", "operator": "=", "value": "active"}],
            },
        ),
        datasource=datasource,
        child_column="id",
        parent_reference="client_id",
        children=[paid_invoices],
    )


@pytest.fixture
def projects(datasource, active_clients) -> ChainNode:  # noqa: ANN001
    """Root: the projects of those clients."""
    return ChainNode(
        tool=tool(
            "projects_by_client",
            "projects",
            {
                "columns": [
                    {"column": "name", "alias": ""},
                    {"column": "revenue", "alias": ""},
                ],
            },
        ),
        datasource=datasource,
        children=[active_clients],
    )


class TestPropagation:
    async def test_a_three_level_chain_answers_from_the_innermost_filter(
        self, projects: ChainNode
    ) -> None:
        """Paid ∩ active is client 1 alone, so only its two projects come back —
        skip either level and the count is wrong."""
        result = await run_chain(projects)

        assert sorted(row["name"] for row in result.rows) == ["apollo", "atlas"]
        assert not result.short_circuited

    async def test_a_two_level_chain_is_the_same_machinery(
        self, active_clients: ChainNode, datasource  # noqa: ANN001
    ) -> None:
        active_clients.children = []

        root = ChainNode(
            tool=tool(
                "projects_by_client",
                "projects",
                {"columns": [{"column": "name", "alias": ""}]},
            ),
            datasource=datasource,
            children=[active_clients],
        )

        result = await run_chain(root)

        assert sorted(row["name"] for row in result.rows) == [
            "apollo", "atlas", "hermes",
        ]

    async def test_only_the_root_s_rows_come_back(
        self, projects: ChainNode
    ) -> None:
        """Sub-query semantics: the inner tools' rows restrict the outer query and
        are then gone. Nothing an inner tool read is in the answer."""
        result = await run_chain(projects)

        assert all(set(row) == {"name", "revenue"} for row in result.rows)

    async def test_a_sql_root_takes_its_values_as_a_bind_parameter(
        self, active_clients: ChainNode, datasource  # noqa: ANN001
    ) -> None:
        active_clients.children = []
        active_clients.parent_reference = "active_clients"

        root = ChainNode(
            tool=tool(
                "written_projects",
                "projects",
                {},
                query_mode="sql",
                sql_query=(
                    "SELECT name FROM projects WHERE client_id IN :active_clients "
                    "ORDER BY name"
                ),
            ),
            datasource=datasource,
            children=[active_clients],
        )

        result = await run_chain(root)

        assert [row["name"] for row in result.rows] == ["apollo", "atlas", "hermes"]

    async def test_the_root_s_own_filters_still_apply(
        self, projects: ChainNode
    ) -> None:
        """A chain narrows a query; it does not replace what the operator wrote."""
        projects.tool.config["filters"] = [
            {"column": "revenue", "operator": ">", "value": "50"}
        ]

        result = await run_chain(projects)

        assert [row["name"] for row in result.rows] == ["apollo"]


class TestTheConditionalStop:
    async def test_an_empty_deepest_node_stops_the_whole_chain(
        self, projects: ChainNode, paid_invoices: ChainNode
    ) -> None:
        paid_invoices.tool.config["filters"] = [
            {"column": "paid", "operator": "=", "value": "9"}
        ]

        result = await run_chain(projects)

        assert result.rows == []
        assert result.stopped_by == "paid_invoices"

    async def test_nothing_above_the_empty_node_is_executed(
        self, projects: ChainNode, paid_invoices: ChainNode, monkeypatch  # noqa: ANN001
    ) -> None:
        """The point of the conditional edge. `IN ()` is not built and the outer
        query is not sent — the run ends at the node that found nothing."""
        from app.services.tool_configs import tool_chain_graph

        paid_invoices.tool.config["filters"] = [
            {"column": "paid", "operator": "=", "value": "9"}
        ]

        calls = []

        async def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            calls.append(args[2])
            return []

        monkeypatch.setattr(tool_chain_graph, "execute_tool_query", spy)

        await run_chain(projects)

        assert calls == []

    async def test_a_middle_node_that_matches_nothing_stops_it_too(
        self, projects: ChainNode, active_clients: ChainNode
    ) -> None:
        active_clients.tool.config["filters"] = [
            {"column": "status", "operator": "=", "value": "nope"}
        ]

        result = await run_chain(projects)

        assert result.rows == []
        assert result.stopped_by == "active_clients"

    async def test_the_stop_is_explained_as_an_answer(
        self, projects: ChainNode, paid_invoices: ChainNode
    ) -> None:
        """A bare "0 rows" leaves a model unable to tell "nothing matched" from a
        broken tool, and it apologises for the data instead of reporting it."""
        paid_invoices.tool.config["filters"] = [
            {"column": "paid", "operator": "=", "value": "9"}
        ]

        described = describe_stop(await run_chain(projects))

        assert "paid_invoices" in described
        assert "not a failure" in described

    async def test_a_completed_chain_has_nothing_to_explain(
        self, projects: ChainNode
    ) -> None:
        assert describe_stop(await run_chain(projects)) is None

    async def test_only_nulls_counts_as_nothing(
        self, projects: ChainNode, paid_invoices: ChainNode, database: Path
    ) -> None:
        """A NULL never matches an `IN`, so a column of them is an empty list — and
        an empty list stops the chain rather than matching everything."""
        connection = sqlite3.connect(database)
        connection.execute("UPDATE invoices SET client_id = NULL")
        connection.commit()
        connection.close()

        result = await run_chain(projects)

        assert result.rows == []
        assert result.stopped_by == "paid_invoices"


class TestNothingCapsTheValuesOrTheRows:
    """
    What replaced the two refusals this class used to assert: an inner tool past 2,000
    values, and a root result past 200 rows. Both are gone, and both were reached by
    tools that were simply about a lot of records.

    The values are the interesting half. They become an ``IN`` comparison and are then
    discarded, so a truncated list did not produce a short answer — it produced a
    *different question*, answered confidently, with nothing in the result to say so.
    Refusing was the honest response to that, and it meant a tool over the limit could
    not be embedded at all. Reading them all removes the choice.
    """

    async def test_a_large_inner_result_becomes_a_large_in_list(
        self, projects: ChainNode, monkeypatch,  # noqa: ANN001
    ) -> None:
        from app.services.tool_configs import tool_chain_graph

        seen: dict = {}

        async def many_values(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            return list(range(5000))

        async def capture(node, state, bindings, row_limit):  # noqa: ANN001, ANN202
            seen["bindings"] = bindings
            return []

        monkeypatch.setattr(tool_chain_graph, "execute_value_query", many_values)
        monkeypatch.setattr(tool_chain_graph, "_run_root", capture)

        await run_chain(projects)

        assert len(seen["bindings"][0]["values"]) == 5000

    async def test_the_root_returns_every_row_it_matched(
        self, projects: ChainNode, monkeypatch,  # noqa: ANN001
    ) -> None:
        """
        No budget is spent and none is checked, so a chain answering about a lot of
        records answers about all of them. This used to refuse past 200.
        """
        from app.services.tool_configs import tool_chain_graph

        async def many_rows(node, state, bindings, row_limit):  # noqa: ANN001, ANN202
            assert row_limit is None, "no caller in the agent path names a budget"
            return [{"n": index} for index in range(4000)]

        monkeypatch.setattr(tool_chain_graph, "_run_root", many_rows)

        result = await run_chain(projects)

        assert len(result.rows) == 4000


class TestTheGraphItself:
    async def test_it_is_compiled_once_and_reusable(
        self, projects: ChainNode
    ) -> None:
        """The factory keeps the compiled graph in the tool's closure, so a second
        call must be an invoke and not a rebuild."""
        graph = build_chain_graph(projects)

        first = await run_chain(projects, graph)
        second = await run_chain(projects, graph)

        assert [row["name"] for row in first.rows] == [
            row["name"] for row in second.rows
        ]

    async def test_the_root_row_limit_is_the_caller_s(
        self, projects: ChainNode
    ) -> None:
        """What Test Query uses: the same graph, one row from the root."""
        result = await run_chain(projects, build_chain_graph(projects, row_limit=1))

        assert len(result.rows) == 1

    async def test_one_tool_embedded_twice_for_one_column_runs_once(
        self, datasource, active_clients: ChainNode  # noqa: ANN001
    ) -> None:
        """Two parents, one answer: nodes are keyed by tool and column, so the
        second parent reads what the first node produced."""
        active_clients.children = []

        second = ChainNode(
            tool=active_clients.tool,
            datasource=datasource,
            child_column="id",
            parent_reference="client_id",
        )
        root = ChainNode(
            tool=tool(
                "projects_by_client",
                "projects",
                {"columns": [{"column": "name", "alias": ""}]},
            ),
            datasource=datasource,
            children=[active_clients, second],
        )

        graph = build_chain_graph(root)

        assert len(graph.get_graph().nodes) == 4  # __start__, the child, root, __end__

    async def test_a_childless_chain_is_just_the_query(
        self, datasource  # noqa: ANN001
    ) -> None:
        """`run_chain` on a lone tool is the ordinary path with a graph around it —
        the factory skips the graph for these, but it must still be correct."""
        root = ChainNode(
            tool=tool(
                "all_projects",
                "projects",
                {"columns": [{"column": "name", "alias": ""}]},
            ),
            datasource=datasource,
        )

        result = await run_chain(root)

        assert len(result.rows) == 4


@pytest.fixture
def every_client(datasource) -> ChainNode:  # noqa: ANN001
    """A child that returns all three client ids, one run of the root each."""
    return ChainNode(
        tool=tool(
            "every_client",
            "clients",
            {"columns": [{"column": "id", "alias": ""}]},
        ),
        datasource=datasource,
        child_column="id",
        parent_reference="client_id",
        binding_mode="each",
    )


class TestIteratingLinks:
    """
    A link that makes the root run once per value, rather than once for all of them.

    The dataset earns its keep here: three clients with different project counts, so
    a run that iterated the wrong number of times, or concatenated the wrong rows,
    produces a different answer rather than the same one arrived at differently.
    """

    @pytest.fixture
    def per_client(self, datasource, every_client) -> ChainNode:  # noqa: ANN001,D102
        return ChainNode(
            tool=tool(
                "projects_per_client",
                "projects",
                {"columns": [{"column": "name", "alias": ""}]},
            ),
            datasource=datasource,
            children=[every_client],
        )

    async def test_the_root_runs_once_per_value_and_the_rows_are_unioned(
        self, per_client: ChainNode
    ) -> None:
        """
        Client 1 has two projects, 2 has one, 3 has one — four rows in client order.

        An `IN` binding would return the same four rows in *table* order, so the
        ordering is the assertion that tells the two apart.
        """
        result = await run_chain(per_client)

        assert [row["name"] for row in result.rows] == [
            "apollo", "atlas", "zeus", "hermes",
        ]

    async def test_a_scalar_binding_can_sit_where_a_list_cannot(
        self, datasource, every_client  # noqa: ANN001
    ) -> None:
        """
        The reason the mode exists: an expanding parameter always renders
        parenthesised, so `'p-' || :x` is a syntax error with one and correct with
        the other.
        """
        root = ChainNode(
            tool=tool(
                "labelled",
                "projects",
                {},
                query_mode="sql",
                sql_query=(
                    "SELECT name, 'client-' || :client_id AS tag FROM projects "
                    "WHERE client_id = :client_id ORDER BY name"
                ),
            ),
            datasource=datasource,
            children=[every_client],
        )
        root.children[0].parent_reference = "client_id"

        result = await run_chain(root)

        assert [(row["name"], row["tag"]) for row in result.rows] == [
            ("apollo", "client-1"), ("atlas", "client-1"),
            ("zeus", "client-2"), ("hermes", "client-3"),
        ]

    async def test_the_value_is_recorded_on_each_row_when_asked_for(
        self, per_client: ChainNode
    ) -> None:
        """Without this, rows from four runs of one statement are indistinguishable
        — and a query that filters on a client without selecting it is ordinary."""
        per_client.children[0].value_alias = "client"

        result = await run_chain(per_client)

        assert [(row["name"], row["client"]) for row in result.rows] == [
            ("apollo", 1), ("atlas", 1), ("zeus", 2), ("hermes", 3),
        ]

    async def test_a_value_alias_that_collides_is_refused_not_overwritten(
        self, datasource, every_client  # noqa: ANN001
    ) -> None:
        """
        Overwriting would replace a real value from the database with one from the
        chain; skipping would leave rows whose label says nothing about them. Both
        look right and are not.
        """
        root = ChainNode(
            tool=tool(
                "projects_per_client",
                "projects",
                {
                    "columns": [
                        {"column": "name", "alias": ""},
                        {"column": "client_id", "alias": ""},
                    ],
                },
            ),
            datasource=datasource,
            children=[every_client],
        )
        root.children[0].value_alias = "client_id"

        with pytest.raises(ToolQueryError, match="already returns a column"):
            await run_chain(root)

    async def test_more_values_than_the_cap_is_refused_before_anything_runs(
        self, per_client: ChainNode, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Refused, not truncated. Rows for the first two clients are indistinguishable
        from rows for every client, and a total over them is a plausible wrong
        number.
        """
        monkeypatch.setattr(graph_module, "MAX_CHAIN_ITERATIONS", 2)

        with pytest.raises(ToolQueryError, match="more than the 2 runs"):
            await run_chain(per_client)

    async def test_the_advice_never_asks_the_visitor_to_narrow_anything(
        self, per_client: ChainNode, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tool takes no argument a visitor could change, so any suggestion that
        they rephrase sends the conversation back to the same refusal forever."""
        monkeypatch.setattr(graph_module, "MAX_CHAIN_ITERATIONS", 1)

        with pytest.raises(ToolQueryError) as caught:
            await run_chain(per_client)

        assert "needs reconfiguring" in caught.value.advice
        assert "Do NOT ask them to narrow" in caught.value.advice

    async def test_a_union_over_every_value_is_returned_whole(
        self, per_client: ChainNode
    ) -> None:
        """
        The union used to be refused once it passed the root's row budget, which meant
        a chain over a handful of clients failed as soon as their projects came to more
        than 200 rows. Now the budget is not there and every iteration contributes.
        """
        result = await run_chain(per_client)

        # Four rows from three clients — every iteration contributed, and the client
        # order is what says they came from separate runs rather than one IN.
        assert [row["name"] for row in result.rows] == [
            "apollo", "atlas", "zeus", "hermes",
        ]

    async def test_a_named_budget_stops_the_union_rather_than_refusing_it(
        self, per_client: ChainNode
    ) -> None:
        """
        The one caller that names one is Test Query, asking for a single row as proof
        the chain executes. It reports column names and a count and never a value, so
        stopping early costs it nothing — where refusing made an iterating chain
        untestable.
        """
        result = await run_chain(per_client, build_chain_graph(per_client, row_limit=1))

        assert len(result.rows) == 1

    async def test_an_empty_iterating_child_still_stops_the_chain(
        self, per_client: ChainNode
    ) -> None:
        """Nothing to iterate over is the same answer as nothing to match: no rows,
        and the tool that produced none is named."""
        per_client.children[0].tool.config["filters"] = [
            {"column": "status", "operator": "=", "value": "dissolved"},
        ]

        result = await run_chain(per_client)

        assert result.rows == []
        assert result.stopped_by == "every_client"

    async def test_a_list_sibling_restricts_every_iteration(
        self, datasource, every_client, paid_invoices  # noqa: ANN001
    ) -> None:
        """
        The two binding modes compose: the list narrows the query, the iteration
        decides how many times it runs. Paid clients are 1 and 2, so client 3's run
        contributes nothing even though it happens.
        """
        paid_invoices.parent_reference = "client_id"
        root = ChainNode(
            tool=tool(
                "projects_per_client",
                "projects",
                {"columns": [{"column": "name", "alias": ""}]},
            ),
            datasource=datasource,
            children=[every_client, paid_invoices],
        )

        result = await run_chain(root)

        assert [row["name"] for row in result.rows] == ["apollo", "atlas", "zeus"]


class TestResolvingBindings:
    """
    ``resolve_chain_bindings`` — what the root *would* be run with, without running
    it. The aggregate path needs this because it reads the root's whole result set
    itself, in batches, rather than taking the first two hundred rows.
    """

    async def test_it_returns_the_values_without_running_the_root(
        self, projects: ChainNode, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The root's query is the expensive one and the caller intends to run it
        differently, so running it here would be the whole cost paid twice."""
        ran: list = []
        original = graph_module.execute_tool_query

        async def spy(*args, **kwargs):  # noqa: ANN002, ANN003
            ran.append(args)
            return await original(*args, **kwargs)

        monkeypatch.setattr(graph_module, "execute_tool_query", spy)

        resolved = await graph_module.resolve_chain_bindings(projects)

        assert ran == []
        assert resolved.bindings == [{"reference": "client_id", "values": [1]}]
        assert not resolved.iterates

    async def test_an_iterating_child_comes_back_as_values_not_a_binding(
        self, datasource, every_client  # noqa: ANN001
    ) -> None:
        root = ChainNode(
            tool=tool(
                "projects_per_client",
                "projects",
                {"columns": [{"column": "name", "alias": ""}]},
            ),
            datasource=datasource,
            children=[every_client],
        )
        root.children[0].value_alias = "client"

        resolved = await graph_module.resolve_chain_bindings(root)

        assert resolved.iterates
        assert resolved.iteration_reference == "client_id"
        assert resolved.iteration_values == [1, 2, 3]
        assert resolved.iteration_alias == "client"
        assert resolved.bindings == []

    async def test_a_short_circuit_is_reported_rather_than_an_empty_binding(
        self, projects: ChainNode
    ) -> None:
        """An empty binding list would read as "no restriction" and widen the
        query — the one outcome worse than no answer."""
        projects.children[0].children[0].tool.config["filters"] = [
            {"column": "paid", "operator": "=", "value": "9"},
        ]

        resolved = await graph_module.resolve_chain_bindings(projects)

        assert resolved.short_circuited
        assert resolved.stopped_by == "paid_invoices"
        assert resolved.bindings == []

    async def test_a_childless_chain_resolves_to_nothing_at_all(
        self, datasource  # noqa: ANN001
    ) -> None:
        root = ChainNode(
            tool=tool("all_projects", "projects", {}),
            datasource=datasource,
        )

        resolved = await graph_module.resolve_chain_bindings(root)

        assert resolved.bindings == []
        assert not resolved.iterates
        assert not resolved.short_circuited
