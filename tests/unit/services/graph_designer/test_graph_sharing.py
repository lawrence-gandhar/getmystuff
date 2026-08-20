"""
Tests for the second way a published graph becomes callable: **shared with a workspace**,
so every data agent assigned to that workspace picks it up, rather than attached to one.

The properties this file holds still, in the order they matter:

* **A shelf is inherited, not assigned.** The point of the feature is that adding a fourth
  agent to a workspace gives it the shared graphs with nobody remembering to attach
  anything. So the test that carries the file is an agent created *after* the sharing.
* **An agent in no workspace inherits nothing.** The obvious way to write the query —
  matching ``workspace_id`` on both sides — hands every unshared graph to every unassigned
  agent, because ``NULL = NULL`` was never the question being asked.
* **The two attachments are mutually exclusive**, and setting either clears the other.
  Holding both would give one agent the same graph twice, once as its own and once through
  its workspace, and a model cannot choose between two tools of one name.
* **A name collision on a shelf is refused where it can be explained.** Two graphs whose
  names reduce to the same identifier are two tools a model cannot tell apart, and the
  place to say so is the control that caused it.

One interaction is worth knowing before reading the last class. Graph *names* are already
unique per user, case-insensitively (``uq_tool_graphs_user_name_lower``), so "Monthly
revenue" and "monthly revenue" can never both exist and the tool-name check would be
unreachable through that door. It is reachable through punctuation: ``_graph_tool_name``
collapses every non-alphanumeric character, so "Monthly revenue" and "monthly-revenue" are
two permitted names that become one identifier. That is why these tests differ by a hyphen
or a full stop rather than by case — the case pairs are refused a layer earlier, for a
different reason, with a different message.

Against the real database and the real services, like its neighbours: what is being tested
is a query with a correlated subquery in it and a rule spread across two write paths, and a
stub would only prove this file calls what it calls.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

pytest.importorskip("langgraph", reason="LangGraph is installed in the container only")

from litestar.exceptions import HTTPException  # noqa: E402

from app.models.data_agents import DataAgent  # noqa: E402
from app.models.datasource import DataSource  # noqa: E402
from app.models.graph_designer import ToolGraph  # noqa: E402
from app.models.workspaces import Workspace  # noqa: E402
from app.services.deep_agents.prompt_sync_service import collect_agent_tools  # noqa: E402
from app.services.graph_designer import graph_service  # noqa: E402


@pytest.fixture
async def workspace(db, user):  # noqa: ANN001, ANN201
    row = Workspace(
        user_id=user.id,
        name=f"team-{uuid_pkg.uuid4().hex[:6]}",
        is_active=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
async def make_agent(db, user):  # noqa: ANN001, ANN201
    """A data agent, optionally assigned to a workspace."""
    async def _make(workspace_row=None) -> DataAgent:  # noqa: ANN001
        row = DataAgent(
            user_id=user.id,
            name=f"agent-{uuid_pkg.uuid4().hex[:6]}",
            is_active=True,
            workspace_id=workspace_row.id if workspace_row is not None else None,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
async def datasource(db, user, tmp_path):  # noqa: ANN001, ANN201
    import sqlite3

    path = tmp_path / "shared.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE departments (id INTEGER PRIMARY KEY, name TEXT);"
        "INSERT INTO departments VALUES (1, 'Eng'), (2, 'Sales');"
    )
    connection.commit()
    connection.close()

    row = DataSource(
        user_id=user.id,
        datasource_name=f"shared-{uuid_pkg.uuid4().hex[:6]}",
        db_type="sqlite",
        database_name=str(path),
        is_active=True,
        password_encrypted="",
        configuration_data={},
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def graph_data(datasource) -> dict:  # noqa: ANN001
    return {
        "nodes": [
            {"id": "s", "type": "start", "position": {}, "data": {"label": "Start"}},
            {
                "id": "q", "type": "sql", "position": {},
                "data": {
                    "label": "departments",
                    "datasource_id": str(datasource.uuid),
                    "table_names": ["departments"],
                    "sql_query": "SELECT id, name FROM departments",
                },
            },
            {"id": "ok", "type": "success", "position": {}, "data": {}},
        ],
        "edges": [
            {"id": "e1", "source": "s", "source_port": "default", "target": "q"},
            {"id": "e2", "source": "q", "source_port": "default", "target": "ok"},
        ],
    }


@pytest.fixture
def published(db, user, datasource):  # noqa: ANN001, ANN201
    """A saved, published graph — the state in which it may be attached or shared."""
    async def _publish(name: str = "Dept lookup") -> ToolGraph:
        graph = await graph_service.create_graph(
            db, user.id, name, "Lists departments.",
        )
        await graph_service.save_graph(db, user.id, graph.uuid, graph_data(datasource))
        await graph_service.set_graph_active(db, user.id, graph.uuid, True)
        return graph

    return _publish


class TestAShelfIsInherited:
    async def test_an_agent_in_the_workspace_can_call_a_shared_graph(
        self, db, user, workspace, make_agent, published,
    ) -> None:  # noqa: ANN001
        agent = await make_agent(workspace)
        graph = await published()

        await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)

        entries = await collect_agent_tools(db, agent.id)

        assert [entry.get("kind") for entry in entries] == ["graph"]
        assert entries[0]["graph_uuid"] == str(graph.uuid)

    async def test_an_agent_added_afterwards_inherits_it(
        self, db, user, workspace, make_agent, published,
    ) -> None:  # noqa: ANN001
        """
        The reason the feature exists. Sharing happens once; the fourth agent somebody
        adds to the team next month gets the graph without anybody attaching anything,
        which is exactly what per-agent attachment cannot do.
        """
        graph = await published()
        await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)

        latecomer = await make_agent(workspace)

        entries = await collect_agent_tools(db, latecomer.id)

        assert [entry["graph_uuid"] for entry in entries] == [str(graph.uuid)]

    async def test_a_shelf_may_hold_several_graphs(
        self, db, user, workspace, make_agent, published,
    ) -> None:  # noqa: ANN001
        """
        Unlike ``data_agent_id``, the workspace column is not unique — a team's shelf is a
        one-to-many by nature. Each graph becomes its own tool with its own name.
        """
        agent = await make_agent(workspace)

        for name in ("Revenue check", "Headcount check", "Churn check"):
            graph = await published(name)
            await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)

        entries = await collect_agent_tools(db, agent.id)

        assert len(entries) == 3
        assert sorted(entry["tool_name"] for entry in entries) == [
            "churn_check", "headcount_check", "revenue_check",
        ]

    async def test_an_agent_in_another_workspace_gets_nothing(
        self, db, user, workspace, make_agent, published,
    ) -> None:  # noqa: ANN001
        outsider_workspace = Workspace(
            user_id=user.id, name=f"other-{uuid_pkg.uuid4().hex[:6]}", is_active=True,
        )
        db.add(outsider_workspace)
        await db.commit()
        await db.refresh(outsider_workspace)

        outsider = await make_agent(outsider_workspace)
        graph = await published()
        await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)

        assert await collect_agent_tools(db, outsider.id) == []

    async def test_an_agent_in_no_workspace_inherits_nothing(
        self, db, user, workspace, make_agent, published,
    ) -> None:  # noqa: ANN001
        """
        The bug the correlated subquery exists to prevent. Matching ``workspace_id`` on
        both sides reads ``NULL = NULL``, which is never true in SQL but *is* the shape
        somebody reaches for — and a join written the obvious way would hand every
        unshared graph to every unassigned agent.
        """
        unassigned = await make_agent(None)
        graph = await published()
        await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)

        assert await collect_agent_tools(db, unassigned.id) == []

    async def test_an_unshared_unattached_graph_reaches_nobody(
        self, db, user, workspace, make_agent, published,
    ) -> None:  # noqa: ANN001
        """The other half of the same property, from the graph's side."""
        assigned = await make_agent(workspace)
        unassigned = await make_agent(None)
        await published()

        assert await collect_agent_tools(db, assigned.id) == []
        assert await collect_agent_tools(db, unassigned.id) == []


