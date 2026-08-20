"""
The one definition of "a SQL statement this application is willing to run".

Three features now hand SQL around — Ask AI writes it, Tool Configs stores it, the
Deep Agents executor runs it — and all three need the same answer to the same
question: *is this a single, read-only statement?* Answering it in three places
would mean three subtly different answers, and the one that matters (the executor)
would be the one nobody looked at. So it is answered here, once, and each caller
phrases the refusal in its own words.

**What this guarantees, and what it does not.** It guarantees the text is one
statement, that the statement is a read, and that it is of a sane length. It does
**not** guarantee the SQL is syntactically valid — that is the database's job, and
faking it with a parser would produce false rejections of perfectly good dialect
syntax (``DISTINCT ON``, ``LATERAL``, ``QUALIFY``, vendor functions). A query that
passes here and is then rejected by the driver is the expected way a typo is
found; a query that fails here is refused before it can reach a connection.

:func:`star_selection_violation`, :func:`forbidden_identifier`,
:func:`missing_identifiers` and :func:`group_by_violation` are the same bargain one
level up: they are *text* checks, not a parse. They can tell that a name appears in a
statement and that a ``*`` is being selected; they cannot tell which clause a name is
in, or that a name in a subquery belongs to a different scope. That is enough for the
questions Ask AI needs answered about a generated query — is it selecting columns
nobody may see, did it spell out the columns at all, and will the grouping be refused
by the database — and honest about being a heuristic. The guarantee that only active
columns are ever *read* is enforced where the query runs
(app.services.deep_agents.query_executor), not here.

The checks run against the statement with **string literals, quoted identifiers
and comments blanked out** (:func:`stripped_literals`). Without that,
``WHERE action = 'delete'`` would read as a DELETE and ``WHERE note = 'a;b'`` as
two statements — both perfectly ordinary reads.
"""

import re
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

# Long enough for any query a person writes by hand or a model produces for one
# question; short enough that a runaway completion or a pasted dump is refused
# before it is stored, previewed and executed.
MAX_SQL_LENGTH = 8000

# The ceiling for a statement this application *composed* rather than received, which
# today means a graph designer union node joining one validated fragment per pass of a
# loop (see `node_runners._run_sql_union`).
#
# A separate number rather than a raised `MAX_SQL_LENGTH`, because the two are guarding
# against different things. 8,000 refuses a pasted dump or a runaway completion — text
# whose length is itself the evidence that nobody wrote it on purpose. Neither describes
# a union of eighty-two copies of a fragment that already passed the 8,000 check: its
# length is the loop's item count, which the author chose and a ceiling elsewhere already
# bounds. What is still needed here is a stop, so this is one — around 1,300 passes of a
# realistic fragment, and well inside MySQL's default `max_allowed_packet`.
MAX_BUILT_SQL_LENGTH = 200_000

# Verbs that make a statement more than a read *from a position a read could
# reach*: `WITH … INSERT`, `SELECT … INTO`, and the DDL a model or a hurried
# operator might append.
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

# A read starts here. WITH is allowed because a CTE is the natural shape for a
# great many analytical queries; a WITH that goes on to write is caught by the
# keyword check above.
_READ_START_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)

# A `*` that means "every column": bare after SELECT (with an optional DISTINCT or
# ALL), or qualified as `t.*`. `COUNT(*)` and `count( * )` are deliberately not
# matched — an aggregate over all rows names no columns at all, and rejecting it
# would refuse every legitimate "how many" query.
_STAR_SELECTION_RE = re.compile(
    r"\bselect\b\s+(?:distinct\s+|all\s+)?\*"  # SELECT *
    r"|\b\w+\s*\.\s*\*",                       # SELECT t.*
    re.IGNORECASE,
)

# Quoted spans and comments, removed before the checks above look for `;` and write
# verbs. Ordered longest-first so `--` inside a string is not treated as a comment.
_LITERAL_RE = re.compile(
    r"'(?:[^']|'')*'"        # single-quoted string, '' being an escaped quote
    r"|\"(?:[^\"]|\"\")*\""  # double-quoted identifier
    r"|`[^`]*`"              # MySQL backtick identifier
    r"|/\*.*?\*/"            # block comment
    r"|--[^\n]*",            # line comment
    re.DOTALL,
)


