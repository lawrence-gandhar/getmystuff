"""
Tests for app/services/tool_configs/tool_config_service.py.

A tool config is the definition of a query a Deep Agent may run on the user's
behalf, assembled from form input. Two areas carry the module:

* **``validated_query_config``** rebuilds the payload field by field rather than
  storing what it received, so only known keys persist and every table and column
  name has been checked. It is public because the AI SQL assistant funnels its
  generated queries through the same function — a config the model produced has
  to be exactly as trustworthy as one the builder produced, and one validator is
  what stops the two diverging. Its rejection cases are the security boundary and
  are tested exhaustively.

* **The agent-id sets returned by the write functions.** ``update_tool_config``
  returns *two* agent ids when a tool moves between agents, because the agent it
  left is still describing it in its routing prompt. Getting that wrong leaves a
  stale prompt advertising a tool the agent no longer has, so it is asserted
  directly.

Live datasource reads (table and column dropdowns) go through
``datasource_service``, which is stubbed at the seam this module imports.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg

import pytest
from litestar.exceptions import HTTPException

from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.tool_configs import ToolConfig
from app.services.tool_configs import tool_config_service as svc
from app.services.tool_configs.tool_config_service import (
    _as_dict,
    _as_list,
    _optional_alias,
    build_query_preview,
    validated_query_config,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def make_agent(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str, **kwargs):  # noqa: ANN001
        row = DataAgent(user_id=owner.id, name=name, **kwargs)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_datasource(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str, **kwargs):  # noqa: ANN001
        row = DataSource(
            user_id=owner.id,
            datasource_name=name,
            db_type=kwargs.pop("db_type", "postgres"),
            password_encrypted="enc",
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_tool_config(db):  # noqa: ANN001, ANN201
    async def _make(agent, datasource, tool_name: str, **kwargs):  # noqa: ANN001
        row = ToolConfig(
            data_agent_id=agent.id,
            datasource_id=datasource.id,
            tool_name=tool_name,
            table_name=kwargs.pop("table_name", "orders"),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
async def other_user(make_user):  # noqa: ANN001, ANN201
    return await make_user("intruder@example.com")


@pytest.fixture
def stub_datasource_reads(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub the two live-read helpers the form dropdowns depend on."""
    state: dict = {
        "objects": ["orders", "customers"],
        "schema": [{"column": "id"}, {"column": "total"}],
        "raise_on": None,
    }

    async def fake_objects(db, datasource_id, user_id):  # noqa: ANN001
        if state["raise_on"] == "objects":
            raise HTTPException(status_code=400, detail="connection failed")
        return {"objects": list(state["objects"])}

    async def fake_schema(db, datasource_id, user_id, table_name):  # noqa: ANN001
        if state["raise_on"] == "schema":
            raise HTTPException(status_code=400, detail="schema failed")
        return {"schema": list(state["schema"])}

    monkeypatch.setattr(
        svc.datasource_service, "get_datasource_objects", fake_objects
    )
    monkeypatch.setattr(
        svc.datasource_service, "get_datasource_table_schema", fake_schema
    )
    return state


def config_json(**sections) -> str:  # noqa: ANN003
    return json.dumps(sections)


# A valid join from the base table ``orders`` onto ``customers``. Needed by every
# test of a qualified column reference: with no joins, ``query_tables`` returns
# ``[]`` and the table-membership check is skipped entirely.
JOIN_TO_CUSTOMERS = {
    "type": "inner",
    "table": "customers",
    "left_table": "orders",
    "left_column": "orders.customer_id",
    "right_column": "customers.id",
}


# ---------------------------------------------------------------------------
# validated_query_config — the shape
# ---------------------------------------------------------------------------
class TestValidatedQueryConfigShape:
    def test_an_empty_config_normalises_to_empty_sections(self) -> None:
        """An empty selection is legal and means "all columns" — the same default
        the builder uses."""
        result = validated_query_config(None, "orders", "postgres")

        assert result == {
            "columns": [],
            "aggregations": [],
            "group_by": [],
            "filters": [],
            "joins": [],
        }

    def test_unknown_keys_are_dropped(self) -> None:
        """The result is rebuilt field by field, so anything the form did not
        declare cannot reach the database."""
        result = validated_query_config(
            config_json(columns=[{"column": "id"}], injected="evil", __proto__="x"),
            "orders",
            "postgres",
        )

        assert set(result) == {"columns", "aggregations", "group_by", "filters", "joins"}

    def test_columns_keep_their_alias(self) -> None:
        result = validated_query_config(
            config_json(columns=[{"column": "total", "alias": "amount"}]),
            "orders",
            "postgres",
        )

        assert result["columns"] == [{"column": "total", "alias": "amount"}]

    def test_a_missing_alias_becomes_an_empty_string(self) -> None:
        """Stored as "" rather than absent so every entry has the same shape."""
        result = validated_query_config(
            config_json(columns=[{"column": "total"}]), "orders", "postgres"
        )

        assert result["columns"] == [{"column": "total", "alias": ""}]

    def test_aggregations_are_lowercased(self) -> None:
        result = validated_query_config(
            config_json(aggregations=[{"type": "  SUM  ", "column": "total"}]),
            "orders",
            "postgres",
        )

        assert result["aggregations"][0]["type"] == "sum"

    def test_filters_are_normalised(self) -> None:
        result = validated_query_config(
            config_json(filters=[{"column": "total", "operator": "  >  ", "value": " 10 "}]),
            "orders",
            "postgres",
        )

        assert result["filters"] == [{"column": "total", "operator": ">", "value": "10"}]

    def test_group_by_is_a_plain_list_of_column_names(self) -> None:
        result = validated_query_config(
            config_json(group_by=["total", "id"]), "orders", "postgres"
        )

        assert result["group_by"] == ["total", "id"]


