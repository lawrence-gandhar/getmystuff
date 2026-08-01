"""
Tests for app/schemas/sql_assist/sql_assist_schemas.py.

Ask AI is a four-step conversation carried entirely in form fields, so the thing
most worth pinning is the *echo*: each step re-posts the context of the one before,
and the partials put those values straight into hidden inputs. A ``None`` reaching a
template would render as the text "None" and be posted back as such on the next
step — which is why ``echo()`` exists and why it is tested for strings rather than
for objects.

``table_names`` is the other one. It arrives as repeated form keys from a
multi-select; read as a single value, a query would be generated against one table
when the user picked four, and nothing would say so.
"""

from __future__ import annotations

import uuid
from typing import Optional

import pytest
from litestar.exceptions import HTTPException

from app.schemas.sql_assist import (
    LLM_MODE_VALUES,
    MAX_SQL_PROMPT_LENGTH,
    MAX_SQL_TABLES,
    SqlAssistCreateToolRequest,
    SqlAssistFormQuery,
    SqlAssistGenerateRequest,
    SqlAssistTablesQuery,
    SqlAssistToolFormRequest,
)

VALID_UUID = "3f4b2c1e-0000-4000-8000-000000000001"

ECHOING_STEPS = [
    SqlAssistGenerateRequest,
    SqlAssistToolFormRequest,
    SqlAssistCreateToolRequest,
]


class _FakeForm(dict):
    """A mapping with ``getall``, the way Litestar's FormMultiDict behaves."""

    def __init__(self, single: dict, repeated: Optional[dict] = None) -> None:
        super().__init__(single)
        self._repeated = repeated or {}
        for key in self._repeated:
            self.setdefault(key, None)

    def getall(self, key, default=None):
        if key in self._repeated:
            return self._repeated[key]
        if key in self:
            return [self[key]]
        return default if default is not None else []


def _detail(schema, data: dict) -> str:
    with pytest.raises(HTTPException) as exc_info:
        schema.parse(data)
    return str(exc_info.value.detail)


def _minimum(schema) -> dict:
    """The smallest valid payload for whichever step is under test."""
    if schema is SqlAssistGenerateRequest:
        return {"prompt": "top 5 products by revenue"}
    if schema is SqlAssistToolFormRequest:
        return {"sql": "SELECT 1"}
    return {"tool_name": "top_products", "table_name": "sales_data"}


@pytest.mark.parametrize("schema", ECHOING_STEPS)
class TestEveryStepEchoesTheSameContext:
    def test_the_echo_fields_are_declared(self, schema) -> None:
        for field in ("datasource_id", "llm_mode", "llm_api_key_id", "agent_filter"):
            assert field in schema.model_fields

    def test_the_echo_renders_strings_not_objects(self, schema) -> None:
        """
        These land in hidden inputs. A ``UUID`` would stringify fine, but a
        ``None`` would render as the literal text "None" and be posted back as a
        selection on the next step.
        """
        echo = schema.parse(_minimum(schema)).echo()
        assert echo == {
            "datasource_id": "",
            "llm_mode": "",
            "llm_api_key_id": "",
            "agent_filter": "",
        }

    def test_the_echo_carries_the_values_through(self, schema) -> None:
        echo = schema.parse(
            {
                **_minimum(schema),
                "datasource_id": VALID_UUID,
                "llm_mode": "in_built",
                "agent_filter": VALID_UUID,
            }
        ).echo()
        assert echo["datasource_id"] == VALID_UUID
        assert echo["llm_mode"] == "in_built"
        assert echo["agent_filter"] == VALID_UUID

    def test_a_blank_model_choice_is_left_to_the_service(self, schema) -> None:
        """"Not chosen yet" has its own message in sql_assist_service."""
        assert schema.parse({**_minimum(schema), "llm_mode": ""}).llm_mode == ""

    def test_an_unknown_model_choice_is_refused(self, schema) -> None:
        assert _detail(schema, {**_minimum(schema), "llm_mode": "magic"}) == (
            "Model choice is not one of the available options"
        )

    @pytest.mark.parametrize("mode", sorted(LLM_MODE_VALUES))
    def test_every_declared_mode_is_accepted(self, schema, mode: str) -> None:
        assert schema.parse({**_minimum(schema), "llm_mode": mode}).llm_mode == mode


