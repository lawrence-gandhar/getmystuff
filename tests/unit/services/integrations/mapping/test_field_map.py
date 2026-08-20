"""
Tests for ``mapping/field_map.py``.

Three properties carry the module and each has a class of its own.

**Contradictions are refused, never resolved.** A mapping with both a ``source`` and a
``const`` does not have a documented winner — it fails to load. The tests assert the
refusal rather than a precedence, because a precedence rule is invisible on the canvas
and asserting one would freeze in the behaviour the module exists to avoid.

**The order of the five steps.** Default *after* transforms is the one that looks wrong
and is not: ``line_items[*].sku`` then ``first`` over a record with no line items is the
shape that makes it matter, and it has a test of its own.

**Nothing is guessed, and every problem is collected.** ``"abc"`` into a number field is
a refusal, and a record with three bad fields reports three.
"""

from __future__ import annotations

import pytest

from app.services.integrations.connectors.spec import FieldSpec
from app.services.integrations.mapping import field_map
from app.services.integrations.mapping.field_map import FieldMapping
from app.services.integrations.mapping.paths import PathError


def mapping(**overrides) -> FieldMapping:
    defaults = dict(target="email", source="customer.email")
    defaults.update(overrides)
    return FieldMapping(**defaults).validated()


class TestRefusingContradictions:
    def test_source_and_const_together_are_refused(self) -> None:
        """
        Not "const wins". A mapping that quietly ignores half of what its author wrote
        is one nobody can debug, and the rule that decides which half is invisible in
        the grid.
        """
        with pytest.raises(ValueError) as caught:
            FieldMapping(target="source_system", source="a.b", const="shopify").validated()

        message = str(caught.value)
        assert "a.b" in message and "shopify" in message, (
            "the refusal has to name both halves — the author has to see what it saw"
        )

    def test_a_transform_on_a_constant_is_refused(self) -> None:
        with pytest.raises(ValueError, match="fixed value"):
            FieldMapping(target="tag", const="new", transform=("upper",)).validated()

    def test_a_default_beside_a_constant_is_refused(self) -> None:
        """A fixed value is never absent, so the default could never fire. Silently
        dropping it would leave the author believing a fallback exists."""
        with pytest.raises(ValueError, match="never absent"):
            FieldMapping(target="tag", const="new", default="old").validated()

    def test_neither_source_nor_const_is_refused(self) -> None:
        with pytest.raises(ValueError, match="nothing to put in it"):
            FieldMapping(target="email").validated()

    def test_no_target_is_refused(self) -> None:
        with pytest.raises(ValueError, match="which field it fills in"):
            FieldMapping(target="   ", source="a").validated()


class TestRefusingAtSaveTime:
    """Everything a mapping can be wrong about, caught while the author is looking at
    the canvas rather than in the middle of a batch at three in the morning."""

    def test_an_unknown_transform_lists_the_real_ones(self) -> None:
        with pytest.raises(ValueError) as caught:
            FieldMapping(target="e", source="a", transform=("uppercase",)).validated()

        message = str(caught.value)
        assert "uppercase" in message
        assert "upper" in message, "a refusal that does not name the alternatives helps nobody"

    def test_an_unknown_type_lists_the_real_ones(self) -> None:
        with pytest.raises(ValueError) as caught:
            FieldMapping(target="e", source="a", type="varchar").validated()
        assert "string" in str(caught.value)

    def test_a_malformed_path_is_refused(self) -> None:
        with pytest.raises(PathError):
            FieldMapping(target="e", source="customer..email").validated()

    def test_an_expression_is_refused_rather_than_read_as_a_field(self) -> None:
        """``concat(a, b)`` parses as a legal key that matches nothing. Refusing it is
        the difference between a mapping that will not save and one that saves, runs and
        writes nulls."""
        with pytest.raises(PathError, match="expression"):
            FieldMapping(target="e", source="concat(first, last)").validated()

    def test_a_const_of_the_wrong_type_is_caught_now_not_per_record(self) -> None:
        """``{"const": "five", "type": "integer"}`` is a mistake the author can fix, and
        this is the only moment anybody is in a position to."""
        with pytest.raises(ValueError, match="five"):
            FieldMapping(target="qty", const="five", type="integer").validated()

    def test_the_source_path_is_parsed_once(self) -> None:
        """Fifty thousand records read the same path. Parsing it per record would be
        fifty thousand regex walks for an answer that cannot change."""
        loaded = mapping(source="line_items[0].sku")
        assert loaded.segments, "the parsed path is carried on the mapping"


