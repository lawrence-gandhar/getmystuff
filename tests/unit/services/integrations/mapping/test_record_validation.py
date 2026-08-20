"""
Tests for ``mapping/record_validation.py``.

The field list *is* the schema — there is no ``jsonschema`` here — so these tests are
about the two rules and the order they run in.

**Required before coercion.** A required integer field with nothing in it reports
"nothing was mapped into it", not a type error. Running coercion first would send the
operator to look at a transform when the problem is upstream in whatever was supposed to
supply the field.

**Never coerce past a failed coercion**, and collect every problem rather than the first.

The last class is about save time rather than run time: the unmapped-required check that
turns the mapping panel's red warning from decoration into a publish refusal, and the
``require_field`` refusal that catches the most damaging hallucination the AI layer can
produce.
"""

from __future__ import annotations

import pytest

from app.services.integrations.connectors.spec import FieldSpec
from app.services.integrations.mapping import record_validation


CONTACT = (
    FieldSpec(name="email", label="Email address", type="string", required=True),
    FieldSpec(name="first_name", type="string"),
    FieldSpec(name="lifetime_value", type="number"),
    FieldSpec(name="signed_up_at", type="datetime"),
)


class TestRequiredComesFirst:
    def test_a_missing_required_field_says_so_rather_than_reporting_a_type(self) -> None:
        """
        The order is the point. "nothing was mapped into it" sends the operator to the
        mapping grid; "expected a number but got None" sends them to a transform that is
        working perfectly.
        """
        outcome = record_validation.validate_record({}, CONTACT)

        assert not outcome.ok
        assert outcome.fields_at_fault() == ("email",)
        assert "required" in outcome.problems[0].message
        assert "Email address" in outcome.problems[0].message, (
            "the label is what the author sees in the picker"
        )

    def test_an_empty_string_counts_as_absent_for_a_required_field(self) -> None:
        """A form field somebody cleared and an API that sends "" for "not set" are the
        same fact, and a destination that requires a value does not want "" either."""
        outcome = record_validation.validate_record({"email": ""}, CONTACT)
        assert not outcome.ok

    def test_an_absent_optional_field_is_not_a_problem(self) -> None:
        outcome = record_validation.validate_record({"email": "a@b.com"}, CONTACT)
        assert outcome.ok

    def test_an_optional_field_is_not_defaulted_here(self) -> None:
        """A default is a mapping's decision, made in ``field_map`` where the author can
        see it. Inventing one here would put a value in a record that no line of the
        drawing asked for."""
        outcome = record_validation.validate_record({"email": "a@b.com"}, CONTACT)
        assert "lifetime_value" not in outcome.record


class TestCoercion:
    def test_a_value_is_returned_coerced_not_merely_approved(self) -> None:
        outcome = record_validation.validate_record(
            {"email": "a@b.com", "lifetime_value": "12.50"}, CONTACT
        )
        assert outcome.record["lifetime_value"] == 12.5

    def test_text_where_a_number_belongs_is_refused_not_zeroed(self) -> None:
        outcome = record_validation.validate_record(
            {"email": "a@b.com", "lifetime_value": "abc"}, CONTACT
        )

        assert not outcome.ok
        assert outcome.fields_at_fault() == ("lifetime_value",)
        assert "lifetime_value" not in outcome.record, (
            "a value that would not coerce must not appear in the coerced record"
        )

    def test_every_bad_field_is_reported(self) -> None:
        outcome = record_validation.validate_record(
            {"email": "a@b.com", "lifetime_value": "abc", "signed_up_at": "never"},
            CONTACT,
        )
        assert outcome.fields_at_fault() == ("lifetime_value", "signed_up_at")

    def test_what_coerced_cleanly_survives_on_an_invalid_record(self) -> None:
        """The dead-letter row stores the payload so the record can be replayed after the
        mapping is fixed, and the half-converted form says how far it got."""
        outcome = record_validation.validate_record(
            {"email": "a@b.com", "first_name": "Ada", "lifetime_value": "abc"}, CONTACT
        )

        assert not outcome.ok
        assert outcome.record["first_name"] == "Ada"


