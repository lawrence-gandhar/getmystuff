"""
Tests for the save-time checks on an AI Fallback block's knowledge base
``kb_pipeline_ids`` / ``kb_tool_config_ids`` — the two new live source kinds a
Knowledge base panel may name alongside its uploaded documents.

Two checks, mirroring `test_download_file_validation.py`'s split of shape vs.
ownership:

* **`_validate_ai_fallback_data`** — synchronous, no database: each field, when
  present, must be a list of non-empty strings. Malformed input never reaches the
  database check.
* **`_assert_ai_fallback_kb_sources`** — needs the database: every named pipeline
  and tool config must belong to the saving user. Wrong-owner and nonexistent ids
  are refused identically, the same 404-shaped-as-400 doctrine
  `graph_service.get_graph` / `tool_config_service.get_tool_config` already apply —
  this function does not itself distinguish "not yours" from "does not exist".
"""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.graph_designer import ToolGraph
from app.models.tool_configs import ToolConfig
from app.services.flow_builder.flow_service import (
    _assert_ai_fallback_kb_sources,
    _validate_ai_fallback_data,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def other_user(make_user):  # noqa: ANN001, ANN201
    return await make_user("intruder@example.com")


@pytest.fixture
def make_graph(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str = "A pipeline"):  # noqa: ANN001
        row = ToolGraph(user_id=owner.id, name=name, graph_data={"nodes": [], "edges": []})
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_agent(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str = "An agent"):  # noqa: ANN001
        row = DataAgent(user_id=owner.id, name=name)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_datasource(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str = "A datasource"):  # noqa: ANN001
        row = DataSource(
            user_id=owner.id,
            datasource_name=name,
            db_type="postgres",
            password_encrypted="enc",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_tool_config(db):  # noqa: ANN001, ANN201
    async def _make(agent, datasource, tool_name: str = "A tool"):  # noqa: ANN001
        row = ToolConfig(
            data_agent_id=agent.id,
            datasource_id=datasource.id,
            tool_name=tool_name,
            table_name="orders",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
async def make_tool_config_for(make_agent, make_datasource, make_tool_config):  # noqa: ANN001, ANN201
    """A tool config owned end-to-end by one user, in one call."""
    async def _make(owner, tool_name: str = "A tool"):  # noqa: ANN001
        agent = await make_agent(owner)
        datasource = await make_datasource(owner)
        return await make_tool_config(agent, datasource, tool_name)

    return _make


def _graph_data(node_id: str = "ai_1", **data) -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "start", "data": {}},
            {"id": node_id, "type": "ai_fallback", "data": data},
        ],
        "edges": [{"source": "start", "target": node_id, "source_port": "default"}],
    }


# ---------------------------------------------------------------------------
# Shape — no database
# ---------------------------------------------------------------------------
class TestShape:
    def test_absent_fields_pass(self) -> None:
        _validate_ai_fallback_data({"context_source": "knowledge_base"})

    def test_empty_lists_pass(self) -> None:
        _validate_ai_fallback_data({"kb_pipeline_ids": [], "kb_tool_config_ids": []})

    def test_a_list_of_ids_passes(self) -> None:
        _validate_ai_fallback_data({"kb_pipeline_ids": ["abc"], "kb_tool_config_ids": ["def"]})

    def test_a_plain_string_is_refused(self) -> None:
        """Not a list at all — the shape a single-select field would have written,
        which this one never has."""
        with pytest.raises(HTTPException) as caught:
            _validate_ai_fallback_data({"kb_pipeline_ids": "abc"})

        assert "must be a list of ids" in str(caught.value.detail)

    def test_a_list_with_a_blank_entry_is_refused(self) -> None:
        with pytest.raises(HTTPException) as caught:
            _validate_ai_fallback_data({"kb_pipeline_ids": ["abc", ""]})

        assert "must be a list of ids" in str(caught.value.detail)

    def test_a_list_with_a_non_string_entry_is_refused(self) -> None:
        with pytest.raises(HTTPException) as caught:
            _validate_ai_fallback_data({"kb_tool_config_ids": [123]})

        assert "must be a list of ids" in str(caught.value.detail)