# ---------------------------------------------------------------------------
# validated_query_config — rejection
# ---------------------------------------------------------------------------
class TestValidatedQueryConfigRejection:
    def test_malformed_json_is_a_400(self) -> None:
        with pytest.raises(HTTPException) as excinfo:
            validated_query_config("{not json", "orders", "postgres")

        assert excinfo.value.status_code == 400

    @pytest.mark.parametrize(
        "section", ["columns", "aggregations", "group_by", "filters"]
    )
    def test_a_section_that_is_not_a_list_is_a_400(self, section: str) -> None:
        with pytest.raises(HTTPException, match="not in the expected format"):
            validated_query_config(
                config_json(**{section: {"a": 1}}), "orders", "postgres"
            )

    @pytest.mark.parametrize(
        ("section", "limit"),
        [("columns", 200), ("aggregations", 50), ("group_by", 50), ("filters", 50)],
    )
    def test_each_section_has_a_size_cap(self, section: str, limit: int) -> None:
        """A caller could otherwise persist an unbounded config and blow up the
        prompt built from it."""
        entries = ["x"] * (limit + 1)
        with pytest.raises(HTTPException, match="cannot have more than"):
            validated_query_config(
                config_json(**{section: entries}), "orders", "postgres"
            )

    @pytest.mark.parametrize(
        "bad",
        ["x;drop", "a'b", 'a"b', "a(b)", "a`b", "a\\b", "a\nb", "a/*b", "*", "x" * 256],
    )
    def test_a_column_name_that_could_break_out_of_an_identifier_is_rejected(
        self, bad: str
    ) -> None:
        """
        The security boundary. These names are interpolated into a generated
        query rather than bound as parameters, so quotes, semicolons, backticks,
        parentheses and comment markers are refused on the way in.
        """
        with pytest.raises(HTTPException):
            validated_query_config(
                config_json(columns=[{"column": bad}]), "orders", "postgres"
            )

    @pytest.mark.parametrize("name", ["Order Date", "order-date", "a.b", "1abc", "_x"])
    def test_spaces_dots_and_hyphens_are_deliberately_allowed(self, name: str) -> None:
        """
        Recorded so the charset is not mistaken for a bug. ``_OBJECT_NAME_PATTERN``
        (app/utils/validators.py:36) permits spaces, dots and hyphens after the
        first character, because real database columns are named things like
        "Order Date" and rejecting them would make those tables unusable. What it
        blocks is the set that can terminate an identifier — quotes, semicolons,
        backticks, parentheses.
        """
        result = validated_query_config(
            config_json(columns=[{"column": name}]), "orders", "postgres"
        )

        assert result["columns"][0]["column"] == name

    @pytest.mark.parametrize("missing", [None, "", "   "])
    def test_a_blank_column_is_rejected(self, missing) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException, match="required"):
            validated_query_config(
                config_json(columns=[{"column": missing}]), "orders", "postgres"
            )

    def test_a_column_qualified_by_an_unjoined_table_is_rejected(self) -> None:
        """
        ``customers.name`` is only legal once ``customers`` is actually joined —
        which is why joins are validated first, before anything may refer to
        them.

        The join list has to be present for this check to apply at all:
        ``query_tables`` returns ``[]`` when there are no joins, and an empty
        allowed-tables list means "single-table query, references are bare".
        """
        with pytest.raises(HTTPException, match="not part of"):
            validated_query_config(
                config_json(
                    joins=[JOIN_TO_CUSTOMERS],
                    columns=[{"column": "suppliers.name"}],
                ),
                "orders",
                "postgres",
            )

    def test_a_column_qualified_by_a_joined_table_is_accepted(self) -> None:
        result = validated_query_config(
            config_json(joins=[JOIN_TO_CUSTOMERS], columns=[{"column": "customers.name"}]),
            "orders",
            "postgres",
        )

        assert result["columns"][0]["column"] == "customers.name"

    def test_a_column_qualified_by_the_base_table_is_accepted(self) -> None:
        result = validated_query_config(
            config_json(joins=[JOIN_TO_CUSTOMERS], columns=[{"column": "orders.total"}]),
            "orders",
            "postgres",
        )

        assert result["columns"][0]["column"] == "orders.total"

    def test_an_unqualified_column_is_still_accepted_alongside_joins(self) -> None:
        """A bare name means the base table. Accepted because it only reaches
        here from a config saved before the join was added, and rejecting it
        would make that config uneditable."""
        result = validated_query_config(
            config_json(joins=[JOIN_TO_CUSTOMERS], columns=[{"column": "total"}]),
            "orders",
            "postgres",
        )

        assert result["columns"][0]["column"] == "total"

    def test_joins_are_refused_for_a_datasource_that_cannot_join(self) -> None:
        with pytest.raises(HTTPException, match="only available for relational"):
            validated_query_config(
                config_json(joins=[JOIN_TO_CUSTOMERS]), "orders", "mongodb"
            )

    def test_joining_the_same_table_twice_is_rejected(self) -> None:
        with pytest.raises(HTTPException, match="already part of this query"):
            validated_query_config(
                config_json(joins=[JOIN_TO_CUSTOMERS, JOIN_TO_CUSTOMERS]),
                "orders",
                "postgres",
            )

    def test_a_join_against_a_table_not_yet_in_the_query_is_rejected(self) -> None:
        """The chain has to be connected and in order — join three may refer to
        tables one and two, never the other way round."""
        out_of_order = {**JOIN_TO_CUSTOMERS, "left_table": "suppliers"}

        with pytest.raises(HTTPException, match="not part of this query"):
            validated_query_config(
                config_json(joins=[out_of_order]), "orders", "postgres"
            )

    @pytest.mark.parametrize("bad", ["", "SUMX", "median", "count(*)", None])
    def test_an_unsupported_aggregation_function_is_rejected(self, bad) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException, match="valid function"):
            validated_query_config(
                config_json(aggregations=[{"type": bad, "column": "total"}]),
                "orders",
                "postgres",
            )

    @pytest.mark.parametrize("operator", ["", "<>", "==", "OR", "; DROP", None])
    def test_an_unsupported_filter_operator_is_rejected(self, operator) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException, match="valid operator"):
            validated_query_config(
                config_json(
                    filters=[{"column": "total", "operator": operator, "value": "1"}]
                ),
                "orders",
                "postgres",
            )

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_a_filter_with_no_value_is_rejected(self, blank) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException, match="needs a value"):
            validated_query_config(
                config_json(filters=[{"column": "total", "operator": "=", "value": blank}]),
                "orders",
                "postgres",
            )

    def test_a_filter_value_at_the_limit_is_accepted(self) -> None:
        result = validated_query_config(
            config_json(
                filters=[{"column": "total", "operator": "=", "value": "x" * 500}]
            ),
            "orders",
            "postgres",
        )

        assert len(result["filters"][0]["value"]) == 500

    def test_an_over_long_filter_value_is_rejected(self) -> None:
        with pytest.raises(HTTPException, match="cannot be longer than 500"):
            validated_query_config(
                config_json(
                    filters=[{"column": "total", "operator": "=", "value": "x" * 501}]
                ),
                "orders",
                "postgres",
            )

    @pytest.mark.parametrize("entry", ["a string", 42, ["nested"], None])
    def test_a_non_object_entry_is_rejected(self, entry) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException, match="not in the expected format"):
            validated_query_config(
                config_json(columns=[entry]), "orders", "postgres"
            )


