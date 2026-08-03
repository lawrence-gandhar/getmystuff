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

The checks run against the statement with **string literals, quoted identifiers
and comments blanked out** (:func:`stripped_literals`). Without that,
``WHERE action = 'delete'`` would read as a DELETE and ``WHERE note = 'a;b'`` as
two statements — both perfectly ordinary reads.
"""

import re
from typing import Optional

# Long enough for any query a person writes by hand or a model produces for one
# question; short enough that a runaway completion or a pasted dump is refused
# before it is stored, previewed and executed.
MAX_SQL_LENGTH = 8000

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


def read_only_violation(sql: Optional[str]) -> Optional[str]:
    """
    Why this statement may not be run, as a phrase, or ``None`` when it may.

    A phrase rather than an exception because the callers are answering different
    questions and owe the user different sentences: Tool Configs is rejecting
    something a person typed (a 400 they can fix), Ask AI is rejecting something a
    model returned (a 502 the user did not cause). Both read as
    ``f"The SQL query {reason}."``

    An empty statement returns ``None`` — "nothing to run" is not a violation, and
    whether a missing query is an error is the caller's rule, not this module's.
    """
    statement = normalised_sql(sql)

    if not statement:
        return None

    if len(statement) > MAX_SQL_LENGTH:
        return f"is longer than {MAX_SQL_LENGTH} characters"

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