class TestBothSwitchesStillApply:
    async def test_a_draft_cannot_be_shared(
        self, db, user, workspace, datasource,
    ) -> None:  # noqa: ANN001
        """
        Refused rather than accepted-and-ignored, exactly as attaching a draft is:
        ``fetch_agent_graphs`` filters on ``is_active``, so it would be a control that
        appears to work and does nothing.
        """
        graph = await graph_service.create_graph(db, user.id, "Draft", "Not ready.")
        await graph_service.save_graph(db, user.id, graph.uuid, graph_data(datasource))

        with pytest.raises(HTTPException) as caught:
            await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)

        assert "still a draft" in caught.value.detail

    async def test_unpublishing_withdraws_it_without_un_sharing(
        self, db, user, workspace, make_agent, published,
    ) -> None:  # noqa: ANN001
        agent = await make_agent(workspace)
        graph = await published()
        await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)

        await graph_service.set_graph_active(db, user.id, graph.uuid, False)

        assert await collect_agent_tools(db, agent.id) == []

        refreshed = await graph_service.get_graph(db, user.id, graph.uuid)
        assert refreshed.workspace_id == workspace.id, "still on the shelf"

    async def test_un_sharing_takes_it_back(
        self, db, user, workspace, make_agent, published,
    ) -> None:  # noqa: ANN001
        agent = await make_agent(workspace)
        graph = await published()
        await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)

        await graph_service.share_graph(db, user.id, graph.uuid, None)

        assert await collect_agent_tools(db, agent.id) == []

    async def test_another_users_workspace_is_refused(
        self, db, user, make_user, published,
    ) -> None:  # noqa: ANN001
        """Ownership of the workspace is the workspace service's to check, and it is."""
        intruder = await make_user("intruder@example.com")
        theirs = Workspace(
            user_id=intruder.id, name=f"theirs-{uuid_pkg.uuid4().hex[:6]}",
            is_active=True,
        )
        db.add(theirs)
        await db.commit()
        await db.refresh(theirs)

        graph = await published()

        with pytest.raises(HTTPException) as caught:
            await graph_service.share_graph(db, user.id, graph.uuid, theirs.uuid)

        assert caught.value.status_code == 404


