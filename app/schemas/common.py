"""
app/schemas/common.py

Response shapes that belong to no single feature.

Two kinds live here:

**The status envelope.** ``{"status": ..., "message": ...}`` is the error and
success format CLAUDE.md specifies for the whole application. Declaring it once
means a handler cannot accidentally send ``{"error": ...}`` or ``{"detail": ...}``
instead, which is how a client ends up with three code paths for one outcome.

**The choice view.** Every dropdown in this application is fed the same triple —
a public uuid, a label, and whether the row is still active — because a form must
be able to show an archived selection that is already saved rather than dropping
it silently. Several services build that list independently; this is the shape
they all have to produce.

Nothing here holds a bigint ``id``. A response schema exposes ``uuid`` only, per
the identifier rule in CLAUDE.md.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import Field

from app.schemas.base import ResponseSchema

#: The two values ``status`` may take, application-wide.
StatusValue = Literal["success", "error"]


class StatusResponse(ResponseSchema):
    """
    The standard JSON envelope for an outcome with no data of its own.

    Used for a delete, a toggle, or any other endpoint whose whole answer is
    "that worked" / "that didn't, and here is why in a sentence".
    """

    status: StatusValue = Field(title="Status")
    message: str = Field(title="Message")

    @classmethod
    def success(cls, message: str) -> "StatusResponse":
        return cls(status="success", message=message)

    @classmethod
    def error(cls, message: str) -> "StatusResponse":
        return cls(status="error", message=message)


class ErrorResponse(ResponseSchema):
    """
    An error on its own, for endpoints whose success body has no ``status`` key.

    Kept separate from :class:`StatusResponse` because the widget and knowledge
    base endpoints answer a success with their own payload shape and an error with
    just a message — merging the two would make every success field optional.
    """

    message: str = Field(title="Message")

    @classmethod
    def of(cls, message: str) -> "ErrorResponse":
        return cls(message=message)


class ChoiceView(ResponseSchema):
    """
    One option in a dropdown: what to submit, what to show, and whether it is
    still active.

    ``is_active`` is not cosmetic. An archived workspace or a disabled agent stays
    in the list so a row already pointing at it can be edited without being
    silently moved off it — the flag is how the template marks that.
    """

    uuid: str = Field(title="Selection")
    name: str = Field(title="Name")
    is_active: bool = Field(default=True, title="Active")


class LabelledChoiceView(ResponseSchema):
    """
    A choice whose display text is a label rather than a name — the AI Settings
    keys, where the provider is shown alongside the user's own label for the key.
    """

    uuid: str = Field(title="Selection")
    label: str = Field(title="Label")
    provider: str = Field(default="", title="Provider")


class DatasourceChoiceView(ChoiceView):
    """
    A datasource option, plus the two facts a query builder needs before it has
    fetched anything: which engine it is, and whether that engine can join.
    """

    db_type: str = Field(default="", title="Database type")
    supports_joins: bool = Field(default=False, title="Supports joins")


class FragmentResponse(ResponseSchema):
    """
    The context every HTMX mutation fragment in this application is rendered with.

    The pattern is uniform across Workspaces, Data Agents, Tool Configs, Actions,
    Flow Builder and AI Settings: a mutation answers with the rebuilt list plus a
    marker saying whether it worked, and the modal's ``after-request`` hook closes
    itself on the success marker. Declaring the marker here is what keeps the six
    of them literally the same contract rather than six similar dicts.

    ``error`` being ``None`` *is* the success signal — that is the existing
    convention, kept rather than replaced so no template has to change.
    """

    error: Optional[str] = Field(default=None, title="Error")

    @property
    def succeeded(self) -> bool:
        return self.error is None

    def context(self, **extra: Any) -> dict[str, Any]:
        """
        This fragment as a template context.

        ``model_dump()`` rather than :meth:`payload` because a template consumes
        Python values — a ``datetime`` should stay a ``datetime`` so Jinja can
        format it, where a JSON body would need it stringified.
        """
        return {**self.model_dump(), **extra}
