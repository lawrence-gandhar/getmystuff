"""
What a model is told exists, built from what actually exists.

**This is the single most effective anti-hallucination measure available here**, and it
works by omission rather than by instruction: a connector the user has no connection for is
simply absent from the catalogue, so a model asked to sync Shopify orders when there is no
Shopify connection has nothing to name. Telling a model not to invent things is a
suggestion; not giving it the vocabulary is a constraint.

**Built per call, never stored.** The ``_RULES_FINGERPRINT`` / ``is_prompt_stale`` machinery
in the Deep Agents module exists because a stored prompt has to track two independently
moving things. Nothing here is stored — and the catalogue *must* be fresh by construction,
because the ordinary sequence is somebody adding a connection and immediately asking for a
workflow that uses it. Staleness here is not a risk to mitigate; it is a bug avoided by not
caching.

**Markdown, not JSON Schema.** ``ai_analytics_service._json_only_instruction`` already
appends the draft's own schema for providers without strict structured output, and a second
JSON blob in the same prompt is what makes a 1.7B model return something unparseable. The
catalogue is a list a person could read.

Every list is capped. The caps are not about cost — they are about a prompt whose useful
part has been pushed out of the model's attention by a hundred field names it will never
use.
"""

import logging
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import CONNECTION_ACTIVE, IntegrationConnection
from app.services.integrations.connectors import registry, spec as connector_spec
from app.services.integrations.errors import IntegrationFailure

logger = logging.getLogger(__name__)

#: How many connections are described. Somebody with forty GoHighLevel locations has forty
#: connections that differ only by name; listing all of them crowds out the operations,
#: which is the part the model actually has to get right.
MAX_CONNECTIONS = 20

#: How many operations per connection.
MAX_OPERATIONS = 15

#: How many fields per operation. The destination's required fields are listed first, so a
#: truncated list still contains the ones a draft cannot omit.
MAX_FIELDS = 25

#: The whole catalogue's ceiling for a hosted provider. Enforced last, over the rendered
#: text, because the per-list caps bound the shape and this bounds the size — and it is the
#: size that decides whether a model can still see the user's own sentence at the end of the
#: prompt.
MAX_CHARS = 12_000

#: The ceiling for the in-built local model — a tenth of the other one, and the measured
#: reason follows because the obvious one is wrong.
#:
#: ``OLLAMA_NUM_CTX`` is 2048 and ``OLLAMA_NUM_PREDICT`` is 512, so a local prompt has 1536
#: tokens. Measured against ``qwen3:1.7b`` with one connection and two operations:
#:
#:     JSON schema appended by ``_json_only_instruction``   971 tokens
#:     system prompt                                        710 tokens
#:     catalogue + the user's sentence                     ~100 tokens
#:                                                        ─────────────
#:                                                         ~1786 tokens
#:
#: **The catalogue is not what fills the window** — the generated schema for
#: ``WorkflowDraft`` is, and it costs more than everything else combined. Capping the
#: catalogue is therefore not what makes the local path work, and this constant should not
#: be read as though it were. What it does is bound the *variable* part: twenty connections
#: with fifteen operations each really would be 12,000 characters, which turns an
#: over-budget prompt into an absurd one.
#:
#: The honest conclusion, recorded in ``documentations/INTEGRATIONS_AI.md``: **this task is
#: out of reach for a 1.7B model at a 2048-token context.** The irreducible cost exceeds the
#: budget before any catalogue is added. The feature degrades correctly — a sentence in the
#: panel, nothing saved, the canvas untouched — and a deployment that wants the local path
#: to work has to raise ``OLLAMA_NUM_CTX``.
#:
#: Truncation drops the *end* of the prompt, which is where ``user_content`` puts the user's
#: own sentence — deliberately, because that is where a model keeps it in view. So an
#: over-long prompt does not merely crowd the request out, it deletes it.
MAX_CHARS_INBUILT = 1_200

#: What a truncated section says. Present rather than silent, because a model that was
#: shown eight of twenty connections and told nothing will confidently report that the
#: ninth does not exist.
_TRUNCATED = "_…and more, not shown._"


async def build(db: AsyncSession, user_id: int) -> Dict[str, Any]:
    """
    Everything this user could actually build a workflow out of.

    Returns the structured form. :func:`render` turns it into the Markdown that goes in the
    prompt, and ``validate_draft`` resolves against the same structure — so what the model
    was offered and what it is checked against cannot disagree.
    """
    connections = await _usable_connections(db, user_id)

    described = []
    for connection in connections[:MAX_CONNECTIONS]:
        described.append(await _describe_connection(db, connection))

    return {
        "connections": described,
        "connections_total": len(connections),
        "truncated": len(connections) > MAX_CONNECTIONS,
    }


