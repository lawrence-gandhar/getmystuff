"""
Tests for app/schemas/base.py — the shared infrastructure the whole schema layer
is built on.

The single most important thing here is the error bridge. Every route in this
application renders ``HTTPException.detail`` straight into a Bootstrap alert, so a
Pydantic ``ValidationError`` escaping a schema would put
``1 validation error for X / name / Value error, ...`` in front of a user. These
tests pin the conversion: what exception comes out, what status it carries, and
what the message actually reads like.

The second is the form/query/JSON source conversion, where the subtleties live —
a repeated key from a multi-select, an untouched field that means "nothing"
rather than empty string, and a file part that must be dropped rather than
type-checked.
"""

from __future__ import annotations

from typing import Optional

import pytest
from litestar.exceptions import HTTPException
from pydantic import Field

from app.schemas.base import (
    MAX_NAME_LENGTH,
    AppBaseSchema,
    CheckboxBool,
    FormRequest,
    IdentifierName,
    JsonArrayField,
    JsonObjectField,
    ObjectName,
    OptionalText,
    OptionalUUID,
    QueryRequest,
    RequiredText,
    RequiredUUID,
    ResponseSchema,
    form_to_dict,
)

VALID_UUID = "3f4b2c1e-0000-4000-8000-000000000001"


class _Demo(FormRequest):
    """One schema exercising every reusable field type."""

    multi_fields = ("tags",)

    name: RequiredText = Field(title="Workspace name", max_length=MAX_NAME_LENGTH)
    nickname: OptionalText = Field(default=None, title="Nickname", max_length=10)
    owner: RequiredUUID = Field(default=VALID_UUID, title="Owner")
    workspace_id: OptionalUUID = Field(default=None, title="Workspace")
    enabled: CheckboxBool = Field(default=False, title="Enabled")
    tool_name: Optional[IdentifierName] = Field(default=None, title="Tool name")
    table_name: Optional[ObjectName] = Field(default=None, title="Table name")
    config: JsonObjectField = Field(default_factory=dict, title="Configuration")
    rules: JsonArrayField = Field(default_factory=list, title="Rules")
    tags: list[str] = Field(default_factory=list, title="Tags")


def _detail(data: dict) -> str:
    """Parse ``data`` and return the message a user would see."""
    with pytest.raises(HTTPException) as exc_info:
        _Demo.parse(data)
    return str(exc_info.value.detail)


class _FakeUpload:
    """A stand-in for a multipart file part: has both `filename` and `read`."""

    filename = "sales.csv"

    async def read(self) -> bytes:  # pragma: no cover - never called
        return b""


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