class TestOptionalAlias:
    @pytest.mark.parametrize("blank", [None, "", "   ", 0, False])
    def test_a_blank_alias_becomes_an_empty_string(self, blank) -> None:  # noqa: ANN001
        assert _optional_alias(blank) == ""

    @pytest.mark.parametrize("alias", ["amount", "_x", "a1", "Total_2"])
    def test_accepts_identifier_shaped_aliases(self, alias: str) -> None:
        assert _optional_alias(alias) == alias

    def test_strips_whitespace(self) -> None:
        assert _optional_alias("  amount  ") == "amount"

    @pytest.mark.parametrize(
        "bad", ["1abc", "a b", "a-b", "a;b", "a'b", "a.b", "x" * 256]
    )
    def test_rejects_anything_that_is_not_an_identifier(self, bad: str) -> None:
        """An alias lands in the SELECT list verbatim, so it gets the same
        charset treatment as a column name."""
        with pytest.raises(HTTPException, match="must start with a letter"):
            _optional_alias(bad)


class TestAsListAndAsDict:
    def test_none_is_an_empty_list(self) -> None:
        assert _as_list(None, "Columns", 10) == []

    def test_a_list_passes_through(self) -> None:
        assert _as_list([1, 2], "Columns", 10) == [1, 2]

    @pytest.mark.parametrize("bad", [{"a": 1}, "text", 42, True])
    def test_a_non_list_is_rejected(self, bad) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException, match="not in the expected format"):
            _as_list(bad, "Columns", 10)

    def test_exactly_the_limit_is_accepted(self) -> None:
        assert len(_as_list(["x"] * 10, "Columns", 10)) == 10

    def test_a_dict_passes_through(self) -> None:
        assert _as_dict({"a": 1}, "Column") == {"a": 1}

    @pytest.mark.parametrize("bad", ["text", 42, [1], None])
    def test_a_non_dict_is_rejected(self, bad) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException, match="not in the expected format"):
            _as_dict(bad, "Column")


