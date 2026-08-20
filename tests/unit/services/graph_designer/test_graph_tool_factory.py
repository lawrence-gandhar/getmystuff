"""
Tests for the seam between the Graph Designer and the agent runtime —
``graph_tool_factory``, and the three additive edits in ``deep_agents`` it depends on.

The property that carries this file is **the one-list guarantee**:
``documentations/TOOL_CHAINING.md`` requires the routing prompt and the callable tool list
to be built from a single list, because two lists can describe different sets — an agent
told about something it cannot call, or handed something the prompt never mentioned. So the
central test compares the names in the prompt against the names of the built tools.

After that, in order:

* **Both switches are required.** A graph appears only when it is attached *and* published,
  so a draft can sit attached while it is finished and a finished one can be parked.
* **A question is relayed, not paraphrased.** The tool returns the operator's exact text
  plus the run id, the ``offer_sentence`` rule — a model rewording a question asks the user
  the wrong thing.
* **An answer that does not fit is not a tool failure.** It is the one failure on this path
  the user can fix, so the model is told to ask again rather than told that nothing they
  say can help.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

pytest.importorskip("langgraph", reason="LangGraph is installed in the container only")

from app.models.data_agents import DataAgent  # noqa: E402
from app.models.datasource import DataSource  # noqa: E402
from app.models.graph_designer import ToolGraph  # noqa: E402
from app.services.deep_agents.prompt_builder import build_tool_routing_prompt  # noqa: E402
from app.services.deep_agents.prompt_sync_service import collect_agent_tools  # noqa: E402
from app.services.deep_agents.tool_factory import (  # noqa: E402
    build_agent_tools,
    find_unsupported_tools,
)
from app.services.graph_designer import graph_service  # noqa: E402


@pytest.fixture
async def agent(db, user):  # noqa: ANN001, ANN201
    row = DataAgent(user_id=user.id, name=f"agent-{uuid_pkg.uuid4().hex[:6]}", is_active=True)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
async def datasource(db, user, tmp_path):  # noqa: ANN001, ANN201
    import sqlite3

    path = tmp_path / "dept.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE departments (id INTEGER PRIMARY KEY, name TEXT);"
        "INSERT INTO departments VALUES (1, 'Eng'), (2, 'Sales');"
    )
    connection.commit()
    connection.close()

    row = DataSource(
        user_id=user.id,
        datasource_name=f"dept-{uuid_pkg.uuid4().hex[:6]}",
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


def graph_data(datasource, *, asks: bool = False, params: list | None = None) -> dict:
    nodes = [
        {"id": "s", "type": "start", "position": {}, "data": {"label": "Start"}},
        {
            "id": "q", "type": "sql", "position": {},
            "data": {
                "label": "departments",
                "datasource_id": str(datasource.uuid),
                "table_names": ["departments"],
                "sql_query": (
                    "SELECT id, name FROM departments WHERE name = :wanted"
                    if params else "SELECT id, name FROM departments"
                ),
                "params": params or [],
            },
        },
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
        edges.append({
            "id": "e2", "source": "q", "source_port": "default", "target": "ask",
        })
        nodes.append({"id": "ok", "type": "success", "position": {}, "data": {}})
        edges.append({
            "id": "e3", "source": "ask", "source_port": "default", "target": "ok",
        })
    else:
        nodes.append({"id": "ok", "type": "success", "position": {}, "data": {}})
        edges.append({
            "id": "e2", "source": "q", "source_port": "default", "target": "ok",
        })

    return {"nodes": nodes, "edges": edges}


@pytest.fixture
def attach(db, user, agent):  # noqa: ANN001, ANN201
    """A graph, published and attached — the state in which an agent may call it."""
    async def _attach(data: dict, name: str = "Dept lookup", **kwargs) -> ToolGraph:
        graph = await graph_service.create_graph(
            db, user.id, f"{name} {uuid_pkg.uuid4().hex[:4]}", "Lists departments.",
        )
        await graph_service.save_graph(db, user.id, graph.uuid, data)

        if kwargs.get("publish", True):
            await graph_service.set_graph_active(db, user.id, graph.uuid, True)
            await graph_service.attach_graph(db, user.id, graph.uuid, agent.uuid)

        return graph

    return _attach


class TestBothSwitchesAreRequired:
    async def test_an_unattached_graph_is_not_offered(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        await attach(graph_data(datasource), publish=False)

        assert await collect_agent_tools(db, agent.id) == []

    async def test_an_attached_published_graph_is_offered(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        await attach(graph_data(datasource))

        entries = await collect_agent_tools(db, agent.id)

        assert [entry.get("kind") for entry in entries] == ["graph"]

    async def test_making_it_a_draft_withdraws_it_without_detaching(
        self, db, user, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        """
        The point of the second switch: a graph can be parked mid-edit and the agent simply
        stops calling it, rather than the operator having to detach and reattach.
        """
        graph = await attach(graph_data(datasource))

        await graph_service.set_graph_active(db, user.id, graph.uuid, False)

        assert await collect_agent_tools(db, agent.id) == []

        refreshed = await graph_service.get_graph(db, user.id, graph.uuid)
        assert refreshed.data_agent_id == agent.id, "still attached"

    async def test_attaching_a_draft_is_refused(
        self, db, user, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        """
        Refused rather than accepted-and-ignored: a control that appears to work and does
        nothing is worse than one that says no.
        """
        from litestar.exceptions import HTTPException

        graph = await attach(graph_data(datasource), publish=False)

        with pytest.raises(HTTPException) as caught:
            await graph_service.attach_graph(db, user.id, graph.uuid, agent.uuid)

        assert "still a draft" in caught.value.detail


class TestTheOneListGuarantee:
    async def test_every_tool_the_prompt_names_is_callable(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        """
        The guarantee the whole design rests on. A name in the prompt that is not in the
        built list is an agent told about something it cannot call.
        """
        await attach(graph_data(datasource, asks=True))

        entries = await collect_agent_tools(db, agent.id)
        prompt = build_tool_routing_prompt(agent.name, entries)
        callable_names = {tool.name for tool in build_agent_tools(entries)}

        named = {
            line[3:].strip() for line in prompt.splitlines() if line.startswith("## ")
        }

        assert named
        assert named <= callable_names

    async def test_the_graph_contributes_its_answer_tool_only_when_it_asks(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        """
        A graph that never pauses gets exactly one new tool. Offering an answering tool for
        a graph that cannot be answered would be a tool that refuses every call.
        """
        await attach(graph_data(datasource, asks=False))

        entries = await collect_agent_tools(db, agent.id)
        names = [tool.name for tool in build_agent_tools(entries)]

        assert len(names) == 1
        assert not any(name.startswith("answer_") for name in names)

    async def test_a_graph_that_asks_contributes_two(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        await attach(graph_data(datasource, asks=True))

        entries = await collect_agent_tools(db, agent.id)
        names = sorted(tool.name for tool in build_agent_tools(entries))

        assert len(names) == 2
        assert names[0].startswith("answer_")

    async def test_editing_a_graph_makes_the_stored_prompt_stale(
        self, db, user, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        """
        Because the entry carries ``updated_at``, ``is_prompt_stale`` already invalidates
        the prompt when a graph changes — no new staleness path had to be written. Asserted
        because that is a load-bearing consequence of one field, and easy to drop.
        """
        from app.services.deep_agents.prompt_sync_service import newest_tool_change

        graph = await attach(graph_data(datasource))
        before = newest_tool_change(await collect_agent_tools(db, agent.id))

        await graph_service.rename_graph(db, user.id, graph.uuid, "Renamed thing", "new")
        after = newest_tool_change(await collect_agent_tools(db, agent.id))

        assert before is not None and after is not None
        assert after >= before

    async def test_a_graph_is_not_reported_as_an_unsupported_tool(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        """
        A graph has no single datasource, so the relational check means nothing for it.
        Without the skip it would be flagged "not a relational datasource" on every agent
        console — wrong, and alarming.
        """
        await attach(graph_data(datasource))

        entries = await collect_agent_tools(db, agent.id)

        assert find_unsupported_tools(entries) == []


class TestToolNaming:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Monthly revenue check", "monthly_revenue_check"),
            ("  spaced  out  ", "spaced_out"),
            ("Q1 report", "q1_report"),
            ("2026 numbers", "graph_2026_numbers"),
            ("!!!", "saved_graph"),
            ("Ünïcode name", "ünïcode_name"),
        ],
    )
    def test_a_human_name_becomes_an_identifier_a_model_can_call(
        self, name: str, expected: str,
    ) -> None:
        """
        A graph is named by a person; a tool name has to be one token. A name a model
        cannot address is a tool it cannot use.
        """
        from app.services.deep_agents.prompt_sync_service import _graph_tool_name

        assert _graph_tool_name(name) == expected


class TestPromptWording:
    async def test_says_the_graph_is_a_sequence_not_one_query(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        await attach(graph_data(datasource))

        entries = await collect_agent_tools(db, agent.id)
        prompt = build_tool_routing_prompt(agent.name, entries)

        assert "not a single query" in prompt

    async def test_requires_a_question_to_be_relayed_word_for_word(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        """
        A model rewording a question asks the user the wrong thing, and a paraphrased
        question makes the next turn's answer unmatchable — ``offer_sentence``'s reason.
        """
        await attach(graph_data(datasource, asks=True))

        entries = await collect_agent_tools(db, agent.id)
        prompt = build_tool_routing_prompt(agent.name, entries)

        assert "word for word" in prompt
        assert "not a failure" in prompt

    async def test_names_the_declared_parameters(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        await attach(graph_data(
            datasource,
            params=[{"param": "wanted", "type": "text", "required": True}],
        ))

        entries = await collect_agent_tools(db, agent.id)
        prompt = build_tool_routing_prompt(agent.name, entries)

        assert "`wanted`" in prompt

    async def test_a_union_nodes_parameters_are_offered_to_the_agent_too(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        """
        A ``sql_union`` node declares parameters in the same shape a ``sql`` node does, and an
        unwired one is filled from the run's inputs the same way — so it has to reach the
        agent's argument list as well.

        Left out, the parameter would be fillable from the test panel and not from a
        conversation: the graph would work when the operator tried it and refuse every call
        the agent made, for a reason visible in neither place.
        """
        data = graph_data(datasource)

        # A union node only saves inside a For each body, so the fixture builds one:
        # `q → loop`, the union in the body with a way back, and `execute` to Success.
        data["edges"] = [
            edge for edge in data["edges"] if edge["target"] != "ok"
        ]
        data["nodes"] += [
            {
                "id": "loop", "type": "for_each", "position": {},
                "data": {"label": "each dept", "source_node": "q", "item_name": "dept"},
            },
            {
                "id": "u", "type": "sql_union", "position": {},
                "data": {
                    "label": "per department",
                    "datasource_id": str(datasource.uuid),
                    "table_names": ["departments"],
                    "sql_query": "SELECT id FROM departments WHERE name = :only",
                    "params": [{"param": "only", "type": "text", "required": True}],
                },
            },
        ]
        data["edges"] += [
            {"id": "e10", "source": "q", "source_port": "default", "target": "loop"},
            {"id": "e11", "source": "loop", "source_port": "body", "target": "u"},
            {"id": "e12", "source": "u", "source_port": "default", "target": "loop"},
            {"id": "e13", "source": "u", "source_port": "execute", "target": "ok"},
            {"id": "e14", "source": "loop", "source_port": "done", "target": "ok"},
        ]

        await attach(data)

        entries = await collect_agent_tools(db, agent.id)

        assert [
            param["param"] for param in (entries[0].get("sql_params") or [])
        ] == ["only"]

    async def test_says_it_takes_no_arguments_when_it_declares_none(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        await attach(graph_data(datasource))

        entries = await collect_agent_tools(db, agent.id)
        prompt = build_tool_routing_prompt(agent.name, entries)

        assert "Takes no arguments." in prompt


class TestCallingTheTool:
    async def test_a_paused_graph_returns_the_question_and_the_run_id(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        await attach(graph_data(datasource, asks=True))

        entries = await collect_agent_tools(db, agent.id)
        tools = {tool.name: tool for tool in build_agent_tools(entries)}
        graph_tool = next(name for name in tools if not name.startswith("answer_"))

        output = await tools[graph_tool].coroutine()

        assert "Shall I include archived ones?" in output
        assert "word for word" in output
        assert "run_id" in output

    async def test_answering_resumes_and_reports_the_real_rows(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        """
        The end of the whole path: the graph's SQL rows come back through
        ``describe_result``, not the Success node's bookkeeping. That distinction was
        observed reporting a graph which read two departments as returning nothing.
        """
        import re

        await attach(graph_data(datasource, asks=True))

        entries = await collect_agent_tools(db, agent.id)
        tools = {tool.name: tool for tool in build_agent_tools(entries)}
        graph_tool = next(name for name in tools if not name.startswith("answer_"))

        asked = await tools[graph_tool].coroutine()
        run_id = re.search(r'run_id "([0-9a-f-]+)"', asked).group(1)

        answered = await tools[f"answer_{graph_tool}"].coroutine(
            run_id=run_id, answer="yes",
        )

        assert "2 row(s)" in answered
        assert "Sales" in answered

    async def test_an_answer_that_does_not_fit_asks_again_rather_than_giving_up(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        """
        The one failure on this path the user can fix. Reporting it as a tool failure would
        tell the model that nothing they say can change it and that an operator has to look
        at it — observed doing exactly that before this branch existed.
        """
        import re

        await attach(graph_data(datasource, asks=True))

        entries = await collect_agent_tools(db, agent.id)
        tools = {tool.name: tool for tool in build_agent_tools(entries)}
        graph_tool = next(name for name in tools if not name.startswith("answer_"))

        asked = await tools[graph_tool].coroutine()
        run_id = re.search(r'run_id "([0-9a-f-]+)"', asked).group(1)

        output = await tools[f"answer_{graph_tool}"].coroutine(
            run_id=run_id, answer="maybe",
        )

        assert "Ask the user again" in output
        assert "TOOL FAILED" not in output

    async def test_an_invented_run_id_is_refused_without_a_stack_trace(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        await attach(graph_data(datasource, asks=True))

        entries = await collect_agent_tools(db, agent.id)
        tools = {tool.name: tool for tool in build_agent_tools(entries)}
        answer_tool = next(name for name in tools if name.startswith("answer_"))

        output = await tools[answer_tool].coroutine(run_id="not-a-uuid", answer="yes")

        assert "not a run id" in output

    async def test_a_graph_that_finishes_reports_its_rows(
        self, db, agent, datasource, attach,
    ) -> None:  # noqa: ANN001
        await attach(graph_data(datasource))

        entries = await collect_agent_tools(db, agent.id)
        tools = {tool.name: tool for tool in build_agent_tools(entries)}
        graph_tool = next(iter(tools))

        output = await tools[graph_tool].coroutine()

        assert "2 row(s)" in output
