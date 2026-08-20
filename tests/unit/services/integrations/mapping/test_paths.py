"""
Tests for ``mapping/paths.py``.

The grammar is three forms and nothing else, and most of this file is about what it
refuses. Each refusal is a feature somebody will ask for, and each one is refused because
allowing it would put an evaluator behind a form field that a user — and in Phase 1
sometimes a language model — writes into.

The one asymmetry worth reading: ``read`` returns ``None`` for a missing field and
``read_records`` **raises** when a path resolves to something that is not a list. A
record without a shipping address is a fact about the record; being handed one object
where a page of records was promised means the path is wrong, and treating it as "one
record" would sync one row per page and report success.
"""

from __future__ import annotations

import pytest

from app.services.integrations.mapping.paths import (
    PathError,
    first_present,
    is_valid,
    parse,
    read,
    read_records,
)

ORDER = {
    "id": 1234,
    "customer": {"email": "jane@example.com", "address": None},
    "line_items": [
        {"sku": "A1", "qty": 2, "price": {"amount": "10.00"}},
        {"sku": "B2", "qty": 1, "price": {"amount": "5.50"}},
    ],
    "@odata.nextLink": "https://api.example.com/orders?skip=100",
}


class TestTheThreeForms:
    def test_a_key(self) -> None:
        assert read(ORDER, "id") == 1234

    def test_nested_keys(self) -> None:
        assert read(ORDER, "customer.email") == "jane@example.com"

    def test_an_index(self) -> None:
        assert read(ORDER, "line_items[0].sku") == "A1"
        assert read(ORDER, "line_items[1].price.amount") == "5.50"

    def test_a_wildcard_gives_a_list(self) -> None:
        assert read(ORDER, "line_items[*].sku") == ["A1", "B2"]

    def test_a_wildcard_reaches_through_nesting(self) -> None:
        assert read(ORDER, "line_items[*].price.amount") == ["10.00", "5.50"]

    def test_a_key_may_contain_punctuation_vendors_use(self) -> None:
        """Dashes, colons and ``@`` are all real key characters."""
        assert read({"a": {"@type": 1, "x-total": 2}}, "a.@type") == 1
        assert read({"a": {"x-total": 2}}, "a.x-total") == 2

    def test_a_quoted_key_holds_a_dot(self) -> None:
        """
        SAP sends ``@odata.nextLink`` as one key, dot included. Without the quoted form
        that path means "the ``nextLink`` inside ``@odata``" and reads nothing —
        silently, as a paged read that stops after page one.
        """
        assert read(ORDER, '["@odata.nextLink"]') == "https://api.example.com/orders?skip=100"

    def test_the_unquoted_form_of_the_same_path_reads_nothing(self) -> None:
        """The control for the test above. Both are legal paths; only one is right."""
        assert read(ORDER, "@odata.nextLink") is None

    def test_a_quoted_key_works_mid_path(self) -> None:
        assert read({"meta": {"a.b": 7}}, 'meta["a.b"]') == 7

    def test_single_quotes_work_too(self) -> None:
        assert read({"a.b": 7}, "['a.b']") == 7


class TestMissingIsNotMalformed:
    @pytest.mark.parametrize(
        "path",
        ["nope", "customer.nope", "customer.address.line1", "line_items[9].sku"],
    )
    def test_a_missing_field_is_none(self, path: str) -> None:
        assert read(ORDER, path) is None

    def test_reading_through_a_scalar_is_none_rather_than_an_error(self) -> None:
        """
        A field that is sometimes an object and sometimes a scalar is an ordinary shape
        in a third-party API, and the record is not at fault.
        """
        assert read(ORDER, "id.something") is None

    def test_a_wildcard_over_something_that_is_not_a_list_is_empty(self) -> None:
        """A vendor returning ``null`` for an empty collection is common enough that
        treating it as a fault would fail records that are fine."""
        assert read(ORDER, "customer[*].email") == []