# ---------------------------------------------------------------------------
# build_query_preview
# ---------------------------------------------------------------------------
class TestBuildQueryPreview:
    def test_an_empty_config_selects_everything(self) -> None:
        assert build_query_preview({}, "orders") == "SELECT * FROM orders"

    def test_plain_columns(self) -> None:
        config = {"columns": [{"column": "id"}, {"column": "total"}]}
        assert build_query_preview(config, "orders") == "SELECT id, total FROM orders"

    def test_a_column_alias_is_rendered(self) -> None:
        config = {"columns": [{"column": "total", "alias": "amount"}]}
        assert build_query_preview(config, "orders") == (
            "SELECT total AS amount FROM orders"
        )

    def test_aggregations_follow_plain_columns(self) -> None:
        config = {
            "columns": [{"column": "status"}],
            "aggregations": [{"type": "sum", "column": "total", "alias": "revenue"}],
        }

        assert build_query_preview(config, "orders") == (
            "SELECT status, SUM(total) AS revenue FROM orders"
        )

    def test_an_aggregation_function_is_uppercased(self) -> None:
        config = {"aggregations": [{"type": "count", "column": "id"}]}
        assert "COUNT(id)" in build_query_preview(config, "orders")

    def test_filters_become_a_where_clause(self) -> None:
        config = {"filters": [{"column": "total", "operator": ">", "value": "10"}]}
        assert build_query_preview(config, "orders") == (
            "SELECT * FROM orders WHERE total > '10'"
        )

    def test_several_filters_are_joined_with_and(self) -> None:
        config = {
            "filters": [
                {"column": "total", "operator": ">", "value": "10"},
                {"column": "status", "operator": "=", "value": "open"},
            ]
        }

        assert "WHERE total > '10' AND status = 'open'" in build_query_preview(
            config, "orders"
        )

    def test_group_by_is_appended(self) -> None:
        config = {
            "aggregations": [{"type": "count", "column": "id"}],
            "group_by": ["status"],
        }

        assert build_query_preview(config, "orders").endswith("GROUP BY status")

    def test_entries_with_no_column_are_skipped(self) -> None:
        config = {"columns": [{"column": ""}, {"column": "id"}], "group_by": ["", "status"]}

        preview = build_query_preview(config, "orders")

        assert preview == "SELECT id FROM orders GROUP BY status"

    def test_an_aggregation_missing_its_type_is_skipped(self) -> None:
        config = {"aggregations": [{"column": "total"}, {"type": "sum", "column": "id"}]}

        assert build_query_preview(config, "orders") == "SELECT SUM(id) FROM orders"

    def test_the_clause_order_is_select_where_group_by(self) -> None:
        config = {
            "columns": [{"column": "status"}],
            "aggregations": [{"type": "sum", "column": "total"}],
            "filters": [{"column": "total", "operator": ">", "value": "1"}],
            "group_by": ["status"],
        }

        preview = build_query_preview(config, "orders")

        assert preview.index("SELECT") < preview.index("WHERE") < preview.index("GROUP BY")


