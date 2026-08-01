"""
Ask AI — turn a plain-English request into a SQL query for one of the user's own
relational datasources.

The rule the whole module is built around: **the model is shown structure, never
data.** What it receives is the reflected schema of the tables the user picked —
table names, column names and types, primary keys, foreign keys — and nothing else.
No row is sampled, no count is taken, and the query that comes back is *not* run
(see :func:`generate_sql`); it is handed to the user to read, refine and use. A
refinement turn re-sends the same schema plus the conversation so far, so a
follow-up cannot reach any further into the datasource than the first attempt did.

That is a different contract from ai_analytics_service, which deliberately profiles
real rows to answer questions *about* the data. This module answers a question about
the *schema*, so it is a separate service rather than another mode of that one — but
it reuses that module's provider plumbing (:func:`answer_structured`), so "in-built
LLM or one of my saved API keys" behaves identically to everywhere else in the app.

It is also its own feature rather than part of Tool Configs, which is where the panel
is opened from today: generating SQL from a schema needs a datasource and nothing
else, so any page with one in view can call it.

The schema itself is read by reflection, never by a query this application wrote:
:func:`app.db.db_utils.fetch_rdbms_metadata` goes through SQLAlchemy's Inspector.

SQL only — so only relational datasources are offered. A CSV or a Mongo collection
has no SQL to generate (they are queried through pandas and aggregation pipelines
respectively), and the reflection this relies on is a relational concept.
"""

import json
import re
import uuid
from typing import Any, List, Optional, Tuple

from litestar.exceptions import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import MAX_REFLECTED_TABLES, CRUDQueryBuilder
from app.models.datasource import DataSource
from app.models.tool_configs import (
    AGGREGATION_FUNCTIONS,
    FILTER_OPERATORS,
    ToolConfig,
)
from app.services.ai_analytics.ai_analytics_service import answer_structured
from app.services.data_agents import data_agent_service
from app.services.datasource import metadata_service
from app.services.tool_configs import tool_config_service
from app.utils.query_joins import (
    RDBMS_DB_TYPES,
    join_types_for,
    query_tables,
    supports_joins,
)
from app.utils.validators import require_object_name

datasource_crud = CRUDQueryBuilder(DataSource)

# Same values and wording as the chatbot's own language-model choice
# (app.models.chatbot.LLM_MODES): "use whichever of my saved AI Settings keys
# applies" versus "use the app's local Ollama model". Spelled out here rather than
# imported so this module doesn't depend on the chatbot feature — the concept is
# shared, the two features are not.
LLM_MODES = (
    ("api_key", "My LLM API key"),
    ("in_built", "In-built LLM"),
)
_VALID_LLM_MODES = frozenset(value for value, _ in LLM_MODES)

_MAX_PROMPT_LEN = 2000

# One prompt covers at most this many tables. Reflection is capped at the same
# number in db_utils; asking for more is refused rather than trimmed, so a query
# is never generated against a schema the user thought included more than it did.
_MAX_TABLES = MAX_REFLECTED_TABLES

# How much of the conversation a refinement carries. Bounded because it all becomes
# prompt: the model needs the last few attempts to improve on them, not the whole
# session.
_MAX_HISTORY_TURNS = 6
_MAX_HISTORY_SQL_LEN = 4000

# A generated statement longer than this is rejected rather than displayed — at that
# length something has gone wrong with the response, not with the request.
_MAX_SQL_LEN = 8000

# Verbs that make a statement more than a read *from a position a read could reach*:
# `WITH … INSERT`, `SELECT … INTO`, and the DDL a model might append. Checked after
# string literals and comments are stripped out (see _strip_literals), so a WHERE
# clause comparing against the text 'delete' is not mistaken for a DELETE.
#
# Deliberately not a list of every dangerous word. PRAGMA, COPY, CALL, SET, VACUUM
# and friends are only valid at the start of a statement, which _READ_START_RE
# already refuses, or after a `;`, which is refused separately — listing them here
# would add nothing but false rejections of valid queries (a column named `call`, a
# table named `copy`).
_WRITE_KEYWORDS = (
    "insert", "update", "delete", "into", "drop", "alter", "create", "truncate",
    "replace", "merge", "grant", "revoke",
)
_WRITE_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(_WRITE_KEYWORDS) + r")\b", re.IGNORECASE,
)

