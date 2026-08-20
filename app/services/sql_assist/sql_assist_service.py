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
:func:`app.db.db_utils.fetch_rdbms_metadata` goes through SQLAlchemy's Inspector —
and then :func:`_load_metadata` removes everything the user has switched off in Data
Sources. That pruning is the whole of the column rule: a model cannot select, join on
or filter by a column it was never told exists, and one that is not in the metadata
is one the model is told to treat as nonexistent. The alternative — showing the model
the full schema and checking its output afterwards — would mean policing a parser we
do not have.

SQL only — so only relational datasources are offered. A CSV or a Mongo collection
has no SQL to generate (they are queried through pandas and aggregation pipelines
respectively), and the reflection this relies on is a relational concept.
"""

import json
import logging
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
    QUERY_MODE_BUILDER,
    QUERY_MODE_SQL,
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
from app.utils.datasource_status import (
    NO_ACTIVE_TABLES_MESSAGE,
    active_table_names,
    inactive_table_names,
    is_column_active,
)
from app.utils.sql_guard import (
    MAX_SQL_LENGTH,
    group_by_violation,
    missing_identifiers,
    normalised_sql,
    read_only_violation,
    star_selection_violation,
)
from app.utils.validators import require_object_name

logger = logging.getLogger(__name__)

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

    ``fits`` false is **not** a refusal to create the tool. A tool config can also
    store the statement as written (``query_mode="sql"``), which is what
    :func:`draft_tool_config` falls back to — the answer decides how the query is
    stored, not whether it can be. That is why ``tool_name`` and ``description`` are
    asked for either way: they are needed in both modes.
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
        "question it answers — e.g. units_sold_per_product. Always fill this in, "
        "whether or not the query fits the builder.",
    )
    description: str = Field(
        default="",
        description="One sentence on what this tool answers, for the agent to decide "
        "when to call it. Always fill this in, whether or not the query fits.",
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
    The active tables and views in one datasource, read by reflection — the same
    source the generated query's schema comes from, so the picker cannot offer a
    table the model would then not be shown.

    A table switched off in Data Sources is not offered, for that same reason: it
    will be pruned out of the metadata by :func:`_load_metadata`, so picking it could
    only ever produce a refusal.
    """
    datasource = await _resolve_datasource(db, user_id, datasource_id)

    try:
        tables = await metadata_service.get_rdbms_reflected_tables(datasource)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Could not read the schema of '{datasource.datasource_name}'. "
                "Check the datasource connection and try again."
            ),
        ) from exc

    active = active_table_names(datasource.configuration_data, tables)

    if tables and not active:
        raise HTTPException(status_code=400, detail=NO_ACTIVE_TABLES_MESSAGE)

    return active


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
    "tables", "omitted_columns", "warnings"}`` — the history includes this turn, ready
    to be posted back with the next refinement, ``omitted_columns`` names the active
    columns the query does not mention so the panel can say so (see
    :func:`_omitted_columns` for why that is a note and not a refusal), and
    ``warnings`` carries anything else the user should read before using the query
    (see :func:`_regrouped`).
    """
    prompt = _validated_prompt(prompt)
    history = _validated_history(history_json)
    forced_key_uuid, use_inbuilt_llm = _validated_llm_choice(llm_mode, llm_api_key_id)

    datasource = await _resolve_datasource(db, user_id, datasource_id)
    metadata = await _load_metadata(datasource, _validated_tables(table_names))

    dialect = _dialect_name(datasource.db_type)
    system_prompt, user_content = _build_prompts(dialect, metadata, prompt, history)

    draft = await _generated_draft(
        db, user_id, system_prompt, user_content, forced_key_uuid, use_inbuilt_llm,
    )

    draft, warnings = await _regrouped(
        db,
        user_id,
        system_prompt,
        user_content,
        draft,
        metadata,
        forced_key_uuid,
        use_inbuilt_llm,
    )

    return {
        "draft": draft,
        "history": history + [{"prompt": prompt, "sql": draft.sql}],
        "dialect": dialect,
        "tables": [entry["table"] for entry in metadata],
        "omitted_columns": _omitted_columns(draft.sql, metadata),
        "warnings": warnings,
    }


async def _generated_draft(
    db: AsyncSession,
    user_id: int,
    system_prompt: str,
    user_content: str,
    forced_key_uuid: Optional[uuid.UUID],
    use_inbuilt_llm: bool,
) -> SqlDraft:
    """One call to the model, with its answer held to the read-only rules."""
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

    return draft


# --------------------------------------------------------------------------
# GROUP BY — a query the database would refuse
#
# MySQL's default sql_mode includes ONLY_FULL_GROUP_BY and PostgreSQL has the same
# rule built in: a grouped query may only select columns that are aggregated,
# grouped, or functionally dependent on what is grouped. A model that writes
#
#     SELECT project_details.client_name, COUNT(*) …  GROUP BY project_details.status
#
# has written a query that cannot run anywhere it will be used, and the database's
# own complaint — "nonaggregated column … not functionally dependent on columns in
# GROUP BY clause" — reaches the user long after the panel has closed, as a tool that
# fails in front of a visitor.
#
# So it is caught here, while the model is still on the line and the schema is still
# in hand. See :func:`_regrouped` for why it is one retry and then a note, rather
# than a refusal or a rewrite.
# --------------------------------------------------------------------------

async def _regrouped(
    db: AsyncSession,
    user_id: int,
    system_prompt: str,
    user_content: str,
    draft: SqlDraft,
    metadata: List[dict],
    forced_key_uuid: Optional[uuid.UUID],
    use_inbuilt_llm: bool,
) -> Tuple[SqlDraft, List[str]]:
    """
    The draft, regenerated once if its grouping would be refused by the database.

    Returns ``(draft, warnings)``.

    **Asked again, never patched.** The obvious repair — add the offending column to
    the GROUP BY — is a change to what the query counts, one row per group becoming
    one row per pair, and doing it silently would hand the user a number that answers
    a different question than the explanation next to it. The model is told what was
    wrong and writes the query again, so the SQL and the words describing it still
    come from the same place.

    **One retry, then a note.** The check is a heuristic
    (:func:`app.utils.sql_guard.group_by_violation`) and the panel does not run the
    query, so a second failure is reported next to the SQL rather than refused: the
    user can see the query, read what is wrong with it, and refine — which beats
    being told to try again with nothing to look at.

    The primary keys go with the check so the shape both databases *do* allow —
    grouping by a table's key and selecting its other columns — is not reported as a
    fault.
    """
    primary_keys = _primary_keys(metadata)
    offender = group_by_violation(draft.sql, primary_keys)

    if not offender:
        return draft, []

    logger.info(
        "Regenerating a query that groups badly on '%s'; asking the model again",
        offender,
    )

    try:
        retry = await _generated_draft(
            db,
            user_id,
            system_prompt,
            user_content + _grouping_repair_note(draft.sql, offender),
            forced_key_uuid,
            use_inbuilt_llm,
        )
    except HTTPException as exc:
        # The second attempt came back unusable — a write, a `SELECT *`, something
        # over length. The first one is still a readable query with a known fault, so
        # it is kept and the fault is reported. Failing the whole turn here would
        # leave the user with nothing over a retry they never asked for.
        logger.info("The regenerated query was refused as well: %s", exc.detail)
        return draft, [_grouping_warning(offender)]

    if retry.sql and not group_by_violation(retry.sql, primary_keys):
        return retry, []

    return draft, [_grouping_warning(offender)]


def _primary_keys(metadata: List[dict]) -> dict:
    """Each reflected table's primary key columns, for the grouping check."""
    return {
        entry["table"]: list(entry.get("primary_key") or [])
        for entry in metadata
    }