class TestTheOrderOfTheSteps:
    def test_a_transform_chain_runs_left_to_right(self) -> None:
        loaded = mapping(source="e", transform=("trim", "lower"))
        assert loaded.read({"e": "  ADA@Example.COM  "}) == "ada@example.com"

    def test_the_default_applies_after_the_transforms(self) -> None:
        """
        The shape that makes the ordering matter: a wildcard read over a record with no
        line items yields an empty list, ``first`` turns that into ``None``, and *that*
        is when the default should fire. Defaulting first would hand ``first`` a literal.
        """
        loaded = mapping(
            target="sku", source="line_items[*].sku", transform=("first",), default="none"
        )

        assert loaded.read({"line_items": []}) == "none"
        assert loaded.read({"line_items": [{"sku": "A1"}]}) == "A1"

    def test_coercion_runs_after_the_default(self) -> None:
        """A default of ``"0"`` on an integer field arrives as ``0``, not ``"0"`` —
        otherwise the destination gets a string in a numeric field and the mapping that
        looks correct fails at the vendor."""
        loaded = mapping(target="qty", source="quantity", type="integer", default="0")
        assert loaded.read({}) == 0

    def test_required_is_decided_last_so_a_default_satisfies_it(self) -> None:
        loaded = mapping(target="tier", source="tier", required=True, default="standard")
        assert loaded.read({}) == "standard"

    def test_required_with_nothing_to_fall_back_on_refuses(self) -> None:
        loaded = mapping(target="email", source="customer.email", required=True)
        with pytest.raises(ValueError, match="required"):
            loaded.read({"customer": {}})

    def test_a_const_skips_straight_to_coercion(self) -> None:
        loaded = FieldMapping(target="qty", const="7", type="integer").validated()
        assert loaded.read({"anything": "ignored"}) == 7


class TestNothingIsGuessed:
    def test_text_where_a_number_belongs_is_a_refusal_not_a_zero(self) -> None:
        """The rule the whole mapping layer rests on. A record in somebody's CRM with a
        silently-zeroed amount is a wrong record with nothing in the log to find it by,
        which is strictly worse than one that failed."""
        loaded = mapping(target="total", source="total", type="number")
        with pytest.raises(ValueError):
            loaded.read({"total": "abc"})

    def test_an_absent_optional_field_is_not_an_error(self) -> None:
        loaded = mapping(target="phone", source="customer.phone")
        assert loaded.read({"customer": {}}) is None


class TestApplyingAWholeMapping:
    def test_a_record_is_built_from_every_mapping(self) -> None:
        mappings = field_map.load_mappings(
            [
                {"source": "customer.email", "target": "email", "transform": "lower"},
                {"source": "customer.name", "target": "name"},
                {"const": "shopify", "target": "source_system"},
            ]
        )

        outcome = field_map.apply_mappings(
            mappings, {"customer": {"email": "ADA@X.COM", "name": "Ada"}}
        )

        assert outcome.ok
        assert outcome.record == {
            "email": "ada@x.com",
            "name": "Ada",
            "source_system": "shopify",
        }

    def test_every_problem_is_collected_not_the_first(self) -> None:
        """
        An operator who fixes one mapping, re-runs fifty thousand records and meets the
        second problem tomorrow has been failed by the tool, not by the data.
        """
        mappings = field_map.load_mappings(
            [
                {"source": "a", "target": "qty", "type": "integer"},
                {"source": "b", "target": "total", "type": "number"},
                {"source": "c", "target": "when", "type": "datetime"},
            ]
        )

        outcome = field_map.apply_mappings(mappings, {"a": "x", "b": "y", "c": "z"})

        assert not outcome.ok
        assert outcome.fields_at_fault() == ("qty", "total", "when")

    def test_a_failed_field_is_absent_rather_than_null(self) -> None:
        """A half-built record with holes punched in it is the thing that gets written to
        a CRM by mistake. An absent key at least fails the destination's own required
        check."""
        mappings = field_map.load_mappings(
            [
                {"source": "a", "target": "qty", "type": "integer"},
                {"source": "b", "target": "name"},
            ]
        )

        outcome = field_map.apply_mappings(mappings, {"a": "x", "b": "Ada"})

        assert "qty" not in outcome.record
        assert outcome.record == {"name": "Ada"}

    def test_the_message_is_bounded_but_the_problem_list_is_not(self) -> None:
        mappings = field_map.load_mappings(
            [
                {"source": f"f{index}", "target": f"t{index}", "type": "integer"}
                for index in range(8)
            ]
        )

        outcome = field_map.apply_mappings(
            mappings, {f"f{index}": "x" for index in range(8)}
        )

        assert len(outcome.problems) == 8, "the full list survives for the log row"
        assert "and 5 more" in outcome.message()

    def test_a_batch_keeps_its_order(self) -> None:
        mappings = field_map.load_mappings([{"source": "n", "target": "n"}])
        outcomes = field_map.apply_to_batch(mappings, [{"n": str(i)} for i in range(5)])
        assert [outcome.record["n"] for outcome in outcomes] == ["0", "1", "2", "3", "4"]