# A read starts here. WITH is allowed because a CTE is the natural shape for the
# kind of query this feature produces; a WITH that goes on to write is caught by
# the keyword check above.
_READ_START_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)

# Quoted spans and comments, removed before the checks above look for `;` and write
# verbs. Ordered longest-first so `--` inside a string is not treated as a comment.
_LITERAL_RE = re.compile(
    r"'(?:[^']|'')*'"       # single-quoted string, '' being an escaped quote
    r"|\"(?:[^\"]|\"\")*\""  # double-quoted identifier
    r"|`[^`]*`"              # MySQL backtick identifier
    r"|/\*.*?\*/"            # block comment
    r"|--[^\n]*",            # line comment
    re.DOTALL,
)

# What the SQL is written for, by datasource type. Only used to tell the model which
# dialect to target.
_DIALECT_NAMES = {
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "sqlite": "SQLite",
}


class ToolDraftColumn(BaseModel):
    column: str = Field(description="A column from the schema, exactly as named there.")
    alias: str = Field(default="", description="Optional AS alias; empty for none.")


class ToolDraftAggregation(BaseModel):
    type: str = Field(description="One of: count, sum, avg, min, max.")
    column: str = Field(description="The column being aggregated.")
    alias: str = Field(default="", description="Optional AS alias; empty for none.")


class ToolDraftFilter(BaseModel):
    column: str = Field(description="The column being compared.")
    operator: str = Field(description="One of: =, !=, >, <, LIKE.")
    value: str = Field(description="The literal value to compare against.")


class ToolDraftJoin(BaseModel):
    type: str = Field(description="One of: inner, left, right, full.")
    table: str = Field(description="The table being joined in.")
    left_table: str = Field(
        description="The table it matches against — the base table, or a table "
        "joined before it.",
    )
    left_column: str = Field(description="Column on left_table, unqualified.")
    right_column: str = Field(description="Column on table, unqualified.")


class ToolDraft(BaseModel):
    """
    A generated query expressed in the shape a Tool Config stores.

    That shape is a builder, not SQL: columns, aggregations, grouping, filters and
    joins. Plenty of valid SQL does not fit it — window functions, HAVING, ORDER BY,
    LIMIT, subqueries, CASE, unions, expressions in the SELECT list — so ``fits`` is
    a real answer the model is expected to give, with ``reason`` naming what is in
    the way. Guessing an approximation of a query the user already read would be far
    worse than declining.
    """

    fits: bool = Field(
        description="True only if the query is fully expressible with columns, "
        "aggregations, group_by, filters and joins, with nothing left over.",
    )
    reason: str = Field(
        default="",
        description="When fits is false, the specific SQL feature that does not fit "
        "— e.g. 'uses ORDER BY and LIMIT', 'has a HAVING clause'. Empty when it fits.",
    )
    tool_name: str = Field(
        default="",
        description="A suggested lowercase identifier for the tool, from the "
        "question it answers — e.g. units_sold_per_product.",
    )
    description: str = Field(
        default="",
        description="One sentence on what this tool answers, for the agent to decide "
        "when to call it.",
    )
    table: str = Field(
        default="",
        description="The base table the query reads FROM (not a joined one).",
    )
    columns: List[ToolDraftColumn] = Field(default_factory=list)
    aggregations: List[ToolDraftAggregation] = Field(default_factory=list)
    group_by: List[str] = Field(default_factory=list)
    filters: List[ToolDraftFilter] = Field(default_factory=list)
    joins: List[ToolDraftJoin] = Field(default_factory=list)


