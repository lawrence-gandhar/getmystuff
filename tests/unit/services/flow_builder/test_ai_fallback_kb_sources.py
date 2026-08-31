"""
Tests for the two live knowledge-base source kinds an AI Fallback node's
``context_source == "knowledge_base"`` branch composes alongside its uploaded
documents: attached **pipelines** and **tool configs**, both run fresh on every
visitor message and neither ever written to the vector store.

DB-free, like the neighbouring ``test_engine_ai_fallback.py``: every seam that
touches the database or another service (``graph_runner``, ``tool_config_service``,
``tool_chain_service``, ``execute_tool_query``, ``datasource_crud``) is stubbed, so
what is asserted here is ``ai_fallback_service``'s own composition and failure-mode
logic, not the modules it calls.

Three properties carry the suite:

* **One bad source never sours the rest of the answer.** A pipeline that times out,
  a tool config whose required parameter has no matching variable this turn, a
  deleted id — every failure mode is "omit and log", never a raised exception that
  would fail the whole AI Fallback answer. See the failure-mode table in
  ``ai_fallback_service.py``'s docstrings.
* **Pipelines run concurrently; tool configs run one at a time.** They cannot share
  a strategy: pipelines each open their own database session and may wait up to
  ``graph_runner.WAIT_SECONDS`` (90s), so running several in sequence could turn one
  chat answer into minutes; tool configs share this call's own ``AsyncSession``,
  which only one coroutine may use at a time.
* **Composition never grows the prompt without bound.** ``_compose_kb_context``
  joins whatever sources actually produced text and caps the result, the same
  "capped, honest about truncation" contract the rest of this module uses.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from app.services.deep_agents.query_executor import ToolQueryError
from app.services.flow_builder import ai_fallback_service as svc
from app.services.tool_configs.tool_chain_graph import ChainResult

USER_ID = 7
RUN_UUID = "run-1"


class _Outcome:
    """A stand-in for ``graph_runner.GraphOutcome`` with only what this module reads."""

    def __init__(self, kind: str, run_id: str = "", reason: str = "") -> None:
        self.kind = kind
        self.run_id = run_id
        self.reason = reason

    @property
    def finished(self) -> bool:
        return self.kind == "finished"


class _ToolConfig:
    def __init__(self, **overrides) -> None:  # noqa: ANN003
        self.id = overrides.pop("id", 1)
        self.uuid = overrides.pop("uuid", "tc-1")
        self.is_enabled = overrides.pop("is_enabled", True)
        self.datasource_id = overrides.pop("datasource_id", 9)
        self.table_name = overrides.pop("table_name", "orders")
        self.sql_query = overrides.pop("sql_query", None)
        self.config = overrides.pop("config", {})
        self.extra_tables = overrides.pop("extra_tables", [])
        self.sql_params = overrides.pop("sql_params", [])


class _Chain:
    def __init__(self, children=None) -> None:  # noqa: ANN001
        self.children = children or []


# ---------------------------------------------------------------------------
# _rows_from_pipeline_result
# ---------------------------------------------------------------------------
class TestRowsFromPipelineResult:
    def test_a_list_of_dicts_passes_through(self) -> None:
        rows = [{"day": "Mon", "orders": 1}]
        assert svc._rows_from_pipeline_result(rows) == rows

    def test_a_bare_list_wraps_each_item(self) -> None:
        assert svc._rows_from_pipeline_result([1, 2]) == [{"value": 1}, {"value": 2}]

    def test_a_dict_becomes_a_one_row_list(self) -> None:
        assert svc._rows_from_pipeline_result({"total": 42}) == [{"total": 42}]

    def test_a_scalar_becomes_a_one_row_list(self) -> None:
        assert svc._rows_from_pipeline_result(42) == [{"value": 42}]


# ---------------------------------------------------------------------------
# _one_pipeline_text
# ---------------------------------------------------------------------------
class TestOnePipelineText:
    async def test_a_finished_pipeline_with_rows_produces_text(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_run_graph(user_id, graph_uuid, inputs):  # noqa: ANN001
            assert user_id == USER_ID
            return _Outcome("finished", run_id=RUN_UUID)

        async def fake_full_result(user_id, run_uuid):  # noqa: ANN001
            assert run_uuid == RUN_UUID
            return [{"day": "Mon", "orders": 1}]

        monkeypatch.setattr(svc.graph_runner, "run_graph", fake_run_graph)
        monkeypatch.setattr(svc.graph_runner, "full_result", fake_full_result)

        text = await svc._one_pipeline_text(USER_ID, "g-1", {})

        assert text is not None
        assert "orders" in text

    @pytest.mark.parametrize("kind", ["question", "failed", "running"])
    async def test_a_pipeline_that_did_not_finish_is_omitted(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, kind: str,
    ) -> None:
        async def fake_run_graph(user_id, graph_uuid, inputs):  # noqa: ANN001
            return _Outcome(kind, reason="it did not finish")

        monkeypatch.setattr(svc.graph_runner, "run_graph", fake_run_graph)

        with caplog.at_level(logging.WARNING):
            text = await svc._one_pipeline_text(USER_ID, "g-1", {})

        assert text is None
        assert "did not finish" in caplog.text

    async def test_a_finished_pipeline_with_nothing_to_read_is_omitted(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def fake_run_graph(user_id, graph_uuid, inputs):  # noqa: ANN001
            return _Outcome("finished", run_id=RUN_UUID)

        async def fake_full_result(user_id, run_uuid):  # noqa: ANN001
            return None

        monkeypatch.setattr(svc.graph_runner, "run_graph", fake_run_graph)
        monkeypatch.setattr(svc.graph_runner, "full_result", fake_full_result)

        with caplog.at_level(logging.WARNING):
            text = await svc._one_pipeline_text(USER_ID, "g-1", {})

        assert text is None
        assert "nothing to read" in caplog.text


# ---------------------------------------------------------------------------
# _pipeline_context_texts
# ---------------------------------------------------------------------------
class TestPipelineContextTexts:
    async def test_no_ids_short_circuits_without_calling_the_runner(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fail(*args, **kwargs):  # noqa: ANN001, ANN003
            raise AssertionError("should not be called")

        monkeypatch.setattr(svc.graph_runner, "run_graph", fail)

        assert await svc._pipeline_context_texts(USER_ID, [], {}) == []

    async def test_several_pipelines_run_concurrently_not_sequentially(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each pipeline sleeps 0.15s; run one after another that is 0.45s+, run
        together it is close to 0.15s — the gap is what proves ``asyncio.gather`` is
        actually in play rather than an ordinary loop."""
        async def fake_run_graph(user_id, graph_uuid, inputs):  # noqa: ANN001
            await asyncio.sleep(0.15)
            return _Outcome("finished", run_id=graph_uuid)

        async def fake_full_result(user_id, run_uuid):  # noqa: ANN001
            return [{"value": run_uuid}]

        monkeypatch.setattr(svc.graph_runner, "run_graph", fake_run_graph)
        monkeypatch.setattr(svc.graph_runner, "full_result", fake_full_result)

        started = time.monotonic()
        texts = await svc._pipeline_context_texts(USER_ID, ["g-1", "g-2", "g-3"], {})
        elapsed = time.monotonic() - started

        assert len(texts) == 3
        assert elapsed < 0.3, f"took {elapsed:.3f}s — pipelines ran sequentially, not concurrently"

    async def test_omitted_pipelines_are_dropped_not_kept_as_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_run_graph(user_id, graph_uuid, inputs):  # noqa: ANN001
            return _Outcome("finished" if graph_uuid == "good" else "failed", run_id="r")

        async def fake_full_result(user_id, run_uuid):  # noqa: ANN001
            return [{"value": 1}]

        monkeypatch.setattr(svc.graph_runner, "run_graph", fake_run_graph)
        monkeypatch.setattr(svc.graph_runner, "full_result", fake_full_result)

        texts = await svc._pipeline_context_texts(USER_ID, ["bad", "good"], {})

        assert len(texts) == 1