class TestLoadingAList:
    def test_a_duplicated_target_names_both_positions(self) -> None:
        with pytest.raises(ValueError) as caught:
            field_map.load_mappings(
                [
                    {"source": "a", "target": "email"},
                    {"source": "b", "target": "name"},
                    {"source": "c", "target": "email"},
                ]
            )

        message = str(caught.value)
        assert "1" in message and "3" in message and "email" in message

    def test_a_bad_mapping_is_reported_by_position(self) -> None:
        with pytest.raises(ValueError, match="Field mapping 2"):
            field_map.load_mappings(
                [{"source": "a", "target": "x"}, {"target": "y"}]
            )

    def test_a_single_transform_may_be_written_as_a_string(self) -> None:
        """Every form this will be posted from writes ``"trim"``. Demanding
        ``["trim"]`` is pedantry enforced with a 400."""
        loaded = field_map.load_mappings([{"source": "a", "target": "b", "transform": "trim"}])
        assert loaded[0].transform == ("trim",)

    def test_nothing_loads_to_nothing(self) -> None:
        assert field_map.load_mappings(None) == ()
        assert field_map.load_mappings([]) == ()

    def test_targets_of_lists_the_destination_fields(self) -> None:
        loaded = field_map.load_mappings(
            [{"source": "a", "target": "email"}, {"const": "x", "target": "tag"}]
        )
        assert field_map.targets_of(loaded) == ("email", "tag")


class TestMatchByName:
    """The "map matching names" button. Exact after normalisation, which is not fuzzy
    matching, and the distinction is the whole safety argument."""

    def test_punctuation_and_case_are_ignored(self) -> None:
        matched = field_map.match_by_name(
            [FieldSpec(name="customerEmail", path="customer.email")],
            [FieldSpec(name="customer_email", type="string")],
        )

        assert len(matched) == 1
        assert matched[0].target == "customer_email"
        assert matched[0].source == "customer.email", (
            "a source field's response path is what a mapping reads, not its name"
        )

    def test_a_near_match_is_not_a_match(self) -> None:
        """
        ``email`` never becomes ``emails``. There is no edit distance here and no
        scoring: two names either reduce to the same string or they do not. A near-match
        that resolves silently writes somebody's data into the wrong field and reports
        success.
        """
        matched = field_map.match_by_name(
            [FieldSpec(name="emails"), FieldSpec(name="billing_email")],
            [FieldSpec(name="email")],
        )
        assert matched == ()

    def test_unmatched_targets_are_left_alone(self) -> None:
        matched = field_map.match_by_name(
            [FieldSpec(name="email")],
            [FieldSpec(name="email"), FieldSpec(name="loyalty_tier")],
        )
        assert field_map.targets_of(matched) == ("email",)

    def test_the_target_type_and_requiredness_come_from_the_destination(self) -> None:
        matched = field_map.match_by_name(
            [FieldSpec(name="qty", type="string")],
            [FieldSpec(name="qty", type="integer", required=True)],
        )
        assert matched[0].type == "integer"
        assert matched[0].required is True

    def test_the_first_source_wins_when_two_normalise_alike(self) -> None:
        """Otherwise which field is picked depends on iteration luck, and the grid shows
        a different answer on a different day."""
        matched = field_map.match_by_name(
            [FieldSpec(name="order_id", path="a"), FieldSpec(name="orderId", path="b")],
            [FieldSpec(name="orderid")],
        )
        assert matched[0].source == "a"
