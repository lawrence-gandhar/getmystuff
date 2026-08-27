"""
Tests for what a flow is *for* — an agent's own conversation, or a callable child.

Before this distinction existed, a Run Flow block offered every published flow the user
owned, which put two unlike things in one list: an agent's live front door, and a flow
built to be reused. The kind makes it explicit, and the property worth protecting is that
**the two lists are now disjoint by construction**:

* an agent's *Conversation Flow* dropdown offers agent flows only, and
* a Run Flow block's list offers generic flows only.

Everything else here guards the invariant that makes those two safe — a generic flow is
never attached to an agent — at each of the three places it can be broken: creating a flow,
switching a flow's kind, and attaching one. The table carries a check constraint saying the
same thing, which is what stops a fourth place appearing.

These use the real database (the `db` fixture) because the rules are queries and a
constraint, not arithmetic — a filter that quietly matched everything would pass any stub.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from litestar.exceptions import HTTPException

from app.models.chatbot import ChatbotApiKey
from app.models.flow_builder import ChatbotFlow
from app.services.flow_builder import flow_service

GRAPH = {"nodes": [{"id": "start", "type": "start", "data": {}}], "edges": []}


@pytest.fixture
def make_flow(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str, *, kind: str = "agent", is_active: bool = True, **kwargs):  # noqa: ANN001
        row = ChatbotFlow(
            user_id=owner.id,
            name=name,
            graph_data=kwargs.pop("graph_data", dict(GRAPH)),
            kind=kind,
            is_active=is_active,
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_key(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str = "Agent"):  # noqa: ANN001
        row = ChatbotApiKey(
            user_id=owner.id,
            name=name,
            api_key=f"key-{uuid_pkg.uuid4().hex[:12]}",
            target_type="datasource",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


# --------------------------------------------------------------------------
# Creating
# --------------------------------------------------------------------------

class TestCreating:
    async def test_a_flow_is_an_agent_flow_by_default(self, db, user) -> None:  # noqa: ANN001
        flow = await flow_service.create_flow(db, user.id, "Front door")

        assert flow.kind == "agent", (
            "the kind every flow had before the column existed, so a caller that does not "
            "mention it keeps creating what it always created"
        )

    async def test_a_generic_flow_can_be_created_directly(self, db, user) -> None:  # noqa: ANN001
        flow = await flow_service.create_flow(db, user.id, "Collect details", "generic")

        assert flow.kind == "generic"

    async def test_an_unknown_kind_is_refused(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as caught:
            await flow_service.create_flow(db, user.id, "Odd one", "sideways")

        assert "agent flow or a generic one" in str(caught.value.detail)

    async def test_both_kinds_start_as_drafts(self, db, user) -> None:  # noqa: ANN001
        """One Active rule for both kinds — a half-drawn child is no more callable than a
        half-drawn agent flow is answerable."""
        agent = await flow_service.create_flow(db, user.id, "A")
        generic = await flow_service.create_flow(db, user.id, "B", "generic")

        assert (agent.is_active, generic.is_active) == (False, False)


# --------------------------------------------------------------------------
# The two lists, which must not overlap
# --------------------------------------------------------------------------

class TestWhatAnAgentMayBeGiven:
    async def test_a_generic_flow_is_not_offered(self, db, user, make_flow) -> None:  # noqa: ANN001
        await make_flow(user, "Front door", kind="agent")
        await make_flow(user, "Collect details", kind="generic")

        offered = [flow.name for flow in await flow_service.get_attachable_flows(db, user.id)]

        assert offered == ["Front door"]

    async def test_a_draft_agent_flow_is_still_not_offered(self, db, user, make_flow) -> None:  # noqa: ANN001
        """The kind filter is additional to the two switches, not a replacement for them."""
        await make_flow(user, "Half done", kind="agent", is_active=False)

        assert await flow_service.get_attachable_flows(db, user.id) == []


class TestWhatARunFlowBlockMayCall:
    async def test_only_generic_flows_are_offered(self, db, user, make_flow) -> None:  # noqa: ANN001
        await make_flow(user, "Front door", kind="agent")
        await make_flow(user, "Collect details", kind="generic")

        offered = [c["label"] for c in await flow_service.callable_flow_choices(db, user.id)]

        assert offered == ["Collect details"], (
            "an agent flow is somebody's live conversation, not a step to be reused"
        )

    async def test_a_draft_generic_flow_is_not_offered(self, db, user, make_flow) -> None:  # noqa: ANN001
        await make_flow(user, "Half drawn", kind="generic", is_active=False)

        assert await flow_service.callable_flow_choices(db, user.id) == []

    async def test_the_flow_being_edited_is_never_offered_to_itself(
        self, db, user, make_flow,
    ) -> None:  # noqa: ANN001
        """
        Still earns its place with the kind filter in force: a generic flow may call
        another generic flow, so the list it sees contains its own siblings — and itself,
        without this.
        """
        editing = await make_flow(user, "Child A", kind="generic")
        await make_flow(user, "Child B", kind="generic")

        offered = [
            c["label"]
            for c in await flow_service.callable_flow_choices(db, user.id, editing.uuid)
        ]

        assert offered == ["Child B"]

    async def test_another_users_generic_flow_is_not_offered(
        self, db, user, make_user, make_flow,
    ) -> None:  # noqa: ANN001
        other = await make_user("someone@example.com")
        await make_flow(other, "Their child", kind="generic")

        assert await flow_service.callable_flow_choices(db, user.id) == []

    async def test_each_entry_carries_what_that_flow_reads_and_writes(
        self, db, user, make_flow,
    ) -> None:  # noqa: ANN001
        """The panel draws its rows from these without a second request."""
        await make_flow(
            user, "Collect details", kind="generic",
            graph_data={
                "nodes": [
                    {"id": "s", "type": "start", "data": {}},
                    {"id": "a", "type": "ask_input",
                     "data": {"prompt_text": "For {{ACCOUNT}}?", "variable_name": "email"}},
                ],
                "edges": [],
            },
        )

        [choice] = await flow_service.callable_flow_choices(db, user.id)

        assert choice["writes"] == ["email"]
        assert choice["reads"] == ["ACCOUNT"]


# --------------------------------------------------------------------------
# Switching, and the invariant it protects
# --------------------------------------------------------------------------

class TestSwitchingKind:
    async def test_an_unattached_flow_switches_freely(self, db, user, make_flow) -> None:  # noqa: ANN001
        flow = await make_flow(user, "Reusable bit", kind="agent")

        switched = await flow_service.set_flow_kind(db, user.id, flow.uuid, "generic")

        assert switched.kind == "generic"

    async def test_switching_back_needs_no_guard(self, db, user, make_flow) -> None:  # noqa: ANN001
        flow = await make_flow(user, "Reusable bit", kind="generic")

        switched = await flow_service.set_flow_kind(db, user.id, flow.uuid, "agent")

        assert switched.kind == "agent"

    async def test_an_attached_flow_cannot_become_generic_and_the_agent_is_named(
        self, db, user, make_flow, make_key,
    ) -> None:  # noqa: ANN001
        """
        Refused rather than silently detached: making it generic would take a live
        conversation away from an agent, and detaching is a decision made on that agent's
        own page where whoever makes it can see what else is running there.
        """
        key = await make_key(user, "Support bot")
        flow = await make_flow(user, "Front door", kind="agent", chatbot_key_id=key.id)

        with pytest.raises(HTTPException) as caught:
            await flow_service.set_flow_kind(db, user.id, flow.uuid, "generic")

        detail = str(caught.value.detail)
        assert "Support bot" in detail, "'detach it first' is useless without saying from what"
        assert "Detach it" in detail

    async def test_publishing_is_untouched(self, db, user, make_flow) -> None:  # noqa: ANN001
        flow = await make_flow(user, "Reusable bit", kind="agent", is_active=True)

        switched = await flow_service.set_flow_kind(db, user.id, flow.uuid, "generic")

        assert switched.is_active is True

    async def test_another_users_flow_is_not_switchable(
        self, db, user, make_user, make_flow,
    ) -> None:  # noqa: ANN001
        other = await make_user("someone@example.com")
        theirs = await make_flow(other, "Theirs", kind="agent")

        with pytest.raises(HTTPException) as caught:
            await flow_service.set_flow_kind(db, user.id, theirs.uuid, "generic")

        assert caught.value.status_code == 404


class TestAttaching:
    async def test_a_generic_flow_cannot_be_attached_to_an_agent(
        self, db, user, make_flow, make_key,
    ) -> None:  # noqa: ANN001
        """
        `get_attachable_flows` already keeps it out of the dropdown, so reaching this needs a
        hand-made request — but the dropdown must not be the only thing between a mistake and
        a live agent, and the check constraint would otherwise refuse it as a database error
        rather than a sentence.
        """
        key = await make_key(user, "Support bot")
        generic = await make_flow(user, "Collect details", kind="generic")

        with pytest.raises(HTTPException) as caught:
            await flow_service.attach_flow(db, user.id, key.uuid, generic.uuid)

        assert "generic child flow" in str(caught.value.detail)

    async def test_an_agent_flow_still_attaches(
        self, db, user, make_flow, make_key,
    ) -> None:  # noqa: ANN001
        key = await make_key(user, "Support bot")
        flow = await make_flow(user, "Front door", kind="agent")

        attached = await flow_service.attach_flow(db, user.id, key.uuid, flow.uuid)

        assert attached is not None
        assert attached.chatbot_key_id == key.id


class TestTheDatabaseSaysItToo:
    async def test_the_constraint_refuses_an_attached_generic_flow(
        self, db, user, make_flow, make_key,
    ) -> None:  # noqa: ANN001
        """
        The service's two refusals are the readable ones; this is the guarantee that no
        third write path can undo them. Written straight to the row, bypassing both.
        """
        from sqlalchemy.exc import IntegrityError

        key = await make_key(user, "Support bot")
        flow = await make_flow(user, "Front door", kind="agent", chatbot_key_id=key.id)

        flow.kind = "generic"
        with pytest.raises(IntegrityError):
            await db.commit()

        await db.rollback()
