"""
Tests for the active-table/active-column half of
app/services/sql_assist/sql_assist_service.py.

Ask AI is shown structure and never data, and this is what decides *which* structure:
the metadata handed to the model is pruned to what the user has left switched on in
Data Sources. Pruning rather than post-checking is the whole design — a model cannot
select, join on or filter by a column it was never told exists, and no SQL parser is
installed to police its output if it were.

The reflection itself is stubbed at the seam this module imports
(``metadata_service``): what is under test is the pruning and the prompt, not
SQLAlchemy's Inspector.
"""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

from app.services.sql_assist import sql_assist_service as svc
from tests.unit.services.sql_assist.conftest import (
    ORDERS,
    configuration as _configuration,
)


class TestTheTablePicker:
    async def test_an_inactive_table_is_not_offered(
        self, db, user, make_datasource, stub_reflection  # noqa: ANN001
    ) -> None:
        """It would be pruned out of the metadata anyway, so offering it could only
        ever produce a refusal."""
        datasource = await make_datasource(
            user, configuration_data=_configuration(orders=None),
        )

        assert await svc.get_table_choices(db, user.id, datasource.uuid) == ["customers"]

    async def test_an_all_inactive_datasource_says_so(
        self, db, user, make_datasource, stub_reflection  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(
            user, configuration_data=_configuration(orders=None, customers=None),
        )

        with pytest.raises(HTTPException, match="inactive"):
            await svc.get_table_choices(db, user.id, datasource.uuid)

    async def test_an_unconfigured_datasource_offers_everything(
        self, db, user, make_datasource, stub_reflection  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, configuration_data={})

        assert await svc.get_table_choices(db, user.id, datasource.uuid) == [
            "orders", "customers",
        ]


class TestTheMetadataTheModelIsShown:
    async def test_inactive_columns_are_removed(
        self, db, user, make_datasource, stub_reflection  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(
            user, configuration_data=_configuration(orders="total"),
        )

        metadata = await svc._load_metadata(datasource, ["orders"])

        assert [column["name"] for column in metadata[0]["columns"]] == [
            "id", "customer_id",
        ]

    async def test_an_inactive_primary_key_column_is_removed(
        self, db, user, make_datasource, stub_reflection  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(
            user, configuration_data=_configuration(orders="id"),
        )

        metadata = await svc._load_metadata(datasource, ["orders"])

        assert metadata[0]["primary_key"] == []

    async def test_a_foreign_key_onto_an_inactive_column_is_not_mentioned(
        self, db, user, make_datasource, stub_reflection  # noqa: ANN001
    ) -> None:
        """Otherwise the model is invited to join on a column it may not select —
        it would write the join and then be unable to select either side of it."""
        datasource = await make_datasource(
            user, configuration_data=_configuration(customers="id"),
        )

        metadata = await svc._load_metadata(datasource, ["orders", "customers"])

        assert metadata[0]["foreign_keys"] == []

    async def test_a_foreign_key_on_an_inactive_local_column_is_not_mentioned(
        self, db, user, make_datasource, stub_reflection  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(
            user, configuration_data=_configuration(orders="customer_id"),
        )

        metadata = await svc._load_metadata(datasource, ["orders"])

        assert metadata[0]["foreign_keys"] == []

    async def test_an_intact_foreign_key_survives(
        self, db, user, make_datasource, stub_reflection  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, configuration_data={})

        metadata = await svc._load_metadata(datasource, ["orders", "customers"])

        assert metadata[0]["foreign_keys"][0]["references_table"] == "customers"

    async def test_a_table_named_in_the_post_but_switched_off_is_refused(
        self, db, user, make_datasource, stub_reflection  # noqa: ANN001
    ) -> None:
        """The table list arrives as form fields, so it can name a table the picker
        no longer offers. Refused and named rather than filtered — a query generated
        against fewer tables than the user asked for looks correct and is not."""
        datasource = await make_datasource(
            user, configuration_data=_configuration(orders=None),
        )

        with pytest.raises(HTTPException, match="orders"):
            await svc._load_metadata(datasource, ["orders", "customers"])

    async def test_a_table_with_every_column_switched_off_is_refused(
        self, db, user, make_datasource, stub_reflection  # noqa: ANN001
    ) -> None:
        """Named per table: with two tables picked and one emptied, the all-tables
        check passes and the model would be handed a table it may read nothing from."""
        datasource = await make_datasource(
            user, configuration_data=_configuration(customers="id,name"),
        )

        with pytest.raises(HTTPException, match="Every column of 'customers'"):
            await svc._load_metadata(datasource, ["orders", "customers"])

    async def test_a_view_is_not_given_keys_it_never_had(
        self, db, user, make_datasource, stub_reflection  # noqa: ANN001
    ) -> None:
        """A view reflects without key entries at all. An empty list would tell the
        model the view has no primary key, which is a different claim."""
        stub_reflection["metadata"] = [
            {
                "table": "orders",
                "kind": "view",
                "columns": [{"name": "id", "type": "INTEGER", "nullable": True}],
            }
        ]
        datasource = await make_datasource(user, configuration_data={})

        metadata = await svc._load_metadata(datasource, ["orders"])

        assert "primary_key" not in metadata[0]
        assert "foreign_keys" not in metadata[0]


class TestThePrompt:
    def test_the_columns_to_select_are_listed_per_table(self) -> None:
        """Spelled out ahead of the schema JSON so the projection is something the
        model copies rather than derives from nested objects."""
        _, user_content = svc._build_prompts("PostgreSQL", [ORDERS], "list them", [])

        assert "orders: orders.id, orders.total, orders.customer_id" in user_content

    def test_selecting_a_star_is_forbidden_outright(self) -> None:
        system_prompt, _ = svc._build_prompts("PostgreSQL", [ORDERS], "list them", [])

        assert "NEVER write SELECT * or table.*" in system_prompt

    def test_every_column_is_required_for_a_row_listing(self) -> None:
        system_prompt, _ = svc._build_prompts("PostgreSQL", [ORDERS], "list them", [])

        assert "select EVERY column listed below for EVERY table" in system_prompt

    def test_aggregates_are_carved_out(self) -> None:
        """A rule the model has to break to answer "how many" is a rule it learns to
        ignore everywhere else."""
        system_prompt, _ = svc._build_prompts("PostgreSQL", [ORDERS], "count", [])

        assert "When the request IS an aggregate" in system_prompt

    def test_the_model_is_told_the_metadata_is_the_whole_permission(self) -> None:
        system_prompt, _ = svc._build_prompts("PostgreSQL", [ORDERS], "list", [])

        assert "does not exist for you" in system_prompt


class TestWhatIsEnforcedOnTheGeneratedQuery:
    def test_a_star_selection_is_refused(self) -> None:
        """`*` is the one selection the database resolves at run time, so a query
        approved today starts returning a switched-off column tomorrow."""
        with pytest.raises(HTTPException) as excinfo:
            svc._validated_sql("SELECT * FROM orders")

        assert excinfo.value.status_code == 502
        assert "SELECT *" in excinfo.value.detail

    def test_a_qualified_star_is_refused_too(self) -> None:
        with pytest.raises(HTTPException, match="o\\.\\*"):
            svc._validated_sql("SELECT o.* FROM orders o")

    def test_an_aggregate_over_all_rows_is_allowed(self) -> None:
        assert svc._validated_sql("SELECT COUNT(*) FROM orders") == (
            "SELECT COUNT(*) FROM orders"
        )

    def test_an_explicit_selection_passes(self) -> None:
        statement = "SELECT orders.id, orders.total FROM orders"

        assert svc._validated_sql(statement) == statement


class TestWhatIsOnlyReported:
    """
    "Includes every active column" is a text check, not a parse: it cannot tell a
    SELECT list from a WHERE clause, and it cannot tell that a CTE's outer query
    legitimately narrows what the inner one read. So it is reported next to the query
    and the decision stays the user's — refusing on it would reject every aggregate.
    """

    def test_the_omitted_columns_are_named(self) -> None:
        omitted = svc._omitted_columns("SELECT orders.id FROM orders", [ORDERS])

        assert omitted == ["orders.total", "orders.customer_id"]

    def test_a_full_selection_omits_nothing(self) -> None:
        omitted = svc._omitted_columns(
            "SELECT orders.id, orders.total, orders.customer_id FROM orders", [ORDERS],
        )

        assert omitted == []

    def test_an_aliased_table_still_counts(self) -> None:
        omitted = svc._omitted_columns(
            "SELECT o.id, o.total, o.customer_id FROM orders o", [ORDERS],
        )

        assert omitted == []

    def test_no_query_omits_nothing(self) -> None:
        """An empty query is the model reporting that the schema cannot answer the
        request — not a query missing its columns."""
        assert svc._omitted_columns("", [ORDERS]) == []
