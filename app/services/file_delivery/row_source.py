"""
Where a Create File block's rows come from, and what "no rows" means in each case.

One function per canvas, both returning the same :class:`Payload`, so
:mod:`file_writer` never learns which canvas it is serving.

**The rule this module exists to keep: what reaches the file is everything, or the block
fails.** Every source below is either exact or refused by name. The temptation each one
offers is a smaller file that looks whole:

* a Run Graph block's ``GraphOutcome.rows`` is a **twenty-row preview** with the real total
  beside it. Reading it would produce a twenty-row CSV of a five-thousand-row result and
  nothing about the file would say so. So the run's *id* is what the flow session keeps,
  and this module reads the whole result back with ``graph_runner.full_result`` at file
  time — the function that exists for exactly this distinction, and whose docstring spells
  it out.
* an AI Fallback's answer table is complete by construction (it is the model's own small
  table), but the flow engine caps what it stores. A table that hit that cap is marked
  ``truncated`` and is **refused** here rather than written short.
* a variable holding prose is not a table, and no amount of splitting on whatever
  separators it happens to contain makes it one. TXT accepts it; the other three refuse it
  and say which choice to change.

**No expression evaluator.** A path into a node's output is read by
``integrations/mapping/paths.py``'s restricted reader, the same discipline
``email_dispatch/variable_sources`` states and for the same reason: these are
operator-authored strings, and anything that *evaluates* one is a way to make the
application compute something nobody reviewed.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file_delivery import SOURCE_BLOCK, SOURCE_NODE, SOURCE_VARIABLE
from app.services.file_delivery import file_writer
from app.services.file_delivery.errors import SourceError
from app.services.integrations.mapping import paths
from app.services.integrations.mapping.paths import PathError

logger = logging.getLogger(__name__)


# The column an unnamed list of values lands in. A list of scalars is a legitimate result
# — one column of ids — and it has no column name of its own, so one is supplied rather
# than the block being refused over a shape it can perfectly well write.
_UNNAMED_COLUMN = "value"


@dataclass(frozen=True)
class Payload:
    """
    What a block is about to write: rows, or text.

    Exactly one of the two is set. Two fields rather than one ``Any`` because the
    difference decides which formats are legal — text is TXT-only — and a caller that has
    to sniff the type is a caller that will get it wrong once.

    ``description`` says where it came from, in words, for the log line the runner writes.
    Not shown to a visitor.
    """

    rows: Optional[List[Dict[str, Any]]] = None
    text: Optional[str] = None
    description: str = ""

    @property
    def row_count(self) -> int:
        """How many rows there are. Text is 0 rows, which is what the file row-count means."""
        return len(self.rows or [])


async def resolve_flow_data(
    db: AsyncSession,
    *,
    user_id: int,
    node_results: Mapping[str, Any],
    variables: Mapping[str, Any],
    data: Mapping[str, Any],
    block_label: str = "",
) -> Payload:
    """
    A flow's Create File block: a named block's result, or a variable.

    ``db`` is accepted and not used, and that is on purpose rather than an oversight: the
    graph-run read goes through ``graph_runner``, which opens its own session — a run's
    rows are read from the checkpointer as the graph's owner, not through the caller's
    conversation session. Keeping the parameter means the signature does not change the
    day a source needs the database, and the runner already has one to hand.
    """
    source = str(data.get("source") or "").strip().lower()

    if source == SOURCE_BLOCK:
        return await _from_block(
            user_id=user_id,
            node_results=node_results,
            block_id=str(data.get("block_id") or "").strip(),
            block_label=block_label,
        )

    if source == SOURCE_VARIABLE:
        return _from_variable(
            variables, str(data.get("name") or "").strip(), block_label,
        )

    raise SourceError(
        f"This block reads its data from '{source}', which a conversation cannot "
        "provide. Choose a block earlier in the flow, or a variable.",
        block=block_label,
    )


def resolve_graph_data(
    *,
    outputs: Mapping[str, Any],
    data: Mapping[str, Any],
    node_label_of,  # noqa: ANN001 — callable(node_id) -> str, supplied by the runner
    block_label: str = "",
) -> Payload:
    """
    A graph's Create File node: an earlier node's output, optionally through a path.

    Synchronous, because everything it needs is already in the run's state. A SQL node's
    output *is* the full row list — ``node_runners._run_sql`` passes ``max_rows=None`` and
    nothing on that path caps — so there is no preview/total distinction to get wrong here,
    which is why the flow side is the longer of the two.
    """
    source = str(data.get("source") or "").strip().lower()

    if source != SOURCE_NODE:
        raise SourceError(
            f"This node reads its data from '{source}', which a graph cannot provide. "
            "Point it at an earlier node's output.",
            block=block_label,
        )

    node_id = str(data.get("source_node") or "").strip()

    if not node_id:
        raise SourceError(
            "This node has no data source chosen. Point it at an earlier node whose "
            "output holds the rows.",
            block=block_label,
        )

    if node_id not in outputs:
        raise SourceError(
            f"'{node_label_of(node_id)}' has not produced anything in this run, so "
            "there is nothing to write. It has to run before this node.",
            block=block_label,
        )

    value = _through_path(outputs[node_id], str(data.get("path") or "").strip(), block_label)

    return _payload_of(
        value,
        described_as=f"the output of '{node_label_of(node_id)}'",
        block_label=block_label,
    )


# --------------------------------------------------------------------------
# Flow sources
# --------------------------------------------------------------------------

async def _from_block(
    *,
    user_id: int,
    node_results: Mapping[str, Any],
    block_id: str,
    block_label: str,
) -> Payload:
    """One earlier block's result, read exactly."""
    if not block_id:
        raise SourceError(
            "This block has no data source chosen. Point it at a Run Graph or AI "
            "Fallback block earlier in the flow, or at a variable.",
            block=block_label,
        )

    record = (node_results or {}).get(block_id)

    if not isinstance(record, dict):
        raise SourceError(
            "The block this one takes its data from has not produced anything in this "
            "conversation yet. It has to run before this block — check the order they "
            "are wired in.",
            block=block_label,
        )

    kind = str(record.get("kind") or "")

    if kind == "graph_run":
        return await _from_graph_run(user_id, record, block_label)

    if kind == "table":
        return _from_table(record, block_label)

    # A kind this version does not know. Refused rather than guessed at: a stored record
    # written by a newer engine could be any shape, and writing a file out of a shape
    # nobody here understands is how a download becomes nonsense.
    raise SourceError(
        "The block this one takes its data from produced something this cannot turn "
        "into a file.",
        block=block_label,
    )


