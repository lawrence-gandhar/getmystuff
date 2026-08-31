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
    "run_flow": "Run Flow",
    "send_email": "Send Email",
    "create_file": "Create File",
    "download_file": "Download File",
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
        braces intact. Still the assertion that matters most on this page, though the reason
        has inverted: message text now *does* interpolate, so ``Thanks {{NAME}}!`` is an
        example somebody will copy, and one rendered to an empty string by this page's own
        template engine would teach them syntax that does not exist.
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


class TestTheCanvasScripts:
    """
    Script order, which is a requirement rather than a preference.

    ``graph_selection.js`` reads ``window.GraphCanvas`` at module scope and
    ``flow_builder.js`` reads ``window.GraphSelection`` in ``init``. Get the order
    wrong and the page comes up with an empty canvas and one "undefined" in the
    console — no server error, nothing in a log. It is also exactly the sort of thing
    an edit reorders without noticing, and the only part of the canvas's JavaScript
    that a Python test can hold on to at all: this repository has no JavaScript test
    runner.
    """

    async def test_all_three_scripts_are_loaded(self, client, user, make_flow) -> None:  # noqa: ANN001
        flow = await make_flow(user, "Scripted")

        body = client.get(f"/flow-builder/{flow.uuid}/edit").text

        assert 'src="/static/js/graph_canvas.js"' in body
        assert 'src="/static/js/graph_selection.js"' in body
        assert 'src="/static/js/graph_insert.js"' in body
        assert 'src="/static/js/flow_builder.js"' in body

    async def test_they_are_loaded_in_dependency_order(
        self, client, user, make_flow,
    ) -> None:  # noqa: ANN001
        flow = await make_flow(user, "Ordered")

        body = client.get(f"/flow-builder/{flow.uuid}/edit").text

        # The `<script src>` occurrences specifically. Searching for the bare
        # filenames would find the comments that mention them and compare those
        # instead — a test that passes or fails on prose.
        canvas_tag = body.index('src="/static/js/graph_canvas.js"')
        selection_tag = body.index('src="/static/js/graph_selection.js"')
        insert_tag = body.index('src="/static/js/graph_insert.js"')
        edges_tag = body.index('src="/static/js/graph_edges.js"')
        builder_tag = body.index('src="/static/js/flow_builder.js"')

        # Both shared modules read `window.GraphCanvas`, and flow_builder.js reads both
        # of them, so either one landing after it is a blank canvas.
        assert canvas_tag < selection_tag < builder_tag
        assert canvas_tag < insert_tag < builder_tag
        assert canvas_tag < edges_tag < builder_tag

    async def test_the_selection_stylesheet_is_linked(
        self, client, user, make_flow,
    ) -> None:  # noqa: ANN001
        """
        The rubber-band box is the one piece of canvas appearance that is shared, so
        it comes from its own sheet rather than from this page's inline styles.
        """
        flow = await make_flow(user, "Styled")

        body = client.get(f"/flow-builder/{flow.uuid}/edit").text

        assert "/static/css/graph_selection.css" in body


class TestTheKindColumn:
    """
    The library has to say what each flow is *for*, because the two kinds behave
    differently in two other places — an agent's dropdown and a Run Flow block's list — and
    a row that did not say which it was would make both look arbitrary.
    """

    async def test_an_agent_flow_shows_as_agent_and_offers_make_generic(
        self, client, user, make_flow,
    ) -> None:  # noqa: ANN001
        await make_flow(user, "Front door", kind="agent")

        body = client.get("/flow-builder/").text

        assert ">Agent" in body or "Agent\n" in body
        assert "Make Generic" in body
        assert "Not attached" in body

    async def test_a_generic_flow_shows_as_generic_and_offers_make_agent(
        self, client, user, make_flow,
    ) -> None:  # noqa: ANN001
        await make_flow(user, "Collect details", kind="generic")

        body = client.get("/flow-builder/").text

        assert "Generic" in body
        assert "Make Agent" in body
        assert "child flow" in body, (
            "a generic flow's Attached Agent cell says 'never attached', not 'not attached "
            "yet' — the second reads as something left undone"
        )

    async def test_the_toggle_switches_a_flow_and_says_so_in_the_rows(
        self, client, user, make_flow,
    ) -> None:  # noqa: ANN001
        flow = await make_flow(user, "Reusable bit", kind="agent")

        body = client.post(
            f"/flow-builder/{flow.uuid}/set-kind", data={"kind": "generic"},
        ).text

        assert "Make Agent" in body, "the row came back showing the new kind"

    async def test_an_unknown_kind_is_refused_with_a_sentence(
        self, client, user, make_flow,
    ) -> None:  # noqa: ANN001
        flow = await make_flow(user, "Reusable bit", kind="agent")

        body = client.post(
            f"/flow-builder/{flow.uuid}/set-kind", data={"kind": "sideways"},
        ).text

        assert "allowed values" in body or "neither" in body
        assert "Make Generic" in body, "still an agent flow"
