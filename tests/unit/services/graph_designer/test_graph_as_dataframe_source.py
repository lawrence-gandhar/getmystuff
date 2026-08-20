"""
A published graph as a source the polars pipeline can read, filter and total.

This is the answer to a real complaint: an agent whose only tool returns every month's
revenue, asked about March, replies "I cannot filter by month because the tool does not
accept a date parameter". That is true of the *graph* and false of what the agent can do —
the records can be filtered after they are read. What was missing was the path, and these
tests are about the two halves of it.

**Why this file lives in the graph_designer test package.** Reading a graph means *running*
it, which needs the three autouse fixtures in this package's ``conftest`` — the session
redirect, the in-memory checkpointer and the run canceller. Without them a test either
writes to the development database or trips the network guard with an error about sockets.
That is the same reason ``test_chain_graph_child.py`` was moved here.

The properties, in the order they matter:

* **the whole result, never the preview.** ``GraphOutcome.rows`` is capped at twenty; a
  filter over twenty of sixty records reports a count that is wrong and looks right. So the
  test that carries the file uses more records than the preview holds.
* **the opt-in is required.** A published graph an agent can call is not automatically one
  whose entire result may be read.
* **a pause is refused, with the fix named.** A graph that stops to ask something cannot be
  read in one step, and the refusal has to point at the graph's own tool, which can.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any, Dict, List

import pytest

pytest.importorskip("langgraph", reason="LangGraph is installed in the container only")

from app.models.data_agents import DataAgent  # noqa: E402
from app.models.datasource import DataSource  # noqa: E402
from app.services.agent_recursive_dataframes import (  # noqa: E402
    aggregate_service,
    filter_algebra as fa,
)
from app.services.deep_agents.prompt_sync_service import collect_agent_tools  # noqa: E402
from app.services.deep_agents.query_executor import ToolQueryError  # noqa: E402
from app.services.graph_designer import graph_service  # noqa: E402

#: The ledger, written out rather than generated from ``index % n``.
#:
#: The first attempt at this fixture derived the department from ``index % 3`` and the month
#: from ``index % 12``, and no Python row could ever fall in March: 3 divides 12, so the two
#: cycles are locked together and only a third of the combinations exist. The tests failed
#: with zero matching records and the *code* was fine. Cycles of 3 and 5 are coprime, so
#: every department/month pair occurs — and both the fixture and the expected figures are
#: read off this one list, so neither can drift from the other.
DEPARTMENTS = ("Python", "Rust", "Go")

LEDGER = [
    {
        "id": index,
        "department": DEPARTMENTS[index % 3],
        "revenue": 100.0 + index,
        "invoice_date": f"2026-{(index % 5) + 1:02d}-15",
    }
    for index in range(1, 61)
]

#: More records than ``result_preview`` holds, which is twenty. The gap is the whole point:
#: a filter built from the preview would silently be a filter over a sample.
LEDGER_ROWS = len(LEDGER)


def expected_sum(department: str, month: int) -> float:
    """What the answer should be, computed from the same list that seeded the database."""
    return sum(
        row["revenue"] for row in LEDGER
        if row["department"] == department
        and int(row["invoice_date"][5:7]) == month
    )


def expected_count(department: str, month: int) -> int:
    return sum(
        1 for row in LEDGER
        if row["department"] == department
        and int(row["invoice_date"][5:7]) == month
    )


@pytest.fixture
async def datasource(db, user, tmp_path):  # noqa: ANN001, ANN201
    """A ledger of revenue by department and month, deliberately larger than a preview."""
    import sqlite3

    path = tmp_path / "ledger.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE ledger (id INTEGER PRIMARY KEY, department TEXT, "
        "revenue REAL, invoice_date TEXT);"
    )
    connection.executemany(
        "INSERT INTO ledger (id, department, revenue, invoice_date) VALUES (?,?,?,?)",
        [
            (row["id"], row["department"], row["revenue"], row["invoice_date"])
            for row in LEDGER
        ],
    )
    connection.commit()
    connection.close()

    row = DataSource(
        user_id=user.id,
        datasource_name=f"ledger-{uuid_pkg.uuid4().hex[:6]}",
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


def ledger_graph(datasource, *, asks: bool = False) -> dict:
    """A graph that returns the whole ledger, optionally stopping to ask something."""
    nodes = [
        {"id": "s", "type": "start", "position": {}, "data": {"label": "Start"}},
        {
            "id": "q", "type": "sql", "position": {},
            "data": {
                "label": "ledger",
                "datasource_id": str(datasource.uuid),
                "table_names": ["ledger"],
                "sql_query": (
                    "SELECT department, revenue, invoice_date FROM ledger ORDER BY id"
                ),
            },
        },
        {"id": "ok", "type": "success", "position": {}, "data": {}},
    ]
    edges = [
        {"id": "e1", "source": "s", "source_port": "default", "target": "q"},
        {"id": "e2", "source": "q", "source_port": "default", "target": "ok"},
    ]

    if asks:
        nodes.insert(1, {
            "id": "ask", "type": "human", "position": {},
            "data": {
                "label": "Confirm",
                "prompt": "Include internal transfers?",
                "expects": "confirm",
            },
        })
        edges[0] = {"id": "e1", "source": "s", "source_port": "default", "target": "ask"}
        edges.insert(1, {
            "id": "e0", "source": "ask", "source_port": "default", "target": "q",
        })

    return {"nodes": nodes, "edges": edges}


@pytest.fixture
def published(db, user, agent, datasource):  # noqa: ANN001, ANN201
    """A published graph attached to the agent, opted in unless told otherwise."""
    async def _publish(*, readable: bool = True, asks: bool = False):
        graph = await graph_service.create_graph(
            db, user.id, f"ledger-{uuid_pkg.uuid4().hex[:6]}", "Revenue by department.",
        )
        await graph_service.save_graph(
            db, user.id, graph.uuid, ledger_graph(datasource, asks=asks),
        )
        await graph_service.set_graph_active(db, user.id, graph.uuid, True)
        await graph_service.attach_graph(db, user.id, graph.uuid, agent.uuid)

        if readable:
            await graph_service.update_graph(
                db, user.id, graph.uuid, graph.name, graph.description,
                agent_id=agent.uuid, allow_recursive_aggregate=True,
            )

        return graph

    return _publish


async def entries(db, agent) -> List[Dict[str, Any]]:  # noqa: ANN001
    return await collect_agent_tools(db, agent.id)


class StubModel:
    """
    A model that returns one prepared plan. The planner's job is tested elsewhere; what
    is under test here is that a *validated* plan reaches a graph's records.
    """

    def __init__(self, plan: Any) -> None:
        self.plan = plan
        self.prompts: List[Any] = []

    def with_structured_output(self, _schema):  # noqa: ANN001, ANN202
        return self

    async def ainvoke(self, messages, config=None):  # noqa: ANN001, ANN202
        self.prompts.append(messages)

        if isinstance(self.plan, Exception):
            raise self.plan

        return self.plan


class TestTheOptIn:
    async def test_a_graph_that_is_not_opted_in_is_not_readable(
        self, db, user, agent, published,
    ) -> None:  # noqa: ANN001
        await published(readable=False)

        assert aggregate_service.readable_tools(await entries(db, agent)) == []

    async def test_an_opted_in_graph_is_readable(
        self, db, user, agent, published,
    ) -> None:  # noqa: ANN001
        graph = await published()

        readable = aggregate_service.readable_tools(await entries(db, agent))

        assert len(readable) == 1
        assert readable[0]["kind"] == "graph"
        assert readable[0]["graph_uuid"] == str(graph.uuid)

    async def test_the_flag_is_under_the_same_key_a_tool_config_uses(
        self, db, user, agent, published,
    ) -> None:  # noqa: ANN001
        """
        One key for both kinds, so ``readable_tools`` is one expression. Two keys would be
        two things to remember, and the forgotten one would opt nothing in.
        """
        await published()
        entry = (await entries(db, agent))[0]

        assert entry["allow_recursive_aggregate"] is True

    async def test_the_public_id_of_a_graph_entry_is_its_graph_uuid(
        self, db, user, agent, published,
    ) -> None:  # noqa: ANN001
        graph = await published()
        entry = (await entries(db, agent))[0]

        assert aggregate_service.public_id(entry) == str(graph.uuid)
        assert "uuid" not in entry


class TestReadingAGraphsWholeResult:
    async def test_the_records_read_are_the_whole_result_not_the_preview(
        self, db, user, agent, published,
    ) -> None:  # noqa: ANN001
        """
        The test that carries the file. ``result_preview`` holds twenty rows; this graph
        returns sixty. A pipeline reading the preview would report totals over a third of
        the ledger and there would be nothing in the answer to say so.
        """
        await published()
        model = StubModel({
            "group_by": [],
            "aggregations": [{"type": "count", "column": ""}],
        })

        outcome = await aggregate_service.aggregate(
            aggregate_service.readable_tools(await entries(db, agent)),
            "how many records are there",
            model,
        )

        assert outcome["total_records"] == LEDGER_ROWS
        assert outcome["records_read"] == LEDGER_ROWS
        assert outcome["rows"][0]["record_count"] == LEDGER_ROWS

    async def test_a_filtered_total_is_over_the_matching_records_only(
        self, db, user, agent, published,
    ) -> None:  # noqa: ANN001
        """The user's own question: one department, one month."""
        await published()
        model = StubModel({
            "group_by": [],
            "aggregations": [{"type": "sum", "column": "revenue"}],
            "filters": [
                {"column": "department", "operator": "==", "value": "Python"},
                {"column": "invoice_date", "part": "month", "operator": "==", "value": "3"},
            ],
        })

        outcome = await aggregate_service.aggregate(
            aggregate_service.readable_tools(await entries(db, agent)),
            "revenue for the Python department in March",
            model,
        )

        assert outcome["rows"][0]["sum_revenue"] == pytest.approx(
            expected_sum("Python", 3),
        )
        assert outcome["records_read"] == LEDGER_ROWS

    async def test_a_filter_with_no_measure_returns_the_matching_records(
        self, db, user, agent, published,
    ) -> None:  # noqa: ANN001
        await published()
        model = StubModel({
            "group_by": [],
            "aggregations": [],
            "filters": [
                {"column": "department", "operator": "==", "value": "Python"},
                {"column": "invoice_date", "part": "month", "operator": "==", "value": "3"},
            ],
        })

        outcome = await aggregate_service.aggregate(
            aggregate_service.readable_tools(await entries(db, agent)),
            "show me the Python department's March revenue",
            model,
        )

        matching = expected_count("Python", 3)

        assert outcome["mode"] == fa.MODE_ROWS
        assert matching > 1                       # or the assertions below prove nothing
        assert len(outcome["rows"]) == matching
        assert {row["department"] for row in outcome["rows"]} == {"Python"}
        assert {row["invoice_date"][5:7] for row in outcome["rows"]} == {"03"}
        # The "out of" number is how many matched, not how many were read.
        assert outcome["group_count"] == matching
        assert outcome["total_records"] == LEDGER_ROWS

    async def test_the_summary_names_the_conditions_that_ran(
        self, db, user, agent, published,
    ) -> None:  # noqa: ANN001
        """
        A filtered figure that does not say what it was filtered by is right about a set
        the reader has to guess at. If the model narrowed further than anybody asked, this
        sentence is where it shows.
        """
        await published()
        model = StubModel({
            "group_by": [],
            "aggregations": [{"type": "sum", "column": "revenue"}],
            "filters": [
                {"column": "department", "operator": "==", "value": "Python"},
                {"column": "invoice_date", "part": "month", "operator": "==", "value": "3"},
            ],
        })

        outcome = await aggregate_service.aggregate(
            aggregate_service.readable_tools(await entries(db, agent)),
            "revenue for the Python department in March",
            model,
        )

        assert "department == Python" in outcome["summary"]
        assert "the month of invoice_date == 3" in outcome["summary"]

    async def test_the_planner_is_shown_the_columns_the_graph_returns(
        self, db, user, agent, published,
    ) -> None:  # noqa: ANN001
        """
        There is nothing to probe on a drawing, so the columns come off the result it
        produced. A model shown the wrong names invents a filter that cannot be resolved.
        """
        await published()
        model = StubModel({
            "group_by": [],
            "aggregations": [{"type": "count", "column": ""}],
        })

        await aggregate_service.aggregate(
            aggregate_service.readable_tools(await entries(db, agent)),
            "how many",
            model,
        )
        sent = str(model.prompts[0])

        for column in ("department", "revenue", "invoice_date"):
            assert column in sent

    async def test_a_column_the_graph_does_not_return_is_refused_by_name(
        self, db, user, agent, published,
    ) -> None:  # noqa: ANN001
        await published()
        model = StubModel({
            "group_by": [],
            "aggregations": [{"type": "sum", "column": "profit"}],
        })

        with pytest.raises(ToolQueryError) as caught:
            await aggregate_service.aggregate(
                aggregate_service.readable_tools(await entries(db, agent)),
                "total profit",
                model,
            )

        assert "profit" in str(caught.value)
        assert "revenue" in str(caught.value)


