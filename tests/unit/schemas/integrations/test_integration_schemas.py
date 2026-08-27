"""
Tests for ``app/schemas/integrations/``.

The two properties that earn a test file of their own.

**No response schema can carry a secret or a bigint ``id``, whatever the layer below it
does.** Asserted by feeding every response schema a payload containing both and checking
they are gone — not by reading the field lists. ``ResponseSchema`` is ``extra="ignore"``,
so this holds for a field somebody adds to a view function next year without touching
this package. That is the difference between a rule and a description.

**A refusal is a sentence somebody can act on.** Pydantic's own wording names the model
class and the internal field path; ``app/schemas/base.py`` exists to replace it, and these
tests pin that the replacement actually happens for this feature's fields — including
inside a list, where the naive implementation reports ``entry 0``.

Everything else here is the ordinary shape checking, kept short: caps that mirror the
engine's own, enum membership against the models' vocabulary rather than a re-declared
one, and the two form quirks that have bitten this codebase before — a repeated key read
with ``get`` instead of ``getall``, and a blank field read as a cleared value.
"""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

from app.models.integrations import (
    MAX_BATCH_SIZE,
    MIN_INTERVAL_SECONDS,
    OPERATION_READ,
    OVERLAP_SKIP,
    RUN_MODE_DRY_RUN,
    RUN_MODE_LIVE,
    TRIGGER_SCHEDULE,
)
from app.schemas.integrations import (
    ConnectionCreateRequest,
    ConnectionTestView,
    ConnectionUpdateRequest,
    ConnectionView,
    FlowCreateRequest,
    FlowGraphRequest,
    FlowSettingsRequest,
    FlowVersionView,
    FlowView,
    OperationSaveRequest,
    OperationSchemaView,
    OperationView,
    PrivateHostRequest,
    RunFrameView,
    RunRecordView,
    RunStartRequest,
    RunStepView,
    TriggerRequest,
    TriggerView,
)

SECRET = "sk-live-never-show-this"

#: Every response schema in the package, with a payload that satisfies its required
#: fields. Listed explicitly rather than discovered by walking the module, so adding a
#: schema without adding it here is a visible omission rather than a silent gap in the
#: one test that guarantees secrets do not leave.
RESPONSE_SCHEMAS = [
    (FlowView, {"uuid": "f1", "name": "Nightly sync"}),
    (FlowVersionView, {"uuid": "v1", "version_number": 1, "status": "published"}),
    (TriggerView, {"uuid": "t1", "node_id": "t1", "kind": TRIGGER_SCHEDULE}),
    (ConnectionView, {"uuid": "c1", "label": "Billing", "connector_id": "rest_generic",
                      "status": "active"}),
    (OperationView, {"operation_id": "list_contacts"}),
    (OperationSchemaView, {"connection_uuid": "c1", "operation_id": "list_contacts"}),
    (ConnectionTestView, {"ok": True, "message": "Connected."}),
    (RunFrameView, {"uuid": "r1", "flow_uuid": "f1", "status": "running", "mode": "live"}),
    (RunStepView, {"uuid": "s1", "node_id": "w1", "status": "succeeded"}),
    (RunRecordView, {"uuid": "rr1", "node_id": "w1", "outcome": "failed"}),
]


class TestNothingSensitiveLeavesInAResponse:
    """
    The rule CLAUDE.md states and this file enforces: the internal bigint ``id`` is a
    foreign-key target and never reaches a payload, and no credential ever does.
    """

    @pytest.mark.parametrize("schema,payload", RESPONSE_SCHEMAS, ids=lambda v: getattr(v, "__name__", ""))
    def test_a_bigint_id_is_dropped_at_the_boundary(self, schema, payload) -> None:
        """
        Not "no schema declares ``id``" — that is a description of today. This is the
        stronger claim: a view function that *starts* emitting one has it dropped here
        rather than serialised, because ``ResponseSchema`` is ``extra="ignore"``.
        """
        built = schema.build({**payload, "id": 4242})

        assert "id" not in built.payload()
        assert "4242" not in repr(built.payload())

    @pytest.mark.parametrize("schema,payload", RESPONSE_SCHEMAS, ids=lambda v: getattr(v, "__name__", ""))
    def test_a_credential_field_is_dropped_at_the_boundary(self, schema, payload) -> None:
        """
        Every name a secret is stored under, in one pass. A response schema that grew a
        credential field would fail here rather than in production, and the encrypted
        column names are included because a payload carrying ciphertext is still a payload
        carrying a secret.
        """
        leaky = {
            **payload,
            "api_key": SECRET,
            "api_key_encrypted": SECRET,
            "password": SECRET,
            "access_token": SECRET,
            "access_token_encrypted": SECRET,
            "client_secret": SECRET,
        }

        assert SECRET not in repr(schema.build(leaky).payload())