class TestTheTwoAttachmentsAreExclusive:
    async def test_sharing_detaches(
        self, db, user, workspace, make_agent, published,
    ) -> None:  # noqa: ANN001
        agent = await make_agent(None)
        graph = await published()
        await graph_service.attach_graph(db, user.id, graph.uuid, agent.uuid)

        await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)

        refreshed = await graph_service.get_graph(db, user.id, graph.uuid)
        assert refreshed.workspace_id == workspace.id
        assert refreshed.data_agent_id is None

    async def test_attaching_un_shares(
        self, db, user, workspace, make_agent, published,
    ) -> None:  # noqa: ANN001
        agent = await make_agent(None)
        graph = await published()
        await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)

        await graph_service.attach_graph(db, user.id, graph.uuid, agent.uuid)

        refreshed = await graph_service.get_graph(db, user.id, graph.uuid)
        assert refreshed.data_agent_id == agent.id
        assert refreshed.workspace_id is None

    async def test_an_agent_never_receives_one_graph_twice(
        self, db, user, workspace, make_agent, published,
    ) -> None:  # noqa: ANN001
        """
        The reason the two are exclusive at all. Attaching a graph to an agent *inside*
        the workspace it is shared with would otherwise collect it once as the agent's own
        and once off the shelf — two tools of one name, which a model cannot choose
        between. Attaching clears the sharing, so the count is one.
        """
        agent = await make_agent(workspace)
        graph = await published()
        await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)

        await graph_service.attach_graph(db, user.id, graph.uuid, agent.uuid)

        entries = await collect_agent_tools(db, agent.id)

        assert len(entries) == 1

    async def test_an_attached_graph_and_a_shared_one_are_both_offered(
        self, db, user, workspace, make_agent, published,
    ) -> None:  # noqa: ANN001
        """
        Exclusive per *graph*, not per agent. An agent may hold one of its own and any
        number off the shelf, and the attached one comes first so an unchanged set
        produces a byte-identical routing prompt.
        """
        agent = await make_agent(workspace)

        own = await published("Own check")
        await graph_service.attach_graph(db, user.id, own.uuid, agent.uuid)

        shared = await published("Shelf check")
        await graph_service.share_graph(db, user.id, shared.uuid, workspace.uuid)

        entries = await collect_agent_tools(db, agent.id)

        assert [entry["graph_uuid"] for entry in entries] == [
            str(own.uuid), str(shared.uuid),
        ]