class TestUnknownKeys:
    def test_a_field_the_list_does_not_mention_is_kept_by_default(self) -> None:
        """The ``validate`` node sits in the middle of a flow. Discarding a field a later
        step maps would break the flow in a way that looks like the vendor's fault."""
        outcome = record_validation.validate_record(
            {"email": "a@b.com", "internal_ref": "X1"}, CONTACT
        )
        assert outcome.record["internal_ref"] == "X1"

    def test_a_write_drops_what_the_destination_never_declared(self) -> None:
        """Sending a vendor a key it does not know is at best ignored and at worst a 400
        naming a field the author never wrote."""
        outcome = record_validation.validate_record(
            {"email": "a@b.com", "internal_ref": "X1"}, CONTACT, keep_unknown=False
        )
        assert "internal_ref" not in outcome.record

    def test_an_empty_field_list_checks_nothing(self) -> None:
        """A node with no field list has nothing to check against, and inventing rules
        would mean guessing at somebody else's API."""
        outcome = record_validation.validate_record({"anything": 1}, ())
        assert outcome.ok


class TestPartitioning:
    def test_a_batch_splits_into_two_recordsets(self) -> None:
        split = record_validation.partition(
            [
                {"email": "a@b.com"},
                {"first_name": "Ada"},
                {"email": "c@d.com", "lifetime_value": "9"},
            ],
            CONTACT,
        )

        assert len(split.valid) == 2
        assert len(split.invalid) == 1
        assert split.counts() == {"valid": 2, "invalid": 1}

    def test_both_halves_are_returned_even_when_one_is_empty(self) -> None:
        """The node writes a handle per port. A missing handle downstream is an error
        rather than an empty batch."""
        split = record_validation.partition([{"email": "a@b.com"}], CONTACT)
        assert split.invalid == []

    def test_order_is_preserved(self) -> None:
        """The record log stores a batch index and a position within it. A shuffled batch
        makes both numbers point at the wrong row."""
        records = [{"email": f"{index}@x.com"} for index in range(6)]
        split = record_validation.partition(records, CONTACT)
        assert [row["email"] for row in split.valid] == [f"{index}@x.com" for index in range(6)]


class TestTheMessage:
    def test_it_is_bounded_in_length(self) -> None:
        fields = tuple(
            FieldSpec(name=f"f{index}", type="integer") for index in range(9)
        )
        outcome = record_validation.validate_record(
            {f"f{index}": "x" for index in range(9)}, fields
        )

        assert len(outcome.problems) == 9
        assert outcome.message().endswith("and 6 more")

    def test_a_clean_record_has_nothing_to_say(self) -> None:
        assert record_validation.validate_record({"email": "a@b.com"}, CONTACT).message() == ""


class TestCheckingAMappingAgainstTheFieldList:
    def test_a_field_is_found_case_insensitively(self) -> None:
        """A user typing into a form and a model writing JSON both get capitalisation
        wrong constantly, and refusing over it helps nobody."""
        assert record_validation.find_field(CONTACT, "Email").name == "email"

    def test_an_exact_match_wins_over_a_case_insensitive_one(self) -> None:
        fields = (FieldSpec(name="Email"), FieldSpec(name="email"))
        assert record_validation.find_field(fields, "email").name == "email"

    def test_a_near_miss_is_not_found(self) -> None:
        """``customer_email`` does not become ``email``. That resolution is what writes
        every customer into the destination without an address, green the whole way."""
        assert record_validation.find_field(CONTACT, "customer_email") is None

    def test_the_refusal_names_the_real_fields(self) -> None:
        with pytest.raises(ValueError) as caught:
            record_validation.require_field(CONTACT, "customer_email", destination="Create contact")

        message = str(caught.value)
        assert "customer_email" in message
        assert "email" in message and "first_name" in message, (
            "the operator or the retrying model is one word away from being right"
        )
        assert "Create contact" in message

    def test_unmapped_required_fields_are_listed(self) -> None:
        assert record_validation.unmapped_required(CONTACT, ["first_name"]) == ("email",)

    def test_a_mapped_required_field_is_not_listed(self) -> None:
        assert record_validation.unmapped_required(CONTACT, ["Email"]) == ()

    def test_optional_fields_are_never_listed(self) -> None:
        assert record_validation.unmapped_required(CONTACT, ["email"]) == ()