async def _from_graph_run(
    user_id: int, record: Mapping[str, Any], block_label: str,
) -> Payload:
    """
    A Run Graph block's result — **every** row, re-read from the run.

    The ceiling is checked twice: once against the total the run already reported, before
    a single row is read back, and again by ``file_writer.write_rows`` against what
    actually arrived. The first is what makes an impossible file cheap to refuse.
    """
    from app.services.graph_designer import graph_runner

    run_id = str(record.get("run_id") or "")

    if not run_id:
        raise SourceError(
            "The Run Graph block this one takes its data from did not record a run.",
            block=block_label,
        )

    file_writer.assert_within_ceiling(int(record.get("total_rows") or 0), block_label)

    result = await graph_runner.full_result(user_id, run_id)

    if result is None:
        raise SourceError(
            "The result of the Run Graph block this one takes its data from could no "
            "longer be read. Runs are kept for a limited time — put the Create File "
            "block in the same conversation turn as the graph it reads.",
            block=block_label,
        )

    return _payload_of(
        result, described_as=f"graph run {run_id}", block_label=block_label,
    )


def _from_table(record: Mapping[str, Any], block_label: str) -> Payload:
    """
    An AI Fallback block's answer table.

    A table the engine had to cut short is **refused**, not written short. The engine marks
    it, precisely so this can be a refusal rather than a silently smaller file — see
    ``engine_service._store_node_result``.
    """
    if record.get("truncated"):
        raise SourceError(
            "The AI answer's table was too large to keep whole, so a file made from it "
            "would be missing rows. Point this block at a Run Graph block instead, "
            "which reads every row.",
            block=block_label,
        )

    columns = [str(name) for name in (record.get("columns") or [])]
    rows = record.get("rows") or []

    if not columns:
        raise SourceError(
            "The AI answer had no table in it, so there is nothing to write. An AI "
            "Fallback block only produces a table when its answer has one.",
            block=block_label,
        )

    return Payload(
        rows=[_row_of(columns, row) for row in rows],
        description="an AI Fallback answer table",
    )