class TestAGraphThatStopsToAsk:
    async def test_it_is_refused_and_the_graphs_own_tool_is_named(
        self, db, user, agent, published,
    ) -> None:  # noqa: ANN001
        """
        Resuming would mean holding a half-read result set across two conversation turns,
        which is a second kind of state for a feature whose whole shape is "read it all
        now". So it is refused — and the refusal points at the graph's own tool, which
        does carry the pause.
        """
        graph = await published(asks=True)
        model = StubModel({
            "group_by": [],
            "aggregations": [{"type": "count", "column": ""}],
        })

        with pytest.raises(ToolQueryError) as caught:
            await aggregate_service.aggregate(
                aggregate_service.readable_tools(await entries(db, agent)),
                "how many records",
                model,
            )

        message = str(caught.value)

        assert "stops part-way through to ask" in message
        assert "Include internal transfers?" in message
        assert graph.name.replace("-", "_") in message


class TestAGraphThatDoesNotFinish:
    async def test_a_failed_graph_is_a_refusal_naming_it_and_the_reason(
        self, db, user, agent, published, monkeypatch,
    ) -> None:  # noqa: ANN001
        """
        A graph run reports failure as an *outcome* rather than raising, so every one of
        its four owners can phrase it. This owner's phrasing is a refusal — and it has to
        carry the reason, because "the graph did not finish" on its own sends the operator
        to look at the wrong thing.
        """
        await published()
        model = StubModel({
            "group_by": [],
            "aggregations": [{"type": "count", "column": ""}],
        })

        from app.services.graph_designer import graph_runner

        async def broken(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return graph_runner.GraphOutcome(
                graph_runner.OUTCOME_FAILED, reason="departments is not a table",
            )

        monkeypatch.setattr(graph_runner, "run_graph", broken)

        with pytest.raises(ToolQueryError) as caught:
            await aggregate_service.aggregate(
                aggregate_service.readable_tools(await entries(db, agent)),
                "how many records",
                model,
            )

        message = str(caught.value)

        assert "did not finish" in message
        assert "departments is not a table" in message

    async def test_a_graph_returning_nothing_is_an_answer_not_a_failure(
        self, db, user, agent, published, monkeypatch,
    ) -> None:  # noqa: ANN001
        """
        No records is a fact somebody can act on. It comes back as an empty result with a
        sentence, exactly as an empty tool result does.
        """
        await published()
        model = StubModel({
            "group_by": [],
            "aggregations": [{"type": "count", "column": ""}],
        })

        async def nothing(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return []

        monkeypatch.setattr(aggregate_service, "graph_rows", nothing)

        outcome = await aggregate_service.aggregate(
            aggregate_service.readable_tools(await entries(db, agent)),
            "how many records",
            model,
        )

        assert outcome["rows"] == []
        assert outcome["total_records"] == 0

    async def test_a_filter_matching_nothing_says_how_many_were_read(
        self, db, user, agent, published,
    ) -> None:  # noqa: ANN001
        """
        "There are no records" and "there are 60 records and none is in December" are
        different facts, and a bare empty result is neither.
        """
        await published()
        model = StubModel({
            "group_by": [],
            "aggregations": [],
            "filters": [
                {"column": "department", "operator": "==", "value": "Haskell"},
            ],
        })

        outcome = await aggregate_service.aggregate(
            aggregate_service.readable_tools(await entries(db, agent)),
            "show me the Haskell department's revenue",
            model,
        )

        assert outcome["rows"] == []
        assert outcome["group_count"] == 0
        assert outcome["records_read"] == LEDGER_ROWS
        assert "none of them matched" in aggregate_service.nothing_matched_message(
            outcome["records_read"],
        )


class TestTheCeilingStillApplies:
    async def test_a_result_past_the_ceiling_is_refused_naming_the_graph(
        self, db, user, agent, published, monkeypatch,
    ) -> None:  # noqa: ANN001
        """
        The rows are already in memory, but folding more of them than a conversation turn
        allows still cannot finish — so the same ceiling refuses, with advice pointing at
        the graph's own query nodes rather than at a tool's filters.
        """
        await published()
        monkeypatch.setattr(aggregate_service, "AGGREGATE_MAX_SOURCE_ROWS", 5)
        model = StubModel({
            "group_by": [],
            "aggregations": [{"type": "count", "column": ""}],
        })

        with pytest.raises(ToolQueryError) as caught:
            await aggregate_service.aggregate(
                aggregate_service.readable_tools(await entries(db, agent)),
                "how many records",
                model,
            )

        message = str(caught.value)

        assert "This graph returns 60 records" in message
        assert "graph's own query nodes" in message


class TestShapesOtherThanRows:
    """
    A graph's last node need not produce records. A list of ids is what a loop's source
    node returns, and "the departments it picked, filtered to the ones starting with P" is
    a reasonable thing to ask — so a list is lifted into one-column records rather than
    refused.
    """

    def test_a_list_of_values_becomes_one_column_records(self) -> None:
        assert aggregate_service._as_records([1, 2]) == [
            {"value": 1}, {"value": 2},
        ]

    def test_a_rows_envelope_is_unwrapped(self) -> None:
        assert aggregate_service._as_records({"rows": [{"a": 1}]}) == [{"a": 1}]

    def test_a_bare_dict_is_one_record(self) -> None:
        assert aggregate_service._as_records({"a": 1}) == [{"a": 1}]

    def test_a_scalar_is_one_record(self) -> None:
        assert aggregate_service._as_records(7) == [{"value": 7}]

    def test_nothing_at_all_is_no_records(self) -> None:
        assert aggregate_service._as_records(None) == []
