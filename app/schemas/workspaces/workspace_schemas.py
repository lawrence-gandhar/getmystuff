"""
app/schemas/workspaces/workspace_schemas.py

Pydantic schemas for the Workspaces module.

A workspace is a grouping for data agents and holds nothing but a name, a
description and an active flag — so these schemas are the simplest in the
application, and they are the reference the other feature packages follow: one
request schema per handler, one view schema per thing the templates render, and
no rule that needs the database.

The uniqueness of a workspace name per user is *not* here. It needs a query, so
it stays in `workspace_service._assert_name_available`, which also handles the
race the unique index catches.
"""

from typing import Optional

from pydantic import Field

from app.schemas.base import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    CheckboxBool,
    FormRequest,
    OptionalText,
    RequiredText,
    ResponseSchema,
)


class WorkspaceCreateRequest(FormRequest):
    """The create-workspace modal."""

    name: RequiredText = Field(title="Workspace name", max_length=MAX_NAME_LENGTH)
    description: OptionalText = Field(
        default=None, title="Description", max_length=MAX_DESCRIPTION_LENGTH
    )


class WorkspaceUpdateRequest(WorkspaceCreateRequest):
    """
    The edit-workspace modal — the same fields as create.

    Subclassed rather than aliased so the two can diverge without every caller
    changing, which is the same reason the datasource pair exists separately.
    """


class WorkspaceSetActiveRequest(FormRequest):
    """The archive / restore toggle."""

    is_active: CheckboxBool = Field(default=False, title="Active")


class WorkspaceView(ResponseSchema):
    """
    One row of the workspaces table.

    ``agent_count`` is what the table links to Data Agents with, filtered to this
    workspace — the agents themselves belong to their own module and are not
    listed here.
    """

    uuid: str = Field(title="Workspace")
    name: str = Field(title="Name")
    description: Optional[str] = Field(default=None, title="Description")
    is_active: bool = Field(default=True, title="Active")
    agent_count: int = Field(default=0, title="Agents")