class SqlDraft(BaseModel):
    """
    One attempt at the user's request.

    ``sql`` is empty when the schema cannot answer the question — that is a valid,
    useful answer ("there is no order date column"), not a failure, and
    ``explanation`` carries the reason.
    """

    sql: str = Field(
        description="A single read-only SELECT statement (a leading WITH is fine), "
        "with no trailing semicolon. Empty if the schema cannot answer the request.",
    )
    explanation: str = Field(
        description="Two or three sentences on what the query returns and how it "
        "gets there — or, when sql is empty, what the schema is missing.",
    )
    assumptions: List[str] = Field(
        default_factory=list,
        description="Up to 5 short notes on anything guessed rather than known "
        "from the schema — a join inferred without a foreign key, a column read as "
        "a date, an ambiguous word in the request.",
    )


# --------------------------------------------------------------------------
# Read — what the form offers
# --------------------------------------------------------------------------

async def get_datasource_choices(db: AsyncSession, user_id: int) -> List[dict]:
    """
    The user's relational datasources, as {uuid, name, is_active, dialect}.

    Filtered rather than flagged: a file or collection datasource has no SQL to
    generate, so offering one would only produce an error on submit.
    """
    datasources = await datasource_crud.get_many(
        db, filters={"user_id": user_id}, order_by="datasource_name",
    )

    return [
        {
            "uuid": str(datasource.uuid),
            "name": datasource.datasource_name,
            "is_active": datasource.is_active,
            "dialect": _dialect_name(datasource.db_type),
        }
        for datasource in datasources
        if datasource.db_type in RDBMS_DB_TYPES
    ]


async def get_table_choices(
    db: AsyncSession,
    user_id: int,
    datasource_id: uuid.UUID,
) -> List[str]:
    """
    The tables and views in one datasource, read by reflection — the same source the
    generated query's schema comes from, so the picker cannot offer a table the
    model would then not be shown.
    """
    datasource = await _resolve_datasource(db, user_id, datasource_id)

    try:
        return await metadata_service.get_rdbms_reflected_tables(datasource)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Could not read the schema of '{datasource.datasource_name}'. "
                "Check the datasource connection and try again."
            ),
        ) from exc


async def get_llm_key_choices(db: AsyncSession, user_id: int) -> List[dict]:
    """
    The user's saved AI keys for the model picker. Reuses the Data Agents module's
    shaping so the same dropdown appears identically in both places.
    """
    return await data_agent_service.get_llm_key_choices(db, user_id)


# --------------------------------------------------------------------------
# Generate
# --------------------------------------------------------------------------

async def generate_sql(
    db: AsyncSession,
    user_id: int,
    datasource_id: Optional[uuid.UUID],
    table_names: List[str],
    prompt: str,
    llm_mode: str,
    llm_api_key_id: Optional[uuid.UUID] = None,
    history_json: Optional[str] = None,
) -> dict:
    """
    Generate (or refine) a SQL query for the chosen tables and return it alongside
    the conversation it belongs to.

    The result is a draft to read, not a query that has been run: nothing here
    executes the statement, which is what keeps "the AI never sees your data" true
    of the whole feature and not just of the prompt.

    Returns ``{"draft": SqlDraft, "history": [{"prompt", "sql"}, ...], "dialect",
    "tables"}`` — the history includes this turn, ready to be posted back with the
    next refinement.
    """
    prompt = _validated_prompt(prompt)
    history = _validated_history(history_json)
    forced_key_uuid, use_inbuilt_llm = _validated_llm_choice(llm_mode, llm_api_key_id)

    datasource = await _resolve_datasource(db, user_id, datasource_id)
    metadata = await _load_metadata(datasource, _validated_tables(table_names))

    dialect = _dialect_name(datasource.db_type)
    system_prompt, user_content = _build_prompts(dialect, metadata, prompt, history)

    draft = await answer_structured(
        db,
        user_id,
        system_prompt,
        user_content,
        SqlDraft,
        forced_key_uuid=forced_key_uuid,
        use_inbuilt_llm=use_inbuilt_llm,
    )

    draft.sql = _validated_sql(draft.sql)
    draft.assumptions = draft.assumptions[:5]

    return {
        "draft": draft,
        "history": history + [{"prompt": prompt, "sql": draft.sql}],
        "dialect": dialect,
        "tables": [entry["table"] for entry in metadata],
    }


