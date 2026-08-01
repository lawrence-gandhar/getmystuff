"""
app/schemas/ai_analytics/ai_analytics_schemas.py

Pydantic schemas for AI Analytics — the "Ask AI" prompt that runs against a
datasource target, and its per-target history.

``target_type`` and ``target_name`` travel together and mean different things
depending on the pair: a ``file`` target is identified by ``file_id``, a ``table``
or ``collection`` by ``target_name``, and ``datasource`` means "everything in it"
and needs neither. The cross-field rule is checked in one place
(:meth:`AiAnalyticsGenerateRequest.check_target`) so a half-specified target is
refused here rather than resolving to a different target than the user picked.

The prompt cap is the same one Ask AI and the Deep Agents console use. The prompt
is sent to a language model, so its length is a cost, not just a column width.
"""

from pydantic import Field, field_validator, model_validator

from app.schemas.base import (
    MAX_NAME_LENGTH,
    FormRequest,
    ObjectName,
    OptionalUUID,
    QueryRequest,
    RequiredText,
)

#: The four scopes a prompt can be aimed at. Mirrors
#: `ai_analytics_service._VALID_TARGET_TYPES` — the service still enforces it,
#: since it is reachable from more than one route.
TARGET_TYPES: frozenset[str] = frozenset({"datasource", "file", "table", "collection"})

#: Target types identified by a name rather than by a file id.
NAMED_TARGET_TYPES: frozenset[str] = frozenset({"table", "collection"})

#: Matches `sql_assist_service._MAX_PROMPT_LEN` and the Deep Agents console.
MAX_ANALYTICS_PROMPT_LENGTH = 2000


class AiAnalyticsGenerateRequest(FormRequest):
    """
    One analytics prompt against one target.

    ``target_name`` is an ``ObjectName``: it is a table or collection name in the
    user's own database and is interpolated into a generated query rather than
    bound as a parameter, so it is held to the identifier-safe character set even
    though it came from a dropdown. It is required for every target type — that is
    what ``generate_analytics`` already enforces, restated here so the failure
    arrives before any work is done.
    """

    target_type: RequiredText = Field(title="Target type")
    target_name: ObjectName = Field(title="Target name", max_length=MAX_NAME_LENGTH)
    prompt: str = Field(
        title="Prompt", min_length=1, max_length=MAX_ANALYTICS_PROMPT_LENGTH
    )
    file_id: OptionalUUID = Field(default=None, title="File")

    @field_validator("target_type")
    @classmethod
    def validate_target_type(cls, v: str) -> str:
        if v not in TARGET_TYPES:
            raise ValueError(
                "Target type must be one of datasource, file, table or collection"
            )
        return v

    @model_validator(mode="after")
    def check_target(self) -> "AiAnalyticsGenerateRequest":
        """
        A ``file`` target must say *which* file.

        Without this the request reached ``_load_one_target``, which refused it
        with "file_id is required for file targets" — accurate, but phrased for a
        developer and arriving only after the datasource had been loaded and a
        history row written.
        """
        if self.target_type == "file" and self.file_id is None:
            raise ValueError("Please choose which file to analyse")

        return self


class AiAnalyticsHistoryQuery(QueryRequest):
    """
    The prompt history for one target.

    Both fields default to empty: the history panel is opened before a target is
    chosen, and "no target" means "no history yet" rather than a bad request.
    """

    target_type: str = Field(default="", title="Target type")
    target_name: str = Field(default="", title="Target name", max_length=MAX_NAME_LENGTH)

    @field_validator("target_type")
    @classmethod
    def validate_target_type(cls, v: str) -> str:
        if v and v not in TARGET_TYPES:
            raise ValueError(
                "Target type must be one of datasource, file, table or collection"
            )
        return v
