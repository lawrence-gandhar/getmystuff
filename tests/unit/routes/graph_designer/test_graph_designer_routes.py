"""
Tests for app/routes/graph_designer/graph_designer_routes.py.

The route layer's own claims, as opposed to the service's: the JSON shapes the canvas
reads, which failures render into the page rather than replacing it, and which ones are
still a 404.

Three properties carry the suite:

* **A refused save renders into the banner, not over the page.** The canvas holds work that
  is not stored anywhere else, so a 400 that navigated away from it would lose that work.
  The refusal comes back as a 200 carrying the reason.
* **A refusal is escaped.** The reason quotes things the user typed — a node label, part of
  a statement — so the response goes through a template rather than an f-string. Asserted
  by putting a tag in a node label and requiring it back escaped.
* **Ownership is a 404 with one sentence.** Another user's graph and a missing one answer
  identically, because a difference is how somebody learns which uuids are real.
"""

from __future__ import annotations

import json
import re
import uuid as uuid_pkg

import pytest

from app.models.graph_designer import ToolGraph
from app.routes.graph_designer import GraphDesignerController


@pytest.fixture
def client(auth_client_factory):  # noqa: ANN001, ANN201
    return auth_client_factory(GraphDesignerController)


@pytest.fixture
def make_graph(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str = "Graph", **kwargs):  # noqa: ANN001
        row = ToolGraph(
            user_id=owner.id,
            name=name,
            graph_data=kwargs.pop("graph_data", {
                "nodes": [{
                    "id": "start", "type": "start",
                    "position": {"x": 0, "y": 0}, "data": {"label": "Start"},
                }],
                "edges": [],
            }),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


def is_selected(html: str, option_value) -> bool:  # noqa: ANN001
    """
    Whether ``<option value="...">`` carries ``selected``.

    A regex over the gap rather than an exact string, because the attributes sit on their
    own indented lines in the template and a test that pinned that indentation would fail
    on a reflow rather than on a defect.
    """
    return re.search(rf'value="{option_value}"\s+selected', html) is not None


def valid_graph() -> dict:
    return {
        "nodes": [
            {"id": "s", "type": "start", "position": {"x": 0, "y": 0}, "data": {}},
            {"id": "ok", "type": "success", "position": {"x": 1, "y": 0}, "data": {}},
        ],
        "edges": [{
            "id": "e1", "source": "s", "source_port": "default", "target": "ok",
        }],
    }


class TestLibrary:
    async def test_the_page_lists_the_users_graphs(self, client, user, make_graph) -> None:  # noqa: ANN001
        await make_graph(user, "Revenue check")

        response = client.get("/graph-designer/")

        assert response.status_code == 200
        assert "Revenue check" in response.text

    async def test_the_page_does_not_list_another_users_graphs(
        self, client, make_user, make_graph,
    ) -> None:  # noqa: ANN001
        other = await make_user("other@example.com")
        await make_graph(other, "Not yours")

        response = client.get("/graph-designer/")

        assert "Not yours" not in response.text

    async def test_creating_returns_the_success_marker_and_the_refreshed_table(
        self, client,
    ) -> None:  # noqa: ANN001
        response = client.post("/graph-designer/create", data={"name": "Fresh"})

        assert response.status_code == 200
        assert 'data-success="true"' in response.text
        assert "Fresh" in response.text

    async def test_a_duplicate_name_returns_the_reason_not_a_marker(
        self, client, user, make_graph,
    ) -> None:  # noqa: ANN001
        await make_graph(user, "Taken")

        response = client.post("/graph-designer/create", data={"name": "taken"})

        assert 'data-success="true"' not in response.text
        assert "already have a graph called" in response.text

    async def test_publishing_a_broken_graph_returns_the_reason(
        self, client, user, make_graph,
    ) -> None:  # noqa: ANN001
        """
        Publishing validates, so this button can be refused — and the reason has to reach
        the page, because an operator pressing Publish is owed an explanation rather than a
        button that appears not to work.
        """
        broken = await make_graph(user, "Broken", graph_data={"nodes": [], "edges": []})

        response = client.post(
            f"/graph-designer/{broken.uuid}/set-active", data={"is_active": "true"},
        )

        assert "at least one node" in response.text

    async def test_deleting_removes_the_row(self, client, user, make_graph) -> None:  # noqa: ANN001
        graph = await make_graph(user, "Doomed")

        response = client.post(f"/graph-designer/{graph.uuid}/delete")

        assert "Doomed" not in response.text
        assert 'data-success="true"' in response.text


class TestTheCallableByColumn:
    """
    Who may call a graph — read in the table, written in the edit dialog.

    The column is a **statement**, not a control: at most one of the two attachments is
    ever set, so a row names whichever it is. Both write endpoints still exist and are
    still the only paths that set those columns; the dialog calls them.
    """

    async def test_the_column_names_the_agent_a_graph_is_attached_to(
        self, client, db, user, make_graph,
    ) -> None:  # noqa: ANN001
        from app.models.data_agents import DataAgent

        agent = DataAgent(user_id=user.id, name="Reporting agent", is_active=True)
        db.add(agent)
        await db.commit()
        await db.refresh(agent)

        await make_graph(
            user, "Revenue check", is_active=True, data_agent_id=agent.id,
        )

        response = client.get("/graph-designer/")

        assert "Reporting agent" in response.text
        assert "One data agent" in response.text

    async def test_the_column_names_the_workspace_a_graph_is_shared_with(
        self, client, db, user, make_graph,
    ) -> None:  # noqa: ANN001
        from app.models.workspaces import Workspace

        workspace = Workspace(user_id=user.id, name="Finance team", is_active=True)
        db.add(workspace)
        await db.commit()
        await db.refresh(workspace)

        await make_graph(
            user, "Revenue check", is_active=True, workspace_id=workspace.id,
        )

        response = client.get("/graph-designer/")

        assert "Finance team" in response.text
        assert "Every agent in this workspace" in response.text

    async def test_an_unattached_graph_says_so(
        self, client, user, make_graph,
    ) -> None:  # noqa: ANN001
        await make_graph(user, "Revenue check")

        response = client.get("/graph-designer/")

        assert "Not callable by an agent yet" in response.text

    async def test_sharing_a_draft_returns_the_reason(
        self, client, db, user, make_graph,
    ) -> None:  # noqa: ANN001
        """
        The refusal has to reach the page for the reason publishing's does: a select that
        appears to have taken a value it did not is worse than one that explains itself.
        """
        from app.models.workspaces import Workspace

        workspace = Workspace(user_id=user.id, name="Finance", is_active=True)
        db.add(workspace)
        await db.commit()
        await db.refresh(workspace)

        graph = await make_graph(user, "Still drafting", is_active=False)

        response = client.post(
            f"/graph-designer/{graph.uuid}/share",
            data={"workspace_id": str(workspace.uuid)},
        )

        assert response.status_code == 200
        assert "still a draft" in response.text
        assert 'data-success="true"' not in response.text

    async def test_sharing_a_published_graph_shows_it_on_the_shelf(
        self, client, db, user, make_graph,
    ) -> None:  # noqa: ANN001
        from app.models.workspaces import Workspace

        workspace = Workspace(user_id=user.id, name="Finance team", is_active=True)
        db.add(workspace)
        await db.commit()
        await db.refresh(workspace)

        graph = await make_graph(
            user, "Revenue check", is_active=True, graph_data=valid_graph(),
        )

        response = client.post(
            f"/graph-designer/{graph.uuid}/share",
            data={"workspace_id": str(workspace.uuid)},
        )

        assert 'data-success="true"' in response.text
        # The refreshed row, not an option list: what a successful share has to show is
        # that the graph is now on that shelf.
        assert "Finance team" in response.text

    async def test_submitting_nothing_stops_the_sharing(
        self, client, db, user, make_graph,
    ) -> None:  # noqa: ANN001
        """The blank option is how the picker un-shares, so it must not be a 422."""
        from app.models.workspaces import Workspace

        workspace = Workspace(user_id=user.id, name="Finance", is_active=True)
        db.add(workspace)
        await db.commit()
        await db.refresh(workspace)

        graph = await make_graph(
            user, "Revenue check", is_active=True, graph_data=valid_graph(),
            workspace_id=workspace.id,
        )

        response = client.post(
            f"/graph-designer/{graph.uuid}/share", data={"workspace_id": ""},
        )

        assert response.status_code == 200
        assert 'data-success="true"' in response.text


class TestTheEditForm:
    """
    The dialog that edits a graph's record: its name, its description and who may call
    it.

    The property that matters most here is the one the old inline pickers got wrong —
    **the form has to come back showing what is stored.** A dialog that opens with its
    pickers blank on an attached graph is indistinguishable from one whose save was
    dropped, and it is how the operator concludes the control does not work.
    """

    @pytest.fixture
    def agent(self, db, user):  # noqa: ANN001, ANN201
        async def _make(name: str = "Reporting agent"):  # noqa: ANN202
            from app.models.data_agents import DataAgent

            row = DataAgent(user_id=user.id, name=name, is_active=True)
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row

        return _make

    @pytest.fixture
    def workspace(self, db, user):  # noqa: ANN001, ANN201
        async def _make(name: str = "Finance team"):  # noqa: ANN202
            from app.models.workspaces import Workspace

            row = Workspace(user_id=user.id, name=name, is_active=True)
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row

        return _make

    async def test_the_form_carries_both_option_lists(
        self, client, user, make_graph, agent, workspace,
    ) -> None:  # noqa: ANN001
        await agent()
        await workspace()
        graph = await make_graph(user, "Revenue check")

        response = client.get(f"/graph-designer/{graph.uuid}/edit-form")

        assert response.status_code == 200
        assert "Not attached to an agent" in response.text
        assert "Not shared with a workspace" in response.text
        assert "Reporting agent" in response.text
        assert "Finance team" in response.text
        assert 'value="Revenue check"' in response.text

    async def test_the_attached_agent_comes_back_selected(
        self, client, user, make_graph, agent,
    ) -> None:  # noqa: ANN001
        """
        The regression this whole change exists for. ``get_graph_views`` reported
        ``agent_id: None`` for every row, so the option was rendered and never marked —
        the attachment saved, and every re-render looked like it had not.
        """
        attached = await agent()
        graph = await make_graph(
            user, "Revenue check", is_active=True, data_agent_id=attached.id,
        )

        response = client.get(f"/graph-designer/{graph.uuid}/edit-form")

        assert f'value="{attached.uuid}"' in response.text
        assert is_selected(response.text, attached.uuid)

    async def test_the_shared_workspace_comes_back_selected(
        self, client, user, make_graph, workspace,
    ) -> None:  # noqa: ANN001
        shelf = await workspace()
        graph = await make_graph(
            user, "Revenue check", is_active=True, workspace_id=shelf.id,
        )

        response = client.get(f"/graph-designer/{graph.uuid}/edit-form")

        assert is_selected(response.text, shelf.uuid)

    async def test_a_draft_is_told_it_cannot_be_made_callable_yet(
        self, client, user, make_graph,
    ) -> None:  # noqa: ANN001
        graph = await make_graph(user, "Still drafting", is_active=False)

        response = client.get(f"/graph-designer/{graph.uuid}/edit-form")

        assert "Publish it before changing who" in response.text

    async def test_another_users_graph_renders_the_reason_into_the_dialog(
        self, client, make_user, make_graph,
    ) -> None:  # noqa: ANN001
        """
        A modal is already open waiting for a body, so a refusal is rendered into it
        rather than raised — the same call ``data_agent_routes.edit_form`` makes.
        """
        other = await make_user("other@example.com")
        graph = await make_graph(other, "Not yours")

        response = client.get(f"/graph-designer/{graph.uuid}/edit-form")

        assert response.status_code == 200
        assert "Could not open" in response.text
        assert "could not be found" in response.text

    async def test_saving_renames_and_attaches_in_one_submit(
        self, client, db, user, make_graph, agent,
    ) -> None:  # noqa: ANN001
        attached = await agent()
        graph = await make_graph(
            user, "Revenue check", is_active=True, graph_data=valid_graph(),
        )

        response = client.post(
            f"/graph-designer/{graph.uuid}/update",
            data={
                "name": "Monthly revenue",
                "description": "What last month earned",
                "data_agent_id": str(attached.uuid),
                "workspace_id": "",
            },
        )

        assert 'data-success="true"' in response.text
        assert "Monthly revenue" in response.text
        assert "Reporting agent" in response.text

        await db.refresh(graph)
        assert graph.name == "Monthly revenue"
        assert graph.data_agent_id == attached.id

    async def test_submitting_both_is_refused_and_nothing_is_written(
        self, client, db, user, make_graph, agent, workspace,
    ) -> None:  # noqa: ANN001
        """
        The one rule this form has that the two single-field endpoints could not break:
        it can carry both. Refused rather than resolved, so nothing is silently dropped —
        and refused *before* the rename, so a rejected form leaves no half-applied edit.
        """
        attached = await agent()
        shelf = await workspace()
        graph = await make_graph(
            user, "Revenue check", is_active=True, graph_data=valid_graph(),
        )

        response = client.post(
            f"/graph-designer/{graph.uuid}/update",
            data={
                "name": "Renamed anyway",
                "data_agent_id": str(attached.uuid),
                "workspace_id": str(shelf.uuid),
            },
        )

        assert 'data-success="true"' not in response.text
        assert "not both" in response.text

        await db.refresh(graph)
        assert graph.name == "Revenue check"
        assert graph.data_agent_id is None
        assert graph.workspace_id is None

    async def test_making_a_draft_callable_is_refused_before_the_rename(
        self, client, db, user, make_graph, agent,
    ) -> None:  # noqa: ANN001
        attached = await agent()
        graph = await make_graph(user, "Still drafting", is_active=False)

        response = client.post(
            f"/graph-designer/{graph.uuid}/update",
            data={"name": "Renamed anyway", "data_agent_id": str(attached.uuid)},
        )

        assert 'data-success="true"' not in response.text
        assert "still a draft" in response.text

        await db.refresh(graph)
        assert graph.name == "Still drafting"

    async def test_renaming_an_unpublished_but_attached_graph_is_allowed(
        self, client, db, user, make_graph, agent,
    ) -> None:  # noqa: ANN001
        """
        Unpublishing keeps the attachment, so a draft carrying one is a real state. The
        form preselects that agent, and re-submitting it unchanged must not be read as a
        request to attach a draft — otherwise a graph in that state can never be renamed.
        """
        attached = await agent()
        graph = await make_graph(
            user, "Parked", is_active=False, data_agent_id=attached.id,
        )

        response = client.post(
            f"/graph-designer/{graph.uuid}/update",
            data={"name": "Parked for now", "data_agent_id": str(attached.uuid)},
        )

        assert 'data-success="true"' in response.text

        await db.refresh(graph)
        assert graph.name == "Parked for now"
        assert graph.data_agent_id == attached.id

    async def test_clearing_both_makes_a_graph_callable_by_nobody(
        self, client, db, user, make_graph, workspace,
    ) -> None:  # noqa: ANN001
        shelf = await workspace()
        graph = await make_graph(
            user, "Revenue check", is_active=True, graph_data=valid_graph(),
            workspace_id=shelf.id,
        )

        response = client.post(
            f"/graph-designer/{graph.uuid}/update",
            data={"name": "Revenue check", "data_agent_id": "", "workspace_id": ""},
        )

        assert 'data-success="true"' in response.text
        assert "Not callable by an agent yet" in response.text

        await db.refresh(graph)
        assert graph.workspace_id is None
        assert graph.data_agent_id is None

    async def test_a_duplicate_name_is_refused_with_the_reason(
        self, client, user, make_graph,
    ) -> None:  # noqa: ANN001
        await make_graph(user, "Taken")
        graph = await make_graph(user, "Mine")

        response = client.post(
            f"/graph-designer/{graph.uuid}/update", data={"name": "taken"},
        )

        assert 'data-success="true"' not in response.text
        assert "already have a graph called" in response.text


class TestTheHelpPage:
    """
    The help page is static, so what is worth testing is that it *renders* and that the
    two pages that link to it actually do. A page whose whole body is SQL, JSON and
    ``{{VARIABLE}}`` samples is one Jinja delimiter away from a 500, and the only person
    who would find that is the operator who needed the page.

    ``/help`` also has to keep winning against ``/{graph_id:uuid}/…``; a literal path
    does, but the assertion is cheap and the failure would be a 404 on a working page.
    """

    def test_it_renders_inside_the_application_layout(self, client) -> None:  # noqa: ANN001
        response = client.get("/graph-designer/help")

        assert response.status_code == 200
        assert "Pipelines — Help" in response.text

    def test_the_examples_survive_template_rendering(self, client) -> None:  # noqa: ANN001
        """
        The whole body sits inside ``{% raw %}``, so the samples must arrive as written —
        braces, colons and placeholders intact. ``{{TABLE}}`` is the one that matters
        most: unescaped, Jinja would render it as an undefined variable and the page
        would teach the opposite of what it says.
        """
        body = client.get("/graph-designer/help").text

        assert "WHERE department_id = :dept_id" in body
        assert "SELECT region, SUM(total) FROM {{TABLE}} GROUP BY region" in body
        assert "dept_id IN :dept_ids" in body
        assert '"elapsed_human": "1h 4m 12s"' in body

    def test_it_names_every_node_type_the_palette_offers(self, client) -> None:  # noqa: ANN001
        """
        A palette entry with nothing about it on the help page is a node somebody has to
        guess at. The labels come from the model's own tuples, so a node type added later
        fails here until the page describes it.
        """
        from app.models.graph_designer import NODE_TYPES

        body = client.get("/graph-designer/help").text

        for _value, label in NODE_TYPES:
            assert label in body, f"the help page does not mention the '{label}' node"

    def test_the_library_links_to_it_in_a_new_tab(self, client) -> None:  # noqa: ANN001
        body = client.get("/graph-designer/").text

        assert 'href="/graph-designer/help"' in body
        assert 'target="_blank"' in body

    async def test_the_canvas_links_to_it_too(self, client, user, make_graph) -> None:  # noqa: ANN001
        """
        The canvas is where a port or a binding mode needs explaining, and going back to
        the library for it would mean leaving unsaved work on the page.
        """
        graph = await make_graph(user, "Drawing")

        body = client.get(f"/graph-designer/{graph.uuid}/edit").text

        assert 'href="/graph-designer/help"' in body


class TestCanvas:
    async def test_the_canvas_page_carries_the_graph_and_the_vocabulary(
        self, client, user, make_graph,
    ) -> None:  # noqa: ANN001
        """
        Both as JSON in the page, so the canvas draws on first paint instead of flashing
        empty, and so the node vocabulary is the server's rather than a second copy in
        JavaScript.
        """
        graph = await make_graph(user, "Drawn")

        response = client.get(f"/graph-designer/{graph.uuid}/edit")

        assert response.status_code == 200
        assert 'id="gdGraphData"' in response.text
        assert 'id="gdVocabulary"' in response.text
        assert "graph_canvas.js" in response.text
        assert "graph_designer.js" in response.text

    async def test_the_shared_canvas_script_is_loaded_before_the_designer(
        self, client, user, make_graph,
    ) -> None:  # noqa: ANN001
        """
        graph_designer.js reads ``window.GraphCanvas`` at module scope, so the order is a
        requirement rather than a preference — and it is the sort of thing an edit
        reorders without noticing.
        """
        graph = await make_graph(user, "Ordered")

        body = client.get(f"/graph-designer/{graph.uuid}/edit").text

        # The `<script src>` occurrences specifically. Searching for the bare filenames
        # would find the HTML comments that mention graph_designer.js further up the page
        # and compare those instead — which is a test that passes or fails on prose.
        canvas_tag = body.index('src="/static/js/graph_canvas.js"')
        designer_tag = body.index('src="/static/js/graph_designer.js"')

        assert canvas_tag < designer_tag

    async def test_the_graph_endpoint_returns_the_stored_drawing(
        self, client, user, make_graph,
    ) -> None:  # noqa: ANN001
        graph = await make_graph(user, "Stored")

        response = client.get(f"/graph-designer/{graph.uuid}/graph")

        assert response.status_code == 200
        assert response.json()["nodes"][0]["type"] == "start"

    async def test_another_users_canvas_is_a_404(
        self, client, make_user, make_graph,
    ) -> None:  # noqa: ANN001
        other = await make_user("other@example.com")
        theirs = await make_graph(other, "Theirs")

        response = client.get(f"/graph-designer/{theirs.uuid}/edit")

        assert response.status_code == 404

    async def test_node_options_answers_with_the_pickers(
        self, client, user, make_graph,
    ) -> None:  # noqa: ANN001
        """
        Scoped to a graph, because the email template list is: a graph shared into a
        workspace picks from that workspace's templates.
        """
        graph = await make_graph(user, "Nightly")

        response = client.get(f"/graph-designer/{graph.uuid}/node-options")

        assert response.status_code == 200
        payload = response.json()
        assert "datasources" in payload
        assert "tool_configs" in payload
        # The two that were being dropped by the response schema, leaving an Email node's
        # pickers empty in the browser with nothing to say why.
        assert "email_templates" in payload
        assert "smtp_configs" in payload
        assert payload["error"] is None

    async def test_another_users_node_options_are_refused_readably(
        self, client, make_user, make_graph,
    ) -> None:  # noqa: ANN001
        """
        A 200 carrying the reason, not a 404 page: a picker that cannot be filled puts one
        sentence beside itself rather than replacing the canvas somebody is working in.
        """
        other = await make_user("stranger@example.com")
        theirs = await make_graph(other, "Theirs")

        response = client.get(f"/graph-designer/{theirs.uuid}/node-options")

        assert response.status_code == 200
        payload = response.json()
        assert payload["error"]
        assert payload["email_templates"] == []


class TestSaving:
    async def test_a_valid_graph_saves_and_reports_its_size(
        self, client, user, make_graph,
    ) -> None:  # noqa: ANN001
        graph = await make_graph(user, "Saveable")

        response = client.post(
            f"/graph-designer/{graph.uuid}/save", json=valid_graph(),
        )

        assert response.status_code == 200
        assert 'data-success="true"' in response.text
        assert "2 nodes" in response.text

    async def test_a_refused_save_is_a_200_carrying_the_reason(
        self, client, user, make_graph,
    ) -> None:  # noqa: ANN001
        """
        Not a 400. The canvas is the page the user is working in and it holds unsaved work;
        a status that navigated away from it would lose that.
        """
        graph = await make_graph(user, "Refusable")

        response = client.post(
            f"/graph-designer/{graph.uuid}/save",
            json={"nodes": [], "edges": []},
        )

        assert response.status_code == 200
        assert 'data-success="false"' in response.text
        assert "at least one node" in response.text

    async def test_a_refusal_quoting_the_users_text_is_escaped(
        self, client, user, make_graph,
    ) -> None:  # noqa: ANN001
        """
        The reason names the node by its label, and a label is whatever the user typed. A
        route that built this response by interpolating into an HTML string would inject
        the markup; the template escapes it.
        """
        graph = await make_graph(user, "Escaping")

        response = client.post(
            f"/graph-designer/{graph.uuid}/save",
            json={
                "nodes": [
                    {"id": "s", "type": "start", "position": {}, "data": {}},
                    {
                        "id": "h", "type": "human", "position": {},
                        "data": {"label": "<script>alert(1)</script>", "expects": "text"},
                    },
                ],
                "edges": [],
            },
        )

        assert "<script>alert(1)</script>" not in response.text
        assert "&lt;script&gt;" in response.text

    async def test_extra_client_owned_keys_survive_the_round_trip(
        self, client, user, make_graph,
    ) -> None:  # noqa: ANN001
        """
        The canvas may carry a viewport or a dock height. The document's shape is the
        client's, so the schema bounds it rather than narrowing it.
        """
        graph = await make_graph(user, "Extras")
        payload = valid_graph()
        payload["viewport"] = {"x": 12, "y": 34}

        client.post(f"/graph-designer/{graph.uuid}/save", json=payload)
        stored = client.get(f"/graph-designer/{graph.uuid}/graph").json()

        assert stored["viewport"] == {"x": 12, "y": 34}

    async def test_a_body_that_is_not_an_object_is_refused_readably(
        self, client, user, make_graph,
    ) -> None:  # noqa: ANN001
        """
        And refused *into the banner*, like every other save refusal — a body the canvas
        could not have produced is still a state the page has to survive, because the work
        on screen is not stored anywhere else.
        """
        graph = await make_graph(user, "Bad body")

        response = client.post(
            f"/graph-designer/{graph.uuid}/save",
            content=json.dumps([1, 2, 3]),
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 200
        assert 'data-success="false"' in response.text
        assert "could not be read" in response.text


class TestRuns:
    async def test_starting_a_run_returns_a_handle_with_relative_urls(
        self, client, user, make_graph, monkeypatch,
    ) -> None:  # noqa: ANN001
        """
        Relative paths, never absolute. Every URL this application hands a browser is a
        path — see the note in DOWNLOADER_AGENTS.md about ``API_BASE + url`` — and a
        server-side absolute URL is the thing that goes stale when a host changes.
        """
        graph = await make_graph(user, "Runnable", graph_data=valid_graph())

        # The run itself is the service's business and is tested there; this asserts the
        # handle, so the background task is stubbed out to keep the route test a route test.
        from app.services.graph_designer import graph_run_service

        async def fake_start(*args, **kwargs) -> str:  # noqa: ANN002, ANN003
            return "11111111-2222-3333-4444-555555555555"

        monkeypatch.setattr(graph_run_service, "start_run", fake_start)

        response = client.post(f"/graph-designer/{graph.uuid}/runs", json={"scope": "full"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["events_url"].startswith("/graph-designer/runs/")
        assert not payload["events_url"].startswith("http")

    async def test_a_refused_run_is_a_200_carrying_the_error(
        self, client, user, make_graph,
    ) -> None:  # noqa: ANN001
        graph = await make_graph(user, "Selective", graph_data=valid_graph())

        response = client.post(
            f"/graph-designer/{graph.uuid}/runs",
            json={"scope": "selection", "node_ids": ["ghost"]},
        )

        assert response.status_code == 200
        assert "no longer in this graph" in response.json()["error"]

    async def test_a_missing_run_is_a_404(self, client) -> None:  # noqa: ANN001
        response = client.get(f"/graph-designer/runs/{uuid_pkg.uuid4()}")

        assert response.status_code == 404

    async def test_another_users_run_gives_the_same_sentence_as_a_missing_one(
        self, client, db, make_user, make_graph,
    ) -> None:  # noqa: ANN001
        """
        Asserted directly: a difference between these two is how somebody finds out which
        run ids are real.
        """
        from app.models.graph_designer import ToolGraphRun

        other = await make_user("other@example.com")
        theirs = await make_graph(other, "Theirs", graph_data=valid_graph())

        run = ToolGraphRun(tool_graph_id=theirs.id, thread_id="t", status="succeeded")
        db.add(run)
        await db.commit()
        await db.refresh(run)

        owned = client.get(f"/graph-designer/runs/{run.uuid}")
        missing = client.get(f"/graph-designer/runs/{uuid_pkg.uuid4()}")

        assert owned.status_code == missing.status_code == 404
        assert owned.json()["detail"] == missing.json()["detail"]

    async def test_the_events_stream_refuses_before_it_opens(self, client) -> None:  # noqa: ANN001
        """
        Ownership is resolved before the generator is handed over, because the status code
        is committed with the first byte — a 404 has to be decided while there is still a
        response to put it in.
        """
        response = client.get(f"/graph-designer/runs/{uuid_pkg.uuid4()}/events")

        assert response.status_code == 404