class TestTheGrammarIsClosed:
    """Each of these is a feature somebody will ask for. See the module docstring."""

    @pytest.mark.parametrize(
        "path",
        [
            "items[?(@.qty > 1)]",     # a predicate
            "items[-1]",               # a negative index
            "items[1:3]",              # a slice
            "concat(a, b)",            # a function
            "$.customer.email",        # a JSONPath root
        ],
    )
    def test_it_is_refused(self, path: str) -> None:
        with pytest.raises(PathError):
            parse(path)

    def test_a_function_is_refused_rather_than_read_as_a_field_name(self) -> None:
        """
        ``concat(a, b)`` is a legal *key* as far as the bare grammar is concerned, so
        without the expression check it would parse and match nothing. A mapping that
        quietly matches nothing is worse than one that will not save.
        """
        with pytest.raises(PathError, match="looks like an expression"):
            parse("concat(a, b)")

    def test_the_refusal_names_what_to_use_instead(self) -> None:
        with pytest.raises(PathError) as caught:
            parse("upper(name)")

        assert "Filter step" in str(caught.value)
        assert "two mappings and a transform" in str(caught.value)

    def test_a_predicate_says_to_use_a_filter_step_instead(self) -> None:
        """
        The refusal names the affordance that does exist, because "not supported" leaves
        the author with a workflow they cannot finish.
        """
        with pytest.raises(PathError, match="Filter step"):
            parse("items[?(@.qty > 1)]")

    def test_recursive_descent_is_not_a_path(self) -> None:
        """``$..email`` silently changes meaning when the vendor adds a field."""
        with pytest.raises(PathError):
            parse("$..email")


class TestMalformedPaths:
    @pytest.mark.parametrize(
        ("path", "message"),
        [
            ("", "cannot be empty"),
            ("customer..email", "empty step"),
            ("customer.", "ends with a"),
            ("line_items[0", "never closed"),
        ],
    )
    def test_the_refusal_says_what_is_wrong(self, path: str, message: str) -> None:
        with pytest.raises(PathError, match=message):
            parse(path)

    def test_is_valid_is_what_the_validator_asks(self) -> None:
        assert is_valid("customer.email")
        assert not is_valid("customer..email")

    def test_a_malformed_path_is_caught_at_save_time(self) -> None:
        """
        ``validate_flow`` calls ``parse`` on every mapping, so this is refused while the
        author is looking at the canvas rather than at three in the morning.
        """
        assert not is_valid("items[?(@.qty > 1)]")


class TestReadRecords:
    def test_an_empty_path_means_the_body_is_the_list(self) -> None:
        assert read_records([{"id": 1}, {"id": 2}]) == [{"id": 1}, {"id": 2}]

    def test_a_path_to_the_list(self) -> None:
        assert read_records({"data": {"orders": [{"id": 1}]}}, "data.orders") == [{"id": 1}]

    def test_a_missing_path_is_an_empty_page(self) -> None:
        """It is how most APIs say "that is all"."""
        assert read_records({"data": {}}, "data.orders") == []

    def test_a_single_object_where_a_list_was_promised_raises(self) -> None:
        """
        The asymmetry in the module docstring. Treating it as one record would sync one
        row per page and report success.
        """
        with pytest.raises(PathError, match="does not hold a list"):
            read_records({"data": {"id": 1, "email": "a@b.com"}}, "data")

    def test_the_refusal_describes_what_it_found(self) -> None:
        with pytest.raises(PathError) as caught:
            read_records({"data": {"id": 1, "email": "a@b.com"}}, "data")

        assert "id, email" in str(caught.value)

    def test_a_scalar_where_a_list_was_promised_raises(self) -> None:
        with pytest.raises(PathError, match="a str"):
            read_records({"data": "none"}, "data")


class TestFirstPresent:
    def test_it_takes_the_first_that_yields_something(self) -> None:
        record = {"external_id": "X-1", "id": 5}

        assert first_present(record, ["id", "external_id"]) == 5
        assert first_present(record, ["external_id", "id"]) == "X-1"

    def test_it_skips_the_ones_that_are_absent(self) -> None:
        assert first_present({"id": 5}, ["uuid", "external_id", "id"]) == 5

    def test_nothing_present_is_none(self) -> None:
        assert first_present({"id": 5}, ["uuid"]) is None
