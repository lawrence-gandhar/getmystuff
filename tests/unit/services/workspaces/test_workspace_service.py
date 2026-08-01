"""
Tests for app/services/workspaces/workspace_service.py.

Two behaviours carry the module and are asserted hardest:

* **Ownership is enforced by returning 404, not 403.** Another user's workspace
  must be indistinguishable from one that does not exist, or the response
  confirms which uuids are real.
* **Duplicate names fail twice over.** ``_assert_name_available`` gives the
  friendly message, and ``_fail_on_duplicate_name`` catches the race between that
  check and the write. The rollback in the second one is load-bearing: without it
  the session is left unusable and the HTMX re-render that follows would 500.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from litestar.exceptions import HTTPException
from sqlalchemy.exc import IntegrityError

from app.models.data_agents import DataAgent
from app.models.workspaces import Workspace
from app.services.workspaces import workspace_service as svc


@pytest.fixture
def make_workspace(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str, **kwargs):  # noqa: ANN001
        row = Workspace(user_id=owner.id, name=name, **kwargs)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
async def other_user(make_user):  # noqa: ANN001, ANN201
    return await make_user("intruder@example.com")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
class TestGetUserWorkspaceViews:
    async def test_returns_an_empty_list_for_a_new_user(self, db, user) -> None:  # noqa: ANN001
        assert await svc.get_user_workspace_views(db, user.id) == []

    async def test_shapes_a_workspace_for_the_list_page(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        await make_workspace(user, "Analytics", description="Team space")

        (view,) = await svc.get_user_workspace_views(db, user.id)

        assert view["name"] == "Analytics"
        assert view["description"] == "Team space"
        assert view["is_active"] is True
        assert view["agent_count"] == 0

    async def test_exposes_the_public_uuid_as_a_string_and_never_the_bigint_id(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        """CLAUDE.md's identifier rule: nothing built for a template may carry
        the internal ``id``."""
        workspace = await make_workspace(user, "Analytics")

        (view,) = await svc.get_user_workspace_views(db, user.id)

        assert view["uuid"] == str(workspace.uuid)
        assert isinstance(view["uuid"], str)
        assert "id" not in view

    async def test_includes_the_agent_count(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        workspace = await make_workspace(user, "Analytics")
        for i in range(2):
            db.add(DataAgent(user_id=user.id, name=f"agent{i}", workspace_id=workspace.id))
        await db.commit()

        (view,) = await svc.get_user_workspace_views(db, user.id)

        assert view["agent_count"] == 2

    async def test_excludes_other_users_workspaces(
        self, db, user, other_user, make_workspace  # noqa: ANN001
    ) -> None:
        await make_workspace(user, "Mine")
        await make_workspace(other_user, "Theirs")

        views = await svc.get_user_workspace_views(db, user.id)

        assert [v["name"] for v in views] == ["Mine"]


class TestGetWorkspace:
    async def test_resolves_by_public_uuid(self, db, user, make_workspace) -> None:  # noqa: ANN001
        workspace = await make_workspace(user, "Analytics")

        assert (await svc.get_workspace(db, user.id, workspace.uuid)).id == workspace.id

    async def test_an_unknown_uuid_is_404(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.get_workspace(db, user.id, uuid_pkg.uuid4())

        assert excinfo.value.status_code == 404
        assert excinfo.value.detail == "Workspace not found"

    async def test_another_users_workspace_is_404_not_403(
        self, db, user, other_user, make_workspace  # noqa: ANN001
    ) -> None:
        """A 403 would confirm the uuid names a real row; 404 leaks nothing."""
        theirs = await make_workspace(other_user, "Theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.get_workspace(db, user.id, theirs.uuid)

        assert excinfo.value.status_code == 404

    async def test_an_archived_workspace_still_resolves(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        """Archiving is not deletion — the row must stay reachable so it can be
        restored."""
        workspace = await make_workspace(user, "Parked", is_active=False)

        assert (await svc.get_workspace(db, user.id, workspace.uuid)).id == workspace.id


class TestGetWorkspacePublicId:
    async def test_maps_an_internal_id_to_the_public_uuid(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        workspace = await make_workspace(user, "Analytics")

        result = await svc.get_workspace_public_id(db, user.id, workspace.id)

        assert result == str(workspace.uuid)

    @pytest.mark.parametrize("empty", [None, 0])
    async def test_no_internal_id_returns_an_empty_string(
        self, db, user, empty  # noqa: ANN001
    ) -> None:
        """Returns "" rather than raising because the caller is rendering a
        dropdown, where an unassigned agent is normal."""
        assert await svc.get_workspace_public_id(db, user.id, empty) == ""

    async def test_an_unknown_id_returns_an_empty_string(self, db, user) -> None:  # noqa: ANN001
        assert await svc.get_workspace_public_id(db, user.id, 999999) == ""

    async def test_another_users_workspace_returns_an_empty_string(
        self, db, user, other_user, make_workspace  # noqa: ANN001
    ) -> None:
        theirs = await make_workspace(other_user, "Theirs")

        assert await svc.get_workspace_public_id(db, user.id, theirs.id) == ""


class TestGetWorkspaceChoices:
    async def test_returns_uuid_name_and_active_flag(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        workspace = await make_workspace(user, "Analytics")

        (choice,) = await svc.get_workspace_choices(db, user.id)

        assert choice == {
            "uuid": str(workspace.uuid),
            "name": "Analytics",
            "is_active": True,
        }

    async def test_archived_workspaces_are_still_listed(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        """An agent already assigned to an archived workspace must be editable
        without being silently moved out of it."""
        await make_workspace(user, "Parked", is_active=False)

        (choice,) = await svc.get_workspace_choices(db, user.id)

        assert choice["is_active"] is False

    async def test_ordered_by_name(self, db, user, make_workspace) -> None:  # noqa: ANN001
        for name in ["Zulu", "Alpha", "Mike"]:
            await make_workspace(user, name)

        choices = await svc.get_workspace_choices(db, user.id)

        assert [c["name"] for c in choices] == ["Alpha", "Mike", "Zulu"]

    async def test_excludes_other_users_workspaces(
        self, db, user, other_user, make_workspace  # noqa: ANN001
    ) -> None:
        await make_workspace(other_user, "Theirs")

        assert await svc.get_workspace_choices(db, user.id) == []


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
class TestCreateWorkspace:
    async def test_creates_an_active_workspace(self, db, user) -> None:  # noqa: ANN001
        workspace = await svc.create_workspace(db, user.id, "Analytics", "Team space")

        assert workspace.name == "Analytics"
        assert workspace.description == "Team space"
        assert workspace.is_active is True
        assert workspace.user_id == user.id
        assert workspace.uuid is not None

    async def test_description_is_optional(self, db, user) -> None:  # noqa: ANN001
        workspace = await svc.create_workspace(db, user.id, "Analytics")
        assert workspace.description is None

    async def test_name_is_trimmed(self, db, user) -> None:  # noqa: ANN001
        workspace = await svc.create_workspace(db, user.id, "  Analytics  ")
        assert workspace.name == "Analytics"

    @pytest.mark.parametrize("blank", ["", "   ", None])
    async def test_rejects_a_blank_name(self, db, user, blank) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.create_workspace(db, user.id, blank)

        assert excinfo.value.status_code == 400

    async def test_rejects_an_over_long_name(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException):
            await svc.create_workspace(db, user.id, "x" * 256)

    async def test_rejects_an_over_long_description(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException):
            await svc.create_workspace(db, user.id, "Analytics", "x" * 2001)

    async def test_rejects_a_duplicate_name_with_a_readable_message(
        self, db, user  # noqa: ANN001
    ) -> None:
        await svc.create_workspace(db, user.id, "Analytics")

        with pytest.raises(HTTPException) as excinfo:
            await svc.create_workspace(db, user.id, "Analytics")

        assert excinfo.value.status_code == 400
        assert excinfo.value.detail == "You already have a workspace named 'Analytics'"

    @pytest.mark.parametrize("variant", ["analytics", "ANALYTICS", "  AnAlYtIcS "])
    async def test_duplicate_detection_ignores_case(
        self, db, user, variant: str  # noqa: ANN001
    ) -> None:
        await svc.create_workspace(db, user.id, "Analytics")

        with pytest.raises(HTTPException, match="already have a workspace named"):
            await svc.create_workspace(db, user.id, variant)

    async def test_two_users_may_share_a_workspace_name(
        self, db, user, other_user  # noqa: ANN001
    ) -> None:
        await svc.create_workspace(db, user.id, "Analytics")

        theirs = await svc.create_workspace(db, other_user.id, "Analytics")

        assert theirs.user_id == other_user.id

    async def test_an_integrityerror_race_is_rolled_back_and_reported(
        self, db, user, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
    ) -> None:
        """
        Simulates the window between the name check and the insert, where the
        unique index is what catches the duplicate. The rollback matters as much
        as the message: the HTMX route re-renders the table in this same session
        afterwards, and a session left in a failed flush would 500.
        """
        # Read the id up front: the rollback expires every instance in the
        # session, so touching ``user.id`` afterwards would trigger a synchronous
        # refresh and raise MissingGreenlet inside the test itself.
        user_id = user.id

        monkeypatch.setattr(svc, "workspace_name_exists", lambda *a, **k: _false())

        async def boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise IntegrityError("insert", {}, Exception("duplicate key"))

        monkeypatch.setattr(svc.workspace_crud, "create", boom)

        with pytest.raises(HTTPException) as excinfo:
            await svc.create_workspace(db, user_id, "Analytics")

        assert excinfo.value.detail == "You already have a workspace named 'Analytics'"
        # The session is usable again, which is the point of the rollback — this
        # query is what the HTMX route does next, and it 500s without it.
        assert await svc.get_user_workspace_views(db, user_id) == []


async def _false() -> bool:
    return False


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
class TestUpdateWorkspace:
    async def test_renames(self, db, user, make_workspace) -> None:  # noqa: ANN001
        workspace = await make_workspace(user, "Old")

        updated = await svc.update_workspace(db, user.id, workspace.uuid, "New", "desc")

        assert updated.name == "New"
        assert updated.description == "desc"

    async def test_saving_without_changing_the_name_is_allowed(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        """``exclude_id`` is what makes this work — without it the workspace
        would report its own name as taken."""
        workspace = await make_workspace(user, "Analytics")

        updated = await svc.update_workspace(db, user.id, workspace.uuid, "Analytics", "new desc")

        assert updated.name == "Analytics"
        assert updated.description == "new desc"

    async def test_rejects_a_name_another_workspace_already_has(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        await make_workspace(user, "Taken")
        workspace = await make_workspace(user, "Mine")

        with pytest.raises(HTTPException, match="already have a workspace named"):
            await svc.update_workspace(db, user.id, workspace.uuid, "Taken")

    async def test_another_users_workspace_is_404(
        self, db, user, other_user, make_workspace  # noqa: ANN001
    ) -> None:
        theirs = await make_workspace(other_user, "Theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.update_workspace(db, user.id, theirs.uuid, "Hijacked")

        assert excinfo.value.status_code == 404

    async def test_an_unknown_uuid_is_404(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.update_workspace(db, user.id, uuid_pkg.uuid4(), "New")

        assert excinfo.value.status_code == 404

    @pytest.mark.parametrize("blank", ["", "   "])
    async def test_rejects_a_blank_name(
        self, db, user, make_workspace, blank: str  # noqa: ANN001
    ) -> None:
        workspace = await make_workspace(user, "Analytics")

        with pytest.raises(HTTPException):
            await svc.update_workspace(db, user.id, workspace.uuid, blank)

    async def test_description_can_be_cleared(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        workspace = await make_workspace(user, "Analytics", description="old")

        updated = await svc.update_workspace(db, user.id, workspace.uuid, "Analytics", "")

        assert updated.description is None


# ---------------------------------------------------------------------------
# Archive / restore
# ---------------------------------------------------------------------------
class TestSetWorkspaceActive:
    async def test_archives(self, db, user, make_workspace) -> None:  # noqa: ANN001
        workspace = await make_workspace(user, "Analytics")

        updated = await svc.set_workspace_active(db, user.id, workspace.uuid, False)

        assert updated.is_active is False

    async def test_restores(self, db, user, make_workspace) -> None:  # noqa: ANN001
        workspace = await make_workspace(user, "Analytics", is_active=False)

        updated = await svc.set_workspace_active(db, user.id, workspace.uuid, True)

        assert updated.is_active is True

    async def test_archiving_leaves_assigned_agents_untouched(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        """Archiving only stops *new* assignments; existing agents keep working,
        which is what makes it a park rather than a teardown."""
        workspace = await make_workspace(user, "Analytics")
        agent = DataAgent(user_id=user.id, name="reporter", workspace_id=workspace.id)
        db.add(agent)
        await db.commit()
        await db.refresh(agent)

        await svc.set_workspace_active(db, user.id, workspace.uuid, False)
        await db.refresh(agent)

        assert agent.workspace_id == workspace.id

    async def test_another_users_workspace_is_404(
        self, db, user, other_user, make_workspace  # noqa: ANN001
    ) -> None:
        theirs = await make_workspace(other_user, "Theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.set_workspace_active(db, user.id, theirs.uuid, False)

        assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
class TestDeleteWorkspace:
    async def test_deletes(self, db, user, make_workspace) -> None:  # noqa: ANN001
        workspace = await make_workspace(user, "Analytics")

        await svc.delete_workspace(db, user.id, workspace.uuid)

        assert await svc.get_user_workspace_views(db, user.id) == []

    async def test_another_users_workspace_is_404_and_survives(
        self, db, user, other_user, make_workspace  # noqa: ANN001
    ) -> None:
        theirs = await make_workspace(other_user, "Theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.delete_workspace(db, user.id, theirs.uuid)

        assert excinfo.value.status_code == 404
        assert len(await svc.get_user_workspace_views(db, other_user.id)) == 1

    async def test_an_unknown_uuid_is_404(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.delete_workspace(db, user.id, uuid_pkg.uuid4())

        assert excinfo.value.status_code == 404

    async def test_the_name_is_free_again_afterwards(
        self, db, user  # noqa: ANN001
    ) -> None:
        workspace = await svc.create_workspace(db, user.id, "Analytics")
        await svc.delete_workspace(db, user.id, workspace.uuid)

        recreated = await svc.create_workspace(db, user.id, "Analytics")

        # Compared by uuid, not id: SQLite reuses the highest rowid after a
        # delete, so the new row can legitimately land on the same integer id.
        assert recreated.uuid != workspace.uuid
        assert recreated.name == "Analytics"