class TestOneNamePerShelf:
    async def test_two_graphs_reducing_to_one_tool_name_are_refused(
        self, db, user, workspace, published,
    ) -> None:  # noqa: ANN001
        """
        ``_graph_tool_name`` lowercases and collapses every non-alphanumeric character, so
        two names that look different to a person become one identifier. A model handed
        two tools called ``monthly_revenue`` has nothing to choose on.
        """
        first = await published("Monthly revenue")
        await graph_service.share_graph(db, user.id, first.uuid, workspace.uuid)

        second = await published("monthly-revenue")

        with pytest.raises(HTTPException) as caught:
            await graph_service.share_graph(db, user.id, second.uuid, workspace.uuid)

        assert "monthly_revenue" in caught.value.detail
        assert "Monthly revenue" in caught.value.detail, "names the one in the way"

    async def test_a_draft_on_the_shelf_still_counts(
        self, db, user, workspace, datasource, published,
    ) -> None:  # noqa: ANN001
        """
        The check reads drafts too. A colliding draft becomes a live collision the moment
        somebody presses Publish, and refusing it *then* — from a different control, on a
        graph they were not editing — is how a refusal ends up looking arbitrary.
        """
        parked = await published("Monthly revenue")
        await graph_service.share_graph(db, user.id, parked.uuid, workspace.uuid)
        await graph_service.set_graph_active(db, user.id, parked.uuid, False)

        clashing = await published("monthly.revenue")

        with pytest.raises(HTTPException, match="monthly_revenue"):
            await graph_service.share_graph(db, user.id, clashing.uuid, workspace.uuid)

    async def test_re_sharing_the_same_graph_is_not_a_collision_with_itself(
        self, db, user, workspace, published,
    ) -> None:  # noqa: ANN001
        graph = await published("Monthly revenue")

        await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)
        await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)

        refreshed = await graph_service.get_graph(db, user.id, graph.uuid)
        assert refreshed.workspace_id == workspace.id

    async def test_attaching_into_a_workspace_checks_its_shelf(
        self, db, user, workspace, make_agent, published,
    ) -> None:  # noqa: ANN001
        """
        The collision is checked from the destination, because that is where it lives: an
        agent assigned to this workspace will hold both the shelf's graph and its own.
        """
        agent = await make_agent(workspace)

        on_the_shelf = await published("Monthly revenue")
        await graph_service.share_graph(db, user.id, on_the_shelf.uuid, workspace.uuid)

        clashing = await published("monthly/revenue")

        with pytest.raises(HTTPException, match="monthly_revenue"):
            await graph_service.attach_graph(db, user.id, clashing.uuid, agent.uuid)

    async def test_the_same_name_in_two_workspaces_is_fine(
        self, db, user, workspace, published,
    ) -> None:  # noqa: ANN001
        """Shelves are separate, so no agent ever sees both."""
        other = Workspace(
            user_id=user.id, name=f"other-{uuid_pkg.uuid4().hex[:6]}", is_active=True,
        )
        db.add(other)
        await db.commit()
        await db.refresh(other)

        first = await published("Monthly revenue")
        second = await published("monthly-revenue")

        await graph_service.share_graph(db, user.id, first.uuid, workspace.uuid)
        await graph_service.share_graph(db, user.id, second.uuid, other.uuid)

        assert (
            await graph_service.get_graph(db, user.id, second.uuid)
        ).workspace_id == other.id


