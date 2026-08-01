"""Tests for app/utils/query_joins.py — multi-table join rules."""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

from app.utils import query_joins


def _join(table: str, left_table: str, **overrides) -> dict:
    entry = {
        "type": "inner",
        "table": table,
        "left_table": left_table,
        "left_column": "id",
        "right_column": f"{left_table}_id",
    }
    entry.update(overrides)
    return entry


class TestSupportsJoins:
    @pytest.mark.parametrize("db_type", ["postgres", "mysql", "sqlite"])
    def test_relational_types_support_joins(self, db_type) -> None:
        assert query_joins.supports_joins(db_type) is True

    @pytest.mark.parametrize("db_type", ["mongodb", "csv", "xlsx", "parquet", None, "", "  "])
    def test_single_object_types_do_not(self, db_type) -> None:
        assert query_joins.supports_joins(db_type) is False

    def test_is_case_and_whitespace_insensitive(self) -> None:
        assert query_joins.supports_joins("  POSTGRES  ") is True


class TestJoinTypesFor:
    def test_postgres_gets_every_join_type(self) -> None:
        assert query_joins.join_types_for("postgres") == query_joins.JOIN_TYPES

    def test_mysql_excludes_full_outer_join(self) -> None:
        """MySQL has no FULL OUTER JOIN; offering one produces a query that fails."""
        values = [value for value, _ in query_joins.join_types_for("mysql")]
        assert values == ["inner", "left", "right"]
        assert "full" not in values

    def test_non_relational_gets_nothing(self) -> None:
        """Empty so a template can use it directly to hide the Joins section."""
        assert query_joins.join_types_for("mongodb") == ()


class TestValidatedJoins:
    @pytest.mark.parametrize("raw", [None, []])
    def test_absent_joins_are_not_an_error(self, raw) -> None:
        assert query_joins.validated_joins(raw, "customers", "postgres") == []

    def test_non_list_is_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc:
            query_joins.validated_joins({"type": "inner"}, "customers", "postgres")
        assert exc.value.detail == "Joins are not in the expected format"

    def test_normalises_and_keeps_only_known_keys(self) -> None:
        """Entries are rebuilt field by field, so injected keys are not persisted."""
        result = query_joins.validated_joins(
            [_join("orders", "customers", type="LEFT", malicious="dropped")],
            "customers",
            "postgres",
        )
        assert result == [
            {
                "type": "left",
                "table": "orders",
                "left_table": "customers",
                "left_column": "id",
                "right_column": "customers_id",
            }
        ]

    def test_joins_on_a_non_relational_datasource_are_refused(self) -> None:
        with pytest.raises(HTTPException) as exc:
            query_joins.validated_joins([_join("orders", "customers")], "customers", "mongodb")
        assert "only available for relational datasources" in exc.value.detail

    def test_mysql_rejects_a_full_outer_join(self) -> None:
        with pytest.raises(HTTPException) as exc:
            query_joins.validated_joins(
                [_join("orders", "customers", type="full")], "customers", "mysql"
            )
        assert "Every join needs a valid type" in exc.value.detail

    def test_rejects_more_than_max_joins(self) -> None:
        entries = [_join(f"t{i}", "customers") for i in range(query_joins.MAX_JOINS + 1)]
        with pytest.raises(HTTPException) as exc:
            query_joins.validated_joins(entries, "customers", "postgres")
        assert f"cannot join more than {query_joins.MAX_JOINS} tables" in exc.value.detail

    def test_accepts_exactly_max_joins(self) -> None:
        entries = [_join(f"t{i}", "customers") for i in range(query_joins.MAX_JOINS)]
        assert len(query_joins.validated_joins(entries, "customers", "postgres")) == 10

    def test_rejects_a_non_dict_entry(self) -> None:
        with pytest.raises(HTTPException) as exc:
            query_joins.validated_joins(["orders"], "customers", "postgres")
        assert exc.value.detail == "Joins are not in the expected format"

    def test_rejects_an_unknown_join_type(self) -> None:
        with pytest.raises(HTTPException) as exc:
            query_joins.validated_joins(
                [_join("orders", "customers", type="cross")], "customers", "postgres"
            )
        assert "Every join needs a valid type" in exc.value.detail

    def test_rejects_joining_the_same_table_twice(self) -> None:
        entries = [_join("orders", "customers"), _join("orders", "customers")]
        with pytest.raises(HTTPException) as exc:
            query_joins.validated_joins(entries, "customers", "postgres")
        assert "already part of this query" in exc.value.detail

    def test_rejects_joining_onto_the_base_table_itself(self) -> None:
        with pytest.raises(HTTPException) as exc:
            query_joins.validated_joins(
                [_join("customers", "customers")], "customers", "postgres"
            )
        assert "already part of this query" in exc.value.detail

    def test_rejects_a_reference_to_a_table_not_yet_joined(self) -> None:
        """The chain must stay connected and in order."""
        with pytest.raises(HTTPException) as exc:
            query_joins.validated_joins(
                [_join("orders", "shipments")], "customers", "postgres"
            )
        assert "is not part of this query" in exc.value.detail

    def test_accepts_a_chain_that_builds_on_a_previous_join(self) -> None:
        entries = [_join("orders", "customers"), _join("line_items", "orders")]
        result = query_joins.validated_joins(entries, "customers", "postgres")
        assert [entry["table"] for entry in result] == ["orders", "line_items"]

    def test_rejects_the_same_chain_in_the_wrong_order(self) -> None:
        entries = [_join("line_items", "orders"), _join("orders", "customers")]
        with pytest.raises(HTTPException):
            query_joins.validated_joins(entries, "customers", "postgres")

    def test_rejects_an_injection_attempt_in_a_table_name(self) -> None:
        with pytest.raises(HTTPException):
            query_joins.validated_joins(
                [_join('orders"; DROP TABLE users; --', "customers")],
                "customers",
                "postgres",
            )

    def test_rejects_a_missing_column(self) -> None:
        entry = _join("orders", "customers")
        del entry["right_column"]
        with pytest.raises(HTTPException) as exc:
            query_joins.validated_joins([entry], "customers", "postgres")
        assert exc.value.detail == "Join right column is required"


