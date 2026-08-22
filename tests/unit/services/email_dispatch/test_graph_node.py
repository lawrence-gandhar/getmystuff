"""
The Email node inside a Graph Designer run.

The behaviour asserted hardest is that the node **queues rather than sends**, and says so.
A downstream branch reading the node's output as "delivered" is the misreading this node
most invites, so the payload carries ``queued: True`` and ``delivered: None`` and a test
pins both — if somebody later changes the output shape to something that reads as delivery
confirmation, this fails.

Also pinned: the node reads *upstream node outputs* and nothing else. A binding to a chat
session or a record is refused by name rather than resolving to blank, because a graph has
neither and an email addressed to "Dear ," is worse than one not sent.
"""

from __future__ import annotations

import pytest

from app.models.email_dispatch import MESSAGE_QUEUED, SOURCE_NODE, EmailMessage
from app.services.email_dispatch.errors import EmailFailure, RenderError
from app.services.email_dispatch.nodes import graph_designer_runner
from sqlalchemy import select


def node(**data) -> dict:
    """One authored Email node, as it appears inside ``graph_data``."""
    base = {
        "recipients": {"to": ["ops@example.com"]},
        "variable_bindings": {},
    }
    base.update(data)
    return {"id": "n-email", "type": "email", "data": base}


def state(outputs) -> dict:
    """A graph state carrying whatever the upstream nodes produced."""
    return {"outputs": outputs}


async def queued(db) -> list:
    return list(
        (await db.execute(select(EmailMessage).order_by(EmailMessage.id))).scalars().all()
    )


class TestQueueing:
    async def test_queues_a_message_and_reports_what_it_queued(
        self, db, user, template, smtp_config
    ):  # noqa: ANN001
        result = await graph_designer_runner.run_email_node(
            node(
                template_id=str(template.uuid),
                smtp_config_id=str(smtp_config.uuid),
                variable_bindings={
                    "WORKFLOW": {"source": "literal", "value": "Nightly report"},
                },
            ),
            state({}),
            user_id=user.id,
            run_ref="17",
        )

        assert result["queued"] is True
        # The load-bearing assertion. See the module docstring.
        assert result["delivered"] is None, (
            "the node must not claim delivery — it queues, and the worker sends"
        )
        assert result["subject"] == "Nightly report failed"
        assert result["to"] == ["ops@example.com"]
        # uuid, never the bigint id: this goes into graph state, which is previewed into the
        # run dock and is therefore something a browser sees.
        assert "-" in result["message_uuid"]
        assert "id" not in result

        messages = await queued(db)
        assert len(messages) == 1
        assert messages[0].status == MESSAGE_QUEUED
        assert messages[0].source == SOURCE_NODE
        # Names the run and the node, so a log row traces back to what queued it.
        assert "17" in messages[0].source_ref
        assert "n-email" in messages[0].source_ref

    async def test_reads_an_upstream_nodes_output_through_a_path(
        self, db, user, template, smtp_config
    ):  # noqa: ANN001
        result = await graph_designer_runner.run_email_node(
            node(
                template_id=str(template.uuid),
                smtp_config_id=str(smtp_config.uuid),
                variable_bindings={
                    "WORKFLOW": {
                        "source": "node",
                        "node_id": "n-sql",
                        "path": "rows[0].name",
                    },
                },
            ),
            state({"n-sql": {"rows": [{"name": "Warehouse sync"}]}}),
            user_id=user.id,
        )

        assert result["subject"] == "Warehouse sync failed"

    async def test_the_declared_default_fills_an_unbound_variable(
        self, db, user, template, smtp_config
    ):  # noqa: ANN001
        """SEVERITY is declared with a default of 'normal' and nothing binds it."""
        await graph_designer_runner.run_email_node(
            node(
                template_id=str(template.uuid),
                smtp_config_id=str(smtp_config.uuid),
                variable_bindings={
                    "WORKFLOW": {"source": "literal", "value": "Nightly"},
                },
            ),
            state({}),
            user_id=user.id,
        )

        messages = await queued(db)
        assert "normal" in messages[0].body_html