def _grouping_repair_note(sql: str, offender: str) -> str:
    """What the model is told about the query it just wrote."""
    return (
        "\n\nYour previous attempt cannot be run, so it has not been shown:\n"
        f"{sql}\n\n"
        f"It selects {offender}, which is neither aggregated nor listed in GROUP BY. "
        "MySQL (which runs with ONLY_FULL_GROUP_BY) and PostgreSQL both refuse a "
        "query like that.\n\n"
        "Write the query again so every non-aggregated column in the SELECT list is "
        "also in GROUP BY. Add the column to GROUP BY if one row per value of it is "
        "what was asked for, wrap it in MIN() or MAX() if any value from the group "
        "will do, or leave it out — and say which you chose in assumptions."
    )


def _grouping_warning(offender: str) -> str:
    """What the user is told when the second attempt is no better."""
    return (
        f"This query selects {offender}, which is neither aggregated nor in its "
        "GROUP BY clause, so MySQL and PostgreSQL will refuse to run it. Ask again "
        "for the column to be grouped as well, for it to be aggregated (MIN, MAX), "
        "or for it to be left out."
    )


# --------------------------------------------------------------------------
# Auto Create Tool — express the generated query as a Tool Config
#
# Any read-only query the panel generated can be saved. Which of the two tool
# config query modes it lands in is a matter of how well the builder can hold it,
# never of whether it is allowed — see draft_tool_config.
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
    Express a generated query as a Tool Config, ready to create.

    **Every valid read-only query can be saved.** The conversion tries the query
    builder's shape first, because a builder tool is the stronger artefact — its
    identifiers are checked against the schema, its filter values are bound
    parameters, and it reopens in the builder for editing. When the query needs SQL
    the builder cannot hold (``DISTINCT``, ``ORDER BY``, a subquery, a window
    function), the tool is created anyway, storing the statement as written
    (``query_mode="sql"``). The user read that SQL and asked for it; declining to
    save it would mean the assistant can write a query it will not let you use.

    The conversion is a second, deliberately narrow AI call rather than one more
    field on :class:`SqlDraft`. Two reasons: it only costs anything when the user
    actually asks for a tool, and converting one known query into one known schema
    is a far smaller task than writing SQL — which matters most for the in-built
    local model (a 1.7B parameter model, see AI_INBUILT.md), where a single request
    producing prose, SQL, assumptions *and* a nested builder config is exactly the
    kind of prompt that comes back malformed.

    In builder mode the model's answer is held to the same validator the query
    builder's own output is held to
    (:func:`tool_config_service.validated_query_config`), so a config from here is
    exactly as trustworthy as a hand-built one. A conversion that fails *that* check
    — an invented column, an ambiguous reference — also falls back to SQL mode
    rather than erroring: the SQL itself was never in doubt, only the model's
    reading of it.

    Returns ``{"mode", "reason", "tool_name", "description", "table", "config",
    "config_json", "sql_query", "preview"}``. ``mode`` is ``"builder"`` or
    ``"sql"``; ``reason`` is filled in for SQL mode and names why the builder could
    not hold the query, for the panel to show alongside the form.
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
        return _sql_tool_draft(draft, tables, sql, draft.reason.strip())

    try:
        return _validated_tool_draft(draft, datasource, tables, metadata)
    except HTTPException as exc:
        # The model claimed the query fits and then described it wrongly — a column
        # that is not in the schema, a base table the user did not pick. The SQL is
        # still exactly what the user approved, so it is saved as SQL rather than
        # thrown away, with the conversion's own message as the reason so the user
        # can see why they are not getting a builder tool.
        logger.info("Tool conversion fell back to SQL mode: %s", exc.detail)
        return _sql_tool_draft(draft, tables, sql, str(exc.detail))