class TestQueryTables:
    @pytest.mark.parametrize("joins", [None, []])
    def test_no_joins_yields_empty(self, joins) -> None:
        """Both callers read [] as 'one table, so column references stay bare'."""
        assert query_joins.query_tables(joins, "customers") == []

    def test_lists_base_table_first_then_each_join(self) -> None:
        joins = [_join("orders", "customers"), _join("line_items", "orders")]
        assert query_joins.query_tables(joins, "customers") == [
            "customers",
            "orders",
            "line_items",
        ]


class TestValidatedColumnReference:
    def test_without_joins_a_bare_name_passes(self) -> None:
        assert query_joins.validated_column_reference("total", "Column") == "total"

    def test_without_joins_a_qualified_name_is_not_checked(self) -> None:
        assert (
            query_joins.validated_column_reference("anything.total", "Column")
            == "anything.total"
        )

    def test_qualified_reference_to_a_known_table_passes(self) -> None:
        assert (
            query_joins.validated_column_reference(
                "orders.total", "Column", ["customers", "orders"]
            )
            == "orders.total"
        )

    def test_bare_reference_still_allowed_with_joins(self) -> None:
        """A pre-join config must stay editable after a join is added."""
        assert (
            query_joins.validated_column_reference("total", "Column", ["customers", "orders"])
            == "total"
        )

    def test_rejects_a_reference_to_an_unjoined_table(self) -> None:
        with pytest.raises(HTTPException) as exc:
            query_joins.validated_column_reference(
                "shipments.total", "Column", ["customers", "orders"]
            )
        assert "which is not part of this query" in exc.value.detail

    def test_rejects_a_trailing_dot(self) -> None:
        with pytest.raises(HTTPException) as exc:
            query_joins.validated_column_reference("orders.", "Column", ["orders"])
        assert "is not a valid name" in exc.value.detail

    def test_rejects_a_doubly_qualified_reference(self) -> None:
        with pytest.raises(HTTPException) as exc:
            query_joins.validated_column_reference("orders.a.b", "Column", ["orders"])
        assert "is not a valid name" in exc.value.detail


class TestBuildJoinSql:
    @pytest.mark.parametrize("joins", [None, []])
    def test_no_joins_yields_no_clauses(self, joins) -> None:
        assert query_joins.build_join_sql(joins) == []

    def test_renders_the_sql_keyword_for_each_type(self) -> None:
        joins = [
            _join("orders", "customers", type="inner"),
            _join("shipments", "orders", type="full"),
        ]
        clauses = query_joins.build_join_sql(joins)
        assert clauses == [
            "INNER JOIN orders ON customers.id = orders.customers_id",
            "FULL OUTER JOIN shipments ON orders.id = shipments.orders_id",
        ]

    def test_skips_an_incomplete_entry_rather_than_rendering_broken_sql(self) -> None:
        assert query_joins.build_join_sql([{"type": "inner", "table": "orders"}]) == []

    def test_skips_an_unknown_join_type(self) -> None:
        assert query_joins.build_join_sql([_join("orders", "customers", type="cross")]) == []
