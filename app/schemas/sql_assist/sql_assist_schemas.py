"""
app/schemas/sql_assist/sql_assist_schemas.py

Pydantic schemas for Ask AI — plain English in, SQL out, and optionally saved as a
Tool Config.

The panel is a four-step conversation carried entirely in form fields, and that is
what the schemas here are shaped around:

  1. ``SqlAssistGenerateRequest`` — write a query, or refine the last one.
  2. ``SqlAssistToolFormRequest`` — express the generated SQL as a Tool Config.
  3. ``SqlAssistCreateToolRequest`` — save it.

Each step re-posts the context of the one before (which datasource, which tables,
which model, the host page's agent filter). That echo is deliberate: nothing is
held server-side between steps, so a tampered value is just another value the
services validate. :class:`SqlAssistEchoMixin` is that echo, declared once so the
three steps cannot disagree about what travels between them.

``table_names`` is the field to be careful with. It arrives as repeated form keys
from a multi-select, so it is listed in ``multi_fields`` — read with a plain
``get`` it would yield one table, and a query would be generated against a schema
narrower than the user selected without anything saying so.

The generated SQL itself is *not* validated here beyond its length. It is written
by a language model, and whether it is safe to run is decided by
``sql_assist_service`` against the reflected schema — a regex over SQL would be a
false reassurance.
"""

from typing import List, Optional

from pydantic import Field, field_validator

from app.schemas.base import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    FormRequest,
    IdentifierName,
    JsonObjectField,
    ObjectName,
    OptionalText,
    OptionalUUID,
    QueryRequest,
)
from app.db.db_utils import MAX_REFLECTED_TABLES
from app.models.chatbot import LLM_MODES
from app.models.tool_configs import QUERY_MODE_BUILDER, QUERY_MODE_VALUES

#: The two ways a model is chosen: the user's own key, or the in-built local model.
LLM_MODE_VALUES: frozenset[str] = frozenset(value for value, _ in LLM_MODES)

#: Matches ``sql_assist_service._MAX_PROMPT_LEN``.
MAX_SQL_PROMPT_LENGTH = 2000

#: One prompt covers at most this many tables. Taken from the reflection cap
#: itself, so the schema can never accept more tables than the schema-reader will
#: actually read — asking for more is refused rather than silently trimmed, or a
#: query would be generated against a schema the user thought was bigger.
MAX_SQL_TABLES = MAX_REFLECTED_TABLES

#: Generated SQL is bounded so a runaway completion cannot be posted back as a
#: multi-megabyte hidden field.
MAX_SQL_LENGTH = 20_000

#: The refinement history, as the JSON the panel carries between turns.
MAX_HISTORY_LENGTH = 200_000


class SqlAssistEchoMixin(FormRequest):
    """
    The context every step of the panel hands to the next.

    ``llm_mode`` is validated but ``llm_api_key_id`` is not required with it: the
    key is only needed in ``api_key`` mode, and which key to use in that mode is
    resolved — and refused if absent — by ``sql_assist_service``, which can also
    say *why* (no active key, no model name on the key).
    """

    multi_fields = ("table_names",)

    datasource_id: OptionalUUID = Field(default=None, title="Datasource")
    llm_mode: str = Field(default="", title="Model choice")
    llm_api_key_id: OptionalUUID = Field(default=None, title="AI API key")
    agent_filter: OptionalUUID = Field(default=None, title="Agent filter")
    table_names: List[ObjectName] = Field(
        default_factory=list, title="Tables", max_length=MAX_SQL_TABLES
    )

    @field_validator("llm_mode")
    @classmethod
    def validate_llm_mode(cls, v: str) -> str:
        """
        Blank is allowed and means "not chosen yet" — the service reports that with
        its own wording. A value that is present but unknown is refused here.
        """
        if v and v not in LLM_MODE_VALUES:
            raise ValueError("Model choice is not one of the available options")
        return v

    def echo(self) -> dict:
        """
        The echo fields as the templates expect them — strings, blank when unset.

        The partials put these straight into hidden inputs, so a ``None`` would be
        rendered as the text "None" and posted back as such on the next step.
        """
        return {
            "datasource_id": str(self.datasource_id) if self.datasource_id else "",
            "llm_mode": self.llm_mode,
            "llm_api_key_id": str(self.llm_api_key_id) if self.llm_api_key_id else "",
            "agent_filter": str(self.agent_filter) if self.agent_filter else "",
        }


class SqlAssistFormQuery(QueryRequest):
    """``?agent=<uuid>`` — the host page's filter, so a tool created here lands on it."""

    agent: OptionalUUID = Field(default=None, title="Data agent")


class SqlAssistTablesQuery(QueryRequest):
    """The datasource → tables cascade."""

    datasource_id: OptionalUUID = Field(default=None, title="Datasource")


class SqlAssistGenerateRequest(SqlAssistEchoMixin):
    """
    One attempt at a query, or a refinement of the last one.

    ``history_json`` carries the conversation so far. It is kept as raw text rather
    than parsed here because the service owns its shape and re-echoes it verbatim
    through a *failed* turn — which is what stops a refinement that times out from
    resetting the whole session.
    """

    prompt: str = Field(title="Prompt", min_length=1, max_length=MAX_SQL_PROMPT_LENGTH)
    history_json: str = Field(default="", title="History", max_length=MAX_HISTORY_LENGTH)


class SqlAssistToolFormRequest(SqlAssistEchoMixin):
    """
    Convert the generated query into a Tool Config draft.

    A POST rather than a GET because the SQL is too long for a query string and
    because converting it re-reads the schema.
    """

    sql: str = Field(title="SQL", min_length=1, max_length=MAX_SQL_LENGTH)
    history_json: str = Field(default="", title="History", max_length=MAX_HISTORY_LENGTH)


class SqlAssistCreateToolRequest(SqlAssistEchoMixin):
    """
    Save the drafted Tool Config, in whichever mode it was drafted.

    ``query_mode`` says which of the two query fields is meant: ``config_json``
    (the builder's shape) or ``sql_query`` (the statement as generated). Both
    travel back in hidden fields and both go through ``tool_config_service``'s own
    validation on the way in — the same gates the Tool Configs form passes — so
    nothing here trusts what the browser posted, including the mode.
    """

    data_agent_id: OptionalUUID = Field(default=None, title="Data agent")
    tool_name: IdentifierName = Field(title="Tool name")
    table_name: ObjectName = Field(title="Table name", max_length=MAX_NAME_LENGTH)
    description: OptionalText = Field(
        default=None, title="Description", max_length=MAX_DESCRIPTION_LENGTH
    )
    query_mode: str = Field(default=QUERY_MODE_BUILDER, title="Query mode")
    config_json: JsonObjectField = Field(default_factory=dict, title="Query")
    sql_query: str = Field(default="", title="SQL query", max_length=MAX_SQL_LENGTH)
    preview: Optional[str] = Field(
        default=None, title="Preview", max_length=MAX_SQL_LENGTH
    )

    @field_validator("query_mode")
    @classmethod
    def validate_query_mode(cls, v: str) -> str:
        """Blank means the builder, matching the Tool Configs form's own default."""
        mode = (v or "").strip().lower() or QUERY_MODE_BUILDER

        if mode not in QUERY_MODE_VALUES:
            raise ValueError("Query mode is not one of the available options")

        return mode