async def _usable_connections(
    db: AsyncSession, user_id: int
) -> List[IntegrationConnection]:
    """
    The connections a workflow could run against **today**.

    A revoked or switched-off connection is excluded rather than listed as unavailable.
    Listing it would invite a draft pointed at something that fails on its first record,
    and "this connection is off" is a sentence for the connections page rather than for a
    prompt.
    """
    from app.services.integrations import connection_service

    connections = await connection_service.list_connections(db, user_id)

    return [
        connection for connection in connections
        if connection.is_active and connection.status == CONNECTION_ACTIVE
    ]


async def _describe_connection(
    db: AsyncSession, connection: IntegrationConnection
) -> Dict[str, Any]:
    """One connection with what it can do. No base URL and no credential — see
    ``registry.describe_connectors``: this text reaches a model and then a log."""
    try:
        connector = registry.require(connection.connector_id)
    except IntegrationFailure:
        # A connector removed from the build under a live connection. Skipped from the
        # catalogue rather than raising: the other connections are still usable, and a
        # generation request should not fail because of one orphaned row.
        logger.warning(
            "Connection %s names connector '%s', which is not registered.",
            connection.uuid, connection.connector_id,
        )
        return {
            "uuid": str(connection.uuid),
            "label": connection.label,
            "connector": connection.connector_id,
            "operations": [],
        }

    return {
        "uuid": str(connection.uuid),
        "label": connection.label,
        "connector": connector.label,
        "account": connection.external_account_id or "",
        "operations": await _operations_for(db, connection, connector),
    }


async def _operations_for(
    db: AsyncSession, connection: IntegrationConnection, connector: Any
) -> List[Dict[str, Any]]:
    """Whatever this connection offers, from whichever source it has one."""
    if connector.operations_are_user_defined:
        from app.db.integrations import queries

        rows = await queries.list_rest_operations(db, connection.id)
        operations = [connector_spec.load_operation(row) for row in rows]
    else:
        operations = list(connector.operations)

    return [_describe_operation(op) for op in operations[:MAX_OPERATIONS]]


def _describe_operation(operation: Any) -> Dict[str, Any]:
    """
    One operation, with its required fields first.

    Required-first is what makes the field cap safe: a list truncated at twenty-five still
    contains every field a draft cannot leave unmapped, so the worst a truncation costs is
    an optional field nobody mapped rather than a workflow that cannot be published.
    """
    inputs = sorted(operation.inputs, key=lambda field: not field.required)

    return {
        "id": operation.operation_id,
        "label": operation.label or operation.operation_id,
        "kind": operation.kind,
        "description": operation.description or "",
        "inputs": [_describe_field(field) for field in inputs[:MAX_FIELDS]],
        "outputs": [_describe_field(field) for field in operation.outputs[:MAX_FIELDS]],
        "inputs_truncated": len(operation.inputs) > MAX_FIELDS,
        "outputs_truncated": len(operation.outputs) > MAX_FIELDS,
    }