def normalised_sql(sql: Optional[str]) -> str:
    """
    The statement as it should be stored: trimmed, unfenced, no trailing semicolon.

    Normalising before checking — rather than rejecting — is deliberate for all
    three: a markdown fence is a model's formatting habit, a trailing semicolon is
    a person's typing habit, and neither is a reason to refuse an otherwise good
    query. Dropping the semicolon here is also what lets the single-statement check
    below be a flat "no `;` anywhere".
    """
    text = (sql or "").strip()

    if text.startswith("```"):
        without_open = text.split("\n", 1)[1] if "\n" in text else ""
        text = without_open.rsplit("```", 1)[0].strip()

    return text.rstrip(";").strip()


def stripped_literals(sql: str) -> str:
    """
    The statement with quoted spans and comments blanked out, for structural checks.

    Exposed rather than private because a caller inspecting SQL for anything else
    (which tables it names, whether it is aggregated) needs to look at code and not
    at content, and should not re-derive how to tell them apart.
    """
    return _LITERAL_RE.sub(" ", sql)


# A `:name` used as a bind parameter. The lookbehind excludes PostgreSQL's `::type`
# cast and any `:name` that is part of a longer word.
_BIND_USAGE_RE = re.compile(r"(?<![:\w]):([a-z][a-z0-9_]*)")

# The same thing typed with a space in it. The lookbehind is the one above's, so
# PostgreSQL's `x :: text` is not read as a colon adrift from `text`.
_SPACED_BIND_RE = re.compile(r"(?<![:\w]):[ \t]+([a-z][a-z0-9_]*)")

# The shape a placeholder sits in, which decides whether its value may be a list.
# Matched against the statement with literals blanked, so what is seen is code.
_IN_SHAPE_RE = r"\bin\s*:{name}\b"
_COMPARISON_SHAPE_RE = r"[=<>]\s*:{name}\b"

#: `placeholder_shape` returns one of these, or ``None`` when the placeholder sits in
#: neither shape (a function argument, say) and nothing can be concluded.
PLACEHOLDER_LIST = "list"
PLACEHOLDER_SINGLE = "single"


def bind_placeholders(sql: Optional[str]) -> Set[str]:
    """
    The ``:name`` placeholders a statement uses.

    Literals and comments are blanked first, so a time inside a string — ``'12:30'`` —
    is not mistaken for a parameter.

    Lives here rather than in either service that needs it because a placeholder is a
    property of a statement's *text*, which is what this module is about. The two
    private copies in ``tool_config_service`` and ``tool_chain_service`` predate this
    one and existed only because neither module could import the other; both now
    delegate here, and the pattern is identical to the one they carried.
    """
    if not (sql or "").strip():
        return set()

    return set(_BIND_USAGE_RE.findall(stripped_literals(sql or "")))


def suffixed_placeholders(sql: Optional[str], suffix: str) -> str:
    """
    Every ``:name`` in a statement rewritten ``:name<suffix>``, literals left alone.

    What makes one copy of a fragment distinguishable from another when several are joined
    into one statement: pass 7's ``:id`` becomes ``:id__p7`` and is bound to pass 7's
    value, so a union of eighty-two fragments carries eighty-two bind parameters and no
    value is ever written into the text. That is the whole reason this exists rather than
    the obvious alternative — substituting the values — and it is the guarantee
    ``TOOL_QUERY_MODES.md`` makes about SQL mode, kept in the one place that composes SQL.

    **Literals and comments are stepped over, not blanked.** Every other reader here can
    afford ``stripped_literals`` because it only needs to *look*; this one rewrites, so the
    content has to survive. It is load-bearing rather than theoretical: a LIKE pattern such
    as ``concat('%s:departs:', :id, '%')`` holds a colon inside a string, and blanking it
    would corrupt the query while renaming exactly the placeholder that was wanted.

    :param suffix: appended to each name; the caller owns its shape and its uniqueness
    """
    text = sql or ""

    if not text.strip() or not suffix:
        return text

    out: List[str] = []
    cursor = 0

    for literal in _LITERAL_RE.finditer(text):
        out.append(_BIND_USAGE_RE.sub(rf":\1{suffix}", text[cursor:literal.start()]))
        out.append(literal.group(0))
        cursor = literal.end()

    out.append(_BIND_USAGE_RE.sub(rf":\1{suffix}", text[cursor:]))

    return "".join(out)