def _sql_tool_draft(
    draft: ToolDraft,
    tables: List[str],
    sql: str,
    reason: str,
) -> dict:
    """
    The same draft, to be stored as a SQL-mode tool config.

    ``table`` is the primary table the tool is labelled with. The model's answer is
    used when it named one of the tables the user actually selected, and the first
    selected table otherwise — it is a label here, not something the query is built
    against, so a wrong guess is worth correcting quietly rather than refusing over.

    ``tables`` is every table the user selected, with that primary one moved to the
    front. All of them are recorded on the tool config: the statement reads them, and
    a SQL-mode tool has no other record of which tables those are — it is what lets
    the routing prompt state the tool's real scope and the executor check each table
    is still switched on.

    The statement is not re-validated: it arrived through :func:`_validated_sql`,
    and ``tool_config_service.validated_tool_sql`` checks it again on save.
    """
    named_table = (draft.table or "").strip()
    table = named_table if named_table in tables else tables[0]

    return {
        "mode": QUERY_MODE_SQL,
        "reason": reason or "This query uses SQL the tool builder cannot represent.",
        "tool_name": draft.tool_name.strip().lower(),
        "description": draft.description.strip(),
        "table": table,
        "tables": _primary_first(table, tables),
        "config": {},
        "config_json": "{}",
        "sql_query": sql,
        "preview": sql,
    }