# --------------------------------------------------------------------------
# Auto Create Tool — express the generated query as a Tool Config
# --------------------------------------------------------------------------

async def draft_tool_config(
    db: AsyncSession,
    user_id: int,
    datasource_id: Optional[uuid.UUID],
    table_names: List[str],
    sql: str,
    llm_mode: str,
    llm_api_key_id: Optional[uuid.UUID] = None,
) -> dict:
    """
    Express a generated query in the shape a Tool Config stores, ready to create.

    A second, deliberately narrow AI call rather than one more field on
    :class:`SqlDraft`. Two reasons: it only costs anything when the user actually
    asks for a tool, and converting one known query into one known schema is a far
    smaller task than writing SQL — which matters most for the in-built local model
    (a 1.7B parameter model, see AI_INBUILT.md), where a single request producing
    prose, SQL, assumptions *and* a nested builder config is exactly the kind of
    prompt that comes back malformed.

    The model's answer is then held to the same validator the query builder's own
    output is held to (:func:`tool_config_service.validated_query_config`), so a
    config from here is exactly as trustworthy as a hand-built one — and reopening it
    in the builder shows what was created, not an approximation of it.

    Returns ``{"fits", "reason", "tool_name", "description", "table", "config",
    "config_json", "preview"}``. ``fits`` false means the query cannot be represented
    and ``reason`` says what is in the way; the caller shows that instead of a form.
    """
    tables = _validated_tables(table_names)
    sql = _validated_sql(sql)
    forced_key_uuid, use_inbuilt_llm = _validated_llm_choice(llm_mode, llm_api_key_id)

    if not sql:
        raise HTTPException(
            status_code=400,
            detail="There is no query to turn into a tool yet — generate one first.",
        )

    datasource = await _resolve_datasource(db, user_id, datasource_id)
    metadata = await _load_metadata(datasource, tables)

    system_prompt, user_content = _build_tool_prompts(
        _dialect_name(datasource.db_type), datasource.db_type, metadata, sql,
    )

    draft = await answer_structured(
        db,
        user_id,
        system_prompt,
        user_content,
        ToolDraft,
        forced_key_uuid=forced_key_uuid,
        use_inbuilt_llm=use_inbuilt_llm,
    )

    if not draft.fits:
        return {
            "fits": False,
            "reason": draft.reason.strip() or (
                "This query uses SQL the tool builder cannot represent."
            ),
        }

    return _validated_tool_draft(draft, datasource, tables, metadata)


def _validated_tool_draft(
    draft: ToolDraft,
    datasource: DataSource,
    tables: List[str],
    metadata: List[dict],
) -> dict:
    """
    Turn the model's answer into a config the Tool Configs form would have produced.

    Two things happen here that the model cannot be trusted to get right on its own:

    * The base table is checked against the tables actually selected. A base table
      the user never picked would produce a tool reading something they did not
      choose.
    * Every column reference is resolved against the reflected schema — see
      :func:`_reference_resolver`. The builder writes ``table.column`` once a query has
      a join and a bare name otherwise (see QUERY_JOINS.md), and a config that
      disagrees would reopen in the builder with its dropdowns unable to match what it
      holds. Resolving from the schema rather than assuming makes "Edit shows what was
      created" a guarantee.
    """
    base_table = require_object_name(draft.table or tables[0], "Base table")

    if base_table not in tables:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The AI put the query on table '{base_table}', which is not one of "
                "the tables you selected. Regenerate, or create the tool by hand."
            ),
        )

    joins = [join.model_dump() for join in draft.joins]
    resolve = _reference_resolver(joins, base_table, metadata)

    raw_config = {
        "columns": [
            {"column": resolve(column.column, "Column"), "alias": column.alias}
            for column in draft.columns
        ],
        "aggregations": [
            {
                "type": aggregation.type,
                "column": resolve(aggregation.column, "Aggregation column"),
                "alias": aggregation.alias,
            }
            for aggregation in draft.aggregations
        ],
        "group_by": [resolve(column, "Group by column") for column in draft.group_by],
        "filters": [
            {
                "column": resolve(entry.column, "Filter column"),
                "operator": entry.operator,
                "value": entry.value,
            }
            for entry in draft.filters
        ],
        "joins": joins,
    }

    # The same gate the form goes through — join chain, aggregation functions,
    # operators, every identifier, every list length.
    config = tool_config_service.validated_query_config(
        json.dumps(raw_config), base_table, datasource.db_type,
    )

    return {
        "fits": True,
        "reason": "",
        "tool_name": draft.tool_name.strip().lower(),
        "description": draft.description.strip(),
        "table": base_table,
        "config": config,
        "config_json": json.dumps(config, indent=2),
        # Built from the validated config, not from the SQL the model was given, so
        # the panel previews what the tool will actually hold.
        "preview": tool_config_service.build_query_preview(config, base_table),
    }