class TestTableNamesMultiSelect:
    def test_every_selected_table_survives(self) -> None:
        """
        The bug this guards: reading the multi-select as a single value generates
        a query against one table when the user picked four.
        """
        form = _FakeForm(
            {"prompt": "p"}, {"table_names": ["orders", "customers", "products"]}
        )
        payload = SqlAssistGenerateRequest.from_form_data(form)
        assert payload.table_names == ["orders", "customers", "products"]

    def test_no_tables_selected_is_an_empty_list(self) -> None:
        assert SqlAssistGenerateRequest.parse({"prompt": "p"}).table_names == []

    def test_the_table_count_is_capped_at_the_reflection_limit(self) -> None:
        """
        Taken from ``MAX_REFLECTED_TABLES`` so the schema can never accept more
        tables than the schema-reader will actually read — otherwise a query would
        be generated against a schema the user thought was bigger.
        """
        assert "cannot have more than" in _detail(
            SqlAssistGenerateRequest,
            {"prompt": "p", "table_names": ["t"] * (MAX_SQL_TABLES + 1)},
        )

    def test_a_table_name_that_could_break_an_identifier_is_refused(self) -> None:
        assert "is not a valid name" in _detail(
            SqlAssistGenerateRequest,
            {"prompt": "p", "table_names": ["orders; drop table users"]},
        )


class TestGenerate:
    def test_the_prompt_is_required(self) -> None:
        assert _detail(SqlAssistGenerateRequest, {"prompt": "  "}) == "Prompt is required"

    def test_the_prompt_is_bounded(self) -> None:
        at_cap = "p" * MAX_SQL_PROMPT_LENGTH
        assert SqlAssistGenerateRequest.parse({"prompt": at_cap}).prompt == at_cap
        assert "cannot be longer than 2000" in _detail(
            SqlAssistGenerateRequest, {"prompt": "p" * (MAX_SQL_PROMPT_LENGTH + 1)}
        )

    def test_the_history_is_kept_as_raw_text(self) -> None:
        """
        The service owns its shape and re-echoes it verbatim through a *failed*
        turn — which is what stops a refinement that times out from resetting the
        whole session.
        """
        raw = '[{"role": "user", "content": "x"}]'
        assert SqlAssistGenerateRequest.parse(
            {"prompt": "p", "history_json": raw}
        ).history_json == raw


class TestToolForm:
    def test_the_sql_is_required(self) -> None:
        assert _detail(SqlAssistToolFormRequest, {"sql": "  "}) == "SQL is required"

    def test_the_generated_sql_is_not_pattern_checked(self) -> None:
        """
        Deliberate. Whether the SQL is safe to run is decided by
        ``sql_assist_service`` against the reflected schema; a regex over SQL here
        would be a false reassurance.
        """
        payload = SqlAssistToolFormRequest.parse(
            {"sql": "SELECT * FROM orders; -- a comment"}
        )
        assert payload.sql.startswith("SELECT")


class TestCreateTool:
    def _valid(self, **extra) -> dict:
        return {"tool_name": "top_products", "table_name": "sales_data", **extra}

    def test_a_valid_form(self) -> None:
        payload = SqlAssistCreateToolRequest.parse(self._valid())
        assert payload.config_json == {}
        assert payload.preview is None

    def test_the_tool_name_must_be_an_identifier(self) -> None:
        assert "must start with a letter" in _detail(
            SqlAssistCreateToolRequest, self._valid(tool_name="9lives")
        )

    def test_the_tool_name_is_lowercased(self) -> None:
        assert SqlAssistCreateToolRequest.parse(
            self._valid(tool_name="Top_Products")
        ).tool_name == "top_products"

    def test_the_drafted_query_is_parsed(self) -> None:
        payload = SqlAssistCreateToolRequest.parse(
            self._valid(config_json='{"columns": [{"column": "revenue"}]}')
        )
        assert payload.config_json["columns"][0]["column"] == "revenue"

    def test_a_malformed_drafted_query_is_refused(self) -> None:
        assert "could not be read" in _detail(
            SqlAssistCreateToolRequest, self._valid(config_json="{oops")
        )

    def test_the_agent_selection_is_optional(self) -> None:
        assert SqlAssistCreateToolRequest.parse(
            self._valid(data_agent_id="")
        ).data_agent_id is None


class TestCascadeQueries:
    def test_the_host_pages_filter_is_parsed(self) -> None:
        assert SqlAssistFormQuery.parse({"agent": VALID_UUID}).agent == (
            uuid.UUID(VALID_UUID)
        )

    def test_no_filter_is_valid(self) -> None:
        assert SqlAssistFormQuery.parse({}).agent is None

    def test_the_tables_cascade_starts_unselected(self) -> None:
        assert SqlAssistTablesQuery.parse({}).datasource_id is None

    def test_an_unreadable_datasource_is_refused(self) -> None:
        with pytest.raises(HTTPException):
            SqlAssistTablesQuery.parse({"datasource_id": "nope"})
