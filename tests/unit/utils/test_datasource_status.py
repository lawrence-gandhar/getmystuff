"""Tests for app/utils/datasource_status.py — reading the Data Sources switches."""

from __future__ import annotations

import pytest

from app.utils import datasource_status as status


def _configuration(table_status: str = "active", **columns) -> dict:
    """One table named ``orders`` with the given per-column statuses."""
    return {
        "orders": {
            "status": table_status,
            "column_data": {
                name: {"column_name": name, "status": value}
                for name, value in columns.items()
            },
        }
    }


class TestAbsentConfigurationMeansActive:
    """
    Every datasource created before metadata collection worked has an empty
    ``configuration_data``. If "not recorded" read as "inactive", those users would
    open every dropdown in the application and find it empty — so the default has to
    be active, and it has to be the default at every level.
    """

    @pytest.mark.parametrize("configuration", [None, {}, {"other_table": {}}])
    def test_a_table_with_no_entry_is_active(self, configuration) -> None:
        assert status.is_table_active(configuration, "orders") is True

    @pytest.mark.parametrize("configuration", [None, {}, {"other_table": {}}])
    def test_a_column_of_an_unconfigured_table_is_active(self, configuration) -> None:
        assert status.is_column_active(configuration, "orders", "id") is True

    def test_a_table_entry_without_a_status_is_active(self) -> None:
        assert status.is_table_active({"orders": {"column_data": {}}}, "orders") is True

    def test_a_column_with_no_entry_is_active(self) -> None:
        """A column discovered after the datasource was configured, or a table whose
        ``column_data`` was never populated — the column view already treats both as
        every-column-active, and this must not contradict it."""
        configuration = _configuration(id="active")
        assert status.is_column_active(configuration, "orders", "total") is True

    def test_an_empty_column_data_leaves_every_column_active(self) -> None:
        configuration = {"orders": {"status": "active", "column_data": {}}}
        assert status.active_column_names(configuration, "orders", ["id", "total"]) == [
            "id", "total",
        ]


class TestOnlyLiteralInactiveSwitchesSomethingOff:
    """
    The check is against ``"inactive"``, never for ``"active"``. An unknown value is
    a value nobody chose, and defaulting it to off would hide data on the strength of
    a typo.
    """

    @pytest.mark.parametrize("value", ["inactive", "INACTIVE", "  Inactive  "])
    def test_the_literal_in_any_casing_deactivates(self, value) -> None:
        assert status.is_table_active({"orders": {"status": value}}, "orders") is False

    @pytest.mark.parametrize("value", ["active", "", None, "disabled", "off", 0])
    def test_anything_else_stays_active(self, value) -> None:
        assert status.is_table_active({"orders": {"status": value}}, "orders") is True


class TestAnInactiveTableHidesItsColumns:
    """
    ``toggle_table_status_service`` cascades the table switch onto its columns when it
    is written, but a row hand-edited in the database can disagree with itself. The
    read side re-applies the cascade so a column can never be reported active under a
    table nobody switched on.
    """

    def test_columns_recorded_active_under_an_inactive_table_are_inactive(self) -> None:
        configuration = _configuration(table_status="inactive", id="active")
        assert status.is_column_active(configuration, "orders", "id") is False

    def test_the_active_column_list_of_an_inactive_table_is_empty(self) -> None:
        configuration = _configuration(table_status="inactive", id="active")
        assert status.active_column_names(configuration, "orders", ["id"]) == []


