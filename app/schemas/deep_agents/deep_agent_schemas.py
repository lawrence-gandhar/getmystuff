"""
app/schemas/deep_agents/deep_agent_schemas.py

Pydantic schemas for the Deep Agents module — the shared workspace → agent
cascade, and the test console.

The console's question is the one free-text field in the application that is sent
straight to a language model, so its length cap is a cost control as much as a
validation: a 200 KB paste would be tokenised and paid for before anything noticed.
The cap matches the one Ask AI applies to its prompt, for the same reason.

There is no model here for what the console *returns*. An answer is rendered into
a partial together with the tool calls it made, and its shape is whatever
`deep_agent_service.answer_with_deep_agent` produced — a contract that belongs to
that service and is documented in DEEP_AGENTS.md.
"""

from pydantic import Field

from app.schemas.base import (
    MAX_NAME_LENGTH,
    CheckboxBool,
    FormRequest,
    OptionalUUID,
    QueryRequest,
)

#: The console's question cap. Ask AI bounds its prompt at the same number
#: (`sql_assist_service._MAX_PROMPT_LEN`); a person typing into a console and a
#: person typing into a prompt box should not hit two different limits.
MAX_QUESTION_LENGTH = 2000


class AgentOptionsQuery(QueryRequest):
    """
    The Workspace → Data Agent cascade, shared by two host forms.

    A blank ``workspace_id`` lists every agent the user owns rather than none —
    an agent's workspace is nullable, so "no workspace chosen" has to be able to
    reach an unassigned agent or those agents would be unpickable.

    ``field_name`` lets each host form name the ``<select>`` it is rendering, so
    neither form dictates the other's markup.

    ``required`` drops the "No data agent" option. One host needs it: a chatbot
    whose ``target_type`` is ``agent`` has no datasource of its own, so detaching
    its agent would leave a published widget that can answer nothing —
    ``chatbot_service.set_chatbot_data_agent`` refuses that, and this stops the
    picker offering it in the first place. It has to survive the cascade, or the
    option would reappear the moment a workspace was chosen.
    """

    workspace_id: OptionalUUID = Field(default=None, title="Workspace")
    selected: OptionalUUID = Field(default=None, title="Data agent")
    field_name: str = Field(
        default="data_agent_id", title="Field name", max_length=MAX_NAME_LENGTH
    )
    required: CheckboxBool = Field(default=False, title="Agent required")

    @property
    def select_name(self) -> str:
        """The field name to render, falling back to the shared default."""
        return self.field_name.strip() or "data_agent_id"


class DeepAgentAskRequest(FormRequest):
    """
    One question for the test console.

    ``min_length`` carries the "Type a question first." case the handler used to
    check by hand; the message below is what the user sees for both an empty box
    and a box holding only spaces, since whitespace is stripped first.
    """

    question: str = Field(
        title="Question", min_length=1, max_length=MAX_QUESTION_LENGTH
    )