def spaced_placeholder(sql: Optional[str]) -> Optional[str]:
    """
    The name after a ``:`` that has come adrift from it — ``= : item``, not ``= :item``.

    A separate check because the space makes the placeholder *invisible* to every other
    one: :func:`bind_placeholders` does not see it, so nothing notices an undeclared
    parameter, nothing notices an unused declaration, and the statement reaches the
    database with a bare colon in it. What comes back is the dialect's own syntax error
    quoting the fragment — ``near ': item'`` — mid-run, with no hint that the fix is one
    deleted space in a form that is no longer open.

    Only same-line whitespace counts. A colon at the end of a line is far more likely to
    be something other than a mistyped placeholder, and a check that is sure of itself is
    worth more here than one that catches every spelling of the mistake.

    :returns: the name, for a message that can quote it, or ``None``
    """
    if not (sql or "").strip():
        return None

    match = _SPACED_BIND_RE.search(stripped_literals(sql or ""))

    return match.group(1) if match else None


def placeholder_shape(sql: Optional[str], name: str) -> Optional[str]:
    """
    Whether ``:name`` is written where a list belongs or where one value belongs.

    An expanding bind parameter always renders parenthesised — ``IN (?, ?, ?)`` — so
    the two are not interchangeable: ``id = :x`` bound to a list becomes
    ``id = (?, ?, ?)``, and ``id IN :x`` bound to one value becomes ``id IN ?``. Both
    are syntax errors, and both are errors the *database* reports later, far from the
    form that caused them.

    A text check over the statement with literals blanked: it reads the shape
    immediately next to the placeholder and nothing cleverer, which is the mistake
    people actually make. ``None`` means the placeholder is in neither shape, and the
    caller should conclude nothing rather than guess.

    :returns: :data:`PLACEHOLDER_LIST`, :data:`PLACEHOLDER_SINGLE`, or ``None``
    """
    if not name or not (sql or "").strip():
        return None

    bare = stripped_literals(sql or "")
    quoted = re.escape(name)

    if re.search(_IN_SHAPE_RE.format(name=quoted), bare, re.IGNORECASE):
        return PLACEHOLDER_LIST

    if re.search(_COMPARISON_SHAPE_RE.format(name=quoted), bare):
        return PLACEHOLDER_SINGLE

    return None


def read_only_violation(
    sql: Optional[str],
    max_length: int = MAX_SQL_LENGTH,
) -> Optional[str]:
    """
    Why this statement may not be run, as a phrase, or ``None`` when it may.

    A phrase rather than an exception because the callers are answering different
    questions and owe the user different sentences: Tool Configs is rejecting
    something a person typed (a 400 they can fix), Ask AI is rejecting something a
    model returned (a 502 the user did not cause). Both read as
    ``f"The SQL query {reason}."``

    An empty statement returns ``None`` — "nothing to run" is not a violation, and
    whether a missing query is an error is the caller's rule, not this module's.

    ``max_length`` is a keyword with a default so that every caller that has one
    statement written by one person keeps exactly the rule it had. It is raised only by
    the one caller whose statement this application composed —
    :data:`MAX_BUILT_SQL_LENGTH` says why that is a different question. Everything else
    checked here applies unchanged to a built statement: it must still be one read, still
    have no second statement in it, and still contain no write verb.
    """
    statement = normalised_sql(sql)

    if not statement:
        return None

    if len(statement) > max_length:
        return f"is longer than {max_length} characters"

    bare = stripped_literals(statement)

    if not _READ_START_RE.match(bare):
        return "is not a read-only query — it has to start with SELECT or WITH"

    if ";" in bare:
        return "contains more than one statement"

    keyword = _WRITE_KEYWORD_RE.search(bare)
    if keyword:
        return (
            f"contains '{keyword.group(1).upper()}', which would change data rather "
            "than read it"
        )

    return None