class TestFlowRequests:
    def test_a_name_is_trimmed(self) -> None:
        assert FlowCreateRequest.parse({"name": "  Nightly sync  "}).name == "Nightly sync"

    def test_a_blank_name_is_refused_by_its_label(self) -> None:
        """
        Pydantic's own message names the model class and the internal field. The whole
        reason ``app/schemas/base.py`` exists is that it must not reach a screen.
        """
        with pytest.raises(HTTPException) as caught:
            FlowCreateRequest.parse({"name": "   "})

        assert caught.value.detail == "Workflow name is required"

    def test_a_batch_size_over_the_engine_ceiling_is_refused(self) -> None:
        """
        The cap mirrors ``MAX_BATCH_SIZE`` rather than restating a number, because the
        ceiling is not a preference — a batch is that many records held in process memory
        at once — and two numbers would eventually disagree.
        """
        with pytest.raises(HTTPException) as caught:
            FlowSettingsRequest.parse({"name": "Nightly", "default_batch_size": MAX_BATCH_SIZE + 1})

        assert str(MAX_BATCH_SIZE) in caught.value.detail

    def test_redacted_fields_survive_as_a_list(self) -> None:
        """
        A repeated key read with ``get`` instead of ``getall`` silently becomes its first
        entry — a deny-list of one, which looks exactly like a deny-list that is working.
        """
        parsed = FlowSettingsRequest.parse(
            {"name": "Nightly", "redacted_fields": [" ssn ", "", "iban"]}
        )

        assert parsed.redacted_fields == ["ssn", "iban"]

    def test_a_drawing_arrives_as_an_object(self) -> None:
        parsed = FlowGraphRequest.parse({"graph_data": {"nodes": [], "edges": []}})

        assert parsed.graph_data == {"nodes": [], "edges": []}

    def test_a_hand_routed_connection_survives(self) -> None:
        """
        The drawing is opaque to this schema, which is what let a bend be added to a
        connection without a schema change. This is the test that keeps it opaque in
        the one direction that matters.
        """
        drawing = {"nodes": [], "edges": [{"id": "e1", "waypoints": [{"x": 8, "y": 9}]}]}
        parsed = FlowGraphRequest.parse({"graph_data": drawing})

        assert parsed.graph_data == drawing

    @pytest.mark.parametrize("value", [float("nan"), float("inf")])
    def test_a_non_finite_bend_is_refused(self, value: float) -> None:
        """
        The one thing inside the drawing this layer does look at. ``NaN`` satisfies
        every other rule and then makes PostgreSQL refuse the ``jsonb`` it is
        written into, turning a bad request into a 500 with no sentence in it.
        """
        with pytest.raises(HTTPException) as caught:
            FlowGraphRequest.parse(
                {"graph_data": {"edges": [{"id": "e1", "waypoints": [{"x": value, "y": 0}]}]}}
            )

        assert "not a valid position" in str(caught.value.detail)

    def test_a_drawing_with_no_connections_is_fine(self) -> None:
        assert FlowGraphRequest.parse({"graph_data": {"nodes": []}}).graph_data == {"nodes": []}

    def test_a_drawing_that_is_not_json_is_refused(self) -> None:
        """Refused rather than silently emptied. The version this replaces swallowed a
        malformed document into ``{}`` — so a browser that posted one had the user's work
        discarded and was told the save succeeded."""
        with pytest.raises(HTTPException):
            FlowGraphRequest.parse({"graph_data": "{not json"})


