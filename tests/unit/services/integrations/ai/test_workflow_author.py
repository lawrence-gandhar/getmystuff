"""
Tests for ``ai/workflow_author.py`` — the layer that decides whether to believe a model.

**Four lies, and each is asserted twice.** A nonexistent connection, an operation the
connector does not have, a mapping target the operation does not accept, and a forward
reference. For each: the refusal names the *real* alternatives, and nothing is saved. The
second half is the point — asserting the refusal alone would pass an implementation that
saved first and validated after, which is the shape this design exists to prevent.

Plus a **negative control**: "shopify eu" resolves and the saved step holds the real uuid.
Without it the suite would pass an implementation that refused everything.

The third and fourth lies matter more than the first two, and it is worth saying why. A
nonexistent connection fails loudly on the first record. A mapping target the operation does
not accept **does not fail at all** — the sync runs, reports success, and simply does not
carry that field. Nothing in the run record says anything is wrong, because as far as the
engine is concerned nobody asked for it.

``validate_draft`` is pure — no database, no network — which is what makes every one of
these a table-driven unit test rather than something needing a stubbed provider.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from app.models.integrations import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    NODE_BATCH,
    NODE_CONNECTOR_READ,
    NODE_CONNECTOR_WRITE,
    NODE_FILTER,
    NODE_SUCCESS,
    NODE_TRIGGER,
)
from app.schemas.integrations.workflow_draft_schemas import WorkflowDraft
from app.services.integrations.ai import catalogue as catalogue_builder
from app.services.integrations.ai.workflow_author import (
    DraftRefused,
    validate_draft,
)

SHOPIFY_UUID = "11111111-1111-4111-8111-111111111111"
CRM_UUID = "22222222-2222-4222-8222-222222222222"


def catalogue() -> Dict[str, Any]:
    """Two connections, one readable and one writable. Fixed uuids rather than generated
    ones so a failure is reproducible."""
    return {
        "connections": [
            {
                "uuid": SHOPIFY_UUID,
                "label": "Shopify EU",
                "connector": "REST API",
                "account": "",
                "operations": [
                    {
                        "id": "list_orders",
                        "label": "List orders",
                        "kind": "read",
                        "description": "",
                        "inputs": [],
                        "outputs": [
                            {"name": "id", "type": "string", "required": False,
                             "description": ""},
                            {"name": "email", "type": "string", "required": False,
                             "description": ""},
                        ],
                        "inputs_truncated": False,
                        "outputs_truncated": False,
                    }
                ],
            },
            {
                "uuid": CRM_UUID,
                "label": "Acme CRM",
                "connector": "REST API",
                "account": "",
                "operations": [
                    {
                        "id": "create_contact",
                        "label": "Create contact",
                        "kind": "write",
                        "description": "",
                        "inputs": [
                            {"name": "email", "type": "string", "required": True,
                             "description": ""},
                            {"name": "name", "type": "string", "required": False,
                             "description": ""},
                        ],
                        "outputs": [],
                        "inputs_truncated": False,
                        "outputs_truncated": False,
                    }
                ],
            },
        ],
        "connections_total": 2,
        "truncated": False,
    }


def draft(steps: List[Dict[str, Any]], **overrides: Any) -> WorkflowDraft:
    payload = {"name": "Orders to CRM", "steps": steps}
    payload.update(overrides)
    return WorkflowDraft.parse(payload)


def read_step(**overrides: Any) -> Dict[str, Any]:
    step = {
        "ref": "read",
        "type": NODE_CONNECTOR_READ,
        "label": "Read orders",
        "connection": "Shopify EU",
        "operation": "list_orders",
    }
    step.update(overrides)
    return step


def batch_step(**overrides: Any) -> Dict[str, Any]:
    step = {"ref": "loop", "type": NODE_BATCH, "label": "Each batch", "batch_size": 500}
    step.update(overrides)
    return step


def write_step(**overrides: Any) -> Dict[str, Any]:
    step = {
        "ref": "write",
        "type": NODE_CONNECTOR_WRITE,
        "label": "Create contact",
        "connection": "Acme CRM",
        "operation": "create_contact",
        "mappings": [{"source": "email", "target": "email"}],
    }
    step.update(overrides)
    return step


def node_of(resolved, node_type: str) -> Dict[str, Any]:
    for node in resolved.graph_data["nodes"]:
        if node["type"] == node_type:
            return node
    raise AssertionError(f"no {node_type} node in the drawing")


# ---------------------------------------------------------------------------
# The four lies
# ---------------------------------------------------------------------------


class TestHallucinations:
    def test_a_connection_that_does_not_exist_is_refused_by_name(self) -> None:
        """
        And the refusal lists the real ones. "That connection does not exist" leaves
        somebody guessing; the same sentence followed by the two names they do have is one
        they can act on.
        """
        with pytest.raises(DraftRefused) as caught:
            validate_draft(draft([read_step(connection="Shopify Prod")]), catalogue())

        assert "Shopify Prod" in caught.value.problems[0]
        assert "Shopify EU" in caught.value.problems[0]
        assert "Acme CRM" in caught.value.alternatives

    def test_a_close_name_is_not_quietly_accepted(self) -> None:
        """
        **No fuzzy matching, and this is a decision rather than an omission.** "Shopify
        Prod" resolving to "Shopify EU" writes somebody's customers into the wrong store,
        silently, at 3am, on a schedule. One more exchange is cheaper than a data migration.
        """
        with pytest.raises(DraftRefused):
            validate_draft(draft([read_step(connection="Shopify")]), catalogue())

    def test_an_operation_the_connection_does_not_have_is_refused(self) -> None:
        with pytest.raises(DraftRefused) as caught:
            validate_draft(draft([read_step(operation="list_customers")]), catalogue())

        assert "list_customers" in caught.value.problems[0]
        assert "list_orders" in caught.value.problems[0]

    def test_a_mapping_target_the_operation_does_not_accept_is_refused(self) -> None:
        """
        **The one that matters most.** A workflow mapping into ``customer_email`` where the
        operation takes ``email`` runs, reports success, and does not carry the address —
        and nothing in the run record says so, because as far as the engine is concerned
        nobody asked for that field.
        """
        steps = [
            read_step(),
            batch_step(),
            write_step(mappings=[{"source": "email", "target": "customer_email"}]),
        ]

        with pytest.raises(DraftRefused) as caught:
            validate_draft(draft(steps), catalogue())

        assert "customer_email" in caught.value.problems[0]
        assert "email" in caught.value.problems[0]

    def test_a_forward_reference_is_refused(self) -> None:
        """A step reading records that do not exist yet. Left alone it produces a workflow
        that reads an empty set and reports success."""
        steps = [read_step(source_ref="write"), batch_step(), write_step()]

        with pytest.raises(DraftRefused) as caught:
            validate_draft(draft(steps), catalogue())

        assert "comes before it" in caught.value.problems[0]

    def test_a_read_step_pointed_at_a_write_operation_is_refused(self) -> None:
        with pytest.raises(DraftRefused) as caught:
            validate_draft(
                draft([read_step(connection="Acme CRM", operation="create_contact")]),
                catalogue(),
            )

        assert "write operation" in caught.value.problems[0]


class TestTheNegativeControl:
    """
    Without these the suite would pass an implementation that refused everything, which is
    a real risk for a module whose whole job is refusing.
    """

    def test_a_real_name_resolves(self) -> None:
        resolved = validate_draft(
            draft([read_step(), batch_step(), write_step()]), catalogue()
        )

        assert resolved.node_count == 5   # trigger, read, batch, write, success

    def test_resolution_replaces_the_spelling_with_the_real_uuid(self) -> None:
        """
        **Replaced, not tolerated.** Nothing downstream can act on a name a model chose,
        which is what makes the saved workflow point at a row rather than at a string.
        """
        resolved = validate_draft(
            draft([read_step(), batch_step(), write_step()]), catalogue()
        )

        read = node_of(resolved, NODE_CONNECTOR_READ)
        assert read["data"]["connection_uuid"] == SHOPIFY_UUID
        assert "Shopify EU" not in str(read["data"].get("connection", ""))

    def test_a_differently_cased_name_still_resolves(self) -> None:
        """Exact, then case-insensitive, then stop. A model writing "shopify eu" got the
        name right."""
        resolved = validate_draft(
            draft([read_step(connection="shopify eu"), batch_step(), write_step()]),
            catalogue(),
        )

        assert node_of(resolved, NODE_CONNECTOR_READ)["data"]["connection_uuid"] == SHOPIFY_UUID

    def test_a_mapping_target_is_stored_with_the_catalogue_spelling(self) -> None:
        """A model writing ``EMAIL`` gets ``email`` stored, because that is what the
        request builder will look for."""
        steps = [
            read_step(), batch_step(),
            write_step(mappings=[{"source": "email", "target": "EMAIL"}]),
        ]

        resolved = validate_draft(draft(steps), catalogue())

        assert node_of(resolved, NODE_CONNECTOR_WRITE)["data"]["mappings"] == [
            {"target": "email", "source": "email"}
        ]


# ---------------------------------------------------------------------------
# Warnings, which are not refusals
# ---------------------------------------------------------------------------


class TestUnmappedRequiredFields:
    def test_they_are_a_warning_rather_than_a_refusal(self) -> None:
        """
        Refusing would throw away a draft that is ninety percent right over a field
        somebody fills in in five seconds. Publishing refuses it, which is the right
        moment: by then a person has looked at it.
        """
        steps = [read_step(), batch_step(), write_step(mappings=[])]

        resolved = validate_draft(draft(steps), catalogue())

        assert resolved.warnings
        assert "email" in resolved.warnings[0]

    def test_the_required_list_is_stamped_for_the_canvas(self) -> None:
        """So the mapping panel is red on first paint rather than after somebody presses
        Publish and is told no."""
        steps = [read_step(), batch_step(), write_step(mappings=[])]

        resolved = validate_draft(draft(steps), catalogue())

        assert node_of(resolved, NODE_CONNECTOR_WRITE)["data"]["required_inputs"] == ["email"]


class TestStepsADraftMayNotUse:
    def test_a_validate_step_is_refused_with_a_reason(self) -> None:
        """
        Deliberately outside what a draft may contain. ``validate`` splits a batch two ways
        and where the invalid half goes is a decision with consequences — a generator
        guessing at it produces a workflow that silently discards records while reporting
        success.
        """
        steps = [read_step(), batch_step(),
                 {"ref": "check", "type": "validate", "label": "Check"}]

        with pytest.raises(DraftRefused) as caught:
            validate_draft(draft(steps), catalogue())

        assert "canvas" in caught.value.problems[0]

    def test_a_filter_step_is_refused_too(self) -> None:
        """
        Subtler than ``validate`` and worse. Compiling "only EU orders" into an operator
        and a typed value is the guess that comes out meaning the opposite, and a filter
        that silently keeps the wrong half is worse than no filter at all.

        ``validate_flow`` agrees from the other direction — it refuses a filter with no
        conditions, because one that lets every record through looks like it is working —
        so a draft that emitted an unfinished filter could not be saved anyway. Refusing it
        here is what makes the *reason* legible.
        """
        steps = [read_step(), batch_step(),
                 {"ref": "keep", "type": NODE_FILTER, "label": "Only EU"},
                 write_step()]

        with pytest.raises(DraftRefused) as caught:
            validate_draft(draft(steps), catalogue())

        assert "canvas" in caught.value.problems[0]

    def test_a_step_type_that_does_not_exist_is_refused(self) -> None:
        steps = [{"ref": "x", "type": "teleport", "label": "Teleport"}]

        with pytest.raises(DraftRefused) as caught:
            validate_draft(draft(steps), catalogue())

        assert "teleport" in caught.value.problems[0]


# ---------------------------------------------------------------------------
# The wiring, which the model does not do
# ---------------------------------------------------------------------------


class TestWiring:
    def test_a_trigger_and_a_success_are_added(self) -> None:
        """The model writes neither. A workflow needs exactly one trigger and the drawing
        has to end somewhere, and neither is a decision worth a token."""
        resolved = validate_draft(
            draft([read_step(), batch_step(), write_step()]), catalogue()
        )

        types = [node["type"] for node in resolved.graph_data["nodes"]]
        assert types[0] == NODE_TRIGGER
        assert types[-1] == NODE_SUCCESS

    def test_a_batch_body_always_returns_to_the_batch(self) -> None:
        """
        **The failure this shape cannot express.** A batch whose body never returns runs
        one batch of a hundred and reports success — and the drawing looks entirely
        reasonable, so nobody reviewing it has a reason to doubt it. Computing the wiring is
        what makes that unrepresentable.
        """
        resolved = validate_draft(
            draft([read_step(), batch_step(), write_step()]), catalogue()
        )

        batch = node_of(resolved, NODE_BATCH)
        returning = [
            edge for edge in resolved.graph_data["edges"]
            if edge["target"] == batch["id"]
        ]

        assert any(edge["source"] != batch["id"] for edge in returning)

    def test_the_batch_done_port_goes_to_success(self) -> None:
        resolved = validate_draft(
            draft([read_step(), batch_step(), write_step()]), catalogue()
        )

        batch = node_of(resolved, NODE_BATCH)
        success = node_of(resolved, NODE_SUCCESS)

        assert any(
            edge["source"] == batch["id"] and edge["source_port"] == "done"
            and edge["target"] == success["id"]
            for edge in resolved.graph_data["edges"]
        )

    def test_a_draft_with_no_batch_is_a_straight_line(self) -> None:
        resolved = validate_draft(draft([read_step()]), catalogue())

        assert len(resolved.graph_data["nodes"]) == 3
        assert len(resolved.graph_data["edges"]) == 2

    def test_the_result_passes_the_same_validator_a_drawing_passes(self) -> None:
        """
        The property that makes a generated workflow exactly as trustworthy as a hand-drawn
        one. ``validate_draft`` runs ``flow_rules.validate_flow`` itself, so this asserts it
        again from outside rather than trusting that it was called.
        """
        from app.services.integrations.engine import flow_rules

        resolved = validate_draft(
            draft([read_step(), batch_step(), write_step()]), catalogue()
        )

        flow_rules.validate_flow(resolved.graph_data)   # must not raise

    def test_no_two_steps_get_the_same_id(self) -> None:
        """A model-chosen id that collided would silently rewire the graph, joining two
        steps that were never meant to meet. Ids are assigned here for that reason."""
        steps = [read_step(), batch_step(), write_step(),
                 write_step(ref="write2", label="Second write")]

        resolved = validate_draft(draft(steps), catalogue())

        ids = [node["id"] for node in resolved.graph_data["nodes"]]
        assert len(ids) == len(set(ids))


class TestSmallThings:
    def test_an_absurd_batch_size_is_clamped_rather_than_refused(self) -> None:
        """A model that wrote 100000 meant "a lot". Refusing a whole draft over a number
        with an obvious right answer is not worth the exchange."""
        resolved = validate_draft(
            draft([read_step(), batch_step(batch_size=100000), write_step()]), catalogue()
        )

        assert node_of(resolved, NODE_BATCH)["data"]["batch_size"] == MAX_BATCH_SIZE

    def test_a_missing_batch_size_gets_the_default(self) -> None:
        resolved = validate_draft(
            draft([read_step(), batch_step(batch_size=None), write_step()]), catalogue()
        )

        assert node_of(resolved, NODE_BATCH)["data"]["batch_size"] == DEFAULT_BATCH_SIZE

    def test_an_unnamed_draft_takes_its_name_from_the_request(self) -> None:
        """So it always arrives as something recognisable rather than "Untitled"."""
        resolved = validate_draft(
            draft([read_step()], name=""), catalogue(),
            fallback_name="sync new orders into the CRM",
        )

        assert resolved.name == "sync new orders into the CRM"

    def test_an_empty_draft_is_refused(self) -> None:
        with pytest.raises(DraftRefused):
            validate_draft(draft([]), catalogue())

    def test_assumptions_survive_to_the_page(self) -> None:
        """A guess the model reported is one somebody can check; one it did not is
        indistinguishable from knowledge."""
        resolved = validate_draft(
            draft([read_step()], assumptions=["Used the order's email as the contact's"]),
            catalogue(),
        )

        assert resolved.assumptions == ["Used the order's email as the contact's"]



class TestTheSchemaBoundsTheShape:
    def test_two_steps_with_the_same_handle_are_refused(self) -> None:
        """
        Not a mistake a person makes and one a model makes. Left alone, the second silently
        wins every ``source_ref`` pointed at either — so the drawing reads correctly and the
        records come from the wrong place.
        """
        from litestar.exceptions import HTTPException

        with pytest.raises(HTTPException):
            draft([read_step(), read_step()])

    def test_a_draft_cannot_switch_a_workflow_on(self) -> None:
        """
        There is no ``is_active`` field anywhere in the draft schemas. A field that cannot
        be set cannot be set wrongly, and the alternative is a generated workflow writing
        into somebody's CRM before a person has read it.
        """
        parsed = draft([read_step()], is_active=True, interval_seconds=60)

        assert not hasattr(parsed, "is_active")
        assert not hasattr(parsed, "interval_seconds")


class TestTheCatalogueFitsTheModel:
    """
    The in-built model has a 1536-token prompt budget, and Ollama truncates from the **end**
    — which is exactly where ``user_content`` puts the user's own sentence, deliberately,
    because that is where a model keeps it in view. An over-long prompt therefore does not
    crowd the request out, it deletes it.

    **What fills that window is not the catalogue**, and the first version of this comment
    said otherwise. Measured against ``qwen3:1.7b`` with one connection and two operations:
    the JSON schema ``_json_only_instruction`` appends is 971 tokens, the system prompt is
    710, and the catalogue plus the request together are about 100. Capping the catalogue is
    not what would make the local path work.

    What the cap does is bound the *variable* part — twenty connections with fifteen
    operations each really would be 12,000 characters — so these tests assert that and
    nothing stronger. The recorded conclusion is that this task is out of reach for a 1.7B
    model at a 2048-token context, and that the feature degrades correctly when it is.
    """

    def big_catalogue(self) -> Dict[str, Any]:
        base = catalogue()
        base["connections"] = [
            {**base["connections"][0], "label": f"Connection {n}", "uuid": SHOPIFY_UUID}
            for n in range(40)
        ]
        return base

    def test_the_local_cap_is_far_below_the_hosted_one(self) -> None:
        """Arithmetic, not taste: 1536 tokens minus a 710-token system prompt minus a
        500-token instruction leaves roughly 300 tokens."""
        assert catalogue_builder.MAX_CHARS_INBUILT * 4 < catalogue_builder.MAX_CHARS

    def test_a_local_prompt_stays_inside_the_window(self) -> None:
        rendered = catalogue_builder.render(
            self.big_catalogue(), max_chars=catalogue_builder.MAX_CHARS_INBUILT
        )

        assert len(rendered) <= catalogue_builder.MAX_CHARS_INBUILT + 200

    def test_a_hosted_prompt_is_allowed_the_whole_catalogue(self) -> None:
        rendered = catalogue_builder.render(self.big_catalogue())

        assert len(rendered) > catalogue_builder.MAX_CHARS_INBUILT

    def test_truncation_is_announced_rather_than_silent(self) -> None:
        """
        A model shown eight of forty connections and told nothing will confidently report
        that the ninth does not exist — which reads to the user as the feature being broken
        rather than as the prompt being full.
        """
        rendered = catalogue_builder.render(
            self.big_catalogue(), max_chars=catalogue_builder.MAX_CHARS_INBUILT
        )

        assert "and more, not shown" in rendered

    def test_the_cut_lands_between_connections_never_mid_field(self) -> None:
        """
        A prompt ending halfway through a field name reads as though that field is called
        something it is not — which is worse than the field being absent, because absent
        means the model cannot map it and half-present means it maps to a name nothing
        accepts.
        """
        rendered = catalogue_builder.render(
            self.big_catalogue(), max_chars=catalogue_builder.MAX_CHARS_INBUILT
        )

        assert rendered.rstrip().endswith("_")   # the truncation marker, not a field