class TestRefusals:
    async def test_no_template_chosen_is_refused_before_anything_is_queued(
        self, db, user, smtp_config
    ):  # noqa: ANN001
        with pytest.raises(EmailFailure, match="no template or no server"):
            await graph_designer_runner.run_email_node(
                node(smtp_config_id=str(smtp_config.uuid)),
                state({}),
                user_id=user.id,
            )

        assert await queued(db) == []

    async def test_a_binding_to_a_source_a_graph_does_not_have_is_refused_by_name(
        self, db, user, template, smtp_config
    ):  # noqa: ANN001
        """A graph has no chat session. Resolving that to blank would send an email with a
        hole in it; naming it tells whoever drew the node what to fix."""
        with pytest.raises(RenderError, match="not available here"):
            await graph_designer_runner.run_email_node(
                node(
                    template_id=str(template.uuid),
                    smtp_config_id=str(smtp_config.uuid),
                    variable_bindings={
                        "WORKFLOW": {"source": "session", "path": "email"},
                    },
                ),
                state({}),
                user_id=user.id,
            )

        assert await queued(db) == []

    async def test_a_binding_to_a_node_that_produced_nothing_is_refused(
        self, db, user, template, smtp_config
    ):  # noqa: ANN001
        """A deleted node, or one a branch skipped. Every reader of a node id inside a JSONB
        drawing has to survive this — the id is not a foreign key."""
        with pytest.raises(RenderError, match="did not produce anything"):
            await graph_designer_runner.run_email_node(
                node(
                    template_id=str(template.uuid),
                    smtp_config_id=str(smtp_config.uuid),
                    variable_bindings={
                        "WORKFLOW": {"source": "node", "node_id": "n-gone"},
                    },
                ),
                state({"n-other": 1}),
                user_id=user.id,
            )

        assert await queued(db) == []

    async def test_a_required_variable_with_nothing_bound_is_refused(
        self, db, user, template, smtp_config
    ):  # noqa: ANN001
        with pytest.raises(RenderError, match="required"):
            await graph_designer_runner.run_email_node(
                node(
                    template_id=str(template.uuid),
                    smtp_config_id=str(smtp_config.uuid),
                ),
                state({}),
                user_id=user.id,
            )

        assert await queued(db) == []

    async def test_another_users_template_is_not_reachable(
        self, db, user, make_user, template, smtp_config
    ):  # noqa: ANN001
        """Ownership is resolved from the uuid every time, never taken on trust — so a node
        in somebody else's graph cannot name this user's template."""
        stranger = await make_user(email="stranger@example.com")

        with pytest.raises(Exception) as caught:
            await graph_designer_runner.run_email_node(
                node(
                    template_id=str(template.uuid),
                    smtp_config_id=str(smtp_config.uuid),
                    variable_bindings={
                        "WORKFLOW": {"source": "literal", "value": "x"},
                    },
                ),
                state({}),
                user_id=stranger.id,
            )

        assert "not found" in str(caught.value).lower()
        assert await queued(db) == []


class TestFailureTranslation:
    def test_an_email_failure_keeps_its_own_sentence(self):
        """The message was written for the operator; replacing it with str(exc) on some
        path that forgot is what this function exists to prevent."""
        message = graph_designer_runner.wrap_failure(
            EmailFailure("The template 'Alerts' is switched off.")
        )
        assert message == "The template 'Alerts' is switched off."

    def test_anything_else_gets_a_generic_sentence_and_no_internals(self):
        message = graph_designer_runner.wrap_failure(
            RuntimeError("psycopg.OperationalError: connection refused at 10.0.0.5")
        )
        assert "10.0.0.5" not in message
        assert "not been sent" in message


class TestTheNodeIsNotTheGraphsResult:
    """
    An Email node can now be drawn *after* a Success node, so it is routinely the last node
    to run — and its output is a receipt, not data.

    ``_result_preview`` works from an allow-list of data node types, which is what makes this
    safe. Tested directly rather than through a run, because the thing being pinned is the
    allow-list decision and a real run would only reach it through three other layers.
    """

    def test_a_queued_email_does_not_become_what_the_graph_returned(self):
        from app.services.graph_designer import graph_run_service

        state = {
            "outputs": {
                "q": {"kind": "rows", "count": 3, "rows": [{"id": 1}]},
                "ok": {"succeeded": True},
                "mail": {"queued": True, "delivered": None},
            },
        }
        node_by_id = {
            "q": {"id": "q", "type": "sql"},
            "ok": {"id": "ok", "type": "success"},
            "mail": {"id": "mail", "type": "email"},
        }

        preview = graph_run_service._result_preview(state, node_by_id)

        assert preview["node_id"] == "q", (
            "the rows are the result; the email is bookkeeping about the rows"
        )