class TestTriggerRequest:
    def test_an_interval_under_the_floor_is_refused(self) -> None:
        with pytest.raises(HTTPException) as caught:
            TriggerRequest.parse(
                {"node_id": "t1", "kind": TRIGGER_SCHEDULE,
                 "interval_seconds": MIN_INTERVAL_SECONDS - 1}
            )

        assert str(MIN_INTERVAL_SECONDS) in caught.value.detail

    def test_an_unknown_kind_lists_the_real_ones(self) -> None:
        """
        Checked against the models' own vocabulary rather than a ``Literal`` re-declared
        here. A second list is one that falls behind the day a kind is added, and the
        symptom is a form refusing something the engine supports.
        """
        with pytest.raises(HTTPException) as caught:
            TriggerRequest.parse({"node_id": "t1", "kind": "whenever"})

        assert TRIGGER_SCHEDULE in caught.value.detail

    def test_an_unknown_overlap_policy_lists_the_real_ones(self) -> None:
        with pytest.raises(HTTPException) as caught:
            TriggerRequest.parse(
                {"node_id": "t1", "kind": TRIGGER_SCHEDULE, "interval_seconds": 3600,
                 "overlap_policy": "pile_up"}
            )

        assert OVERLAP_SKIP in caught.value.detail

    def test_an_absent_checkbox_is_off_rather_than_missing(self) -> None:
        """A form that simply does not include the box means "not ticked", and reading it
        as an absent field would make every unticked save a 400."""
        parsed = TriggerRequest.parse({"node_id": "t1", "kind": TRIGGER_SCHEDULE,
                                       "interval_seconds": 3600})

        assert parsed.is_enabled is False


class TestRunStartRequest:
    def test_it_defaults_to_live(self) -> None:
        """
        The more consequential of the two, deliberately. The button that posts this says
        Run, and a Run button that quietly touched nobody's data would be worse — somebody
        would press it, see a green run, and believe the sync happened.
        """
        assert RunStartRequest.parse({}).mode == RUN_MODE_LIVE

    def test_a_dry_run_is_accepted(self) -> None:
        assert RunStartRequest.parse({"mode": RUN_MODE_DRY_RUN}).mode == RUN_MODE_DRY_RUN

    def test_a_nonsense_mode_is_refused(self) -> None:
        """A dry run that quietly became a live one would write to somebody's production
        system on the strength of a typo."""
        with pytest.raises(HTTPException):
            RunStartRequest.parse({"mode": "LIVE!"})


class TestConnectionRequests:
    def test_a_blank_key_arrives_as_none_not_empty_string(self) -> None:
        """
        **The distinction the edit form depends on.** ``update_connection`` reads ``None``
        as "leave the stored credential alone"; an empty string would be indistinguishable
        from somebody deliberately clearing it, and the form posts a blank on every save
        where they only fixed a typo in the label.
        """
        parsed = ConnectionUpdateRequest.parse({"label": "Billing API", "api_key": ""})

        assert parsed.api_key is None

    def test_a_supplied_key_survives_untouched(self) -> None:
        """Trimmed, not otherwise altered. A credential that got normalised on the way in
        is one that no longer authenticates."""
        parsed = ConnectionUpdateRequest.parse(
            {"label": "Billing API", "api_key": f"  {SECRET}  "}
        )

        assert parsed.api_key == SECRET

    def test_a_connector_is_required(self) -> None:
        with pytest.raises(HTTPException) as caught:
            ConnectionCreateRequest.parse({"label": "Billing API"})

        assert caught.value.detail == "Connector is required"

    def test_the_allowlist_is_bounded(self) -> None:
        """An allow-list long enough to be convenient is one nobody audits — and this is
        the one setting that lets a request reach inside the network."""
        with pytest.raises(HTTPException) as caught:
            PrivateHostRequest.parse(
                {"allow": "true", "hosts": [f"h{n}.internal:443" for n in range(20)],
                 "cidrs": ["10.42.0.0/16"]}
            )

        assert "10" in caught.value.detail

    def test_blank_allowlist_rows_are_dropped(self) -> None:
        """An HTML list control posts an empty row for the one somebody is halfway through
        typing. Stored, it would be an entry matching nothing that nobody can explain."""
        parsed = PrivateHostRequest.parse(
            {"allow": "true", "hosts": ["sap.internal:443", "  "], "cidrs": ["10.42.0.0/16"]}
        )

        assert parsed.hosts == ["sap.internal:443"]


