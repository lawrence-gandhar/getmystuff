"""
Tests for app/schemas/datasource.py — the Pydantic DTOs that normalize and
validate ``datasource_name`` before it reaches the ORM.

The name ends up in generated SQL identifiers and in the table's unique index,
so the character-set contract is a correctness boundary rather than cosmetic.
Both schemas share one validator; the parametrized class below runs the whole
matrix against each of them so the two can never drift apart.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from litestar.exceptions import HTTPException
from pydantic import ValidationError

from app.schemas.datasource import (
    ALL_DB_TYPES,
    CONNECTION_DB_TYPES,
    OBJECT_STATUSES,
    DatasourceCreateRequest,
    DatasourceCreateSchema,
    DatasourceDetailsResponse,
    DatasourceFileView,
    DatasourceNameRequest,
    DatasourceUpdateSchema,
    FileExistsRequest,
    FileExistsResponse,
    FilePreviewQuery,
    FilePreviewResponse,
    FileUploadRequest,
    ObjectStatusRequest,
    TableListQuery,
    TableStatusView,
    ToolBaseConfigCreateRequest,
    ToolNameRequest,
)
from app.utils.file_utils import FILE_BASED_TYPES

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


# ==========================================================================
# The request / response schemas added when the module gained a full schema
# package. The two DTOs above stay as they are — they are what
# datasource_service validates the name with, and the request schemas below
# reuse their normalizer so there is exactly one definition of what a
# datasource name may be.
# ==========================================================================

VALID_UUID = "3f4b2c1e-0000-4000-8000-000000000001"


def _http_detail(schema, data: dict) -> str:
    """The message a user would see for a rejected payload."""
    with pytest.raises(HTTPException) as exc_info:
        schema.parse(data)
    return str(exc_info.value.detail)


class TestCreateRequest:
    def _connection(self, **extra) -> dict:
        return {
            "datasource_name": "sales_db",
            "db_type": "postgres",
            "host": "localhost",
            "port": "5432",
            "database_name": "sales",
            **extra,
        }

    def test_a_valid_connection_datasource(self) -> None:
        payload = DatasourceCreateRequest.parse(self._connection())
        assert payload.datasource_name == "sales_db"
        assert payload.is_file_based is False

    def test_a_valid_file_datasource_needs_no_connection_fields(self) -> None:
        payload = DatasourceCreateRequest.parse(
            {"datasource_name": "sales_csv", "db_type": "csv"}
        )
        assert payload.is_file_based is True
        assert payload.host is None

    def test_the_name_shares_the_dtos_normalizer(self) -> None:
        """One definition of what a datasource name may be, not two."""
        payload = DatasourceCreateRequest.parse(
            {"datasource_name": "  Sales_DB  ", "db_type": "csv"}
        )
        assert payload.datasource_name == "sales_db"

    def test_the_name_rules_are_the_dtos_rules(self) -> None:
        assert "may only contain lowercase" in _http_detail(
            DatasourceCreateRequest, {"datasource_name": "sales-db", "db_type": "csv"}
        )

    @pytest.mark.parametrize("db_type", sorted(ALL_DB_TYPES))
    def test_every_engine_the_form_offers_is_accepted(self, db_type: str) -> None:
        payload = DatasourceCreateRequest.parse(
            {"datasource_name": "x", "db_type": db_type}
        )
        assert payload.db_type == db_type

    def test_the_file_types_come_from_file_utils_not_a_copied_list(self) -> None:
        """Adding a file format in one place is enough."""
        assert FILE_BASED_TYPES <= ALL_DB_TYPES
        assert not (FILE_BASED_TYPES & CONNECTION_DB_TYPES)

    def test_an_unsupported_engine_is_refused(self) -> None:
        assert _http_detail(
            DatasourceCreateRequest, {"datasource_name": "x", "db_type": "cassandra"}
        ) == "Database type is not one we support. Please pick one from the list."

    def test_the_engine_is_required(self) -> None:
        assert _http_detail(DatasourceCreateRequest, {"datasource_name": "x"}) == (
            "Database type is required"
        )

    @pytest.mark.parametrize("port", ["abc", "0", "65536", "-1", "54 32"])
    def test_a_port_that_is_not_a_port_is_refused(self, port: str) -> None:
        """
        Otherwise the connection attempt fails with a driver error no user can
        act on.
        """
        assert _http_detail(DatasourceCreateRequest, self._connection(port=port)) == (
            "Port must be a number between 1 and 65535"
        )

    @pytest.mark.parametrize("port", ["1", "5432", "65535"])
    def test_a_valid_port_is_kept_as_a_string_for_the_driver(self, port: str) -> None:
        payload = DatasourceCreateRequest.parse(self._connection(port=port))
        assert payload.port == port

    def test_a_blank_port_is_none(self) -> None:
        assert DatasourceCreateRequest.parse(self._connection(port="")).port is None

    def test_which_connection_fields_are_required_is_left_to_the_service(self) -> None:
        """
        It depends on ``db_type``, and ``create_datasource`` owns that rule along
        with the reachability test that has to run anyway.
        """
        payload = DatasourceCreateRequest.parse(
            {"datasource_name": "x", "db_type": "postgres"}
        )
        assert payload.database_name is None


class TestNameRequest:
    def test_the_rename_and_blur_check_share_one_schema(self) -> None:
        assert DatasourceNameRequest.parse(
            {"datasource_name": "  New_Name "}
        ).datasource_name == "new_name"

    def test_it_rejects_exactly_what_the_dto_rejects(self) -> None:
        assert "cannot be empty" in _http_detail(
            DatasourceNameRequest, {"datasource_name": "  "}
        )


class TestObjectStatus:
    @pytest.mark.parametrize("status", sorted(OBJECT_STATUSES))
    def test_both_states_are_accepted(self, status: str) -> None:
        assert ObjectStatusRequest.parse({"status": status}).status == status

    def test_an_unknown_status_now_gets_a_sentence(self) -> None:
        """
        Regression on behaviour: this used to be a bare
        ``HTTPException(status_code=400)`` — a failed request with no explanation.
        """
        assert _http_detail(ObjectStatusRequest, {"status": "archived"}) == (
            "Status must be either active or inactive"
        )

    def test_a_missing_status_is_refused(self) -> None:
        assert _http_detail(ObjectStatusRequest, {}) == "Status is required"


class TestTableListQuery:
    def test_the_unfiltered_defaults(self) -> None:
        query = TableListQuery.parse({})
        assert (query.search, query.status_filter, query.sort_by) == ("", "all", "az")

    def test_search_is_lowercased_here_rather_than_in_the_route(self) -> None:
        """
        The service compares against lowercased table names and cannot rely on
        every caller having lowercased first.
        """
        assert TableListQuery.parse({"search": "SALES"}).search == "sales"

    def test_an_unknown_status_filter_is_refused(self) -> None:
        assert _http_detail(TableListQuery, {"status_filter": "deleted"}) == (
            "Status filter must be all, active or inactive"
        )

    def test_an_unknown_sort_order_is_refused(self) -> None:
        assert _http_detail(TableListQuery, {"sort_by": "random"}) == (
            "Sort order must be either az or za"
        )


class TestFileRequests:
    def test_a_filename_is_required(self) -> None:
        assert _http_detail(FileExistsRequest, {"filename": "  "}) == (
            "Filename is required"
        )

    def test_a_filename_is_trimmed(self) -> None:
        assert FileExistsRequest.parse({"filename": " a.csv "}).filename == "a.csv"

    @pytest.mark.parametrize(("raw", "expected"), [("yes", True), ("no", False)])
    def test_the_override_flag_reads_the_widgets_tokens(self, raw, expected) -> None:
        assert FileUploadRequest.parse({"override": raw}).override is expected

    def test_an_absent_override_flag_is_false(self) -> None:
        assert FileUploadRequest.parse({}).override is False


class TestFilePreviewQuery:
    def test_the_first_page_is_the_default(self) -> None:
        assert FilePreviewQuery.parse({}).page == 1

    def test_a_valid_page_and_file(self) -> None:
        query = FilePreviewQuery.parse({"page": "3", "file_id": VALID_UUID})
        assert query.page == 3
        assert query.file_id == uuid_pkg.UUID(VALID_UUID)

    def test_an_unreadable_page_is_refused_rather_than_silently_page_one(self) -> None:
        """
        Behaviour change, deliberately. ``?page=abc`` used to fall back to page 1,
        so a broken link showed the wrong data with nothing saying so.
        """
        assert _http_detail(FilePreviewQuery, {"page": "abc"}) == (
            "Page must be a whole number"
        )

    @pytest.mark.parametrize("page", ["0", "-2"])
    def test_a_page_below_one_is_refused(self, page: str) -> None:
        assert "cannot be less than 1" in _http_detail(FilePreviewQuery, {"page": page})

    def test_an_absurd_page_is_refused(self) -> None:
        assert "cannot be greater than" in _http_detail(
            FilePreviewQuery, {"page": "99999999"}
        )

    def test_a_blank_file_id_means_the_most_recent_file(self) -> None:
        assert FilePreviewQuery.parse({"file_id": ""}).file_id is None


class TestToolBaseConfig:
    def _valid(self, **extra) -> dict:
        return {"tool_name": "total_units", "table_name": "sales_data", **extra}

    def test_a_valid_config(self) -> None:
        payload = ToolBaseConfigCreateRequest.parse(
            self._valid(base_config='{"a": 1}', subquery_configs='[{"b": 2}]')
        )
        assert payload.base_config == {"a": 1}
        assert payload.subquery_configs == [{"b": 2}]

    def test_empty_json_fields_default_to_empty_containers(self) -> None:
        payload = ToolBaseConfigCreateRequest.parse(self._valid())
        assert payload.base_config == {}
        assert payload.subquery_configs == []

    def test_a_malformed_subquery_list_is_refused_not_discarded(self) -> None:
        """
        Regression on behaviour. This used to be a ``json.loads`` whose ``except``
        fell back to ``[]`` — a malformed payload silently discarded every
        subquery the user had built and then reported success.
        """
        assert "could not be read" in _http_detail(
            ToolBaseConfigCreateRequest, self._valid(subquery_configs="[{oops")
        )

    def test_json_that_parses_but_is_not_a_list_is_refused(self) -> None:
        assert "not in the expected format" in _http_detail(
            ToolBaseConfigCreateRequest, self._valid(subquery_configs='{"a": 1}')
        )

    def test_the_tool_name_must_be_an_identifier(self) -> None:
        assert "must start with a letter" in _http_detail(
            ToolBaseConfigCreateRequest, self._valid(tool_name="9lives")
        )

    def test_a_table_name_that_could_break_an_identifier_is_refused(self) -> None:
        assert "is not a valid name" in _http_detail(
            ToolBaseConfigCreateRequest, self._valid(table_name="sales; drop table x")
        )


class TestToolNameRequest:
    def test_the_blur_check_and_the_save_share_one_schema(self) -> None:
        assert ToolNameRequest.parse({"tool_name": "Total_Units"}).tool_name == (
            "total_units"
        )

    def test_it_is_required(self) -> None:
        assert _http_detail(ToolNameRequest, {"tool_name": ""}) == "Tool name is required"


class TestResponses:
    def test_the_file_view_carries_the_public_uuid_under_the_key_the_script_reads(
        self,
    ) -> None:
        """
        ``id`` here is the file's *public uuid*. The key is named ``id`` because
        the preview script builds its ``<option>`` list from ``f.id``.
        """
        payload = DatasourceFileView.payload_for({"id": "f-uuid", "filename": "a.csv"})
        assert payload == {"id": "f-uuid", "filename": "a.csv"}

    def test_the_table_status_view(self) -> None:
        view = TableStatusView.build({"table_name": "orders", "status": "inactive"})
        assert (view.table_name, view.status) == ("orders", "inactive")

    def test_the_details_response_keeps_the_users_own_table_names(self) -> None:
        """
        ``configuration_data``'s keys are the user's table names, so it stays an
        open dict rather than being modelled field by field.
        """
        payload = DatasourceDetailsResponse.payload_for(
            {
                "datasource_name": "sales_db",
                "objects": ["orders"],
                "configuration_data": {"orders": {"status": "active"}},
            }
        )
        assert payload["configuration_data"]["orders"]["status"] == "active"

    def test_a_preview_failure_uses_the_shape_the_widget_reads(self) -> None:
        payload = FilePreviewResponse.failure("File not found on disk.").payload()
        assert payload["error"] == "File not found on disk."
        assert payload["rows"] == []
        assert payload["columns"] == []

    def test_a_tabular_preview(self) -> None:
        payload = FilePreviewResponse.build(
            {
                "type": "table",
                "columns": ["a", "b"],
                "rows": [["1", "2"]],
                "page": 2,
                "has_next": True,
                "file_id": "f-1",
                "filename": "a.csv",
                "files": [{"id": "f-1", "filename": "a.csv"}],
            }
        ).payload()
        assert payload["has_next"] is True
        assert payload["files"][0]["id"] == "f-1"
        assert payload["error"] is None

    def test_a_document_preview_carries_content_instead_of_rows(self) -> None:
        """
        The JSON and XML readers return a formatted document; the widget branches
        on ``type``, so one schema covers both shapes.
        """
        payload = FilePreviewResponse.build(
            {"type": "json", "content": "[]", "page": 1, "has_next": False}
        ).payload()
        assert payload["content"] == "[]"
        assert payload["rows"] == []

    def test_the_file_exists_response_reports_the_next_version(self) -> None:
        response = FileExistsResponse.build({"exists": True, "version": 2})
        assert response.next_version == 3

    def test_a_new_file_has_no_version_yet(self) -> None:
        response = FileExistsResponse.build({"exists": False})
        assert (response.version, response.next_version) == (0, 1)
