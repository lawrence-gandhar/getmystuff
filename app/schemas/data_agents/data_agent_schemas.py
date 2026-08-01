"""
app/schemas/data_agents/data_agent_schemas.py

Pydantic schemas for the Data Agents module.

Two things here are worth knowing about before adding a field.

**The workspace filter travels with every mutation.** The agents list can be
narrowed to one workspace, and each mutation posts that filter back as a hidden
field so the rebuilt table keeps showing the same subset. It is therefore part of
every mutation's request schema rather than a separate concern — an invalid filter
must fail the same way an invalid form field does, not silently widen the list to
everything.

**A blank dropdown means "none", not "invalid".** Both ``workspace_id`` and
``llm_api_key_id`` are nullable columns: an agent may belong to no workspace and
may resolve its model from the user's active keys rather than a named one. That is
what ``OptionalUUID`` encodes, and it is why neither field is required.

Whether the chosen workspace and key actually belong to the caller is checked by
`data_agent_service`, which has the database. This layer only decides whether the
value is a selection at all.
"""

from typing import Optional

from pydantic import Field

from app.schemas.base import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    MAX_PROMPT_LENGTH,
    CheckboxBool,
    FormRequest,
    OptionalText,
    OptionalUUID,
    QueryRequest,
    RequiredText,
    ResponseSchema,
)


class DataAgentFilterMixin(FormRequest):
    """
    The hidden workspace filter carried by every mutation on the agents page.

    A mixin rather than a repeated field so the four mutation schemas cannot
    drift, and so a fifth one cannot forget it — forgetting it silently resets the
    user's filter, which looks like the mutation went to the wrong place.
    """

    workspace_filter: OptionalUUID = Field(default=None, title="Workspace filter")


class DataAgentCreateRequest(DataAgentFilterMixin):
    """The create-agent modal."""

    name: RequiredText = Field(title="Agent name", max_length=MAX_NAME_LENGTH)
    description: OptionalText = Field(
        default=None, title="Description", max_length=MAX_DESCRIPTION_LENGTH
    )
    system_prompt: OptionalText = Field(
        default=None, title="System prompt", max_length=MAX_PROMPT_LENGTH
    )
    workspace_id: OptionalUUID = Field(default=None, title="Workspace")
    llm_api_key_id: OptionalUUID = Field(default=None, title="AI API key")


class DataAgentUpdateRequest(DataAgentCreateRequest):
    """The edit-agent modal — the same fields as create."""


class DataAgentSetActiveRequest(DataAgentFilterMixin):
    """The enable / disable toggle."""

    is_active: CheckboxBool = Field(default=False, title="Active")


class DataAgentDeleteRequest(DataAgentFilterMixin):
    """
    Delete carries no fields of its own — only the filter.

    It still gets a schema: the filter has to be validated on the way in, and a
    handler that reads it raw would be the one place the rule is not enforced.
    """


class DataAgentListQuery(QueryRequest):
    """
    The agents page's own filter, as it arrives in the URL.

    ``?workspace=<uuid>`` is what the agent count on the Workspaces page links to.
    """

    workspace: OptionalUUID = Field(default=None, title="Workspace")


class DataAgentView(ResponseSchema):
    """
    One row of the agents table.

    ``workspace_id`` and ``llm_api_key_id`` are the public uuids of the related
    rows — empty strings when unset, because that is what an unselected
    ``<option value="">`` needs to match for the edit form to preselect correctly.
    """

    uuid: str = Field(title="Agent")
    name: str = Field(title="Name")
    description: Optional[str] = Field(default=None, title="Description")
    system_prompt: Optional[str] = Field(default=None, title="System prompt")
    is_active: bool = Field(default=True, title="Active")
    workspace_id: str = Field(default="", title="Workspace")
    workspace_name: Optional[str] = Field(default=None, title="Workspace name")
    llm_api_key_id: str = Field(default="", title="AI API key")
    llm_api_key_label: Optional[str] = Field(default=None, title="AI API key label")
    tool_count: int = Field(default=0, title="Tools")
