"""
Tests for app/schemas/datasource.py — the Pydantic DTOs that normalize and
validate ``datasource_name`` before it reaches the ORM.

The name ends up in generated SQL identifiers and in the table's unique index,
so the character-set contract is a correctness boundary rather than cosmetic.
Both schemas share one validator; the parametrized class below runs the whole
matrix against each of them so the two can never drift apart.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.datasource import DatasourceCreateSchema, DatasourceUpdateSchema

BOTH_SCHEMAS = [DatasourceCreateSchema, DatasourceUpdateSchema]


@pytest.mark.parametrize("schema", BOTH_SCHEMAS, ids=["create", "update"])
class TestNormalization:
    def test_strips_surrounding_whitespace(self, schema) -> None:  # noqa: ANN001
        assert schema(datasource_name="  sales_data  ").datasource_name == "sales_data"

    def test_lowercases(self, schema) -> None:  # noqa: ANN001
        assert schema(datasource_name="SALES_DATA").datasource_name == "sales_data"

    def test_strip_and_lowercase_combined(self, schema) -> None:  # noqa: ANN001
        assert schema(datasource_name="  Sales_Data\t").datasource_name == "sales_data"

    @pytest.mark.parametrize(
        "name",
        ["sales_data", "a", "s3", "with_many_under_scores", "0123", "a" * 255],
    )
    def test_accepts_valid_names(self, schema, name: str) -> None:  # noqa: ANN001
        assert schema(datasource_name=name).datasource_name == name


@pytest.mark.parametrize("schema", BOTH_SCHEMAS, ids=["create", "update"])
class TestRejection:
    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
    def test_rejects_blank(self, schema, blank: str) -> None:  # noqa: ANN001
        with pytest.raises(ValidationError, match="cannot be empty"):
            schema(datasource_name=blank)

    def test_rejects_over_255_characters(self, schema) -> None:  # noqa: ANN001
        with pytest.raises(ValidationError, match="maximum length of 255"):
            schema(datasource_name="a" * 256)

    def test_boundary_255_is_accepted_256_is_not(self, schema) -> None:  # noqa: ANN001
        assert len(schema(datasource_name="a" * 255).datasource_name) == 255
        with pytest.raises(ValidationError):
            schema(datasource_name="a" * 256)

    @pytest.mark.parametrize(
        "name",
        [
            "sales data",      # space
            "sales-data",      # dash
            "sales.data",      # dot
            "sales/data",      # slash
            "sales;drop",      # semicolon
            'sales"data',      # quote
            "sales'data",      # apostrophe
            "sales(data)",     # parentheses
            "salés_data",      # accented letter
            "sales_data!",     # punctuation
            "sales\ndata",     # newline
        ],
    )
    def test_rejects_characters_outside_a_z_0_9_underscore(
        self, schema, name: str  # noqa: ANN001
    ) -> None:
        """This is the injection guard: the name is interpolated into SQL
        identifiers downstream, so quotes, semicolons and whitespace must never
        get through."""
        with pytest.raises(ValidationError, match="may only contain lowercase"):
            schema(datasource_name=name)

    def test_uppercase_passes_because_it_is_lowercased_first(self, schema) -> None:  # noqa: ANN001
        """Normalization runs before the pattern check, so 'ABC' is valid while
        'A-B' is not — worth pinning, since the regex alone would reject both."""
        assert schema(datasource_name="ABC").datasource_name == "abc"
        with pytest.raises(ValidationError):
            schema(datasource_name="A-B")


@pytest.mark.parametrize("schema", BOTH_SCHEMAS, ids=["create", "update"])
class TestRequiredness:
    def test_datasource_name_is_required(self, schema) -> None:  # noqa: ANN001
        with pytest.raises(ValidationError):
            schema()

    @pytest.mark.parametrize("value", [None, 12345, 3.5, ["a"], {"a": 1}, True])
    def test_a_non_string_is_a_validationerror(self, schema, value) -> None:  # noqa: ANN001
        """
        Regression test for a fixed defect.

        ``mode="before"`` hands the validator the raw value, so a non-string used
        to reach ``.strip()`` and raise ``AttributeError`` — which Pydantic does
        NOT convert into a ``ValidationError``. The exception escaped the schema
        entirely, and a JSON body with ``"datasource_name": null`` reached the
        user as ``'NoneType' object has no attribute 'strip'``.

        ``_normalize_datasource_name`` now rejects non-strings up front, so every
        failure stays inside the ValidationError contract that callers — and the
        route layer — actually catch.
        """
        with pytest.raises(ValidationError, match="must be text"):
            schema(datasource_name=value)

    def test_the_error_is_catchable_as_validationerror(self, schema) -> None:  # noqa: ANN001
        """The point of the fix, stated directly: a route catching
        ValidationError now catches this."""
        try:
            schema(datasource_name=None)
        except ValidationError:
            pass
        else:  # pragma: no cover - the call above always raises
            pytest.fail("expected a ValidationError")


class TestSchemasAgree:
    @pytest.mark.parametrize(
        "name", ["Sales_Data", "  x  ", "abc123", "A" * 10],
    )
    def test_both_schemas_normalize_identically(self, name: str) -> None:
        """The two DTOs exist only to differ in requiredness later; while they
        share ``_normalize_datasource_name`` their output must stay identical."""
        assert (
            DatasourceCreateSchema(datasource_name=name).datasource_name
            == DatasourceUpdateSchema(datasource_name=name).datasource_name
        )