class TestTheLibraryViewReportsTheAttachment:
    """
    ``get_graph_views`` has to report *which* agent or workspace, not only its name.

    It reported ``agent_id: None`` for every row and omitted ``workspace_id`` entirely,
    so the library's pickers rendered every option and marked none of them. The
    attachment was saved correctly and every re-render of the table said it was not —
    which is indistinguishable, from the outside, from a control that does nothing.
    """

    async def test_the_view_carries_the_attached_agents_public_uuid(
        self, db, user, make_agent, published,
    ) -> None:  # noqa: ANN001
        agent = await make_agent()
        graph = await published("Dept lookup")
        await graph_service.attach_graph(db, user.id, graph.uuid, agent.uuid)

        view = next(
            row for row in await graph_service.get_graph_views(db, user.id)
            if row["uuid"] == str(graph.uuid)
        )

        assert view["agent_id"] == str(agent.uuid)
        assert view["agent_name"] == agent.name
        assert view["workspace_id"] is None

    async def test_the_view_carries_the_shared_workspaces_public_uuid(
        self, db, user, workspace, published,
    ) -> None:  # noqa: ANN001
        graph = await published("Dept lookup")
        await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)

        view = next(
            row for row in await graph_service.get_graph_views(db, user.id)
            if row["uuid"] == str(graph.uuid)
        )

        assert view["workspace_id"] == str(workspace.uuid)
        assert view["workspace_name"] == workspace.name
        assert view["agent_id"] is None

    async def test_an_unattached_graph_reports_neither(
        self, db, user, published,
    ) -> None:  # noqa: ANN001
        graph = await published("Dept lookup")

        view = next(
            row for row in await graph_service.get_graph_views(db, user.id)
            if row["uuid"] == str(graph.uuid)
        )

        assert view["agent_id"] is None
        assert view["workspace_id"] is None

    async def test_the_view_never_carries_an_internal_id(
        self, db, user, make_agent, published,
    ) -> None:  # noqa: ANN001
        """
        The uuids come from joins on the internal FKs, which is exactly the place a
        bigint leaks into a form field if the wrong column is selected.
        """
        agent = await make_agent()
        graph = await published("Dept lookup")
        await graph_service.attach_graph(db, user.id, graph.uuid, agent.uuid)

        view = next(
            row for row in await graph_service.get_graph_views(db, user.id)
            if row["uuid"] == str(graph.uuid)
        )

        assert view["agent_id"] != str(agent.id)
        uuid_pkg.UUID(view["agent_id"])  # raises if it is not a uuid


