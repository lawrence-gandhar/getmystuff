"""
Tests for the two small pieces the Run Flow block is built on: what a flow *says* it reads
and writes, and what happens to ``{{NAME}}`` in a block's text.

``flow_io`` is what a Run Flow block's property panel draws its rows from. Deriving those
lists from the callee's own graph rather than asking an operator to declare them twice is
the point: a list read off the flow cannot drift from what the flow does. So what is
asserted here is that it reads the whole vocabulary — every block that stores something is
a value that can be handed back — and that it does not offer as a *parameter* something the
flow collects for itself, which would invite somebody to overwrite the answer they are
about to ask for.

``_render_text`` is the substitution that makes a returned value worth having. Its one
surprising rule is that an unknown placeholder is **left standing**: a visible
``{{ORDR_REF}}`` names the misspelling, where a blank would look like a value that failed
to arrive. That is deliberately different from the email renderer, which refuses the whole
send — an email cannot be recalled and a chat bubble is one message in a conversation.
"""

from __future__ import annotations

import uuid

import pytest
from litestar.exceptions import HTTPException

from app.services.flow_builder.engine_service import _render_text
from app.services.flow_builder.flow_service import (
    _validate_run_flow_data,
    _validate_graph,
    flow_io,
)

SELF_UUID = uuid.UUID("11111111-2222-3333-4444-555555555555")
OTHER_UUID = uuid.UUID("99999999-8888-7777-6666-555555555555")


class TestFlowIoWrites:
    def test_every_kind_of_storing_block_is_offered(self) -> None:
        graph = {
            "nodes": [
                {"id": "a", "type": "ask_input", "data": {"variable_name": "email"}},
                {"id": "b", "type": "menu", "data": {"variable_name": "choice", "options": []}},
                {"id": "c", "type": "ai_fallback", "data": {"variable_name": "answer"}},
                {"id": "d", "type": "run_graph", "data": {"variable_name": "found"}},
                {"id": "e", "type": "send_email", "data": {"variable_name": "mail_id"}},
            ],
            "edges": [],
        }

        assert flow_io(graph)["writes"] == ["email", "choice", "answer", "found", "mail_id"]

    def test_a_nested_calls_returned_values_count_as_written(self) -> None:
        """
        A flow that calls another and keeps a value under its own name can hand that value
        on again — it is stored in exactly the same place as anything else.
        """
        graph = {
            "nodes": [
                {"id": "a", "type": "run_flow",
                 "data": {"flow_id": str(OTHER_UUID), "outputs": {"email": "CUSTOMER_EMAIL"}}},
            ],
            "edges": [],
        }

        assert flow_io(graph)["writes"] == ["CUSTOMER_EMAIL"]

    def test_order_is_the_order_on_the_canvas(self) -> None:
        """Not sorted: the operator's arrangement is information, the rule the email module
        states for a template's declared variables."""
        graph = {
            "nodes": [
                {"id": "a", "type": "ask_input", "data": {"variable_name": "zebra"}},
                {"id": "b", "type": "ask_input", "data": {"variable_name": "apple"}},
            ],
            "edges": [],
        }

        assert flow_io(graph)["writes"] == ["zebra", "apple"]

    def test_blank_and_repeated_names_are_dropped(self) -> None:
        graph = {
            "nodes": [
                {"id": "a", "type": "ask_input", "data": {"variable_name": "  "}},
                {"id": "b", "type": "ask_input", "data": {"variable_name": "email"}},
                {"id": "c", "type": "ask_input", "data": {"variable_name": "email"}},
                {"id": "d", "type": "send_message", "data": {"message_text": "hello"}},
            ],
            "edges": [],
        }

        assert flow_io(graph)["writes"] == ["email"]