# ---------------------------------------------------------------------------
# _one_tool_config_text
# ---------------------------------------------------------------------------
class TestOneToolConfigText:
    async def test_a_malformed_id_is_omitted(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING):
            text = await svc._one_tool_config_text(None, USER_ID, "not-a-uuid", {})

        assert text is None
        assert "malformed" in caplog.text

    async def test_an_unavailable_tool_config_is_omitted(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from litestar.exceptions import HTTPException

        async def fake_get(db, user_id, tool_config_id):  # noqa: ANN001
            raise HTTPException(status_code=404, detail="Tool config not found")

        monkeypatch.setattr(svc.tool_config_service, "get_tool_config", fake_get)

        with caplog.at_level(logging.WARNING):
            text = await svc._one_tool_config_text(None, USER_ID, "9c858d5a-0000-4000-8000-000000000001", {})

        assert text is None
        assert "unavailable" in caplog.text

    async def test_a_disabled_tool_config_is_omitted(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def fake_get(db, user_id, tool_config_id):  # noqa: ANN001
            return _ToolConfig(is_enabled=False)

        monkeypatch.setattr(svc.tool_config_service, "get_tool_config", fake_get)

        with caplog.at_level(logging.WARNING):
            text = await svc._one_tool_config_text(None, USER_ID, "9c858d5a-0000-4000-8000-000000000001", {})

        assert text is None
        assert "disabled" in caplog.text

    async def test_a_tool_config_with_no_datasource_is_omitted(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def fake_get(db, user_id, tool_config_id):  # noqa: ANN001
            return _ToolConfig()

        async def fake_get_one(db, filters):  # noqa: ANN001
            return None

        monkeypatch.setattr(svc.tool_config_service, "get_tool_config", fake_get)
        monkeypatch.setattr(svc.datasource_crud, "get_one", fake_get_one)

        with caplog.at_level(logging.WARNING):
            text = await svc._one_tool_config_text(None, USER_ID, "9c858d5a-0000-4000-8000-000000000001", {})

        assert text is None
        assert "no datasource" in caplog.text

    async def test_a_standalone_tool_config_runs_its_own_query_with_agent_values(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No chain, or a chain with no children: ``execute_tool_query`` runs the
        tool's own stored query, and the conversation's variables stand in for the
        agent's usual parameter values."""
        tool_config = _ToolConfig(sql_query="SELECT * FROM orders")
        calls: dict = {}

        async def fake_get(db, user_id, tool_config_id):  # noqa: ANN001
            return tool_config

        async def fake_get_one(db, filters):  # noqa: ANN001
            return object()

        async def fake_build_chains(db, pairs):  # noqa: ANN001
            return {tool_config.id: _Chain(children=[])}

        async def fake_execute_tool_query(datasource, config, table_name, **kwargs):  # noqa: ANN001
            calls["agent_values"] = kwargs.get("agent_values")
            return [{"id": 1}]

        monkeypatch.setattr(svc.tool_config_service, "get_tool_config", fake_get)
        monkeypatch.setattr(svc.datasource_crud, "get_one", fake_get_one)
        monkeypatch.setattr(svc.tool_chain_service, "build_chains", fake_build_chains)
        monkeypatch.setattr(svc, "execute_tool_query", fake_execute_tool_query)

        variables = {"CITY": "Berlin"}
        text = await svc._one_tool_config_text(
            None, USER_ID, "9c858d5a-0000-4000-8000-000000000001", variables,
        )

        assert text is not None
        assert calls["agent_values"] == variables

    async def test_a_chain_that_runs_to_completion_uses_its_rows(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tool_config = _ToolConfig()
        chain = _Chain(children=[object()])

        async def fake_get(db, user_id, tool_config_id):  # noqa: ANN001
            return tool_config

        async def fake_get_one(db, filters):  # noqa: ANN001
            return object()

        async def fake_build_chains(db, pairs):  # noqa: ANN001
            return {tool_config.id: chain}

        async def fake_run_chain(chain_arg, agent, variables):  # noqa: ANN001
            assert chain_arg is chain
            return ChainResult(rows=[{"total": 9}])

        monkeypatch.setattr(svc.tool_config_service, "get_tool_config", fake_get)
        monkeypatch.setattr(svc.datasource_crud, "get_one", fake_get_one)
        monkeypatch.setattr(svc.tool_chain_service, "build_chains", fake_build_chains)
        monkeypatch.setattr(svc, "run_chain", fake_run_chain)

        text = await svc._one_tool_config_text(
            None, USER_ID, "9c858d5a-0000-4000-8000-000000000001", {},
        )

        assert text is not None
        assert "total" in text

    async def test_a_chain_that_asks_a_question_is_omitted(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        tool_config = _ToolConfig()
        chain = _Chain(children=[object()])

        async def fake_get(db, user_id, tool_config_id):  # noqa: ANN001
            return tool_config

        async def fake_get_one(db, filters):  # noqa: ANN001
            return object()

        async def fake_build_chains(db, pairs):  # noqa: ANN001
            return {tool_config.id: chain}

        async def fake_run_chain(chain_arg, agent, variables):  # noqa: ANN001
            return ChainResult(rows=[], asked={"question": "Which city?"})

        monkeypatch.setattr(svc.tool_config_service, "get_tool_config", fake_get)
        monkeypatch.setattr(svc.datasource_crud, "get_one", fake_get_one)
        monkeypatch.setattr(svc.tool_chain_service, "build_chains", fake_build_chains)
        monkeypatch.setattr(svc, "run_chain", fake_run_chain)

        with caplog.at_level(logging.WARNING):
            text = await svc._one_tool_config_text(
                None, USER_ID, "9c858d5a-0000-4000-8000-000000000001", {},
            )

        assert text is None
        assert "cannot pause" in caplog.text

    async def test_a_short_circuited_chain_says_which_child_stopped_it(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tool_config = _ToolConfig()
        chain = _Chain(children=[object()])

        async def fake_get(db, user_id, tool_config_id):  # noqa: ANN001
            return tool_config

        async def fake_get_one(db, filters):  # noqa: ANN001
            return object()

        async def fake_build_chains(db, pairs):  # noqa: ANN001
            return {tool_config.id: chain}

        async def fake_run_chain(chain_arg, agent, variables):  # noqa: ANN001
            return ChainResult(rows=[], stopped_by="clients")

        monkeypatch.setattr(svc.tool_config_service, "get_tool_config", fake_get)
        monkeypatch.setattr(svc.datasource_crud, "get_one", fake_get_one)
        monkeypatch.setattr(svc.tool_chain_service, "build_chains", fake_build_chains)
        monkeypatch.setattr(svc, "run_chain", fake_run_chain)

        text = await svc._one_tool_config_text(
            None, USER_ID, "9c858d5a-0000-4000-8000-000000000001", {},
        )

        assert text is not None
        assert svc.describe_stop(ChainResult(rows=[], stopped_by="clients")) in text

    async def test_a_failed_query_is_omitted_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Includes a required parameter with no matching variable this turn —
        ``execute_tool_query`` raises ``ToolQueryError`` for that the same as any
        other failed query, and it is not this function's job to tell them apart."""
        tool_config = _ToolConfig()

        async def fake_get(db, user_id, tool_config_id):  # noqa: ANN001
            return tool_config

        async def fake_get_one(db, filters):  # noqa: ANN001
            return object()

        async def fake_build_chains(db, pairs):  # noqa: ANN001
            return {tool_config.id: _Chain(children=[])}

        async def fake_execute_tool_query(*args, **kwargs):  # noqa: ANN001, ANN003
            raise ToolQueryError("Missing required parameter: CITY")

        monkeypatch.setattr(svc.tool_config_service, "get_tool_config", fake_get)
        monkeypatch.setattr(svc.datasource_crud, "get_one", fake_get_one)
        monkeypatch.setattr(svc.tool_chain_service, "build_chains", fake_build_chains)
        monkeypatch.setattr(svc, "execute_tool_query", fake_execute_tool_query)

        with caplog.at_level(logging.WARNING):
            text = await svc._one_tool_config_text(
                None, USER_ID, "9c858d5a-0000-4000-8000-000000000001", {},
            )

        assert text is None
        assert "query failed" in caplog.text


# ---------------------------------------------------------------------------
# _tool_config_context_texts
# ---------------------------------------------------------------------------
class TestToolConfigContextTexts:
    async def test_runs_one_at_a_time_in_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        order: list = []

        async def fake_one(db, user_id, tool_config_id, variables):  # noqa: ANN001
            order.append(("start", tool_config_id))
            await asyncio.sleep(0)
            order.append(("end", tool_config_id))
            return f"text-{tool_config_id}"

        monkeypatch.setattr(svc, "_one_tool_config_text", fake_one)

        texts = await svc._tool_config_context_texts(None, USER_ID, ["a", "b"], {})

        assert texts == ["text-a", "text-b"]
        assert order == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")]

    async def test_omitted_ones_are_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_one(db, user_id, tool_config_id, variables):  # noqa: ANN001
            return None if tool_config_id == "bad" else "ok"

        monkeypatch.setattr(svc, "_one_tool_config_text", fake_one)

        texts = await svc._tool_config_context_texts(None, USER_ID, ["bad", "good"], {})

        assert texts == ["ok"]


# ---------------------------------------------------------------------------
# _compose_kb_context
# ---------------------------------------------------------------------------
class TestComposeKbContext:
    def test_joins_non_empty_blocks_with_the_separator(self) -> None:
        assert svc._compose_kb_context("doc text", "pipeline text") == "doc text\n\n---\n\npipeline text"

    def test_none_and_empty_blocks_are_skipped(self) -> None:
        assert svc._compose_kb_context(None, "", "the only one", None) == "the only one"

    def test_every_block_empty_returns_none(self) -> None:
        assert svc._compose_kb_context(None, "", None) is None

    def test_the_composed_text_is_capped(self) -> None:
        huge = "x" * (svc._MAX_KB_CONTEXT_CHARS + 500)
        composed = svc._compose_kb_context(huge)
        assert composed is not None
        assert len(composed) == svc._MAX_KB_CONTEXT_CHARS