class TestFilteringUsesTheCallersList:
    def test_order_is_preserved(self) -> None:
        configuration = _configuration(id="active", total="inactive", note="active")
        assert status.active_column_names(
            configuration, "orders", ["note", "total", "id"],
        ) == ["note", "id"]

    def test_a_name_only_in_the_configuration_is_never_returned(self) -> None:
        """A column dropped from the real table can linger in ``configuration_data``.
        Offering it would produce a query against a column that does not exist."""
        configuration = _configuration(id="active", dropped_column="active")
        assert status.active_column_names(configuration, "orders", ["id"]) == ["id"]

    def test_inactive_table_names_reports_the_other_side(self) -> None:
        configuration = {"orders": {"status": "inactive"}}
        assert status.inactive_table_names(configuration, ["orders", "customers"]) == [
            "orders",
        ]

    def test_active_columns_by_table_keys_stay_put(self) -> None:
        configuration = {
            "orders": {"status": "active",
                       "column_data": {"total": {"status": "inactive"}}},
            "customers": {"status": "active", "column_data": {}},
        }
        assert status.active_columns_by_table(
            configuration, {"orders": ["id", "total"], "customers": ["id"]},
        ) == {"orders": ["id"], "customers": ["id"]}


class TestMalformedConfigurationNeverRaises:
    """
    ``configuration_data`` is a JSON column written by the user's own toggling and
    editable by hand. A shape nobody anticipated must read as unconfigured, not take
    the page down.
    """

    @pytest.mark.parametrize("configuration", [[], "orders", 7, {"orders": "active"}])
    def test_unusable_shapes_read_as_active(self, configuration) -> None:
        assert status.is_table_active(configuration, "orders") is True
        assert status.is_column_active(configuration, "orders", "id") is True

    def test_a_non_dict_column_entry_reads_as_active(self) -> None:
        configuration = {"orders": {"status": "active", "column_data": {"id": "on"}}}
        assert status.is_column_active(configuration, "orders", "id") is True

    def test_a_non_dict_column_data_reads_as_active(self) -> None:
        configuration = {"orders": {"status": "active", "column_data": ["id"]}}
        assert status.is_column_active(configuration, "orders", "id") is True


class TestFirstInactiveReference:
    """
    The executor validates stored references — ``"column"`` or ``"table.column"`` —
    and has to split them exactly the way ``query_joins.validated_column_reference``
    does, or an unjoined query whose column name contains a dot would be read as a
    reference to a table that is not in the query.
    """

    def test_a_bare_name_means_the_base_table(self) -> None:
        configuration = _configuration(total="inactive")
        assert status.first_inactive_reference(
            configuration, ["id", "total"], "orders",
        ) == "total"

    def test_a_dotted_name_is_split_only_when_the_query_is_joined(self) -> None:
        configuration = {
            "orders": {"status": "active",
                       "column_data": {"customers.id": {"status": "inactive"}}},
            "customers": {"status": "active",
                          "column_data": {"id": {"status": "inactive"}}},
        }

        # Unjoined: the whole string is a column of the base table.
        assert status.first_inactive_reference(
            configuration, ["customers.id"], "orders",
        ) == "customers.id"

        # Joined: it is customers.id, which is also inactive — but resolved there.
        assert status.first_inactive_reference(
            configuration, ["customers.id"], "orders",
            known_tables=["orders", "customers"],
        ) == "customers.id"

    def test_an_active_joined_reference_passes(self) -> None:
        configuration = {
            "orders": {"status": "active", "column_data": {}},
            "customers": {"status": "active", "column_data": {}},
        }
        assert status.first_inactive_reference(
            configuration, ["orders.id", "customers.name"], "orders",
            known_tables=["orders", "customers"],
        ) is None

    @pytest.mark.parametrize("references", [None, [], ["", "   "]])
    def test_nothing_to_check_passes(self, references) -> None:
        assert status.first_inactive_reference({}, references, "orders") is None


class TestMessages:
    """Pinned so a reword is a deliberate change — these sentences are what a user
    sees in a form and what an agent relays to a visitor."""

    def test_no_active_tables(self) -> None:
        assert "Activate at least one in Data Sources" in status.NO_ACTIVE_TABLES_MESSAGE

    def test_the_table_and_column_are_named(self) -> None:
        assert "'orders'" in status.inactive_table_message("orders")
        assert "'orders'" in status.no_active_columns_message("orders")
        assert "'orders.total'" in status.inactive_column_message("orders.total")