# ---------------------------------------------------------------------------
# Ownership resolution
# ---------------------------------------------------------------------------
class TestResolveAgentAndDatasource:
    async def test_a_missing_agent_id_is_a_400(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc._resolve_agent(db, user.id, None)

        assert excinfo.value.status_code == 400
        assert excinfo.value.detail == "Data agent is required"

    async def test_a_missing_datasource_id_is_a_400(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc._resolve_datasource(db, user.id, None)

        assert excinfo.value.detail == "Datasource is required"

    async def test_another_users_datasource_is_404(
        self, db, user, other_user, make_datasource  # noqa: ANN001
    ) -> None:
        """This is what stops one user pointing a tool at another user's
        datasource by pasting its uuid into the form."""
        theirs = await make_datasource(other_user, "theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc._resolve_datasource(db, user.id, theirs.uuid)

        assert excinfo.value.status_code == 404

    async def test_another_users_agent_is_404(
        self, db, user, other_user, make_agent  # noqa: ANN001
    ) -> None:
        theirs = await make_agent(other_user, "theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc._resolve_agent(db, user.id, theirs.uuid)

        assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# create_tool_config
# ---------------------------------------------------------------------------
class TestCreateToolConfig:
    async def test_creates_an_enabled_tool(
        self, db, user, make_agent, make_datasource  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")

        config = await svc.create_tool_config(
            db, user.id, agent.uuid, datasource.uuid, "query_orders", "orders"
        )

        assert config.tool_name == "query_orders"
        assert config.table_name == "orders"
        assert config.is_enabled is True
        assert config.data_agent_id == agent.id
        assert config.datasource_id == datasource.id

    async def test_the_query_config_is_normalised_and_stored(
        self, db, user, make_agent, make_datasource  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")

        config = await svc.create_tool_config(
            db,
            user.id,
            agent.uuid,
            datasource.uuid,
            "query_orders",
            "orders",
            config_json=config_json(columns=[{"column": "total", "alias": "amount"}]),
        )

        assert config.config["columns"] == [{"column": "total", "alias": "amount"}]
        assert config.config["joins"] == []

    @pytest.mark.parametrize("bad", ["", "   ", "1tool", "bad name", "tool-name"])
    async def test_an_invalid_tool_name_is_rejected(
        self, db, user, make_agent, make_datasource, bad: str  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")

        with pytest.raises(HTTPException):
            await svc.create_tool_config(
                db, user.id, agent.uuid, datasource.uuid, bad, "orders"
            )

    @pytest.mark.parametrize("bad", ["", "   ", "bad;name"])
    async def test_an_invalid_table_name_is_rejected(
        self, db, user, make_agent, make_datasource, bad: str  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")

        with pytest.raises(HTTPException):
            await svc.create_tool_config(
                db, user.id, agent.uuid, datasource.uuid, "query_orders", bad
            )

    async def test_a_duplicate_tool_name_on_the_same_agent_is_rejected(
        self, db, user, make_agent, make_datasource  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        await svc.create_tool_config(
            db, user.id, agent.uuid, datasource.uuid, "query_orders", "orders"
        )

        with pytest.raises(HTTPException) as excinfo:
            await svc.create_tool_config(
                db, user.id, agent.uuid, datasource.uuid, "QUERY_ORDERS", "orders"
            )

        assert excinfo.value.status_code == 400
        assert "already has a tool named" in excinfo.value.detail

    async def test_two_agents_may_share_a_tool_name(
        self, db, user, make_agent, make_datasource  # noqa: ANN001
    ) -> None:
        """Uniqueness is per agent, not global — two agents can each expose their
        own ``query_orders``."""
        first = await make_agent(user, "first")
        second = await make_agent(user, "second")
        datasource = await make_datasource(user, "warehouse")

        await svc.create_tool_config(
            db, user.id, first.uuid, datasource.uuid, "query_orders", "orders"
        )
        config = await svc.create_tool_config(
            db, user.id, second.uuid, datasource.uuid, "query_orders", "orders"
        )

        assert config.data_agent_id == second.id

    async def test_an_over_long_description_is_rejected(
        self, db, user, make_agent, make_datasource  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")

        with pytest.raises(HTTPException):
            await svc.create_tool_config(
                db,
                user.id,
                agent.uuid,
                datasource.uuid,
                "query_orders",
                "orders",
                description="x" * 2001,
            )


# ---------------------------------------------------------------------------
# update_tool_config — and the two-agent return
# ---------------------------------------------------------------------------
class TestUpdateToolConfig:
    async def test_updates_in_place_and_returns_the_one_agent(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        config = await make_tool_config(agent, datasource, "query_orders")

        affected = await svc.update_tool_config(
            db, user.id, config.uuid, agent.uuid, datasource.uuid, "renamed_tool", "orders"
        )

        assert affected == {agent.id}
        await db.refresh(config)
        assert config.tool_name == "renamed_tool"

    async def test_moving_a_tool_returns_both_agents(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        """
        The case the docstring exists for. The agent the tool *left* is still
        describing it in its routing prompt, so it needs regenerating too —
        returning only the new agent would leave a stale prompt advertising a
        tool that agent no longer has.
        """
        origin = await make_agent(user, "origin")
        destination = await make_agent(user, "destination")
        datasource = await make_datasource(user, "warehouse")
        config = await make_tool_config(origin, datasource, "query_orders")

        affected = await svc.update_tool_config(
            db,
            user.id,
            config.uuid,
            destination.uuid,
            datasource.uuid,
            "query_orders",
            "orders",
        )

        assert affected == {origin.id, destination.id}

    async def test_the_tool_actually_moves(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        origin = await make_agent(user, "origin")
        destination = await make_agent(user, "destination")
        datasource = await make_datasource(user, "warehouse")
        config = await make_tool_config(origin, datasource, "query_orders")

        await svc.update_tool_config(
            db, user.id, config.uuid, destination.uuid, datasource.uuid,
            "query_orders", "orders",
        )
        await db.refresh(config)

        assert config.data_agent_id == destination.id

    async def test_the_datasource_can_be_changed(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        warehouse = await make_datasource(user, "warehouse")
        crm = await make_datasource(user, "crm")
        config = await make_tool_config(agent, warehouse, "query_orders")

        await svc.update_tool_config(
            db, user.id, config.uuid, agent.uuid, crm.uuid, "query_orders", "orders"
        )
        await db.refresh(config)

        assert config.datasource_id == crm.id

    async def test_keeping_the_same_name_is_allowed(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        """``exclude_id`` — without it a tool would report its own name as
        taken on every save."""
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        config = await make_tool_config(agent, datasource, "query_orders")

        affected = await svc.update_tool_config(
            db, user.id, config.uuid, agent.uuid, datasource.uuid,
            "query_orders", "orders", description="updated",
        )

        assert affected == {agent.id}

    async def test_a_name_another_tool_on_the_agent_has_is_rejected(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        await make_tool_config(agent, datasource, "taken_name")
        config = await make_tool_config(agent, datasource, "query_orders")

        with pytest.raises(HTTPException, match="already has a tool named"):
            await svc.update_tool_config(
                db, user.id, config.uuid, agent.uuid, datasource.uuid,
                "taken_name", "orders",
            )

    async def test_another_users_tool_config_is_404(
        self, db, user, other_user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        their_agent = await make_agent(other_user, "theirs")
        their_datasource = await make_datasource(other_user, "their_ds")
        config = await make_tool_config(their_agent, their_datasource, "query_orders")
        my_agent = await make_agent(user, "mine")
        my_datasource = await make_datasource(user, "my_ds")

        with pytest.raises(HTTPException) as excinfo:
            await svc.update_tool_config(
                db, user.id, config.uuid, my_agent.uuid, my_datasource.uuid,
                "hijacked", "orders",
            )

        assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# Enable / delete
# ---------------------------------------------------------------------------
class TestSetToolConfigEnabled:
    @pytest.mark.parametrize("enabled", [True, False])
    async def test_sets_the_flag_and_returns_the_agent(
        self, db, user, make_agent, make_datasource, make_tool_config, enabled: bool  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        config = await make_tool_config(
            agent, datasource, "query_orders", is_enabled=not enabled
        )

        affected = await svc.set_tool_config_enabled(db, user.id, config.uuid, enabled)

        assert affected == {agent.id}
        await db.refresh(config)
        assert config.is_enabled is enabled

    async def test_another_users_tool_is_404(
        self, db, user, other_user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        agent = await make_agent(other_user, "theirs")
        datasource = await make_datasource(other_user, "their_ds")
        config = await make_tool_config(agent, datasource, "query_orders")

        with pytest.raises(HTTPException) as excinfo:
            await svc.set_tool_config_enabled(db, user.id, config.uuid, False)

        assert excinfo.value.status_code == 404


class TestDeleteToolConfig:
    async def test_deletes_and_returns_the_owning_agent(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        """The agent id is read *before* the delete — afterwards there is no row
        to read it from, and the prompt still needs regenerating."""
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        config = await make_tool_config(agent, datasource, "query_orders")

        affected = await svc.delete_tool_config(db, user.id, config.uuid)

        assert affected == {agent.id}
        assert await svc.get_tool_config_views(db, user.id) == []

    async def test_the_agent_and_datasource_survive(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        """Deleting a tool removes a capability, not the agent that had it or the
        datasource it read."""
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        config = await make_tool_config(agent, datasource, "query_orders")

        await svc.delete_tool_config(db, user.id, config.uuid)

        assert (await svc.get_datasource_choices(db, user.id))[0]["name"] == "warehouse"
        assert (await svc.data_agent_service.get_data_agent(db, user.id, agent.uuid)).id == (
            agent.id
        )

    async def test_an_unknown_uuid_is_404(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.delete_tool_config(db, user.id, uuid_pkg.uuid4())

        assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------
class TestGetToolConfigViews:
    async def test_shapes_a_row_with_public_uuids_only(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        config = await make_tool_config(agent, datasource, "query_orders")

        (view,) = await svc.get_tool_config_views(db, user.id)

        assert view["uuid"] == str(config.uuid)
        assert view["agent_uuid"] == str(agent.uuid)
        assert view["agent_name"] == "reporter"
        assert view["datasource_name"] == "warehouse"
        assert "id" not in view

    async def test_includes_a_readable_query_preview(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        await make_tool_config(
            agent,
            datasource,
            "query_orders",
            config={"columns": [{"column": "total"}], "joins": []},
        )

        (view,) = await svc.get_tool_config_views(db, user.id)

        assert view["query_preview"] == "SELECT total FROM orders"

    async def test_filtering_by_agent(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        first = await make_agent(user, "first")
        second = await make_agent(user, "second")
        datasource = await make_datasource(user, "warehouse")
        await make_tool_config(first, datasource, "tool_a")
        await make_tool_config(second, datasource, "tool_b")

        views = await svc.get_tool_config_views(db, user.id, agent_id=first.uuid)

        assert [v["tool_name"] for v in views] == ["tool_a"]

    async def test_filtering_by_another_users_agent_is_404(
        self, db, user, other_user, make_agent  # noqa: ANN001
    ) -> None:
        """404 rather than an empty list — quietly returning nothing would hide
        the fact that the uuid belongs to someone else."""
        theirs = await make_agent(other_user, "theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.get_tool_config_views(db, user.id, agent_id=theirs.uuid)

        assert excinfo.value.status_code == 404


class TestGetDatasourceChoices:
    async def test_lists_datasources_with_join_support(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        await make_datasource(user, "warehouse", db_type="postgres")
        await make_datasource(user, "events", db_type="mongodb")

        choices = {c["name"]: c for c in await svc.get_datasource_choices(db, user.id)}

        assert choices["warehouse"]["supports_joins"] is True
        assert choices["events"]["supports_joins"] is False

    async def test_inactive_datasources_are_offered_but_flagged(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        """A tool is often defined before its datasource is switched on."""
        await make_datasource(user, "warehouse", is_active=False)

        (choice,) = await svc.get_datasource_choices(db, user.id)

        assert choice["is_active"] is False

    async def test_uses_public_uuids(self, db, user, make_datasource) -> None:  # noqa: ANN001
        datasource = await make_datasource(user, "warehouse")

        (choice,) = await svc.get_datasource_choices(db, user.id)

        assert choice["uuid"] == str(datasource.uuid)

    async def test_excludes_other_users_datasources(
        self, db, user, other_user, make_datasource  # noqa: ANN001
    ) -> None:
        await make_datasource(other_user, "theirs")

        assert await svc.get_datasource_choices(db, user.id) == []


class TestGetJoinOptions:
    async def test_no_datasource_selected_means_no_joins_section(
        self, db, user  # noqa: ANN001
    ) -> None:
        """Returned as a dict so the template has one thing to check — an empty
        ``join_types`` is exactly "don't render the Joins section", and
        ``supports_sql`` false is "don't offer the SQL-query mode"."""
        assert await svc.get_join_options(db, user.id, None) == {
            "supports_joins": False,
            "join_types": (),
            "supports_sql": False,
        }

    async def test_a_relational_datasource_supports_joins(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "warehouse", db_type="postgres")

        options = await svc.get_join_options(db, user.id, datasource.uuid)

        assert options["supports_joins"] is True
        assert options["join_types"]

    async def test_mongo_does_not_support_joins(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "events", db_type="mongodb")

        options = await svc.get_join_options(db, user.id, datasource.uuid)

        assert options["supports_joins"] is False


class TestGetTableAndColumnChoices:
    async def test_table_choices_are_sorted(
        self, db, user, make_datasource, stub_datasource_reads  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "warehouse")

        assert await svc.get_table_choices(db, user.id, datasource.uuid) == [
            "customers",
            "orders",
        ]

    async def test_file_datasource_objects_are_unwrapped(
        self, db, user, make_datasource, stub_datasource_reads  # noqa: ANN001
    ) -> None:
        """RDBMS and Mongo return plain names; a file datasource returns
        ``{"name", "file_id"}``. Both must reduce to a list of names."""
        datasource = await make_datasource(user, "uploaded", db_type="csv")
        stub_datasource_reads["objects"] = [
            {"name": "products.csv", "file_id": "abc"},
            {"name": "sales.csv", "file_id": "def"},
        ]

        assert await svc.get_table_choices(db, user.id, datasource.uuid) == [
            "products.csv",
            "sales.csv",
        ]

    async def test_blank_names_are_dropped(
        self, db, user, make_datasource, stub_datasource_reads  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "warehouse")
        stub_datasource_reads["objects"] = ["orders", "", None]

        assert await svc.get_table_choices(db, user.id, datasource.uuid) == ["orders"]

    async def test_a_connection_failure_propagates_its_message(
        self, db, user, make_datasource, stub_datasource_reads  # noqa: ANN001
    ) -> None:
        """A broken datasource shows a message in the form rather than an empty
        dropdown with no explanation."""
        datasource = await make_datasource(user, "warehouse")
        stub_datasource_reads["raise_on"] = "objects"

        with pytest.raises(HTTPException, match="connection failed"):
            await svc.get_table_choices(db, user.id, datasource.uuid)

    @pytest.mark.parametrize(
        "schema",
        [
            [{"column": "id"}, {"column": "total"}],
            [{"name": "id"}, {"name": "total"}],
            [{"column_name": "id"}, {"column_name": "total"}],
            ["id", "total"],
        ],
    )
    async def test_column_choices_accept_every_producer_shape(
        self, db, user, make_datasource, stub_datasource_reads, schema  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "warehouse")
        stub_datasource_reads["schema"] = schema

        assert await svc.get_column_choices(
            db, user.id, datasource.uuid, "orders"
        ) == ["id", "total"]

    @pytest.mark.parametrize("bad", ["", "   ", "bad;name"])
    async def test_column_choices_reject_an_invalid_table_name(
        self, db, user, make_datasource, stub_datasource_reads, bad: str  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "warehouse")

        with pytest.raises(HTTPException):
            await svc.get_column_choices(db, user.id, datasource.uuid, bad)

    async def test_column_map_covers_every_requested_table(
        self, db, user, make_datasource, stub_datasource_reads  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "warehouse")

        column_map = await svc.get_column_map(
            db, user.id, datasource.uuid, ["orders", "customers"]
        )

        assert set(column_map) == {"orders", "customers"}

    async def test_column_map_skips_blanks_and_duplicates(
        self, db, user, make_datasource, stub_datasource_reads  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "warehouse")

        column_map = await svc.get_column_map(
            db, user.id, datasource.uuid, ["orders", "orders", "", None]
        )

        assert list(column_map) == ["orders"]

    async def test_column_map_raises_rather_than_dropping_an_unreadable_table(
        self, db, user, make_datasource, stub_datasource_reads  # noqa: ANN001
    ) -> None:
        """Dropping it would leave the builder with a dropdown quietly missing
        half its options; raising turns it into a visible warning."""
        datasource = await make_datasource(user, "warehouse")
        stub_datasource_reads["raise_on"] = "schema"

        with pytest.raises(HTTPException, match="schema failed"):
            await svc.get_column_map(db, user.id, datasource.uuid, ["orders"])


# ---------------------------------------------------------------------------
# SQL mode
# ---------------------------------------------------------------------------
class TestValidatedToolSql:
    """
    The gate on a raw tool query.

    The rule is "any single read-only statement", which is a much wider door than
    the query builder's — so the tests that matter most are the ones showing that
    perfectly ordinary SQL the *builder* cannot hold is accepted here without
    complaint. That is the whole point of the mode existing.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT DISTINCT name FROM inventory_items",
            "SELECT name FROM items ORDER BY name LIMIT 10",
            "SELECT sku, COUNT(*) c FROM sales GROUP BY sku HAVING COUNT(*) > 5",
            "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent",
            "SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            "SELECT name FROM a UNION SELECT name FROM b",
            "SELECT RANK() OVER (ORDER BY total DESC) AS r, total FROM sales",
            "SELECT CASE WHEN qty > 0 THEN 'in' ELSE 'out' END AS state FROM stock",
            "SELECT * FROM orders WHERE id IN (SELECT order_id FROM refunds)",
            "select name from items",
        ],
    )
    def test_accepts_any_read_only_statement(self, sql: str) -> None:
        assert svc.validated_tool_sql(sql) == sql

    def test_a_trailing_semicolon_is_dropped_rather_than_refused(self) -> None:
        """Typing one is a habit, not a second statement."""
        assert svc.validated_tool_sql("SELECT 1 FROM t;  ") == "SELECT 1 FROM t"

    def test_a_markdown_fence_is_stripped(self) -> None:
        """Pasted straight out of the Ask AI panel or a chat window."""
        assert svc.validated_tool_sql("```sql\nSELECT 1 FROM t\n```") == "SELECT 1 FROM t"

    @pytest.mark.parametrize("blank", ["", "   ", None, "```sql\n```"])
    def test_an_empty_statement_is_required(self, blank) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException, match="Write the SQL query"):
            svc.validated_tool_sql(blank)

    @pytest.mark.parametrize(
        "sql",
        [
            "DELETE FROM orders",
            "UPDATE orders SET total = 0",
            "INSERT INTO orders (id) VALUES (1)",
            "DROP TABLE orders",
            "TRUNCATE orders",
            "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
            "SELECT * INTO backup FROM orders",
            "SELECT 1; DROP TABLE orders",
        ],
    )
    def test_anything_that_is_not_a_single_read_is_refused(self, sql: str) -> None:
        with pytest.raises(HTTPException) as excinfo:
            svc.validated_tool_sql(sql)

        assert excinfo.value.status_code == 400
        assert "read-only" in str(excinfo.value.detail)

    def test_a_write_word_inside_a_literal_is_not_a_write(self) -> None:
        """``WHERE action = 'delete'`` is an ordinary read, and the most likely
        false positive a keyword scan can produce."""
        sql = "SELECT id FROM audit WHERE action = 'delete' AND note = 'a;b'"

        assert svc.validated_tool_sql(sql) == sql

    def test_an_unusably_long_statement_is_refused(self) -> None:
        with pytest.raises(HTTPException, match="longer than"):
            svc.validated_tool_sql("SELECT " + ("x" * 9000) + " FROM t")


class TestSqlModeToolConfigs:
    async def test_creates_a_sql_tool_and_leaves_the_config_empty(
        self, db, user, make_agent, make_datasource  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")

        config = await svc.create_tool_config(
            db,
            user.id,
            agent.uuid,
            datasource.uuid,
            "distinct_items",
            "inventory_items",
            query_mode="sql",
            sql_query="SELECT DISTINCT name FROM inventory_items",
        )

        assert config.query_mode == "sql"
        assert config.sql_query == "SELECT DISTINCT name FROM inventory_items"
        assert config.config == {}

    async def test_a_builder_tool_stores_no_sql(
        self, db, user, make_agent, make_datasource  # noqa: ANN001
    ) -> None:
        """The two never coexist: whichever mode is saved clears the other."""
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")

        config = await svc.create_tool_config(
            db,
            user.id,
            agent.uuid,
            datasource.uuid,
            "query_orders",
            "orders",
            config_json=config_json(columns=[{"column": "total", "alias": ""}]),
            sql_query="SELECT 1 FROM orders",
        )

        assert config.query_mode == "builder"
        assert config.sql_query is None

    async def test_switching_a_sql_tool_back_to_the_builder_clears_the_sql(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        """Left behind, the stale statement would be what the executor ran — it
        prefers sql_query whenever it is set."""
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        tool = await make_tool_config(
            agent,
            datasource,
            "distinct_items",
            query_mode="sql",
            sql_query="SELECT DISTINCT name FROM inventory_items",
        )

        await svc.update_tool_config(
            db,
            user.id,
            tool.uuid,
            agent.uuid,
            datasource.uuid,
            "distinct_items",
            "orders",
            config_json=config_json(columns=[{"column": "total", "alias": ""}]),
        )
        await db.refresh(tool)

        assert tool.query_mode == "builder"
        assert tool.sql_query is None

    async def test_an_unknown_mode_is_refused(
        self, db, user, make_agent, make_datasource  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")

        with pytest.raises(HTTPException, match="built or written as SQL"):
            await svc.create_tool_config(
                db,
                user.id,
                agent.uuid,
                datasource.uuid,
                "query_orders",
                "orders",
                query_mode="freehand",
            )

    async def test_a_blank_mode_means_the_builder(
        self, db, user, make_agent, make_datasource  # noqa: ANN001
    ) -> None:
        """Every caller written before SQL mode existed sends no mode at all."""
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")

        config = await svc.create_tool_config(
            db, user.id, agent.uuid, datasource.uuid, "query_orders", "orders"
        )

        assert config.query_mode == "builder"

    async def test_sql_mode_is_refused_for_a_non_relational_datasource(
        self, db, user, make_agent, make_datasource  # noqa: ANN001
    ) -> None:
        """Refused at save time rather than on the agent's first call — a tool
        that can never run is a mistake the operator can still fix."""
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "events", db_type="mongodb")

        with pytest.raises(HTTPException, match="not a relational datasource"):
            await svc.create_tool_config(
                db,
                user.id,
                agent.uuid,
                datasource.uuid,
                "distinct_items",
                "events",
                query_mode="sql",
                sql_query="SELECT 1 FROM events",
            )

    async def test_a_sql_tool_previews_as_its_own_statement(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        await make_tool_config(
            agent,
            datasource,
            "distinct_items",
            query_mode="sql",
            sql_query="SELECT DISTINCT name FROM inventory_items",
        )

        (view,) = await svc.get_tool_config_views(db, user.id)

        assert view["query_mode"] == "sql"
        assert view["query_preview"] == "SELECT DISTINCT name FROM inventory_items"

    async def test_the_edit_view_carries_both_queries(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        """The form renders both panels and shows one, so a mode switch mid-edit
        needs no round trip and loses nothing."""
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        tool = await make_tool_config(
            agent,
            datasource,
            "distinct_items",
            query_mode="sql",
            sql_query="SELECT DISTINCT name FROM inventory_items",
        )

        view = await svc.get_tool_config_view(db, user.id, tool.uuid)

        assert view["query_mode"] == "sql"
        assert view["sql_query"] == "SELECT DISTINCT name FROM inventory_items"
        assert view["config"] == {}


class TestBuildQueryPreviewWithSql:
    def test_a_stored_statement_is_the_preview(self) -> None:
        assert (
            build_query_preview({}, "items", "SELECT DISTINCT name FROM items")
            == "SELECT DISTINCT name FROM items"
        )

    def test_a_blank_statement_falls_back_to_the_built_query(self) -> None:
        """Which is what a builder-mode config passes — sql_query is NULL there."""
        assert build_query_preview({}, "items", None) == "SELECT * FROM items"
        assert build_query_preview({}, "items", "  ") == "SELECT * FROM items"


class TestSupportsSql:
    @pytest.mark.parametrize("db_type", ["postgres", "mysql", "sqlite", "POSTGRES"])
    def test_relational_datasources_can_run_sql(self, db_type: str) -> None:
        assert svc.supports_sql(db_type) is True

    @pytest.mark.parametrize("db_type", ["mongodb", "csv", "", None])
    def test_everything_else_cannot(self, db_type) -> None:  # noqa: ANN001
        assert svc.supports_sql(db_type) is False