class TestOperationSaveRequest:
    def base(self, **overrides) -> dict:
        form = {
            "operation_id": "list_contacts",
            "label": "List contacts",
            "kind": OPERATION_READ,
            "method": "GET",
            "path": "/contacts",
        }
        form.update(overrides)
        return form

    def test_a_lowercase_method_is_upper_cased(self) -> None:
        """
        The verb decides whether the retry rules treat a call as a write. A ``post`` that
        failed that comparison would be retried after a timeout — which is how a timed-out
        order becomes two orders.
        """
        assert OperationSaveRequest.parse(self.base(method="post")).method == "POST"

    def test_an_unknown_method_is_refused(self) -> None:
        with pytest.raises(HTTPException) as caught:
            OperationSaveRequest.parse(self.base(method="FETCH"))

        assert "GET" in caught.value.detail

    def test_json_templates_arrive_parsed(self) -> None:
        """They travel as JSON in hidden inputs, so a browser that posts a malformed one is
        refused by name rather than having the work silently discarded."""
        parsed = OperationSaveRequest.parse(
            self.base(query_template='{"since": "{updated_after}"}')
        )

        assert parsed.query_template == {"since": "{updated_after}"}

    def test_a_malformed_template_is_refused_by_its_label(self) -> None:
        """
        Named for the field somebody filled in. ``schemas/base`` builds this from the
        field's *name* rather than its ``title`` — which is why the four template fields
        here are titled to match their names, so the form and the error say the same word.
        """
        with pytest.raises(HTTPException) as caught:
            OperationSaveRequest.parse(self.base(query_template="{not json"))

        assert caught.value.detail.startswith("Query template")

    def test_a_field_list_arrives_as_a_list(self) -> None:
        parsed = OperationSaveRequest.parse(
            self.base(inputs='[{"name": "email", "required": true}]')
        )

        assert parsed.inputs == [{"name": "email", "required": True}]

    def test_a_field_inside_a_list_is_named_in_the_refusal(self) -> None:
        """
        The naive implementation reports ``entry 0``. Somebody counting rows on a screen
        starts at one, and being told the wrong row is worse than being told no row.
        """
        with pytest.raises(HTTPException) as caught:
            OperationSaveRequest.parse(
                self.base(inputs=[{"name": "email"}, {"name": "amount"}, "not-an-object"])
            )

        assert "entry 3" in caught.value.detail

    def test_it_hands_the_service_one_mapping(self) -> None:
        """
        ``save_operation`` takes the operation as a single mapping, because that is what it
        is — the table's columns *are* ``OperationSpec``'s fields. A call site enumerating
        twenty keyword arguments would be a third place that list is written down.
        """
        operation = OperationSaveRequest.parse(self.base()).operation()

        assert operation["operation_id"] == "list_contacts"
        assert operation["method"] == "GET"


class TestTheRunFrame:
    def test_the_counters_are_absolute(self) -> None:
        """
        Whole state for the numbers, deliberately: a consumer that missed a frame must not
        be left holding a wrong total, and a delta-based frame is wrong for anything
        somebody bills on.
        """
        frame = RunFrameView.build(
            {"uuid": "r1", "flow_uuid": "f1", "status": "running", "mode": "live",
             "counts": {"read": 50_000, "written": 49_997, "failed": 3, "skipped": 0}}
        )

        assert frame.counts.written == 49_997

    def test_a_truncated_log_is_stated_rather_than_inferred(self) -> None:
        """
        ``counts.failed`` is how many failed; the log holds at most a thousand of them. A
        page showing only what the log kept would quietly under-report a bad sync, so the
        flag travels with the frame.
        """
        frame = RunFrameView.build(
            {"uuid": "r1", "flow_uuid": "f1", "status": "partial", "mode": "live",
             "counts": {"failed": 8_000}, "records_log_truncated": True}
        )

        assert frame.counts.failed == 8_000
        assert frame.records_log_truncated is True

    def test_a_collapsed_step_says_how_many_passes_it_stands_for(self) -> None:
        """
        After five hundred passes for one node the engine stops inserting and updates a
        single row. Without ``rollup_count`` a 50,000-record run's log would read as though
        it stopped at pass five hundred.
        """
        step = RunStepView.build(
            {"uuid": "s1", "node_id": "w1", "status": "succeeded",
             "is_rollup": True, "rollup_count": 100}
        )

        assert step.rollup_count == 100

    def test_the_record_payload_does_not_shadow_the_serialiser(self) -> None:
        """
        ``payload`` is a *method* on every schema in this application — the one that
        renders a model as JSON. A field of that name would shadow it, and
        ``RunRecordView(...).payload()`` would raise ``TypeError`` in whichever handler
        reached for it first. Hence ``record``, read from the row's ``payload`` by alias.
        """
        view = RunRecordView.build(
            {"uuid": "rr1", "node_id": "w1", "outcome": "failed",
             "payload": {"email": "someone@example.com"}}
        )

        assert view.record == {"email": "someone@example.com"}
        assert view.payload()["record"] == {"email": "someone@example.com"}
