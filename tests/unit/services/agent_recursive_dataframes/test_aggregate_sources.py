"""
Tests for ``aggregate_service.record_sources`` — a tool entry as the things the
reader will actually read.

This is the seam where the aggregation stops being about one query. An ordinary
tool is one source; a nested tool is one *restricted* source; a tool with an
iterating child is one source per value, and the fold across all of them is the
same fold as across one query's batches.

The case worth pinning hardest is the middle one. Before this existed, an
aggregation over a nested tool built its source from the stored config alone and
dropped the child's values — so the totals were over a wider result set than the
tool has ever returned, and nothing about the answer said so. There is a test below
whose only job is to keep that from coming back.

Requires LangGraph (the chain is resolved by running its graph), so it runs in the
container: ``docker compose exec app python -m pytest
tests/unit/services/agent_recursive_dataframes/test_aggregate_sources.py``.
"""

from __future__ import annotations

import sqlite3
import uuid as uuid_pkg
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict

import pytest

from app.services.deep_agents.query_executor import ToolQueryError

pytest.importorskip("langgraph", reason="LangGraph is installed in the container only")

from app.services.agent_recursive_dataframes import (  # noqa: E402
    aggregate_service,
    row_supply,
)
from app.services.tool_configs.tool_chain_service import ChainNode  # noqa: E402


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    """
    Clients and their projects, with the counts deliberately uneven.

    Client 1 has two projects, 2 has one, 3 has one and 4 has none — so a fan-out
    that ran the wrong number of times, or skipped the empty client, produces a
    different total rather than the same one arrived at differently.
    """
    path = tmp_path / f"warehouse_{uuid_pkg.uuid4().hex[:8]}.db"

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE clients (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY, client_id INTEGER, revenue INTEGER
        );
        INSERT INTO clients VALUES
            (1, 'active'), (2, 'active'), (3, 'churned'), (4, 'active');
        INSERT INTO projects VALUES
            (1, 1, 100), (2, 1, 30), (3, 2, 50), (4, 3, 70);
        """
    )
    connection.commit()
    connection.close()

    return path


@pytest.fixture
def datasource(warehouse: Path) -> SimpleNamespace:
    return SimpleNamespace(
        db_type="sqlite",
        database_name=str(warehouse),
        datasource_name="warehouse",
        host=None,
        port=None,
        username=None,
        password_encrypted=None,
        configuration_data={},
    )


def _tool(name: str, table: str, config: dict, **kwargs) -> SimpleNamespace:
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


def _entry(datasource: SimpleNamespace, chain: Any = None) -> Dict[str, Any]:
    """A tool entry as ``collect_agent_tools`` shapes it."""
    return {
        "uuid": str(uuid_pkg.uuid4()),
        "tool_name": "projects_per_client",
        "table_name": "projects",
        "table_names": ["projects"],
        "query_mode": "builder",
        "config": {"columns": [{"column": "revenue", "alias": ""}]},
        "sql_query": None,
        "sql_params": [],
        "datasource": datasource,
        "datasource_name": "warehouse",
        "db_type": "sqlite",
        "chain": chain,
        "allow_recursive_aggregate": True,
    }


@pytest.fixture
def active_clients(datasource: SimpleNamespace) -> Callable:
    """A child returning the ids of active clients — 1, 2 and 4."""

    def _make(binding_mode: str = "in_list", value_alias: str = "") -> ChainNode:
        return ChainNode(
            tool=_tool(
                "active_clients",
                "clients",
                {
                    "columns": [{"column": "id", "alias": ""}],
                    "filters": [
                        {"column": "status", "operator": "=", "value": "active"},
                    ],
                },
            ),
            datasource=datasource,
            child_column="id",
            parent_reference="client_id",
            binding_mode=binding_mode,
            value_alias=value_alias,
        )

    return _make


@pytest.fixture
def root(datasource: SimpleNamespace) -> Callable:
    def _make(child: ChainNode | None = None) -> ChainNode:
        return ChainNode(
            tool=_tool(
                "projects_per_client",
                "projects",
                {"columns": [{"column": "revenue", "alias": ""}]},
            ),
            datasource=datasource,
            children=[child] if child else [],
        )

    return _make


class TestAToolWithNoChain:
    async def test_it_is_one_unrestricted_source(
        self, datasource: SimpleNamespace,
    ) -> None:
        resolved = await aggregate_service.record_sources(_entry(datasource))

        assert len(resolved.sources) == 1
        assert resolved.sources[0].value_bindings == []
        assert resolved.sources[0].label is None
        assert not resolved.short_circuited

    async def test_the_declared_parameters_travel_with_it(
        self, datasource: SimpleNamespace,
    ) -> None:
        entry = _entry(datasource)
        entry["sql_params"] = [{"param": "floor", "type": "number"}]

        resolved = await aggregate_service.record_sources(entry)

        assert resolved.sources[0].sql_params == [
            {"param": "floor", "type": "number"},
        ]


class TestAToolWithAListChild:
    async def test_the_source_carries_the_child_s_values(
        self, datasource: SimpleNamespace, root: Callable, active_clients: Callable,
    ) -> None:
        """
        The regression this file exists for. Without the binding the source is a
        *wider* query than the tool, so every total taken over it is too big — and
        nothing about the answer says so.
        """
        resolved = await aggregate_service.record_sources(
            _entry(datasource, root(active_clients())),
        )

        assert len(resolved.sources) == 1
        assert resolved.sources[0].value_bindings == [
            {"reference": "client_id", "values": [1, 2, 4]},
        ]

    async def test_a_short_circuit_produces_no_source_at_all(
        self, datasource: SimpleNamespace, root: Callable, active_clients: Callable,
    ) -> None:
        """
        An empty binding list would read as "no restriction" and widen the query,
        which is the one outcome worse than no answer — so the chain reports that it
        stopped instead.
        """
        child = active_clients()
        child.tool.config["filters"] = [
            {"column": "status", "operator": "=", "value": "dissolved"},
        ]

        resolved = await aggregate_service.record_sources(
            _entry(datasource, root(child)),
        )

        assert resolved.sources == []
        assert resolved.stopped_by == "active_clients"


class TestAToolWithAnIteratingChild:
    async def test_one_source_per_value(
        self, datasource: SimpleNamespace, root: Callable, active_clients: Callable,
    ) -> None:
        resolved = await aggregate_service.record_sources(
            _entry(datasource, root(active_clients(binding_mode="each"))),
        )

        assert [source.value_bindings for source in resolved.sources] == [
            [{"reference": "client_id", "values": [value], "expanding": False}]
            for value in (1, 2, 4)
        ]

    async def test_every_source_is_otherwise_the_same_query(
        self, datasource: SimpleNamespace, root: Callable, active_clients: Callable,
    ) -> None:
        """Which is what lets the planner probe one of them and know the columns of
        all of them."""
        resolved = await aggregate_service.record_sources(
            _entry(datasource, root(active_clients(binding_mode="each"))),
        )

        assert {source.table_name for source in resolved.sources} == {"projects"}
        assert {source.is_sql_mode for source in resolved.sources} == {False}

    async def test_the_alias_is_carried_as_each_source_s_label(
        self, datasource: SimpleNamespace, root: Callable, active_clients: Callable,
    ) -> None:
        """The fold groups by it like any other column, so it has to arrive on the
        rows — which is what a source's label does."""
        resolved = await aggregate_service.record_sources(
            _entry(
                datasource,
                root(active_clients(binding_mode="each", value_alias="client")),
            ),
        )

        assert [source.label for source in resolved.sources] == [
            {"client": 1}, {"client": 2}, {"client": 4},
        ]

    async def test_no_alias_means_no_label(
        self, datasource: SimpleNamespace, root: Callable, active_clients: Callable,
    ) -> None:
        resolved = await aggregate_service.record_sources(
            _entry(datasource, root(active_clients(binding_mode="each"))),
        )

        assert all(source.label is None for source in resolved.sources)

    async def test_more_values_than_the_cap_is_refused(
        self,
        datasource: SimpleNamespace,
        root: Callable,
        active_clients: Callable,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Refused before a single record is read, and refused rather than sampled:
        three clients' totals reported as every client's is a plausible wrong number.
        """
        monkeypatch.setattr(aggregate_service, "MAX_CHAIN_ITERATIONS", 2)

        with pytest.raises(ToolQueryError, match="more than the 2 runs"):
            await aggregate_service.record_sources(
                _entry(datasource, root(active_clients(binding_mode="each"))),
            )


class TestReadingThemBackAsOne:
    async def test_the_sources_read_as_the_tool_s_whole_result_set(
        self, datasource: SimpleNamespace, root: Callable, active_clients: Callable,
    ) -> None:
        """
        End to end through the reader: three runs, four rows between them, and the
        client each one came from written alongside. Client 3 is churned so its
        project is absent, and client 4 has none — so a run that ignored either
        would come back with a different set.
        """
        from app.services.downloader_agents.base import record_reader

        resolved = await aggregate_service.record_sources(
            _entry(
                datasource,
                root(active_clients(binding_mode="each", value_alias="client")),
            ),
        )

        chained = record_reader.ChainedBatchReader(resolved.sources, batch_size=50)
        rows: list = []
        number = 1

        try:
            while True:
                batch = await chained.read(number)
                if not batch:
                    break
                rows.extend(batch)
                number += 1
        finally:
            await chained.close()

        assert sorted((row["revenue"], row["client"]) for row in rows) == [
            (30, 1), (50, 2), (100, 1),
        ]

    async def test_the_total_is_summed_across_every_source(
        self, datasource: SimpleNamespace, root: Callable, active_clients: Callable,
    ) -> None:
        from app.services.downloader_agents.base import record_reader

        resolved = await aggregate_service.record_sources(
            _entry(datasource, root(active_clients(binding_mode="each"))),
        )

        counted = await record_reader.count_all(resolved.sources)

        assert counted.total == 3
        assert counted.is_lower_bound is False


class TestAggregatingAcrossTheFanOut:
    """
    The claim the whole feature makes: the totals are over **every** record of every
    iteration, and they equal what the database would say.

    Checked against SQLite running the equivalent ``GROUP BY`` directly, because the
    only thing worth asserting about an aggregate is that it is right — a test that
    compared the fold against another fold would agree with itself.
    """

    async def test_the_totals_match_a_group_by_run_by_the_database(
        self,
        warehouse: Path,
        datasource: SimpleNamespace,
        root: Callable,
        active_clients: Callable,
    ) -> None:
        from app.services.agent_recursive_dataframes import aggregate_graph

        resolved = await aggregate_service.record_sources(
            _entry(
                datasource,
                root(active_clients(binding_mode="each", value_alias="client")),
            ),
        )

        plan = {
            "group_by": ["client"],
            "aggregations": [
                {"type": "count", "column": "", "alias": "record_count"},
                {"type": "sum", "column": "revenue", "alias": "sum_revenue"},
            ],
        }

        outcome = await aggregate_graph.run_aggregation(
            row_supply.for_sources(resolved.sources), plan, "fan-out-under-test",
        )

        connection = sqlite3.connect(warehouse)
        expected = {
            client: (count, total)
            for client, count, total in connection.execute(
                "SELECT client_id, COUNT(*), SUM(revenue) FROM projects "
                "WHERE client_id IN (SELECT id FROM clients WHERE status = 'active') "
                "GROUP BY client_id"
            )
        }
        connection.close()

        assert {
            row["client"]: (row["record_count"], row["sum_revenue"])
            for row in outcome["rows"]
        } == expected

    async def test_it_reads_every_record_across_every_iteration(
        self, datasource: SimpleNamespace, root: Callable, active_clients: Callable,
    ) -> None:
        """
        Three records over three runs, one of which contributes none. Reading only
        the first source would report one, and reading only the ones that matched
        would still be three — so the count is asserted against the total, which is
        the number the answer is allowed to claim.
        """
        from app.services.agent_recursive_dataframes import aggregate_graph

        resolved = await aggregate_service.record_sources(
            _entry(datasource, root(active_clients(binding_mode="each"))),
        )

        outcome = await aggregate_graph.run_aggregation(
            row_supply.for_sources(resolved.sources),
            {
                "group_by": [],
                "aggregations": [
                    {"type": "count", "column": "", "alias": "record_count"},
                ],
            },
            "fan-out-count",
        )

        assert outcome["records_read"] == 3
        assert outcome["total_records"] == 3
        assert outcome["rows"] == [{"record_count": 3}]

    async def test_a_chain_that_matched_nothing_is_an_answer_not_a_failure(
        self, datasource: SimpleNamespace, root: Callable, active_clients: Callable,
    ) -> None:
        """
        "No clients matched" and "those clients have no projects" are two different
        things, and a bare zero is neither.
        """
        child = active_clients(binding_mode="each")
        child.tool.config["filters"] = [
            {"column": "status", "operator": "=", "value": "dissolved"},
        ]

        resolved = await aggregate_service.record_sources(
            _entry(datasource, root(child)),
        )

        assert resolved.short_circuited
        assert "active_clients" in aggregate_service.chain_stopped_message(
            resolved.stopped_by,
        )
        assert "not a failure" in aggregate_service.chain_stopped_message(
            resolved.stopped_by,
        )