def _reference_resolver(joins: List[dict], base_table: str, metadata: List[dict]):
    """
    A function that resolves one column reference against the reflected schema and
    returns it in the form the builder writes.

    A bare name in a joined query is **looked up, not assumed**. Qualifying it with
    the base table would be a guess, and a wrong guess is the worst possible outcome
    here: the tool would be created, would validate, would open in the builder, and
    would quietly answer a different question than the SQL the user approved. So:

    * already qualified — kept, and checked to be a real column of a table the query
      reads;
    * bare, and exactly one of the query's tables has that column — qualified with
      that table (or left bare when there are no joins, matching the builder);
    * bare, and several tables have it — rejected as ambiguous, naming them;
    * in no table at all — rejected, because the model invented it.

    The last two are worth an error rather than a best effort. The user has the query
    in front of them and can refine it or build the tool by hand; what they cannot do
    is notice that a saved tool silently reads the wrong table's column.
    """
    columns_by_table = {
        entry["table"]: [column["name"] for column in entry["columns"]]
        for entry in metadata
    }
    joined = query_tables(joins, base_table)
    in_query = joined or [base_table]

    def resolve(reference: Any, field_label: str) -> str:
        name = require_object_name(reference, field_label)

        table, separator, column = name.partition(".")
        if separator:
            if table not in in_query:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{field_label} '{name}' is on table '{table}', which this "
                        "query does not read. Regenerate, or create the tool by hand."
                    ),
                )
            if column not in columns_by_table.get(table, []):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{field_label} '{name}' is not a column of '{table}'. "
                        "Regenerate, or create the tool by hand."
                    ),
                )
            return name

        owners = [
            table_name for table_name in in_query
            if name in columns_by_table.get(table_name, [])
        ]

        if not owners:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{field_label} '{name}' is not a column of any table this query "
                    "reads. Regenerate, or create the tool by hand."
                ),
            )

        if len(owners) > 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{field_label} '{name}' exists in {' and '.join(owners)}, so it "
                    "is ambiguous. Regenerate the query with the table spelled out, "
                    "or create the tool by hand."
                ),
            )

        # Bare while the query reads one table, qualified once it joins — exactly what
        # the builder itself writes.
        return name if not joined else f"{owners[0]}.{name}"

    return resolve