class TestUpdateGraphComposesTheWritePaths:
    """
    ``update_graph`` is the library's edit form: one submit carrying the name, the
    description and the attachment.

    It is composed from ``rename_graph`` / ``attach_graph`` / ``share_graph`` rather than
    writing those columns itself, so what this class is really asserting is that every
    rule those three enforce still holds on this path — and that the one rule only this
    path can break, carrying *both* attachments, is refused rather than resolved.
    """

    async def test_it_renames_and_attaches_in_one_call(
        self, db, user, make_agent, published,
    ) -> None:  # noqa: ANN001
        agent = await make_agent()
        graph = await published("Dept lookup")

        updated = await graph_service.update_graph(
            db, user.id, graph.uuid, "Department lookup", "Lists them.",
            agent_id=agent.uuid,
        )

        assert updated.name == "Department lookup"
        assert updated.description == "Lists them."
        assert updated.data_agent_id == agent.id

    async def test_both_attachments_at_once_are_refused(
        self, db, user, workspace, make_agent, published,
    ) -> None:  # noqa: ANN001
        agent = await make_agent()
        graph = await published("Dept lookup")

        with pytest.raises(HTTPException) as refusal:
            await graph_service.update_graph(
                db, user.id, graph.uuid, "Renamed anyway",
                agent_id=agent.uuid, workspace_id=workspace.uuid,
            )

        assert "not both" in str(refusal.value.detail)
        assert (await graph_service.get_graph(db, user.id, graph.uuid)).name == "Dept lookup"

    async def test_a_draft_is_refused_before_the_rename_is_written(
        self, db, user, make_agent, datasource,
    ) -> None:  # noqa: ANN001
        """
        The refusal has to come first. Committing the rename and then refusing the
        attachment leaves a half-applied form, which is harder to reason about than a
        rejected one.
        """
        agent = await make_agent()
        graph = await graph_service.create_graph(db, user.id, "Still drafting", None)
        await graph_service.save_graph(db, user.id, graph.uuid, graph_data(datasource))

        with pytest.raises(HTTPException) as refusal:
            await graph_service.update_graph(
                db, user.id, graph.uuid, "Renamed anyway", agent_id=agent.uuid,
            )

        assert "still a draft" in str(refusal.value.detail)
        assert (await graph_service.get_graph(db, user.id, graph.uuid)).name == "Still drafting"

    async def test_an_unchanged_attachment_on_a_draft_does_not_trip_the_refusal(
        self, db, user, make_agent, published,
    ) -> None:  # noqa: ANN001
        """
        Unpublishing keeps the attachment, so a draft holding one is a real state. The
        form preselects that agent, and re-submitting it must not be read as a request to
        attach a draft — otherwise a parked graph could never be renamed.
        """
        agent = await make_agent()
        graph = await published("Dept lookup")
        await graph_service.attach_graph(db, user.id, graph.uuid, agent.uuid)
        await graph_service.set_graph_active(db, user.id, graph.uuid, False)

        updated = await graph_service.update_graph(
            db, user.id, graph.uuid, "Parked for now", agent_id=agent.uuid,
        )

        assert updated.name == "Parked for now"
        assert updated.data_agent_id == agent.id

    async def test_submitting_neither_clears_both_columns(
        self, db, user, workspace, published,
    ) -> None:  # noqa: ANN001
        """
        Each write path only clears its own column, so "callable by nobody" needs both
        called — the case a single ``attach_graph(None)`` would silently half-do.
        """
        graph = await published("Dept lookup")
        await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)

        updated = await graph_service.update_graph(
            db, user.id, graph.uuid, "Dept lookup",
        )

        assert updated.workspace_id is None
        assert updated.data_agent_id is None

    async def test_switching_from_an_agent_to_a_workspace_clears_the_agent(
        self, db, user, workspace, make_agent, published,
    ) -> None:  # noqa: ANN001
        agent = await make_agent()
        graph = await published("Dept lookup")
        await graph_service.attach_graph(db, user.id, graph.uuid, agent.uuid)

        updated = await graph_service.update_graph(
            db, user.id, graph.uuid, "Dept lookup", workspace_id=workspace.uuid,
        )

        assert updated.workspace_id == workspace.id
        assert updated.data_agent_id is None

    async def test_the_rename_happens_before_the_collision_check(
        self, db, user, workspace, published,
    ) -> None:  # noqa: ANN001
        """
        ``attach_graph`` checks the graph's name against the destination's shelf, so it
        has to see the name the operator just typed. Renaming *into* a collision and
        sharing in the same submit must be refused — if the order were reversed the check
        would pass on the old name and the collision would land.
        """
        await graph_service.share_graph(
            db, user.id, (await published("Monthly revenue")).uuid, workspace.uuid,
        )
        graph = await published("Something else")

        with pytest.raises(HTTPException) as refusal:
            await graph_service.update_graph(
                db, user.id, graph.uuid, "monthly-revenue",
                workspace_id=workspace.uuid,
            )

        assert "same tool name" in str(refusal.value.detail)

    async def test_a_duplicate_name_is_refused_and_the_attachment_is_untouched(
        self, db, user, make_agent, published,
    ) -> None:  # noqa: ANN001
        agent = await make_agent()
        await published("Taken")
        graph = await published("Mine")

        with pytest.raises(HTTPException) as refusal:
            await graph_service.update_graph(
                db, user.id, graph.uuid, "taken", agent_id=agent.uuid,
            )

        assert "already have a graph called" in str(refusal.value.detail)
        assert (await graph_service.get_graph(db, user.id, graph.uuid)).data_agent_id is None

    async def test_another_users_graph_is_refused(
        self, db, user, make_user, published,
    ) -> None:  # noqa: ANN001
        other = await make_user("someone-else@example.com")
        graph = await published("Dept lookup")

        with pytest.raises(HTTPException) as refusal:
            await graph_service.update_graph(
                db, other.id, graph.uuid, "Mine now",
            )

        assert refusal.value.status_code == 404