def star_selection_violation(sql: Optional[str]) -> Optional[str]:
    """
    The offending text when the statement selects ``*``, or ``None`` when it spells
    its columns out.

    Worth refusing on its own terms: ``*`` is the one selection whose meaning is
    decided by the database rather than by the query, so a statement that passed
    every column check when it was written starts returning a column the user has
    since switched off — without the statement changing.

    ``COUNT(*)`` is not a star selection. It names no columns, and treating it as one
    would refuse every "how many" query there is.
    """
    statement = normalised_sql(sql)

    if not statement:
        return None

    match = _STAR_SELECTION_RE.search(stripped_literals(statement))

    return " ".join(match.group(0).split()) if match else None


def _identifier_pattern(name: str, *, any_qualifier: bool = False) -> re.Pattern:
    """
    A word-boundary matcher for one identifier.

    The default refuses a preceding dot, so a bare ``id`` does not match inside
    ``orders.id`` — that is what lets a caller forbid a column on one table without
    rejecting a query that reads a same-named column on another.

    ``any_qualifier`` drops only the dot from that guard, so ``total`` matches
    ``o.total`` but still not ``subtotal``. Used when looking for a column whatever
    table alias it was written against.
    """
    prefix = r"(?<!\w)" if any_qualifier else r"(?<![\w.])"
    return re.compile(prefix + re.escape(name) + r"\b", re.IGNORECASE)


def forbidden_identifier(
    sql: Optional[str],
    forbidden: Iterable[str],
) -> Optional[str]:
    """
    The first forbidden name the statement mentions, or ``None``.

    Matched against the statement with literals stripped, so ``WHERE note =
    'salary'`` cannot be mistaken for a reference to a ``salary`` column. The
    leading ``(?<![\\w.])`` is what stops a bare ``id`` matching inside
    ``orders.id`` — a caller that wants the qualified form forbidden passes the
    qualified form.
    """
    statement = normalised_sql(sql)

    if not statement:
        return None

    bare = stripped_literals(statement)

    for name in forbidden or []:
        if name and _identifier_pattern(str(name)).search(bare):
            return str(name)

    return None


def missing_identifiers(
    sql: Optional[str],
    required: Iterable[str],
) -> List[str]:
    """
    Which of the required names the statement never mentions, in the order given.

    **Advisory.** Presence anywhere in the text is not proof a name is in the SELECT
    list — it might be in a WHERE clause, or inside a CTE the outer query narrows —
    so the answer is reported to the user rather than used to refuse the query. The
    caller that needs a guarantee builds the column list itself instead of asking a
    model for one.

    A qualified name counts as present when either the qualified form or its bare
    column appears, because ``orders.total`` written against a table aliased ``o``
    is ``o.total`` and is not missing at all. Being advisory, the check errs towards
    not crying wolf.
    """
    statement = normalised_sql(sql)
    bare = stripped_literals(statement)

    missing = []
    for name in required or []:
        if not name:
            continue

        column = str(name).rpartition(".")[2] or str(name)

        if not _identifier_pattern(column, any_qualifier=True).search(bare):
            missing.append(str(name))

    return missing


# --------------------------------------------------------------------------
# GROUP BY — the ONLY_FULL_GROUP_BY rule
#
# MySQL runs with ONLY_FULL_GROUP_BY in its default sql_mode and PostgreSQL has
# always worked the same way: once a query groups, every column in the SELECT list
# has to be aggregated, grouped, or functionally dependent on what is grouped.
# Anything else is refused by the database with
#
#     SELECT list is not in GROUP BY clause and contains nonaggregated column
#     'x.y' which is not functionally dependent on columns in GROUP BY clause
#
# which is a query that was never going to run — not a data problem, and not
# something the user who asked for it can be expected to spot in a statement they
# did not write. It is checked here so a generated query can be caught and rewritten
# before it is shown, saved as a tool, and run in front of a visitor.
# --------------------------------------------------------------------------