def _build_tool_prompts(
    dialect: str,
    db_type: Any,
    metadata: List[dict],
    sql: str,
) -> Tuple[str, str]:
    """
    The (system, user) pair for the conversion.

    The available join types are listed from the dialect's own set, so the model is
    never invited to name one this database cannot run.
    """
    join_types = ", ".join(value for value, _ in join_types_for(db_type)) or "none"
    aggregations = ", ".join(value for value, _ in AGGREGATION_FUNCTIONS)
    operators = ", ".join(FILTER_OPERATORS)

    system_prompt = (
        f"You convert a {dialect} SELECT statement into the structured query "
        "configuration used by the GetMyStuff tool builder.\n\n"
        "You are given the schema metadata and the query. You have not seen a row of "
        "this database, and this task needs none.\n\n"
        "The configuration can express exactly five things and nothing else:\n"
        f"- columns: plain column selections, with an optional alias\n"
        f"- aggregations: {aggregations}, over one column, with an optional alias\n"
        "- group_by: a list of columns\n"
        f"- filters: column, operator ({operators}), literal value — combined with AND\n"
        f"- joins: type ({join_types}), the table joined in, and one equality "
        "matching a column on a table already in the query against a column on it\n\n"
        "Set fits=false, and name what is in the way in reason, whenever the query "
        "needs anything else: ORDER BY, LIMIT, HAVING, DISTINCT, a subquery, a CTE, a "
        "window function, CASE, UNION, an expression or function in the SELECT list, "
        "OR between filters, a non-equality join condition, or a filter compared "
        "against another column instead of a literal. Do not approximate — a tool "
        "that quietly differs from the query is worse than no tool.\n\n"
        "When it does fit:\n"
        "- Use column names exactly as the schema spells them.\n"
        "- table is the table in the FROM clause, never a joined one.\n"
        "- List joins in the order they can be applied: each one may only match "
        "against the base table or a table joined before it.\n"
        "- Give unqualified column names; the caller adds table prefixes.\n"
        "- Suggest tool_name as a lowercase identifier (letters, digits, "
        "underscores, starting with a letter) naming what the query answers.\n"
        "- Write description as one sentence an agent can use to decide when to "
        "call this tool."
    )

    user_content = (
        f"Schema metadata (JSON):\n{json.dumps(metadata, default=str)}\n\n"
        f"Query to convert:\n{sql}"
    )

    return system_prompt, user_content


async def create_tool_from_draft(
    db: AsyncSession,
    user_id: int,
    datasource_id: Optional[uuid.UUID],
    agent_id: Optional[uuid.UUID],
    tool_name: str,
    table_name: str,
    description: Optional[str],
    config_json: Optional[str],
) -> ToolConfig:
    """
    Create the Tool Config the panel just drafted.

    Goes through ``tool_config_service.create_tool_config`` rather than writing the
    row here, so an AI-created tool is subject to every rule a hand-made one is:
    agent and datasource ownership, the per-agent unique name, and the full query
    config validation. Nothing about this row records that an AI drafted it — there is
    no second kind of tool config to maintain.
    """
    return await tool_config_service.create_tool_config(
        db,
        user_id,
        agent_id=agent_id,
        datasource_id=datasource_id,
        tool_name=tool_name,
        table_name=table_name,
        description=description,
        config_json=config_json,
    )


async def get_agent_choices(db: AsyncSession, user_id: int) -> List[dict]:
    """
    The user's agents for the create form. A tool config belongs to exactly one agent
    — ``data_agents.id`` is a NOT NULL foreign key — so this is a required choice, not
    a convenience.
    """
    return await data_agent_service.get_agent_choices(db, user_id)


# --------------------------------------------------------------------------
# Prompt building
# --------------------------------------------------------------------------