# ---------------------------------------------------------------------------
# Ownership — needs the database
# ---------------------------------------------------------------------------
class TestPipelineOwnership:
    async def test_a_real_owned_pipeline_passes(self, db, user, make_graph) -> None:  # noqa: ANN001
        graph = await make_graph(user)

        await _assert_ai_fallback_kb_sources(
            db, user.id, _graph_data(kb_pipeline_ids=[str(graph.uuid)]),
        )

    async def test_a_nonexistent_pipeline_id_is_refused(self, db, user) -> None:  # noqa: ANN001
        import uuid as uuid_pkg

        with pytest.raises(HTTPException) as caught:
            await _assert_ai_fallback_kb_sources(
                db, user.id, _graph_data(kb_pipeline_ids=[str(uuid_pkg.uuid4())]),
            )

        assert "pipeline you don't have access to" in str(caught.value.detail)

    async def test_another_users_pipeline_is_refused_the_same_way(
        self, db, user, other_user, make_graph,
    ) -> None:  # noqa: ANN001
        theirs = await make_graph(other_user, "Theirs")

        with pytest.raises(HTTPException) as caught:
            await _assert_ai_fallback_kb_sources(
                db, user.id, _graph_data(kb_pipeline_ids=[str(theirs.uuid)]),
            )

        assert "pipeline you don't have access to" in str(caught.value.detail)

    async def test_a_malformed_pipeline_id_is_refused(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as caught:
            await _assert_ai_fallback_kb_sources(
                db, user.id, _graph_data(kb_pipeline_ids=["not-a-uuid"]),
            )

        assert "pipeline you don't have access to" in str(caught.value.detail)

    async def test_the_block_naming_it_is_named_in_the_message(
        self, db, user,
    ) -> None:  # noqa: ANN001
        """A canvas with several AI Fallback blocks needs to say which one is wrong."""
        import uuid as uuid_pkg

        with pytest.raises(HTTPException) as caught:
            await _assert_ai_fallback_kb_sources(
                db, user.id,
                _graph_data(
                    label="Support answers", kb_pipeline_ids=[str(uuid_pkg.uuid4())],
                ),
            )

        assert "Support answers" in str(caught.value.detail)


class TestToolConfigOwnership:
    async def test_a_real_owned_tool_config_passes(
        self, db, user, make_tool_config_for,
    ) -> None:  # noqa: ANN001
        tool_config = await make_tool_config_for(user)

        await _assert_ai_fallback_kb_sources(
            db, user.id, _graph_data(kb_tool_config_ids=[str(tool_config.uuid)]),
        )

    async def test_a_nonexistent_tool_config_id_is_refused(self, db, user) -> None:  # noqa: ANN001
        import uuid as uuid_pkg

        with pytest.raises(HTTPException) as caught:
            await _assert_ai_fallback_kb_sources(
                db, user.id, _graph_data(kb_tool_config_ids=[str(uuid_pkg.uuid4())]),
            )

        assert "tool config you don't have access to" in str(caught.value.detail)

    async def test_another_users_tool_config_is_refused_the_same_way(
        self, db, user, other_user, make_tool_config_for,
    ) -> None:  # noqa: ANN001
        theirs = await make_tool_config_for(other_user, "Theirs")

        with pytest.raises(HTTPException) as caught:
            await _assert_ai_fallback_kb_sources(
                db, user.id, _graph_data(kb_tool_config_ids=[str(theirs.uuid)]),
            )

        assert "tool config you don't have access to" in str(caught.value.detail)


class TestNonAiFallbackNodesAreIgnored:
    async def test_a_graph_with_no_ai_fallback_block_passes_untouched(self, db, user) -> None:  # noqa: ANN001
        await _assert_ai_fallback_kb_sources(
            db, user.id, {"nodes": [{"id": "start", "type": "start", "data": {}}], "edges": []},
        )

    async def test_an_ai_fallback_block_naming_nothing_passes(self, db, user) -> None:  # noqa: ANN001
        await _assert_ai_fallback_kb_sources(db, user.id, _graph_data())