_SELECT_RE = re.compile(r"\bselect\b", re.IGNORECASE)
_FROM_RE = re.compile(r"\bfrom\b", re.IGNORECASE)
_GROUP_BY_RE = re.compile(r"\bgroup\s+by\b", re.IGNORECASE)

# Where the GROUP BY list stops, and where the FROM clause stops for the purpose of
# reading table aliases out of it.
_AFTER_GROUP_BY_RE = re.compile(
    r"\b(having|order\s+by|limit|offset|fetch|window|union|intersect|except)\b",
    re.IGNORECASE,
)
_AFTER_FROM_RE = re.compile(
    r"\b(where|group\s+by|having|order\s+by|limit|offset|fetch|window)\b",
    re.IGNORECASE,
)

# `SELECT DISTINCT a, …` — dropped before the selection is read, so the first item
# is the column and not the keyword.
_SELECT_QUALIFIER_RE = re.compile(r"^\s*(?:distinct|all)\b", re.IGNORECASE)

# A column, optionally qualified. Anything else in the SELECT list — a function
# call, an expression, a literal, a `*` — is not something this check reasons about.
_PLAIN_REFERENCE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)?$",
)

# Bare words that look like a column and are not one.
_VALUE_KEYWORDS = frozenset({
    "null", "true", "false", "default", "current_date", "current_time",
    "current_timestamp", "current_user", "session_user", "localtime",
    "localtimestamp",
})

# Words that can follow a table name in a FROM clause without being its alias.
_NOT_AN_ALIAS = frozenset({
    "inner", "left", "right", "full", "cross", "outer", "join", "on", "using",
    "natural", "as", "where", "group", "order", "limit", "having",
})


def group_by_violation(
    sql: Optional[str],
    primary_keys: Optional[Mapping[str, Sequence[str]]] = None,
) -> Optional[str]:
    """
    The first column the statement selects without aggregating or grouping it, or
    ``None`` when the grouping is sound — or when this check cannot be sure.

    ``primary_keys`` maps table name to its primary key columns. Passing it is what
    lets ``SELECT p.id, p.name, COUNT(*) … GROUP BY p.id`` through: both MySQL and
    PostgreSQL allow a column that is functionally dependent on a grouped primary
    key, and reporting that as broken would be a false alarm on one of the most
    ordinary shapes there is. Callers that have the reflected schema to hand should
    always pass it; without it, that shape is reported.

    **Deliberately silent when it cannot be certain.** The answer drives a warning
    and a regeneration, so a missed violation costs a clear error from the database
    later, while a false one sends the model off rewriting a query that was already
    correct. Whenever the statement is more than this check can read honestly it
    returns ``None``:

    * more than one ``SELECT`` — a CTE, a subquery, a ``UNION``. Each has its own
      scope, and telling them apart needs a parser;
    * a ``GROUP BY`` holding anything but plain column names — an ordinal
      (``GROUP BY 1``), an expression (``GROUP BY DATE(created_at)``);
    * a selected item that is not a plain column reference — a function call, a
      ``CASE``, an arithmetic expression, an alias written without ``AS``.

    A bare column matches a qualified grouping and the other way round, because
    ``total`` and ``o.total`` are usually the same column and refusing to see that
    would report a query that runs perfectly well.
    """
    statement = normalised_sql(sql)

    if not statement:
        return None

    bare = stripped_literals(statement)

    # One SELECT and one GROUP BY, or this is not a statement to reason about: a
    # second SELECT means a second scope, and a second GROUP BY belongs to it.
    if len(_SELECT_RE.findall(bare)) != 1 or len(_GROUP_BY_RE.findall(bare)) != 1:
        return None

    depths = paren_depths(bare)

    select = at_depth_zero(_SELECT_RE, bare, depths)
    group_by = at_depth_zero(_GROUP_BY_RE, bare, depths)
    if not select or not group_by:
        return None

    from_clause = at_depth_zero(_FROM_RE, bare, depths, select.end())
    if not from_clause or from_clause.start() > group_by.start():
        return None

    grouped = _grouping_keys(bare, depths, group_by.end())
    if grouped is None:
        return None

    aliases = _table_aliases(bare, depths, from_clause.end(), group_by.start())

    return _ungrouped_selection(
        _SELECT_QUALIFIER_RE.sub("", bare[select.end():from_clause.start()], count=1),
        _grouped_columns(grouped, aliases),
        aliases,
        primary_keys,
    )