def _build_prompts(
    dialect: str,
    metadata: List[dict],
    prompt: str,
    history: List[dict],
) -> Tuple[str, str]:
    """
    Assemble the (system, user) pair.

    The system prompt states the one hard fact about this feature — that the model
    has been given structure and nothing else — because a model that believes it has
    seen the data will happily describe rows that do not exist.
    """
    system_prompt = (
        f"You write SQL for a {dialect} database, inside the GetMyStuff analytics "
        "platform.\n\n"
        "You are given ONLY schema metadata: table names, column names and their "
        "types, primary keys and foreign keys. You have not seen a single row of "
        "this database and you never will — so never describe, count or quote its "
        "contents, and never claim a result you cannot know.\n\n"
        "Rules:\n"
        "- Use only the tables and columns in the metadata. Never invent one, and "
        "never assume a column exists because it usually would.\n"
        "- If the request cannot be answered from the metadata given, return an "
        "empty sql and say in explanation exactly what is missing.\n"
        "- Produce ONE read-only statement: a SELECT, optionally led by a WITH "
        "clause. Never INSERT, UPDATE, DELETE or any DDL, even if asked.\n"
        "- No trailing semicolon, and no markdown code fences around the SQL.\n"
        "- Join on the foreign keys in the metadata wherever they exist. If you "
        "join without one, say so in assumptions.\n"
        "- Qualify every column with its table once more than one table is "
        "involved.\n"
        "- List the columns you select; avoid SELECT *.\n"
        f"- Write {dialect} syntax, and use LIMIT when the request implies a "
        "top-N or a sample.\n"
        "- Put anything you guessed in assumptions, one short line each."
    )

    user_content = f"Schema metadata (JSON):\n{json.dumps(metadata, default=str)}\n\n"

    if history:
        user_content += (
            "This is a refinement. Earlier turns in this conversation, oldest "
            "first — improve on the most recent query rather than starting over:\n"
        )
        for index, turn in enumerate(history, start=1):
            user_content += f"\n{index}. Asked: {turn['prompt']}\n"
            user_content += f"   Produced:\n{turn['sql'] or '(no query)'}\n"
        user_content += "\n"

    user_content += f"Request: {prompt}"

    return system_prompt, user_content


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _validated_prompt(prompt: str) -> str:
    prompt = (prompt or "").strip()

    if not prompt:
        raise HTTPException(status_code=400, detail="Tell the AI what you need — the prompt is required")

    if len(prompt) > _MAX_PROMPT_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Prompt cannot be longer than {_MAX_PROMPT_LEN} characters",
        )

    return prompt


def _validated_tables(table_names: List[str]) -> List[str]:
    """
    At least one table, each a name safe to reflect, and no more than the cap.

    Over the cap is refused rather than trimmed: silently reflecting the first N
    would generate a query against a schema the user believed was larger.
    """
    names = []
    for name in table_names or []:
        if not (name or "").strip():
            continue
        name = require_object_name(name, "Table")
        if name not in names:
            names.append(name)

    if not names:
        raise HTTPException(
            status_code=400,
            detail="Pick at least one table for the AI to write the query against",
        )

    if len(names) > _MAX_TABLES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Pick at most {_MAX_TABLES} tables — narrow the selection to the "
                "ones the query needs."
            ),
        )

    return names


def _validated_llm_choice(
    llm_mode: str,
    llm_api_key_id: Optional[uuid.UUID],
) -> Tuple[Optional[uuid.UUID], bool]:
    """
    Turn the form's model choice into the two flags answer_structured takes.

    Mutually exclusive by construction: "in_built" calls the local model and ignores
    any key, "api_key" either pins the key given or falls back to whichever key is
    active in AI Settings.
    """
    if llm_mode not in _VALID_LLM_MODES:
        raise HTTPException(status_code=400, detail="Choose which language model to use")

    if llm_mode == "in_built":
        return None, True

    return llm_api_key_id, False