class TestErrorBridge:
    """Every failure leaves as the project's own exception, never Pydantic's."""

    def test_a_bad_payload_raises_httpexception_not_validationerror(self) -> None:
        with pytest.raises(HTTPException):
            _Demo.parse({})

    def test_the_status_code_is_400(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _Demo.parse({})
        assert exc_info.value.status_code == 400

    def test_pydantics_own_wording_never_reaches_the_message(self) -> None:
        """
        The regression this bridge exists to prevent.

        Pydantic's message names the model class and prefixes the validator's text
        with "Value error, ". Neither belongs on a screen.
        """
        detail = _detail({"name": "x", "workspace_id": "not-a-uuid"})
        assert "validation error" not in detail.lower()
        assert "Value error" not in detail
        assert "_Demo" not in detail

    def test_only_the_first_failure_is_reported(self) -> None:
        """
        A form posts every field at once, so one mistake can cascade. The user
        gets the one thing to fix, not a wall of them inside an alert.
        """
        detail = _detail(
            {"name": "x", "nickname": "far too long to fit", "enabled": "maybe"}
        )
        assert detail == "Nickname cannot be longer than 10 characters"
        assert "must be either" not in detail


class TestMessagesUseTheFieldTitle:
    """The `title` is the user-facing name, so it has to appear in the message."""

    def test_missing_required_field(self) -> None:
        assert _detail({}) == "Workspace name is required"

    def test_blank_required_field_reads_as_missing_not_as_too_short(self) -> None:
        """Whitespace is stripped first, so "   " is empty rather than 3 chars."""
        assert _detail({"name": "   "}) == "Workspace name is required"

    def test_too_long(self) -> None:
        assert (
            _detail({"name": "x", "nickname": "abcdefghijk"})
            == "Nickname cannot be longer than 10 characters"
        )

    def test_bad_uuid(self) -> None:
        assert (
            _detail({"name": "x", "workspace_id": "nope"})
            == "Workspace is not a valid selection"
        )

    def test_non_string_where_text_is_expected(self) -> None:
        assert _detail({"name": 12345}) == "Workspace name must be text"

    def test_unrecognised_boolean_token(self) -> None:
        """Named for the field, so the user knows which control was wrong."""
        assert _detail({"name": "x", "enabled": "perhaps"}) == (
            "Enabled must be either on or off"
        )


class TestNormalization:
    def test_text_is_trimmed(self) -> None:
        assert _Demo.parse({"name": "  Sales  "}).name == "Sales"

    def test_a_blank_optional_becomes_none_so_the_column_is_cleared(self) -> None:
        """
        An untouched textarea posts ``""``. Storing that would leave an empty
        string where the schema means "no value".
        """
        assert _Demo.parse({"name": "x", "nickname": "   "}).nickname is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("true", True), ("on", True), ("yes", True), ("1", True),
         ("false", False), ("off", False), ("no", False), ("0", False),
         ("", False), (None, False)],
    )
    def test_boolean_tokens_an_html_control_can_send(self, raw, expected) -> None:
        assert _Demo.parse({"name": "x", "enabled": raw}).enabled is expected

    def test_an_absent_checkbox_is_false_rather_than_a_missing_field(self) -> None:
        """A form that simply omits the box behaves as "not ticked"."""
        assert _Demo.parse({"name": "x"}).enabled is False

    def test_an_identifier_is_lowercased(self) -> None:
        assert _Demo.parse({"name": "x", "tool_name": "Total_Units"}).tool_name == (
            "total_units"
        )


class TestSharedValidatorsAreReused:
    """
    The identifier and object-name rules come from app/utils/validators.py, so the
    schema and the services reject the same input with the same sentence.
    """

    def test_identifier_message_is_the_validators_own(self) -> None:
        detail = _detail({"name": "x", "tool_name": "9lives"})
        assert "must start with a letter" in detail

    def test_object_name_rejects_a_name_that_could_break_an_identifier(self) -> None:
        """
        The injection guard: an object name is interpolated into a generated
        query rather than bound as a parameter.
        """
        detail = _detail({"name": "x", "table_name": "sales; drop table users"})
        assert "is not a valid name" in detail

    def test_object_name_allows_a_file_datasource_object(self) -> None:
        """File-based datasources have object names like "sales_data.csv"."""
        assert _Demo.parse(
            {"name": "x", "table_name": "sales_data.csv"}
        ).table_name == "sales_data.csv"


class TestJsonFields:
    def test_a_json_object_field_parses(self) -> None:
        assert _Demo.parse({"name": "x", "config": '{"a": 1}'}).config == {"a": 1}

    def test_a_blank_json_object_field_is_an_empty_dict(self) -> None:
        assert _Demo.parse({"name": "x", "config": ""}).config == {}

    def test_a_malformed_json_object_is_refused_not_swallowed(self) -> None:
        assert "could not be read" in _detail({"name": "x", "config": "{oops"})

    def test_a_json_array_field_parses(self) -> None:
        assert _Demo.parse({"name": "x", "rules": "[1, 2]"}).rules == [1, 2]

    def test_a_blank_json_array_field_is_an_empty_list(self) -> None:
        assert _Demo.parse({"name": "x", "rules": ""}).rules == []

    def test_a_malformed_json_array_is_refused(self) -> None:
        """
        Regression guard. The hand-rolled version this replaces fell back to
        ``[]`` on a parse failure, so a browser posting a malformed list had the
        user's work silently discarded and was told the save succeeded.
        """
        assert "could not be read" in _detail({"name": "x", "rules": "[1,"})

    def test_json_that_parses_but_is_not_a_list_is_refused(self) -> None:
        assert "not in the expected format" in _detail({"name": "x", "rules": "{}"})