def _ungrouped_selection(
    selection: str,
    grouped: Set[Tuple[str, str]],
    aliases: Dict[str, str],
    primary_keys: Optional[Mapping[str, Sequence[str]]],
) -> Optional[str]:
    """
    The first column in the SELECT list the grouping does not account for.

    Every item that is not a plain column reference is passed over — see
    :func:`group_by_violation` for why silence beats a guess here.
    """
    for item in _split_top_level(selection):
        reference = _without_alias(item)

        if not _PLAIN_REFERENCE_RE.match(reference):
            continue
        if reference.lower() in _VALUE_KEYWORDS:
            continue
        if _is_grouped(reference, grouped, aliases, primary_keys):
            continue
        if _determined_by_grouping(reference, grouped, aliases, primary_keys):
            continue

        return reference

    return None


def _without_alias(item: str) -> str:
    """
    One SELECT list item with an ``AS`` alias cut off — the alias is a name for the
    result, not a column being selected.

    An alias written without ``AS`` (``client_name cn``) is left as it is: the item
    then fails the plain-reference test and is passed over, which is the safe way to
    be unsure.
    """
    words = item.split()

    if len(words) == 3 and words[1].lower() == "as":
        return words[0]

    return item.strip()


def paren_depths(text: str) -> List[int]:
    """
    The bracket nesting depth at every character, so a keyword or a comma inside a
    function call or a derived table is not mistaken for a clause of the statement.

    Public alongside :func:`at_depth_zero` for the reason :func:`stripped_literals` is: a
    caller asking a different question about the same text — the graph designer refusing a
    union fragment that carries its own ``ORDER BY`` — needs to tell a clause of the
    statement from one inside a subquery, and should not re-derive how.
    """
    depths: List[int] = []
    depth = 0

    for char in text:
        if char == "(":
            depths.append(depth)
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
            depths.append(depth)
        else:
            depths.append(depth)

    return depths


def at_depth_zero(
    pattern: re.Pattern,
    text: str,
    depths: List[int],
    start: int = 0,
) -> Optional[re.Match]:
    """The first match of ``pattern`` that is not inside brackets. See :func:`paren_depths`."""
    for match in pattern.finditer(text, start):
        if depths[match.start()] == 0:
            return match

    return None


def _split_top_level(text: str) -> List[str]:
    """Split on commas that are not inside brackets, dropping empty pieces."""
    parts: List[str] = []
    depth = 0
    start = 0

    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1

    parts.append(text[start:])

    return [part.strip() for part in parts if part.strip()]


def _grouping_keys(
    text: str,
    depths: List[int],
    start: int,
) -> Optional[Set[str]]:
    """
    The grouped columns, lowercased, or ``None`` when the GROUP BY holds something
    this check cannot compare a column against.
    """
    end = at_depth_zero(_AFTER_GROUP_BY_RE, text, depths, start)
    entries = _split_top_level(text[start:end.start() if end else len(text)])

    if not entries:
        return None

    keys = set()
    for entry in entries:
        if not _PLAIN_REFERENCE_RE.match(entry) or entry.lower() in _VALUE_KEYWORDS:
            return None
        keys.add(entry.lower())

    return keys