def _validated_history(history_json: Optional[str]) -> List[dict]:
    """
    Rebuild the conversation from the hidden field the previous response wrote.

    Only the two fields that matter are kept, both length-capped, so a refinement
    carries forward a bounded prompt no matter what was posted back.
    """
    raw = (history_json or "").strip()
    if not raw:
        return []

    try:
        turns = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                "The conversation so far could not be read. Close the panel and "
                "start a new question."
            ),
        ) from exc

    if not isinstance(turns, list):
        raise HTTPException(
            status_code=400, detail="Conversation history is not in the expected format",
        )

    history = []
    for turn in turns[-_MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        earlier_prompt = str(turn.get("prompt") or "").strip()[:_MAX_PROMPT_LEN]
        earlier_sql = str(turn.get("sql") or "").strip()[:_MAX_HISTORY_SQL_LEN]
        if earlier_prompt:
            history.append({"prompt": earlier_prompt, "sql": earlier_sql})

    return history


def _validated_sql(sql: str) -> str:
    """
    Check what the model produced is a single read-only statement before it is shown.

    An empty query is allowed — that is how the model reports that the schema cannot
    answer the request. Anything else is held to being one SELECT: the person
    reading this panel is likely to run it, and a tool config's query is a read by
    definition, so a generated write is a bug to surface rather than display.
    """
    sql = _strip_fences((sql or "").strip()).rstrip(";").strip()

    if not sql:
        return ""

    if len(sql) > _MAX_SQL_LEN:
        raise HTTPException(
            status_code=502,
            detail=(
                "The AI returned an unusably long query. Try describing what you "
                "need more narrowly."
            ),
        )

    bare = _strip_literals(sql)

    if not _READ_START_RE.match(bare):
        raise HTTPException(
            status_code=502,
            detail=(
                "The AI returned something that is not a read-only query, so it "
                "was not shown. Rephrase your request as a question about the data."
            ),
        )

    if ";" in bare:
        raise HTTPException(
            status_code=502,
            detail=(
                "The AI returned more than one statement, so it was not shown. "
                "Ask for a single query."
            ),
        )

    keyword = _WRITE_KEYWORD_RE.search(bare)
    if keyword:
        raise HTTPException(
            status_code=502,
            detail=(
                f"The AI returned a query containing '{keyword.group(1).upper()}', "
                "which would change data, so it was not shown. Rephrase your "
                "request as a question about the data."
            ),
        )

    return sql


def _strip_fences(sql: str) -> str:
    """
    Drop a markdown code fence the model added anyway.

    Asked for in the system prompt, but some models fence regardless, and a fence is
    formatting rather than a reason to reject an otherwise good query.
    """
    if not sql.startswith("```"):
        return sql

    without_open = sql.split("\n", 1)[1] if "\n" in sql else ""
    return without_open.rsplit("```", 1)[0].strip()


def _strip_literals(sql: str) -> str:
    """
    Blank out quoted spans and comments so the structural checks read only code.

    Without this, ``WHERE action = 'delete'`` would be rejected as a DELETE and
    ``WHERE note = 'a;b'`` as two statements.
    """
    return _LITERAL_RE.sub(" ", sql)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

async def _resolve_datasource(
    db: AsyncSession,
    user_id: int,
    datasource_id: Optional[uuid.UUID],
) -> DataSource:
    """
    Resolve the datasource, scoped to its owner, and require that SQL is even a
    meaningful thing to write for it.
    """
    if datasource_id is None:
        raise HTTPException(status_code=400, detail="Datasource is required")

    datasource = await datasource_crud.get_by_uuid(
        db, datasource_id, extra_filters={"user_id": user_id},
    )
    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    if not supports_joins(datasource.db_type):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{datasource.datasource_name}' is not a relational datasource, so "
                "there is no SQL to generate for it. Pick a PostgreSQL, MySQL or "
                "SQLite datasource."
            ),
        )

    return datasource


async def _load_metadata(datasource: DataSource, table_names: List[str]) -> List[dict]:
    """
    Reflect the chosen tables, and insist every one of them came back.

    A name that no longer exists is named in the error rather than dropped: a query
    generated against three tables when the user picked four looks correct and is
    not.
    """
    try:
        metadata = await metadata_service.get_rdbms_reflected_metadata(
            datasource, table_names,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Could not read the schema of '{datasource.datasource_name}'. "
                "Check the datasource connection and try again."
            ),
        ) from exc

    found = {entry["table"] for entry in metadata}
    missing = [name for name in table_names if name not in found]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                "These tables are no longer in the datasource: "
                f"{', '.join(missing)}. Reopen the panel to reload the table list."
            ),
        )

    if not any(entry["columns"] for entry in metadata):
        raise HTTPException(
            status_code=400,
            detail=(
                "The selected tables report no columns, so there is nothing to "
                "write a query against."
            ),
        )

    return metadata


def _dialect_name(db_type: Any) -> str:
    return _DIALECT_NAMES.get(str(db_type or "").strip().lower(), str(db_type or ""))