class TestFormToDict:
    def test_a_repeated_key_keeps_every_value(self) -> None:
        """
        The multi-select bug this exists to prevent: read as a single value, a
        query built against four tables silently becomes a query against one.
        """
        form = _FakeForm({"name": "x"}, {"tags": ["a", "b", "c"]})
        assert _Demo.from_form_data(form).tags == ["a", "b", "c"]

    def test_a_declared_multi_field_the_form_omitted_is_an_empty_list(self) -> None:
        """"Nothing selected" is a valid state, not an absent field."""
        assert _Demo.from_form_data(_FakeForm({"name": "x"})).tags == []

    def test_upload_parts_are_dropped(self) -> None:
        """
        A file is a stream, not a value. Leaving it in would fail the field's
        type check for a reason that has nothing to do with the user.
        """
        data = form_to_dict(_FakeForm({"name": "x", "logo": _FakeUpload()}))
        assert "logo" not in data
        assert data["name"] == "x"

    def test_upload_parts_are_dropped_from_a_repeated_key_too(self) -> None:
        form = _FakeForm({"name": "x"}, {"tags": ["a", _FakeUpload()]})
        assert form_to_dict(form, ("tags",))["tags"] == ["a"]

    def test_overrides_are_applied_last(self) -> None:
        """Used for a value that comes from the URL rather than the body."""
        form = _FakeForm({"name": "from-form"})
        assert _Demo.from_form_data(form, name="from-url").name == "from-url"


class TestExtraFieldsAreIgnored:
    def test_an_undeclared_form_field_does_not_fail_the_request(self) -> None:
        """
        Every HTMX form here carries fields a given handler does not care about —
        the page's current filter, hidden state a partial needs. Rejecting one
        would break every form on the site.
        """
        payload = _Demo.parse({"name": "x", "csrf_token": "abc", "page": "3"})
        assert payload.name == "x"
        assert not hasattr(payload, "csrf_token")


class TestQueryRequest:
    class _Filters(QueryRequest):
        search: str = Field(default="", title="Search")
        agent: OptionalUUID = Field(default=None, title="Agent")

    def test_an_absent_filter_means_unfiltered_rather_than_bad_request(self) -> None:
        filters = self._Filters.parse({})
        assert filters.search == ""
        assert filters.agent is None

    def test_a_present_but_unreadable_filter_is_refused(self) -> None:
        """
        Deliberately not defaulted: a broken link must not silently render real
        figures for a different scope than the one asked for.
        """
        with pytest.raises(HTTPException):
            self._Filters.parse({"agent": "nonsense"})


class TestResponseSchema:
    class _Row:
        uuid = "abc"
        name = "Sales"
        secret = "do-not-send"

    class _View(ResponseSchema):
        uuid: str = Field(title="Row")
        name: str = Field(title="Name")

    def test_builds_from_an_orm_style_object(self) -> None:
        view = self._View.build(self._Row())
        assert (view.uuid, view.name) == ("abc", "Sales")

    def test_undeclared_attributes_are_not_carried_into_the_payload(self) -> None:
        """A view sends what it declares — nothing rides along by accident."""
        assert "secret" not in self._View.payload_for(self._Row())

    def test_build_many(self) -> None:
        assert len(self._View.build_many([self._Row(), self._Row()])) == 2

    def test_build_many_of_nothing_is_an_empty_list(self) -> None:
        assert self._View.build_many(None) == []

    def test_a_response_that_fails_its_own_schema_is_a_500_not_a_400(self) -> None:
        """
        A malformed response is this application's defect, not the caller's, so it
        must not be reported as a bad request — and its internal reason must not
        be shown.
        """
        with pytest.raises(HTTPException) as exc_info:
            self._View.build({"name": "Sales"})

        assert exc_info.value.status_code == 500
        assert "uuid" not in str(exc_info.value.detail)

    def test_payload_renders_json_safe_values(self) -> None:
        """
        ``mode="json"`` is what turns a UUID or a datetime into a string. The
        hand-built dicts this layer replaced were doing that by hand.
        """
        import uuid as uuid_pkg

        class _WithUuid(ResponseSchema):
            uuid: uuid_pkg.UUID = Field(title="Row")

        payload = _WithUuid.payload_for({"uuid": VALID_UUID})
        assert payload["uuid"] == VALID_UUID
        assert isinstance(payload["uuid"], str)


class TestAppBaseSchemaConfig:
    def test_whitespace_stripping_applies_to_every_schema(self) -> None:
        class _Plain(AppBaseSchema):
            value: str = Field(title="Value")

        assert _Plain.parse({"value": "  hi  "}).value == "hi"