def _primary_first(primary: str, tables: List[str]) -> List[str]:
    """
    The selected tables with the primary one at the front, order otherwise kept.

    The tool config's first table *is* its primary one, so the ordering carries
    meaning rather than being presentation — see
    ``tool_config_service._validated_tables``.
    """
    return [primary, *[name for name in tables if name != primary]]


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

    # The base table plus every table the joins bring in — not every table the user
    # selected. A builder query reads exactly what it joins, and recording a table it
    # never touches would overstate the tool's scope as surely as the old
    # single-table record understated it.
    joined_tables = query_tables(config.get("joins"), base_table) or [base_table]

    return {
        "mode": QUERY_MODE_BUILDER,
        "reason": "",
        "tool_name": draft.tool_name.strip().lower(),
        "description": draft.description.strip(),
        "table": base_table,
        "tables": joined_tables,
        "config": config,
        "config_json": json.dumps(config, indent=2),
        "sql_query": "",
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
                        f"{field_label} '{name}' is not an active column of "
                        f"'{table}' — it may be switched off in Data Sources. "
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
                    f"{field_label} '{name}' is not an active column of any table this "
                    "query reads — it may be switched off in Data Sources. "
                    "Regenerate, or create the tool by hand."
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
        f"- filters: column, operator ({operators}), literal value — combined with AND. "
        "The last four operators take NO value: IS NULL and IS NOT NULL are the SQL of "
        "the same name, IS BLANK is 'null, empty or whitespace' and IS NOT BLANK is its "
        "opposite. Convert `col IS NULL` and `col IS NOT NULL` with them, and use "
        "IS NOT BLANK for the common `col IS NOT NULL AND TRIM(col) <> ''` pair, which "
        "the builder could not express as two filters before. Omit value for these.\n"
        f"- joins: type ({join_types}), the table joined in, and one equality "
        "matching a column on a table already in the query against a column on it\n\n"
        "Set fits=false, and name what is in the way in reason, whenever the query "
        "needs anything else: ORDER BY, LIMIT, HAVING, DISTINCT, a subquery, a CTE, a "
        "window function, CASE, UNION, an expression or function in the SELECT list, "
        "OR between filters, a non-equality join condition, or a filter compared "
        "against another column instead of a literal. Do not approximate — a tool "
        "that quietly differs from the query is worse than no tool. fits=false is a "
        "normal answer and does not stop the tool being created: the query is then "
        "saved as SQL exactly as written, so say plainly what did not fit rather "
        "than bending the query to make it fit.\n\n"
        "Fill in tool_name and description either way — they are needed whichever "
        "way the query ends up stored.\n\n"
        "When it does fit:\n"
        "- Use column names exactly as the schema spells them.\n"
        "- table is the table in the FROM clause, never a joined one.\n"
        "- List joins in the order they can be applied: each one may only match "
        "against the base table or a table joined before it.\n"
        "- Give unqualified column names; the caller adds table prefixes.\n"
        "- Every column you put in columns must also appear in group_by whenever the "
        "query has any aggregations or grouping — a tool that selects a column the "
        "grouping does not cover is refused by the database when it runs. Copy the "
        "query's own GROUP BY; never add a column to it that the query does not "
        "group by.\n"
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
    table_names: List[str],
    description: Optional[str],
    config_json: Optional[str],
    query_mode: Optional[str] = None,
    sql_query: Optional[str] = None,
) -> ToolConfig:
    """
    Create the Tool Config the panel just drafted, in whichever mode it drafted.

    Goes through ``tool_config_service.create_tool_config`` rather than writing the
    row here, so an AI-created tool is subject to every rule a hand-made one is:
    agent and datasource ownership, the per-agent unique name, and the same
    validation of whichever query it holds — the builder config, or the statement.
    Nothing about this row records that an AI drafted it — there is no second kind
    of tool config to maintain.
    """
    return await tool_config_service.create_tool_config(
        db,
        user_id,
        agent_id=agent_id,
        datasource_id=datasource_id,
        tool_name=tool_name,
        table_names=table_names,
        description=description,
        config_json=config_json,
        query_mode=query_mode,
        sql_query=sql_query,
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

    It also states the projection rule: spell every column out, and for a plain
    row-listing query select **all** of them. That rule exists because the metadata
    has already been pruned to the columns the user allows, so "all of them" and "all
    the ones you may read" are the same set — and a ``SELECT *`` would quietly stop
    being that set the moment a column is added or switched off.

    Aggregates are carved out explicitly. "Include every column" cannot hold for
    ``SELECT COUNT(*) … GROUP BY status`` without changing what the query counts, and
    a rule the model has to break to answer the question is a rule it learns to
    ignore everywhere else.

    That carve-out is also where the grouping rule has to be stated. Told to select
    every column and then to group, a model will do both — and produce exactly the
    query MySQL and PostgreSQL refuse, selecting a column the grouping does not
    determine. So the two are written as one instruction: when the request is an
    aggregate, the SELECT list is the grouping columns and the aggregates, and
    nothing else.
    """
    system_prompt = (
        f"You write SQL for a {dialect} database, inside the GetMyStuff analytics "
        "platform.\n\n"
        "You are given ONLY schema metadata: table names, column names and their "
        "types, primary keys and foreign keys. You have not seen a single row of "
        "this database and you never will — so never describe, count or quote its "
        "contents, and never claim a result you cannot know.\n\n"
        "The metadata is the complete list of what you may read. Columns the owner "
        "of this data has switched off are not in it — a column that is not listed "
        "does not exist for you, whatever you would expect the table to have.\n\n"
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
        "- NEVER write SELECT * or table.*. Spell every column out.\n"
        "- Unless the request is an aggregate or a GROUP BY, select EVERY column "
        "listed below for EVERY table your query reads, joined tables included.\n"
        "- When the request IS an aggregate, select only the grouping columns and "
        "the aggregates, and note in assumptions that the other columns were left "
        "out.\n"
        "- Once a query has GROUP BY, EVERY column in the SELECT list must either be "
        "inside an aggregate function or be listed in the GROUP BY. This database "
        "refuses anything else — MySQL with ONLY_FULL_GROUP_BY, PostgreSQL by the "
        "same rule — so a query that breaks it cannot be run at all. If a column is "
        "wanted alongside an aggregate, either group by it too or wrap it in MIN() "
        "or MAX().\n"
        "- The same holds without GROUP BY: a SELECT list may not mix an aggregate "
        "with a plain column. SELECT client_name, COUNT(*) FROM projects is refused; "
        "add GROUP BY client_name.\n"
        f"- Write {dialect} syntax, and use LIMIT when the request implies a "
        "top-N or a sample.\n"
        "- Put anything you guessed in assumptions, one short line each."
    )

    user_content = _required_columns_block(metadata)
    user_content += f"Schema metadata (JSON):\n{json.dumps(metadata, default=str)}\n\n"

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


def _table_qualified_columns(entry: dict) -> List[str]:
    """One reflected table's columns as ``table.column``, in reflected order."""
    return [
        f"{entry['table']}.{column['name']}"
        for column in entry.get("columns") or []
    ]


def _qualified_columns(metadata: List[dict]) -> List[str]:
    """
    Every column in the metadata as ``table.column``, in metadata order.

    One function because two things need the same list from the same source: the block
    the model is told to select, and the check on what it returned
    (:func:`_omitted_columns`). Deriving it twice is how the prompt and the check would
    come to disagree about what "every column" meant.
    """
    return [name for entry in metadata for name in _table_qualified_columns(entry)]


def _required_columns_block(metadata: List[dict]) -> str:
    """
    The literal per-table column list, put ahead of the schema JSON.

    The same names are in the JSON below it, but as one blob of nested objects the
    model has to walk. Spelling out the projection it is being asked for turns "work
    out which columns you may read and select all of them" into something it copies,
    which is the difference between the rule being followed and being approximately
    followed.
    """
    lines = ["Columns to select (all of them, unless this request is an aggregate):"]

    for entry in metadata:
        columns = ", ".join(_table_qualified_columns(entry))
        if columns:
            lines.append(f"  {entry['table']}: {columns}")

    return "\n".join(lines) + "\n\n"


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
    answer the request. Anything else is held to being one read, by the same rule
    Tool Configs applies to a hand-written statement and the Deep Agents executor
    applies before running one (:mod:`app.utils.sql_guard`). Shared, because a query
    shown here is likely to be run and may well be saved as a tool: three different
    ideas of "read-only" would mean the loosest one wins.

    The status is 502, not 400: the user asked a perfectly reasonable question and
    the model returned something unusable. That is the upstream's mistake, and the
    wording says so rather than blaming the prompt.
    """
    statement = normalised_sql(sql)

    if not statement:
        return ""

    if len(statement) > MAX_SQL_LENGTH:
        raise HTTPException(
            status_code=502,
            detail=(
                "The AI returned an unusably long query. Try describing what you "
                "need more narrowly."
            ),
        )

    violation = read_only_violation(statement)
    if violation:
        raise HTTPException(
            status_code=502,
            detail=(
                f"The AI returned a query that {violation}, so it was not shown. "
                "Rephrase your request as a question about the data."
            ),
        )

    star = star_selection_violation(statement)
    if star:
        # Refused rather than shown with a warning. `*` is the one selection whose
        # column list the database decides at run time, so a query saved as a tool
        # today would start returning a column switched off tomorrow — the exact
        # thing the pruning above exists to prevent.
        raise HTTPException(
            status_code=502,
            detail=(
                f"The AI returned a query using '{star}', which would read columns "
                "that may be switched off in this datasource. Ask again — the query "
                "was not shown."
            ),
        )

    return statement


def _omitted_columns(sql: str, metadata: List[dict]) -> List[str]:
    """
    Which of the columns the model was told to select are nowhere in its query.

    **Reported, never refused.** The check is a text search, not a parse: it cannot
    tell a SELECT list from a WHERE clause, and it cannot tell that a CTE's outer
    query legitimately narrows what the inner one read. Refusing on it would reject
    every aggregate and every CTE the panel exists to help write. So the answer goes
    back to the user as a note next to the query, and the decision — ask again, or
    use it as it is — stays theirs.

    An empty query has nothing omitted: the model reporting that the schema cannot
    answer the request is not a query missing its columns.
    """
    if not normalised_sql(sql):
        return []

    return missing_identifiers(sql, _qualified_columns(metadata))


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
    Reflect the chosen tables, prune everything switched off, and insist there is
    still something to write a query against.

    A name that no longer exists is named in the error rather than dropped: a query
    generated against three tables when the user picked four looks correct and is
    not. An inactive table is named for the same reason — and refused rather than
    filtered, because ``table_names`` arrives from a form post and can name a table
    the picker no longer offers.

    Pruning the columns here, before the prompt is built, is what makes the column
    rule real: the model is not asked to avoid a column, it is never shown one.
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

    inactive = inactive_table_names(datasource.configuration_data, table_names)
    if inactive:
        raise HTTPException(
            status_code=400,
            detail=(
                f"These tables are inactive in this datasource: {', '.join(inactive)}. "
                "Activate them in Data Sources or deselect them."
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

    return [_pruned_table(datasource.configuration_data, entry) for entry in metadata]


def _pruned_table(configuration_data: Any, entry: dict) -> dict:
    """
    One reflected table with everything switched off removed.

    The foreign keys are pruned too, not just the column list. A key whose own column
    or whose referenced column is inactive is a join the model would be invited to
    write and then could not select either side of — so the relationship is not
    mentioned at all, and the model joins on what it can actually read or says it
    cannot answer.

    A table left with no columns is refused here rather than by the caller's
    all-tables check: with four tables selected and one emptied, that check passes and
    the model would be handed a table it may read nothing from.
    """
    table_name = entry["table"]

    def active(column_name: Any) -> bool:
        return is_column_active(configuration_data, table_name, str(column_name or ""))

    columns = [column for column in entry.get("columns") or [] if active(column.get("name"))]

    if not columns:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Every column of '{table_name}' is inactive, so there is nothing to "
                "write a query against. Activate the columns you need in Data Sources."
            ),
        )

    pruned = dict(entry)
    pruned["columns"] = columns

    # A view is reflected without keys at all, so the two are only rewritten when
    # they were there — an empty list would tell the model a view has no primary key,
    # which is a different statement from not mentioning one.
    if "primary_key" in entry:
        pruned["primary_key"] = [
            name for name in entry.get("primary_key") or [] if active(name)
        ]

    if "foreign_keys" in entry:
        pruned["foreign_keys"] = [
            key for key in entry.get("foreign_keys") or []
            if all(active(name) for name in key.get("columns") or [])
            and all(
                is_column_active(
                    configuration_data,
                    str(key.get("references_table") or ""),
                    str(name or ""),
                )
                for name in key.get("references_columns") or []
            )
        ]

    return pruned


def _dialect_name(db_type: Any) -> str:
    return _DIALECT_NAMES.get(str(db_type or "").strip().lower(), str(db_type or ""))
