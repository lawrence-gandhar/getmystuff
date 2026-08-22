"""
Which templates a picker offers, and which it merely flags.

The rule under test is the house one, and it is easy to get backwards: an unavailable
option is **listed with a reason**, never dropped. An operator hunting for the template
they know exists needs to be told why they cannot pick it — a list that silently omits it
sends them off to check the wrong thing.

``choices_for_workspace`` exists because the Graph Designer's Email node picks from the
templates of the workspace its graph is shared into. A template is *owned* by a user and
*shared* into a workspace, so one with no workspace at all is the owner's own and is
always theirs to send — that is the case most worth pinning, because getting it wrong
gives an unshared graph an empty picker and no explanation.
"""

from __future__ import annotations

import pytest

from app.models.email_dispatch import EmailTemplate
from app.models.workspaces.workspaces import Workspace
from app.services.email_dispatch import template_service


@pytest.fixture
async def workspace(db, user) -> Workspace:  # noqa: ANN001
    row = Workspace(user_id=user.id, name="Sales Ops")
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
async def other_workspace(db, user) -> Workspace:  # noqa: ANN001
    row = Workspace(user_id=user.id, name="Finance")
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def make_template(db, user, name: str, **overrides) -> EmailTemplate:  # noqa: ANN001
    row = EmailTemplate(
        user_id=user.id,
        name=name,
        subject_template="Hello",
        body_html_template="<p>Hello</p>",
        variables=[],
        **overrides,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def by_name(rows: list, name: str) -> dict:
    found = [row for row in rows if row["label"] == name]
    assert found, f"{name!r} is not in the list at all — it should be flagged, not dropped"
    return found[0]


class TestUnscopedChoices:
    """The list every other canvas gets, where there is no workspace to scope by."""

    async def test_every_template_is_selectable(self, db, user):  # noqa: ANN001
        await make_template(db, user, "Anywhere")

        rows = await template_service.choices(db, user.id)

        assert by_name(rows, "Anywhere")["disabled_reason"] == ""

    async def test_a_switched_off_template_is_offered_and_flagged(self, db, user):  # noqa: ANN001
        await make_template(db, user, "Parked", is_active=False)

        assert by_name(await template_service.choices(db, user.id), "Parked")[
            "disabled_reason"
        ] == "Switched off"

    async def test_what_it_declares_travels_with_it(self, db, user, template):  # noqa: ANN001
        """
        So the panel can draw one binding row per variable the instant a template is
        chosen. A second round trip would let somebody save the node before its bindings
        had loaded.
        """
        row = by_name(await template_service.choices(db, user.id), "Run failed")

        assert [variable["name"] for variable in row["variables"]] == ["WORKFLOW", "SEVERITY"]


class TestWorkspaceScopedChoices:
    """What an Email node on a graph shared into one workspace may pick."""

    async def test_a_template_in_this_workspace_is_selectable(
        self, db, user, workspace,
    ):  # noqa: ANN001
        await make_template(db, user, "Ours", workspace_id=workspace.id)

        rows = await template_service.choices_for_workspace(db, user.id, workspace.id)

        assert by_name(rows, "Ours")["disabled_reason"] == ""

    async def test_a_template_in_no_workspace_is_selectable(
        self, db, user, workspace,
    ):  # noqa: ANN001
        """
        Owned by the user and shared with nobody, so always theirs to send. Getting this
        wrong is what would give a shared graph a picker missing the operator's own
        templates.
        """
        await make_template(db, user, "Personal")

        rows = await template_service.choices_for_workspace(db, user.id, workspace.id)

        assert by_name(rows, "Personal")["disabled_reason"] == ""

    async def test_a_template_shared_with_another_team_is_still_selectable(
        self, db, user, workspace, other_workspace,
    ):  # noqa: ANN001
        """
        The one that matters, and the one an earlier version of this got backwards.

        ``workspace_id`` records who *else* may use a template — it does not take it away
        from its owner. Refusing it here would mean sharing a template with a team quietly
        removed the owner's own access, and would make it impossible for any graph
        attached to a data agent (which therefore has no workspace at all) to send a
        shared template.
        """
        await make_template(db, user, "Theirs", workspace_id=other_workspace.id)

        row = by_name(
            await template_service.choices_for_workspace(db, user.id, workspace.id),
            "Theirs",
        )

        assert row["disabled_reason"] == ""

    async def test_it_says_which_team_that_template_is_shared_with(
        self, db, user, workspace, other_workspace,
    ):  # noqa: ANN001
        """Context, not permission — so two similarly named templates can be told apart."""
        await make_template(db, user, "Theirs", workspace_id=other_workspace.id)

        row = by_name(
            await template_service.choices_for_workspace(db, user.id, workspace.id),
            "Theirs",
        )

        assert "Finance" in row["detail"]

    async def test_a_template_in_this_workspace_is_not_labelled(
        self, db, user, workspace,
    ):  # noqa: ANN001
        """Naming the workspace an operator is already in is noise."""
        await make_template(db, user, "Ours", workspace_id=workspace.id)

        row = by_name(
            await template_service.choices_for_workspace(db, user.id, workspace.id),
            "Ours",
        )

        assert "shared with" not in row["detail"]

    async def test_only_a_switched_off_template_is_flagged_unavailable(
        self, db, user, workspace, other_workspace,
    ):  # noqa: ANN001
        """
        ``disabled_reason`` means "picking this will not work". A workspace never makes
        that true, so being switched off is the only thing that earns it.
        """
        await make_template(
            db, user, "Parked elsewhere",
            workspace_id=other_workspace.id, is_active=False,
        )

        row = by_name(
            await template_service.choices_for_workspace(db, user.id, workspace.id),
            "Parked elsewhere",
        )

        assert row["disabled_reason"] == "Switched off"

    async def test_a_graph_with_no_workspace_can_still_send_everything(
        self, db, user, other_workspace,
    ):  # noqa: ANN001
        """
        A graph attached to a data agent has no workspace, and attachment and sharing are
        mutually exclusive — so this is the state most graphs are in. It must not be the
        state that can send the least.
        """
        await make_template(db, user, "Personal")
        await make_template(db, user, "Theirs", workspace_id=other_workspace.id)

        rows = await template_service.choices_for_workspace(db, user.id, None)

        assert by_name(rows, "Personal")["disabled_reason"] == ""
        assert by_name(rows, "Theirs")["disabled_reason"] == ""

    async def test_another_users_templates_are_not_listed_at_all(
        self, db, user, workspace, make_user,
    ):  # noqa: ANN001
        """
        Scoping is a *flag*; ownership is a filter. Somebody else's template is not
        "unavailable", it is none of this user's business.
        """
        stranger = await make_user("stranger@example.com")
        row = EmailTemplate(
            user_id=stranger.id, name="Not yours",
            subject_template="x", body_html_template="<p>x</p>", variables=[],
        )
        db.add(row)
        await db.commit()

        rows = await template_service.choices_for_workspace(db, user.id, workspace.id)

        assert [entry for entry in rows if entry["label"] == "Not yours"] == []