def _row_of(columns: Sequence[str], row: Any) -> Dict[str, Any]:
    """
    One ``AnalyticsTable`` row — a list of cells — against its column names.

    Short rows are padded and long ones keep their extra cells under generated names.
    Neither should happen (the model is asked for a rectangular table), and both are
    better than dropping a value or failing the file: a cell that arrives under
    ``column_4`` is visible and fixable, a cell that vanished is not.
    """
    cells = list(row or [])
    paired = {name: (cells[index] if index < len(cells) else None) for index, name in enumerate(columns)}

    for index in range(len(columns), len(cells)):
        paired[f"column_{index + 1}"] = cells[index]

    return paired


def _from_variable(
    variables: Mapping[str, Any], name: str, block_label: str,
) -> Payload:
    """
    A conversation variable: a dataset if it holds one, text otherwise.

    An **absent** variable is refused rather than written as an empty file. The two are
    genuinely different — "the flow never set FILE_DATA" is a wiring mistake, "the query
    matched nothing" is an answer — and only the first is something the operator needs to
    hear about.
    """
    if not name:
        raise SourceError(
            "This block has no variable chosen to take its data from.",
            block=block_label,
        )

    if name not in (variables or {}):
        raise SourceError(
            f"This conversation has no value for {{{{{name}}}}}, so there is nothing "
            "to write. Check the name against the block that sets it.",
            block=block_label,
        )

    return _payload_of(
        (variables or {}).get(name),
        described_as=f"the variable {{{{{name}}}}}",
        block_label=block_label,
    )


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------

def _through_path(value: Any, path: str, block_label: str) -> Any:
    """Read ``path`` out of ``value``, or the value itself when no path was given."""
    if not path:
        return value

    try:
        return paths.read(value, path)
    except PathError as exc:
        raise SourceError(
            f"The path '{path}' could not be read: {exc}", block=block_label,
        ) from exc


def _payload_of(value: Any, *, described_as: str, block_label: str) -> Payload:
    """
    One resolved value as rows or as text.

    The shapes, and why each is what it is:

    ``[{...}, {...}]``
        Rows. What every SQL query on either canvas produces.
    ``[1, 2, 3]``
        One column, named ``value`` — a legitimate result with no column name of its own.
    ``{...}``
        One row. A single record is a table with one line in it, not an error.
    ``[[...], [...]]``
        **Refused.** There are no column names anywhere in it, and inventing
        ``column_1..n`` for somebody's data would put invented headers in a file they send
        on. The sentence says to name the columns in the query.
    anything else
        Text. Legal for TXT, refused for the other three by ``file_writer.write_text``.
    """
    if isinstance(value, Mapping):
        return Payload(rows=[dict(value)], description=described_as)

    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        return _text_or_dataset(value, described_as, block_label)

    items = list(value)

    if not items:
        return Payload(rows=[], description=described_as)

    if all(isinstance(item, Mapping) for item in items):
        return Payload(rows=[dict(item) for item in items], description=described_as)

    if any(isinstance(item, (list, tuple, Mapping)) for item in items):
        raise SourceError(
            f"{described_as.capitalize()} holds rows with no column names, so a file "
            "made from it would have invented headers. Name the columns in the query "
            "behind it.",
            block=block_label,
        )

    return Payload(
        rows=[{_UNNAMED_COLUMN: item} for item in items], description=described_as,
    )


def _text_or_dataset(value: Any, described_as: str, block_label: str) -> Payload:
    """
    A scalar, or a string that might be a dataset somebody stored as JSON.

    The JSON attempt is what makes "a dataset as a variable" work: a flow variable is a
    string, so rows that travelled through one arrive as ``'[{"id": 1}]'``. It is tried
    only when the string starts with ``[`` or ``{`` — otherwise every sentence an AI
    Fallback ever produced would be run through a JSON parser to be told it is not JSON.
    """
    if value is None:
        return Payload(text="", description=described_as)

    text = value if isinstance(value, str) else str(value)
    trimmed = text.strip()

    if trimmed[:1] in ("[", "{"):
        try:
            parsed = json.loads(trimmed)
        except ValueError:
            logger.info(
                "%s looks like JSON but could not be parsed; writing it as text",
                described_as,
            )
        else:
            return _payload_of(
                parsed, described_as=described_as, block_label=block_label,
            )

    return Payload(text=text, description=described_as)