def _describe_field(field: Any) -> Dict[str, Any]:
    return {
        "name": field.name,
        "type": field.type,
        "required": bool(field.required),
        "description": field.description or "",
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(catalogue: Dict[str, Any], *, max_chars: int = MAX_CHARS) -> str:
    """
    The catalogue as compact Markdown, capped to what the chosen model can actually read.

    ``max_chars`` is :data:`MAX_CHARS_INBUILT` for the local model and :data:`MAX_CHARS` for
    a hosted one — see the former on why the difference is arithmetic rather than taste.

    The cap is applied **at a section boundary**, never mid-line. A prompt that ends halfway
    through a field name reads as though that field is called something it is not, which is
    a worse failure than the field being absent: absent means the model cannot map it, and
    half-present means it maps to a name nothing accepts.
    """
    connections = catalogue.get("connections") or []

    if not connections:
        return (
            "## Connections\n\n"
            "**There are none.** No workflow can be built until the user adds a "
            "connection, so decline and say that.\n"
        )

    sections = ["## Connections\n"]

    for connection in connections:
        block = _render_connection(connection)
        if _length(sections) + len(block) > max_chars:
            sections.append(_TRUNCATED + "\n")
            break
        sections.append(block)

    if catalogue.get("truncated"):
        sections.append(_TRUNCATED + "\n")

    return "".join(sections)


def _length(sections: Sequence[str]) -> int:
    return sum(len(section) for section in sections)


def _render_connection(connection: Dict[str, Any]) -> str:
    """
    One connection and its operations.

    **The name is what the model is asked to use, and the uuid is not shown.** A model
    handed identifiers writes identifiers, including ones it invents; a model handed names
    either gets the name right or gets it recognisably wrong, and ``validate_draft``
    resolves the name to the real uuid. That is what makes the negative case — a made-up
    connection — detectable rather than plausible.
    """
    lines = [
        "\n### " + connection["label"],
        " (" + connection["connector"] + ")"
        if connection.get("connector") else "",
        "\n",
    ]

    if connection.get("account"):
        lines.append("Account: " + connection["account"] + "\n")

    operations = connection.get("operations") or []
    if not operations:
        lines.append("_No operations set up yet — this connection cannot be used._\n")
        return "".join(lines)

    for operation in operations:
        lines.append(_render_operation(operation))

    return "".join(lines)


def _render_operation(operation: Dict[str, Any]) -> str:
    lines = [
        "\n- **" + operation["id"] + "** (" + operation["kind"] + ") — " +
        (operation.get("label") or operation["id"]),
    ]

    if operation.get("description"):
        lines.append(": " + operation["description"])
    lines.append("\n")

    inputs = operation.get("inputs") or []
    if inputs:
        lines.append("  - accepts: " + ", ".join(_field_word(f) for f in inputs))
        if operation.get("inputs_truncated"):
            lines.append(", …")
        lines.append("\n")

    outputs = operation.get("outputs") or []
    if outputs:
        lines.append("  - returns: " + ", ".join(f["name"] for f in outputs))
        if operation.get("outputs_truncated"):
            lines.append(", …")
        lines.append("\n")

    return "".join(lines)


def _field_word(field: Dict[str, Any]) -> str:
    """``email (string, required)``. The type and the requirement are both said, because a
    draft that omits a required field cannot be published and a draft that maps text into a
    number field fails on its first record."""
    parts = [field["type"]]
    if field.get("required"):
        parts.append("required")
    return field["name"] + " (" + ", ".join(parts) + ")"


# ---------------------------------------------------------------------------
# Resolving, against the same structure the model was shown
# ---------------------------------------------------------------------------


def find_connection(catalogue: Dict[str, Any], spelling: str) -> Optional[Dict[str, Any]]:
    """
    The connection a model named, or ``None``.

    **Exact, then case-insensitive, then stop.** No fuzzy matching, no closest-match, no
    edit distance — and this is a decision rather than an omission. "Shopify Prod" quietly
    resolving to "Shopify EU" writes somebody's customers into the wrong store, and it does
    it silently, at 3am, on a schedule. A refusal that lists the real names costs one more
    exchange; the alternative costs a data migration.
    """
    wanted = str(spelling or "").strip()
    if not wanted:
        return None

    connections = catalogue.get("connections") or []

    for connection in connections:
        if connection["label"] == wanted:
            return connection

    lowered = wanted.lower()
    for connection in connections:
        if connection["label"].lower() == lowered:
            return connection

    return None


def find_operation(
    connection: Dict[str, Any], operation_id: str
) -> Optional[Dict[str, Any]]:
    """The operation a model named on this connection, exact then case-insensitive."""
    wanted = str(operation_id or "").strip()
    if not wanted:
        return None

    operations = connection.get("operations") or []

    for operation in operations:
        if operation["id"] == wanted:
            return operation

    lowered = wanted.lower()
    for operation in operations:
        if operation["id"].lower() == lowered:
            return operation

    return None


def connection_names(catalogue: Dict[str, Any]) -> List[str]:
    """The real names, for a refusal. **What makes a refusal useful** — "there is no
    'Shopify Prod'" leaves somebody guessing; the same sentence followed by the three names
    they do have is one they can act on."""
    return [connection["label"] for connection in (catalogue.get("connections") or [])]


def operation_ids(connection: Dict[str, Any], kind: str = "") -> List[str]:
    """The operation ids on one connection, optionally of one kind."""
    return [
        operation["id"]
        for operation in (connection.get("operations") or [])
        if not kind or operation["kind"] == kind
    ]


def input_names(operation: Dict[str, Any]) -> List[str]:
    return [field["name"] for field in (operation.get("inputs") or [])]


def required_inputs(operation: Dict[str, Any]) -> List[str]:
    return [
        field["name"] for field in (operation.get("inputs") or []) if field.get("required")
    ]
