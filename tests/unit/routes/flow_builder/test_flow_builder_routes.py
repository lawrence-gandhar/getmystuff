"""
Route tests for the Flow Builder — currently the help page.

The help page is static, so what is worth testing is that it *renders* and that the two
pages that link to it actually do. A page whose body is full of ``{{VARIABLE}}`` samples
is one Jinja delimiter away from a 500, and the only person who would find that is the
operator who needed the page.

The one test here that is not about rendering is
``test_it_describes_every_block_the_canvas_can_save``. Flow Builder has no Python
label vocabulary — the canvas labels live in ``static/js/flow_builder.js`` and the server
knows only ``flow_service._VALID_NODE_TYPES`` — so a new block type could be added and
saved with nothing on the help page describing it. That test pins the two together: add a
type to the validator and it fails until the page's block table follows.

``/help`` also has to keep winning against ``/{flow_id:uuid}/…``; a literal path does, but
the assertion is cheap and the failure would be a 404 on a working page.
"""

from __future__ import annotations

import pytest

from app.models.flow_builder import ChatbotFlow
from app.routes.flow_builder import FlowBuilderController

#: Every node type the server will accept, and the name the help page calls it by. The
#: keys are checked against the validator's own set, so this mapping cannot fall behind
#: without a failure that says so.
_BLOCK_LABELS = {
    "start": "Start",
    "send_message": "Send Message",
    "ask_input": "Ask for Input",
    "menu": "Menu / Buttons",
    "dropdown": "Dropdown",
    "if_else": "If / Else",
    "goto": "Goto",
    "ai_fallback": "AI Fallback",
    "run_graph": "Run Graph",
    "send_email": "Send Email",
    "end": "End Flow",
}


@pytest.fixture
def client(auth_client_factory):  # noqa: ANN001, ANN201
    return auth_client_factory(FlowBuilderController)


@pytest.fixture
def make_flow(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str = "Flow", **kwargs):  # noqa: ANN001
        row = ChatbotFlow(
            user_id=owner.id,
            name=name,
            graph_data=kwargs.pop(
                "graph_data",
                {"nodes": [{"id": "n1", "type": "start", "data": {}}], "edges": []},
            ),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


class TestTheHelpPage:
    def test_it_renders_inside_the_application_layout(self, client) -> None:  # noqa: ANN001
        response = client.get("/flow-builder/help")

        assert response.status_code == 200
        assert "Flow Builder — Help" in response.text

    def test_it_is_not_mistaken_for_a_flow_id(self, client) -> None:  # noqa: ANN001
        """``/help`` sits beside ``/{flow_id:uuid}/edit``. The uuid converter will not
        match the word, but a 404 here would be a working page nobody could reach."""
        assert client.get("/flow-builder/help").status_code == 200

    def test_the_variable_examples_survive_template_rendering(self, client) -> None:  # noqa: ANN001
        """
        The whole body sits inside ``{% raw %}``, so the samples must arrive as written —
        braces intact. This is the assertion that matters most on this page: its central
        warning is that message text does *not* interpolate, and an unescaped
        ``{{NAME}}`` would render as an empty string and teach the exact opposite.
        """
        body = client.get("/flow-builder/help").text

        assert "Thanks {{NAME}}!" in body
        assert "{{FROM_EMAIL}}" in body
        assert "at least one TO address (it may be a {{VARIABLE}})" in body

    def test_it_describes_every_block_the_canvas_can_save(self, client) -> None:  # noqa: ANN001
        """
        A block the server accepts with nothing about it on the help page is a block
        somebody has to guess at. See the module docstring on why this is a mapping
        rather than a read of a label vocabulary — there is no Python one.
        """
        from app.services.flow_builder.flow_service import _VALID_NODE_TYPES

        assert set(_BLOCK_LABELS) == _VALID_NODE_TYPES, (
            "a node type was added to or removed from the validator without the help "
            "page's block table following it"
        )

        body = client.get("/flow-builder/help").text

        for label in _BLOCK_LABELS.values():
            assert label in body, f"the help page does not mention the '{label}' block"

    def test_it_lists_the_refusals_a_save_can_produce(self, client) -> None:  # noqa: ANN001
        """
        The refusal table is the reason somebody opens this page mid-build, so the
        wording has to match what ``flow_service._validate_graph`` actually raises.
        """
        body = client.get("/flow-builder/help").text

        for message in (
            "Flow must contain exactly one Start node",
            "Start node cannot have incoming edges",
            "End node cannot have outgoing edges",
            "Goto node must target a valid node",
            "If/Else node is missing a variable name",
            "AI Fallback is set to use an attached LLM API but no key is selected",
        ):
            assert message in body, f"the help page does not explain '{message}'"

    def test_the_library_links_to_it_in_a_new_tab(self, client) -> None:  # noqa: ANN001
        body = client.get("/flow-builder/").text

        assert 'href="/flow-builder/help"' in body
        assert 'target="_blank"' in body

    async def test_the_canvas_links_to_it_too(self, client, user, make_flow) -> None:  # noqa: ANN001
        """
        The canvas is where a port or an operator needs explaining, and going back to the
        library for it would mean leaving unsaved work on the page.
        """
        flow = await make_flow(user, "Drawing")

        body = client.get(f"/flow-builder/{flow.uuid}/edit").text

        assert 'href="/flow-builder/help"' in body