def _table_aliases(
    text: str,
    depths: List[int],
    start: int,
    end: int,
) -> Dict[str, str]:
    """
    ``alias -> table`` for the tables the FROM clause names, both lowercased.

    Only used to look a primary key up for a column written against an alias. A
    clause this cannot read yields fewer entries, never wrong ones — the caller then
    simply has no primary key to reason with and stays quiet.
    """
    clause_end = at_depth_zero(_AFTER_FROM_RE, text, depths, start)
    clause = text[start:min(clause_end.start() if clause_end else end, end)]

    aliases: Dict[str, str] = {}

    for piece in re.split(r"\bjoin\b|,", clause, flags=re.IGNORECASE):
        # `orders o ON o.id = …` — everything from ON onwards is a condition, not a
        # name.
        piece = re.split(r"\bon\b|\busing\b", piece, flags=re.IGNORECASE)[0]
        words = [word for word in piece.split() if word]

        if not words or not _PLAIN_REFERENCE_RE.match(words[0]):
            continue

        table = words[0].lower()
        aliases[table] = table

        rest = [word for word in words[1:] if word.lower() != "as"]
        if rest and _PLAIN_REFERENCE_RE.match(rest[0]):
            candidate = rest[0].lower()
            if candidate not in _NOT_AN_ALIAS:
                aliases[candidate] = table

    return aliases


def _grouped_columns(
    grouped: Set[str],
    aliases: Dict[str, str],
) -> Set[Tuple[str, str]]:
    """
    The grouped columns as ``(table, column)``, resolving any alias the FROM clause
    explained. ``table`` is ``""`` when the reference was bare and the query reads
    more than one table — unknown, rather than assumed to be the base one.
    """
    return {_owned_column(key, aliases) for key in grouped}


def _owned_column(reference: str, aliases: Dict[str, str]) -> Tuple[str, str]:
    """One column reference as ``(table, column)``, both lowercased."""
    qualifier, _, column = reference.lower().rpartition(".")

    if qualifier:
        return aliases.get(qualifier, qualifier), column

    tables = set(aliases.values())

    return (next(iter(tables)) if len(tables) == 1 else ""), column


def _is_grouped(
    reference: str,
    grouped: Set[Tuple[str, str]],
    aliases: Dict[str, str],
    primary_keys: Optional[Mapping[str, Sequence[str]]],
) -> bool:
    """
    Whether this column is one of the grouped ones.

    Strict when both sides name a table — ``customers.name`` is not grouped by
    ``orders.name`` — and lenient when either side is a bare column in a query whose
    tables this check could not read, where ``total`` and ``o.total`` are far more
    likely to be the same column than not.
    """
    table, column = _owned_column(reference, aliases)

    if not table and primary_keys and len(primary_keys) == 1:
        table = str(next(iter(primary_keys))).lower()

    return any(
        column == grouped_column
        and (not table or not grouped_table or table == grouped_table)
        for grouped_table, grouped_column in grouped
    )


def _determined_by_grouping(
    reference: str,
    grouped: Set[Tuple[str, str]],
    aliases: Dict[str, str],
    primary_keys: Optional[Mapping[str, Sequence[str]]],
) -> bool:
    """
    Whether the grouping already fixes one row per group for this column's table —
    the functional dependency both MySQL and PostgreSQL allow, and the reason
    ``SELECT c.id, c.name, COUNT(*) … GROUP BY c.id`` is a perfectly good query.

    True only when every primary key column of the column's **own** table is
    grouped. A column whose table cannot be identified has no primary key to check,
    so the answer is False and the caller reports the column — which is the right way
    round: the statement then goes back to the model, not to the database.
    """
    if not primary_keys:
        return False

    table, _ = _owned_column(reference, aliases)

    if not table and len(primary_keys) == 1:
        table = str(next(iter(primary_keys))).lower()

    key_columns = _primary_key_of(table, primary_keys) if table else []

    if not key_columns:
        return False

    grouped_here = {column for owner, column in grouped if owner == table}

    return all(column.lower() in grouped_here for column in key_columns)


def _primary_key_of(
    table: str,
    primary_keys: Mapping[str, Sequence[str]],
) -> List[str]:
    """One table's primary key columns, matched without regard to case."""
    for name, columns in primary_keys.items():
        if str(name).lower() == str(table).lower():
            return [str(column) for column in columns or [] if column]

    return []