class TestFlowIoReads:
    def test_an_if_else_condition_counts(self) -> None:
        graph = {
            "nodes": [{"id": "a", "type": "if_else", "data": {"variable_name": "tier"}}],
            "edges": [],
        }

        assert flow_io(graph)["reads"] == ["tier"]

    def test_placeholders_in_message_and_prompt_text_count(self) -> None:
        graph = {
            "nodes": [
                {"id": "a", "type": "send_message",
                 "data": {"message_text": "Hello {{NAME}}, ref {{ORDER_REF}}"}},
                {"id": "b", "type": "ask_input", "data": {"prompt_text": "Still {{NAME}}?"}},
            ],
            "edges": [],
        }

        assert flow_io(graph)["reads"] == ["NAME", "ORDER_REF"]

    def test_what_the_flow_collects_itself_is_not_a_parameter(self) -> None:
        """
        A flow that asks for an email and then says "thanks {{email}}" needs nothing passed
        in. Offering it would invite an operator to overwrite the answer it is about to
        collect.
        """
        graph = {
            "nodes": [
                {"id": "a", "type": "ask_input",
                 "data": {"prompt_text": "Your email?", "variable_name": "email"}},
                {"id": "b", "type": "send_message", "data": {"message_text": "Thanks {{email}}"}},
            ],
            "edges": [],
        }

        io = flow_io(graph)

        assert io["writes"] == ["email"]
        assert io["reads"] == []

    def test_an_empty_graph_is_two_empty_lists(self) -> None:
        assert flow_io({}) == {"writes": [], "reads": []}


class TestRenderText:
    def test_a_known_name_is_substituted(self) -> None:
        assert _render_text("Thanks {{NAME}}!", {"NAME": "Jo"}) == "Thanks Jo!"

    def test_inner_whitespace_is_allowed(self) -> None:
        assert _render_text("Hi {{ NAME }}", {"NAME": "Jo"}) == "Hi Jo"

    def test_an_unknown_name_is_left_standing(self) -> None:
        """
        Visible rather than blank: it names the misspelling. Blanking it would look like a
        value that failed to arrive, and refusing the turn would break a conversation over a
        typo.
        """
        assert _render_text("Ref {{ORDR_REF}}", {"ORDER_REF": "A-1"}) == "Ref {{ORDR_REF}}"

    def test_names_are_matched_exactly(self) -> None:
        """`email` and `EMAIL` are two different variables to every other block."""
        assert _render_text("{{email}}", {"EMAIL": "a@b.c"}) == "{{email}}"

    def test_a_number_is_stringified(self) -> None:
        assert _render_text("{{n}} found", {"n": 12}) == "12 found"

    @pytest.mark.parametrize("text", ["", None, "no placeholders here", "a lone { brace"])
    def test_text_with_nothing_to_do_comes_back_unchanged(self, text) -> None:  # noqa: ANN001
        assert _render_text(text, {"NAME": "Jo"}) == (text or "")

    def test_no_variables_at_all_leaves_everything_standing(self) -> None:
        assert _render_text("Hi {{NAME}}", None) == "Hi {{NAME}}"


class TestRunFlowValidation:
    def _graph(self, data: dict) -> dict:
        return {
            "nodes": [
                {"id": "start", "type": "start", "data": {}},
                {"id": "call", "type": "run_flow", "data": data},
            ],
            "edges": [{"source": "start", "target": "call", "source_port": "default"}],
        }

    def test_a_flow_may_not_run_itself(self) -> None:
        with pytest.raises(HTTPException) as caught:
            _validate_graph(self._graph({"flow_id": str(SELF_UUID)}), self_uuid=SELF_UUID)

        assert "cannot run the flow it is in" in str(caught.value.detail)

    def test_another_flow_is_fine(self) -> None:
        _validate_graph(self._graph({"flow_id": str(OTHER_UUID)}), self_uuid=SELF_UUID)

    def test_no_flow_chosen_is_refused(self) -> None:
        with pytest.raises(HTTPException) as caught:
            _validate_run_flow_data({"flow_id": ""}, SELF_UUID)

        assert "missing a flow" in str(caught.value.detail)

    def test_an_input_source_a_conversation_cannot_serve_is_refused(self) -> None:
        with pytest.raises(HTTPException) as caught:
            _validate_run_flow_data(
                {"flow_id": str(OTHER_UUID), "inputs": {"x": {"source": "record"}}},
                SELF_UUID,
            )

        assert "no value source chosen" in str(caught.value.detail)

    def test_two_outputs_stored_under_one_name_are_refused(self) -> None:
        """One would silently overwrite the other, and which won would depend on dict order."""
        with pytest.raises(HTTPException) as caught:
            _validate_run_flow_data(
                {"flow_id": str(OTHER_UUID), "outputs": {"a": "RESULT", "b": "RESULT"}},
                SELF_UUID,
            )

        assert "two different values in 'RESULT'" in str(caught.value.detail)

    def test_a_blank_destination_is_allowed(self) -> None:
        """Blank is how the panel says "do not bring this one back", not an error."""
        _validate_run_flow_data(
            {"flow_id": str(OTHER_UUID), "outputs": {"a": "", "b": ""}}, SELF_UUID,
        )
