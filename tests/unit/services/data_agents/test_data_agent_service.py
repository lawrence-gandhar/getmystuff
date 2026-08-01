"""
Tests for app/services/data_agents/data_agent_service.py.

An agent is a user-owned entity with two optional foreign keys — a workspace and
an AI API key — and both are resolved from a *submitted uuid* rather than trusted
from the form. That resolution is the security-relevant part of this module and
gets the most attention here: pasting another user's workspace or key uuid must
fail, not quietly attach.

The other behaviour worth pinning is the archived-workspace rule, which is
asymmetric on purpose: moving an agent *into* an archived workspace is refused,
but an agent already sitting in one stays editable. Without that exception,
archiving a workspace would make every agent inside it unsavable.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from litestar.exceptions import HTTPException

from app.models.ai_settings import AIApiKey
from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.tool_configs import ToolConfig
from app.models.workspaces import Workspace
from app.services.data_agents import data_agent_service as svc
from app.utils.crypto import encrypt_password


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def make_workspace(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str, **kwargs):  # noqa: ANN001
        row = Workspace(user_id=owner.id, name=name, **kwargs)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


SECRET_API_KEY = "sk-super-secret-value"


@pytest.fixture
def make_ai_key(db):  # noqa: ANN001, ANN201
    """
    Stores a genuinely Fernet-encrypted key. ``get_user_api_keys`` decrypts on
    read to build its masked display value, so a placeholder string here fails
    with InvalidToken — and the round trip is what proves the secret never
    reaches the dropdown.
    """

    async def _make(owner, label: str, **kwargs):  # noqa: ANN001
        row = AIApiKey(
            user_id=owner.id,
            provider=kwargs.pop("provider", "anthropic"),
            label=label,
            api_key_encrypted=encrypt_password(SECRET_API_KEY),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_agent(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str, **kwargs):  # noqa: ANN001
        row = DataAgent(user_id=owner.id, name=name, **kwargs)
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
class TestGetAgentViews:
    async def test_a_new_user_has_none(self, db, user) -> None:  # noqa: ANN001
        assert await svc.get_agent_views(db, user.id) == []

    async def test_shapes_an_unassigned_agent(self, db, user, make_agent) -> None:  # noqa: ANN001
        await make_agent(user, "reporter", description="reads orders")

        (view,) = await svc.get_agent_views(db, user.id)

        assert view["name"] == "reporter"
        assert view["description"] == "reads orders"
        assert view["tool_count"] == 0
        assert view["workspace_name"] is None
        assert view["llm_key_label"] is None

    async def test_an_unassigned_workspace_uuid_is_an_empty_string(
        self, db, user, make_agent  # noqa: ANN001
    ) -> None:
        """The template puts this straight into a ``value`` attribute, so it must
        be "" rather than the string "None"."""
        await make_agent(user, "reporter")

        (view,) = await svc.get_agent_views(db, user.id)

        assert view["workspace_uuid"] == ""

    async def test_exposes_public_uuids_only(
        self, db, user, make_workspace, make_agent  # noqa: ANN001
    ) -> None:
        workspace = await make_workspace(user, "Analytics")
        agent = await make_agent(user, "reporter", workspace_id=workspace.id)

        (view,) = await svc.get_agent_views(db, user.id)

        assert view["uuid"] == str(agent.uuid)
        assert view["workspace_uuid"] == str(workspace.uuid)
        assert "id" not in view

    async def test_includes_the_tool_count(
        self, db, user, make_agent  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = DataSource(
            user_id=user.id,
            datasource_name="warehouse",
            db_type="postgres",
            password_encrypted="enc",
        )
        db.add(datasource)
        await db.commit()
        await db.refresh(datasource)
        for i in range(2):
            db.add(
                ToolConfig(
                    data_agent_id=agent.id,
                    datasource_id=datasource.id,
                    tool_name=f"tool{i}",
                    table_name="orders",
                )
            )
        await db.commit()

        (view,) = await svc.get_agent_views(db, user.id)

        assert view["tool_count"] == 2

    async def test_the_workspace_filter_narrows_the_list(
        self, db, user, make_workspace, make_agent  # noqa: ANN001
    ) -> None:
        workspace = await make_workspace(user, "Analytics")
        await make_agent(user, "inside", workspace_id=workspace.id)
        await make_agent(user, "outside")

        views = await svc.get_agent_views(db, user.id, workspace_id=workspace.uuid)

        assert [v["name"] for v in views] == ["inside"]

    async def test_filtering_by_another_users_workspace_is_404(
        self, db, user, other_user, make_workspace  # noqa: ANN001
    ) -> None:
        """404 rather than an empty list — returning nothing would hide that the
        uuid belongs to someone else."""
        theirs = await make_workspace(other_user, "Theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.get_agent_views(db, user.id, workspace_id=theirs.uuid)

        assert excinfo.value.status_code == 404

    async def test_excludes_other_users_agents(
        self, db, user, other_user, make_agent  # noqa: ANN001
    ) -> None:
        await make_agent(user, "mine")
        await make_agent(other_user, "theirs")

        assert [v["name"] for v in await svc.get_agent_views(db, user.id)] == ["mine"]


class TestGetDataAgent:
    async def test_resolves_by_public_uuid(self, db, user, make_agent) -> None:  # noqa: ANN001
        agent = await make_agent(user, "reporter")

        assert (await svc.get_data_agent(db, user.id, agent.uuid)).id == agent.id

    async def test_an_unknown_uuid_is_404(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.get_data_agent(db, user.id, uuid_pkg.uuid4())

        assert excinfo.value.status_code == 404
        assert excinfo.value.detail == "Data agent not found"

    async def test_another_users_agent_is_404_not_403(
        self, db, user, other_user, make_agent  # noqa: ANN001
    ) -> None:
        theirs = await make_agent(other_user, "theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.get_data_agent(db, user.id, theirs.uuid)

        assert excinfo.value.status_code == 404


class TestGetDataAgentView:
    async def test_exposes_related_rows_by_their_own_public_uuid(
        self, db, user, make_workspace, make_ai_key, make_agent  # noqa: ANN001
    ) -> None:
        """The edit form's dropdowns preselect by uuid, so the view has to carry
        the workspace's and the key's uuids — not their bigint FKs."""
        workspace = await make_workspace(user, "Analytics")
        key = await make_ai_key(user, "prod-key")
        agent = await make_agent(
            user, "reporter", workspace_id=workspace.id, llm_api_key_id=key.id
        )

        view = await svc.get_data_agent_view(db, user.id, agent.uuid)

        assert view["workspace_id"] == str(workspace.uuid)
        assert view["workspace_name"] == "Analytics"
        assert view["llm_api_key_id"] == str(key.uuid)
        assert view["llm_key_label"] == "prod-key"

    async def test_unassigned_relations_are_empty_strings(
        self, db, user, make_agent  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")

        view = await svc.get_data_agent_view(db, user.id, agent.uuid)

        assert view["workspace_id"] == ""
        assert view["llm_api_key_id"] == ""
        assert view["workspace_name"] is None
        assert view["llm_key_label"] is None

    async def test_another_users_agent_is_404(
        self, db, user, other_user, make_agent  # noqa: ANN001
    ) -> None:
        theirs = await make_agent(other_user, "theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.get_data_agent_view(db, user.id, theirs.uuid)

        assert excinfo.value.status_code == 404


class TestGetAgentChoices:
    async def test_returns_uuid_name_and_active_flag(
        self, db, user, make_agent  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")

        assert await svc.get_agent_choices(db, user.id) == [
            {"uuid": str(agent.uuid), "name": "reporter", "is_active": True}
        ]

    async def test_disabled_agents_are_listed_but_marked(
        self, db, user, make_agent  # noqa: ANN001
    ) -> None:
        """A tool config can be prepared before its agent is switched on."""
        await make_agent(user, "parked", is_active=False)

        (choice,) = await svc.get_agent_choices(db, user.id)

        assert choice["is_active"] is False

    async def test_ordered_by_name(self, db, user, make_agent) -> None:  # noqa: ANN001
        for name in ["Zulu", "Alpha", "Mike"]:
            await make_agent(user, name)

        choices = await svc.get_agent_choices(db, user.id)

        assert [c["name"] for c in choices] == ["Alpha", "Mike", "Zulu"]


class TestGetAgentPublicId:
    async def test_maps_an_internal_id_to_the_public_uuid(
        self, db, user, make_agent  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")

        assert await svc.get_agent_public_id(db, user.id, agent.id) == str(agent.uuid)

    @pytest.mark.parametrize("empty", [None, 0])
    async def test_no_internal_id_returns_an_empty_string(
        self, db, user, empty  # noqa: ANN001
    ) -> None:
        assert await svc.get_agent_public_id(db, user.id, empty) == ""

    async def test_an_unknown_id_returns_an_empty_string(self, db, user) -> None:  # noqa: ANN001
        """"Nothing selected" is the right outcome in a dropdown, not a 404."""
        assert await svc.get_agent_public_id(db, user.id, 999999) == ""

    async def test_another_users_agent_returns_an_empty_string(
        self, db, user, other_user, make_agent  # noqa: ANN001
    ) -> None:
        theirs = await make_agent(other_user, "theirs")

        assert await svc.get_agent_public_id(db, user.id, theirs.id) == ""


class TestGetLlmKeyChoices:
    async def test_returns_uuid_label_and_provider_but_never_the_secret(
        self, db, user, make_ai_key  # noqa: ANN001
    ) -> None:
        key = await make_ai_key(user, "prod-key")

        (choice,) = await svc.get_llm_key_choices(db, user.id)

        assert choice["uuid"] == str(key.uuid)
        assert choice["label"] == "prod-key"
        assert "api_key_encrypted" not in choice
        assert SECRET_API_KEY not in str(choice)

    async def test_excludes_other_users_keys(
        self, db, user, other_user, make_ai_key  # noqa: ANN001
    ) -> None:
        await make_ai_key(other_user, "theirs")

        assert await svc.get_llm_key_choices(db, user.id) == []


# ---------------------------------------------------------------------------
# Create / update
# ---------------------------------------------------------------------------
class TestCreateDataAgent:
    async def test_creates_an_active_unassigned_agent(self, db, user) -> None:  # noqa: ANN001
        """A fresh agent has no tools and so can reach nothing — capabilities are
        added afterwards in the Tool Configs module."""
        agent = await svc.create_data_agent(db, user.id, "reporter")

        assert agent.name == "reporter"
        assert agent.is_active is True
        assert agent.workspace_id is None
        assert agent.llm_api_key_id is None

    async def test_attaches_a_workspace_and_key_by_uuid(
        self, db, user, make_workspace, make_ai_key  # noqa: ANN001
    ) -> None:
        workspace = await make_workspace(user, "Analytics")
        key = await make_ai_key(user, "prod-key")

        agent = await svc.create_data_agent(
            db, user.id, "reporter", workspace_id=workspace.uuid, llm_api_key_id=key.uuid
        )

        assert agent.workspace_id == workspace.id
        assert agent.llm_api_key_id == key.id

    async def test_stores_description_and_system_prompt(self, db, user) -> None:  # noqa: ANN001
        agent = await svc.create_data_agent(
            db, user.id, "reporter", description="d", system_prompt="be brief"
        )

        assert agent.description == "d"
        assert agent.system_prompt == "be brief"

    @pytest.mark.parametrize("blank", ["", "   ", None])
    async def test_a_blank_name_is_rejected(self, db, user, blank) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.create_data_agent(db, user.id, blank)

        assert excinfo.value.status_code == 400

    async def test_an_over_long_name_is_rejected(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException):
            await svc.create_data_agent(db, user.id, "x" * 256)

    async def test_an_over_long_system_prompt_is_rejected(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException):
            await svc.create_data_agent(db, user.id, "reporter", system_prompt="x" * 20001)

    async def test_a_duplicate_name_is_rejected(self, db, user) -> None:  # noqa: ANN001
        await svc.create_data_agent(db, user.id, "reporter")

        with pytest.raises(HTTPException) as excinfo:
            await svc.create_data_agent(db, user.id, "REPORTER")

        assert excinfo.value.status_code == 400
        assert excinfo.value.detail == "You already have a data agent named 'REPORTER'"

    async def test_two_users_may_share_an_agent_name(
        self, db, user, other_user  # noqa: ANN001
    ) -> None:
        """Unlike datasources, agent names are scoped per user."""
        await svc.create_data_agent(db, user.id, "reporter")

        theirs = await svc.create_data_agent(db, other_user.id, "reporter")

        assert theirs.user_id == other_user.id


class TestForeignKeyResolutionIsOwnershipChecked:
    async def test_another_users_workspace_is_404(
        self, db, user, other_user, make_workspace  # noqa: ANN001
    ) -> None:
        """Pasting a foreign workspace uuid into the form must not file the agent
        under it."""
        theirs = await make_workspace(other_user, "Theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.create_data_agent(db, user.id, "reporter", workspace_id=theirs.uuid)

        assert excinfo.value.status_code == 404

    async def test_an_unknown_workspace_is_404(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.create_data_agent(
                db, user.id, "reporter", workspace_id=uuid_pkg.uuid4()
            )

        assert excinfo.value.status_code == 404

    async def test_another_users_ai_key_is_404_with_a_useful_message(
        self, db, user, other_user, make_ai_key  # noqa: ANN001
    ) -> None:
        theirs = await make_ai_key(other_user, "theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.create_data_agent(
                db, user.id, "reporter", llm_api_key_id=theirs.uuid
            )

        assert excinfo.value.status_code == 404
        assert "Pick one from AI Settings" in excinfo.value.detail

    async def test_an_unknown_ai_key_is_404(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.create_data_agent(
                db, user.id, "reporter", llm_api_key_id=uuid_pkg.uuid4()
            )

        assert excinfo.value.status_code == 404


class TestArchivedWorkspaceRule:
    async def test_moving_an_agent_into_an_archived_workspace_is_refused(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        archived = await make_workspace(user, "Parked", is_active=False)

        with pytest.raises(HTTPException) as excinfo:
            await svc.create_data_agent(
                db, user.id, "reporter", workspace_id=archived.uuid
            )

        assert excinfo.value.status_code == 400
        assert "is archived" in excinfo.value.detail

    async def test_an_agent_already_inside_an_archived_workspace_stays_editable(
        self, db, user, make_workspace, make_agent  # noqa: ANN001
    ) -> None:
        """
        The asymmetry that makes archiving usable: without this exception,
        archiving a workspace would make every agent inside it unsavable, so the
        only way to edit one would be to un-archive first.
        """
        workspace = await make_workspace(user, "Parked")
        agent = await make_agent(user, "reporter", workspace_id=workspace.id)
        workspace.is_active = False
        await db.commit()

        updated = await svc.update_data_agent(
            db, user.id, agent.uuid, "renamed", workspace_id=workspace.uuid
        )

        assert updated.name == "renamed"
        assert updated.workspace_id == workspace.id

    async def test_moving_out_of_an_archived_workspace_is_allowed(
        self, db, user, make_workspace, make_agent  # noqa: ANN001
    ) -> None:
        archived = await make_workspace(user, "Parked", is_active=False)
        agent = await make_agent(user, "reporter", workspace_id=archived.id)

        updated = await svc.update_data_agent(
            db, user.id, agent.uuid, "reporter", workspace_id=None
        )

        assert updated.workspace_id is None


class TestUpdateDataAgent:
    async def test_renames(self, db, user, make_agent) -> None:  # noqa: ANN001
        agent = await make_agent(user, "old")

        updated = await svc.update_data_agent(db, user.id, agent.uuid, "new")

        assert updated.name == "new"

    async def test_saving_without_changing_the_name_is_allowed(
        self, db, user, make_agent  # noqa: ANN001
    ) -> None:
        """``exclude_id`` — otherwise the agent would report its own name taken."""
        agent = await make_agent(user, "reporter")

        updated = await svc.update_data_agent(
            db, user.id, agent.uuid, "reporter", description="new"
        )

        assert updated.description == "new"

    async def test_a_name_another_agent_has_is_rejected(
        self, db, user, make_agent  # noqa: ANN001
    ) -> None:
        await make_agent(user, "taken")
        agent = await make_agent(user, "mine")

        with pytest.raises(HTTPException, match="already have a data agent named"):
            await svc.update_data_agent(db, user.id, agent.uuid, "taken")

    async def test_clearing_the_workspace_and_key(
        self, db, user, make_workspace, make_ai_key, make_agent  # noqa: ANN001
    ) -> None:
        workspace = await make_workspace(user, "Analytics")
        key = await make_ai_key(user, "prod-key")
        agent = await make_agent(
            user, "reporter", workspace_id=workspace.id, llm_api_key_id=key.id
        )

        updated = await svc.update_data_agent(db, user.id, agent.uuid, "reporter")

        assert updated.workspace_id is None
        assert updated.llm_api_key_id is None

    async def test_another_users_agent_is_404(
        self, db, user, other_user, make_agent  # noqa: ANN001
    ) -> None:
        theirs = await make_agent(other_user, "theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.update_data_agent(db, user.id, theirs.uuid, "hijacked")

        assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# Enable / delete
# ---------------------------------------------------------------------------
class TestSetDataAgentActive:
    @pytest.mark.parametrize("is_active", [True, False])
    async def test_sets_the_flag(self, db, user, make_agent, is_active: bool) -> None:  # noqa: ANN001
        agent = await make_agent(user, "reporter", is_active=not is_active)

        updated = await svc.set_data_agent_active(db, user.id, agent.uuid, is_active)

        assert updated.is_active is is_active

    async def test_disabling_leaves_the_tool_configs_intact(
        self, db, user, make_agent  # noqa: ANN001
    ) -> None:
        """Re-enabling brings back exactly the setup the agent had."""
        agent = await make_agent(user, "reporter")
        datasource = DataSource(
            user_id=user.id,
            datasource_name="warehouse",
            db_type="postgres",
            password_encrypted="enc",
        )
        db.add(datasource)
        await db.commit()
        await db.refresh(datasource)
        db.add(
            ToolConfig(
                data_agent_id=agent.id,
                datasource_id=datasource.id,
                tool_name="query_orders",
                table_name="orders",
            )
        )
        await db.commit()

        await svc.set_data_agent_active(db, user.id, agent.uuid, False)

        (view,) = await svc.get_agent_views(db, user.id)
        assert view["tool_count"] == 1

    async def test_another_users_agent_is_404(
        self, db, user, other_user, make_agent  # noqa: ANN001
    ) -> None:
        theirs = await make_agent(other_user, "theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.set_data_agent_active(db, user.id, theirs.uuid, False)

        assert excinfo.value.status_code == 404


class TestDeleteDataAgent:
    async def test_deletes(self, db, user, make_agent) -> None:  # noqa: ANN001
        agent = await make_agent(user, "reporter")

        await svc.delete_data_agent(db, user.id, agent.uuid)

        assert await svc.get_agent_views(db, user.id) == []

    async def test_another_users_agent_is_404_and_survives(
        self, db, user, other_user, make_agent  # noqa: ANN001
    ) -> None:
        theirs = await make_agent(other_user, "theirs")

        with pytest.raises(HTTPException):
            await svc.delete_data_agent(db, user.id, theirs.uuid)

        assert len(await svc.get_agent_views(db, other_user.id)) == 1

    async def test_an_unknown_uuid_is_404(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.delete_data_agent(db, user.id, uuid_pkg.uuid4())

        assert excinfo.value.status_code == 404

    async def test_the_name_is_free_again_afterwards(self, db, user) -> None:  # noqa: ANN001
        agent = await svc.create_data_agent(db, user.id, "reporter")
        await svc.delete_data_agent(db, user.id, agent.uuid)

        recreated = await svc.create_data_agent(db, user.id, "reporter")

        assert recreated.uuid != agent.uuid
