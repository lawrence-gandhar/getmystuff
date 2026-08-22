"""
The Email node inside a chatbot conversation.

The behaviour asserted hardest is the one the feature was asked for: **a binding can read a
variable from the Agents section**, and it resolves through
``chatbot_ai_settings_service.variables_map`` rather than off the JSONB column — so
``{{AGENT_NAME}}``, which is synthesised from the ``agent_name`` field rather than declared
as a variable, works here exactly as it does in a system prompt. Reading the column directly
would miss it, and the miss would be silent.

Also pinned: a flow cannot bind to an upstream node's output. This engine's whole state is
one flat string map — there are no node outputs — so offering that source would build a
panel the server refuses.
"""

from __future__ import annotations

import pytest

from app.models.chatbot import TARGET_TYPE_DATASOURCE, ChatbotApiKey
from app.models.email_dispatch import MESSAGE_QUEUED, SOURCE_NODE, EmailMessage
from app.services.email_dispatch.errors import EmailFailure, RenderError
from app.services.email_dispatch.nodes import flow_builder_runner
from sqlalchemy import select


@pytest.fixture
async def chatbot_key(db, user) -> ChatbotApiKey:
    """A chatbot whose AI settings row is created on first read, with its seeded
    variables — which is what makes ``agent_variables_for`` need no empty-case branch."""
    key = ChatbotApiKey(
        user_id=user.id,
        api_key="test-widget-key",
        name="Support bot",
        # NOT NULL, and the flow path never reads it — a chatbot answering from a flow does
        # not need a datasource or an agent behind it, which is exactly the configuration
        # these tests are about.
        target_type=TARGET_TYPE_DATASOURCE,
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)
    return key


def node(**data) -> dict:
    base = {"recipients": {"to": ["ops@example.com"]}, "variable_bindings": {}}
    base.update(data)
    return {"id": "n-email", "type": "send_email", "data": base}


async def queued(db) -> list:
    return list(
        (await db.execute(select(EmailMessage).order_by(EmailMessage.id))).scalars().all()
    )


class TestAgentVariables:
    async def test_the_agents_section_variables_are_available(
        self, db, chatbot_key
    ):  # noqa: ANN001
        """The seeded defaults include COMPANY, and AGENT_NAME is synthesised from the
        agent_name field rather than declared — so it must be present too."""
        variables = await flow_builder_runner.agent_variables_for(db, chatbot_key)

        assert "AGENT_NAME" in variables, (
            "AGENT_NAME is synthesised by variables_map, not stored as a declared "
            "variable — reading the JSONB column directly would miss it"
        )
        assert "COMPANY" in variables

    async def test_a_binding_reads_an_agent_variable(
        self, db, user, chatbot_key, template, smtp_config
    ):  # noqa: ANN001
        result = await flow_builder_runner.run_email_node(
            db,
            node(
                template_id=str(template.uuid),
                smtp_config_id=str(smtp_config.uuid),
                variable_bindings={
                    "WORKFLOW": {"source": "agent", "path": "AGENT_NAME"},
                },
            ),
            chatbot_key=chatbot_key,
            session_variables={},
            session_token="visitor-abc",
        )
        await db.commit()

        # The default agent name is "Assistant", per ChatbotAiSettings.
        assert "Assistant" in result["subject"]
        assert result["queued"] is True
        assert result["delivered"] is None


class TestSessionVariables:
    async def test_a_binding_reads_what_the_conversation_collected(
        self, db, user, chatbot_key, template, smtp_config
    ):  # noqa: ANN001
        result = await flow_builder_runner.run_email_node(
            db,
            node(
                template_id=str(template.uuid),
                smtp_config_id=str(smtp_config.uuid),
                variable_bindings={
                    "WORKFLOW": {"source": "session", "path": "topic"},
                },
            ),
            chatbot_key=chatbot_key,
            session_variables={"topic": "Broken widget"},
            session_token="visitor-abc",
        )
        await db.commit()

        assert result["subject"] == "Broken widget failed"

    async def test_a_recipient_can_come_from_the_conversation(
        self, db, user, chatbot_key, template, smtp_config
    ):  # noqa: ANN001
        """"Email whoever this was about" without a rules engine — the reason recipients
        are rendered rather than stored already resolved."""
        await flow_builder_runner.run_email_node(
            db,
            node(
                template_id=str(template.uuid),
                smtp_config_id=str(smtp_config.uuid),
                recipients={"to": ["{{WORKFLOW}}"]},
                variable_bindings={
                    "WORKFLOW": {"source": "session", "path": "email"},
                },
            ),
            chatbot_key=chatbot_key,
            session_variables={"email": "visitor@example.com"},
        )
        await db.commit()

        messages = await queued(db)
        assert messages[0].to_addresses == ["visitor@example.com"]

    async def test_the_message_records_which_conversation_queued_it(
        self, db, user, chatbot_key, template, smtp_config
    ):  # noqa: ANN001
        """When somebody asks "why did this customer get an email", the answer is which
        conversation — so the token is what identifies it, not the flow's id."""
        await flow_builder_runner.run_email_node(
            db,
            node(
                template_id=str(template.uuid),
                smtp_config_id=str(smtp_config.uuid),
                variable_bindings={"WORKFLOW": {"source": "literal", "value": "x"}},
            ),
            chatbot_key=chatbot_key,
            session_variables={},
            session_token="visitor-xyz",
        )
        await db.commit()

        messages = await queued(db)
        assert messages[0].status == MESSAGE_QUEUED
        assert messages[0].source == SOURCE_NODE
        assert "visitor-xyz" in messages[0].source_ref


class TestRefusals:
    async def test_a_flow_cannot_bind_to_an_upstream_nodes_output(
        self, db, user, chatbot_key, template, smtp_config
    ):  # noqa: ANN001
        """This engine's state is one flat string map — there are no node outputs at all."""
        with pytest.raises(RenderError, match="not available here"):
            await flow_builder_runner.run_email_node(
                db,
                node(
                    template_id=str(template.uuid),
                    smtp_config_id=str(smtp_config.uuid),
                    variable_bindings={
                        "WORKFLOW": {"source": "node", "node_id": "n-1"},
                    },
                ),
                chatbot_key=chatbot_key,
                session_variables={},
            )

    async def test_an_unset_session_variable_is_refused_rather_than_blanked(
        self, db, user, chatbot_key, template, smtp_config
    ):  # noqa: ANN001
        """
        The conversation has not reached the Ask-for-Input block yet.

        The binding finds nothing, so the variable is *omitted* — and WORKFLOW is declared
        required with no default, so the template refuses. That indirection is the design:
        strictness is set per variable in the template rather than hard-coded here, so a
        naturally-absent field can carry a default while a required one stops the send.
        Either way nothing goes out with a hole in it — "Dear ," is worse than not sending.
        """
        with pytest.raises(RenderError, match="required by this template"):
            await flow_builder_runner.run_email_node(
                db,
                node(
                    template_id=str(template.uuid),
                    smtp_config_id=str(smtp_config.uuid),
                    variable_bindings={
                        "WORKFLOW": {"source": "session", "path": "never_asked"},
                    },
                ),
                chatbot_key=chatbot_key,
                session_variables={},
            )

    async def test_no_template_chosen_is_refused(
        self, db, chatbot_key, smtp_config
    ):  # noqa: ANN001
        with pytest.raises(EmailFailure, match="no template or no server"):
            await flow_builder_runner.run_email_node(
                db,
                node(smtp_config_id=str(smtp_config.uuid)),
                chatbot_key=chatbot_key,
                session_variables={},
            )
