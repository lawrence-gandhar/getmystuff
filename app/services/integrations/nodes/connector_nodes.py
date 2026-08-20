"""
The two nodes that talk to somebody else's system.

Everything above this file is arithmetic on records; everything below it is HTTP. This is
where the two meet, and it is deliberately the only place — a second way to call an
endpoint is a second place to get authentication wrong.

**Reading opens a supply and hands back a handle.** ``connector_read`` does not read
fifty thousand records; it builds a :class:`~batching.PagedSupply` over the operation's
pagination rule and stashes it in the record buffer. The ``batch`` node pulls from it a
batch at a time. Nothing large ever enters the LangGraph state, and a run that is
cancelled on page three has fetched three pages rather than all of them.

**Writing is where the care goes.** Four rules, each of which exists because of a way a
merchant's data gets damaged:

*Chunked and bounded.* A batch of five hundred becomes ten calls of fifty, run
concurrently under a semaphore the run owns. Never across nodes — fanning out over the
drawing makes "same input, same API calls in the same order" untrue — and never at all
when the operation declares ``ordered``, because SAP IDoc sequences mean what their order
says they mean.

*One outcome per record, not one per chunk.* A chunk of fifty where record seven was
rejected is forty-nine written and one failed, with a row naming the record. Reporting
the chunk as failed loses forty-nine successful writes and tells the operator to re-run
something that already worked.

*A retry only where a retry is safe.* ``idempotency.write_may_be_retried`` decides, and
its default is not to. A read timeout on a ``POST /orders.json`` may well have created
the order, and no amount of backoff makes re-sending it safe.

*A dry run calls nobody.* Every request is built, every payload validated, and what would
have been sent is recorded as a ``sample`` row. Building the request is the point — a dry
run that skipped the builder would not catch the template that refers to a field the
operation does not declare.
"""

import asyncio
import logging
import uuid as uuid_pkg
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.db.db_utils import CRUDQueryBuilder
from app.models.integrations import (
    CONNECTION_ACTIVE,
    RECORD_FAILED,
    RECORD_SAMPLE,
    RECORD_SKIPPED,
    IntegrationConnection,
)
from app.services.integrations.connectors import registry
from app.services.integrations.connectors.spec import ConnectorSpec, OperationSpec
from app.services.integrations.credentials import credential_service
from app.services.integrations.engine import batching, record_log
from app.services.integrations.errors import IntegrationFailure, NodeFailure
from app.services.integrations.mapping import dedupe, paths
from app.services.integrations.runtime import (
    http_client,
    pagination,
    request_builder,
    response_reader,
    sender,
)
from app.utils import outbound_http

logger = logging.getLogger(__name__)

connection_crud = CRUDQueryBuilder(IntegrationConnection)

#: How many records go in one request when the operation does not say. Small on purpose:
#: a destination that takes a hundred at a time will say so, and guessing high means a
#: rejected batch takes a hundred records down instead of ten.
DEFAULT_CHUNK_SIZE = 50

#: How many chunks of one batch are in flight at once, when nothing narrower applies. The
#: run's own semaphore bounds this further; this is the per-node ceiling.
DEFAULT_PARALLELISM = 4


@dataclass
class ResolvedTarget:
    """A connection, its connector and the operation a node named — resolved once."""

    connection: IntegrationConnection
    connector: ConnectorSpec
    operation: OperationSpec
    context: sender.SendContext

    @property
    def base_url(self) -> str:
        """
        Where this connection's requests go, computed by the connector.

        Delegated rather than read off the row because a vendor connector has no stored
        URL — it derives one from the shop domain, and deliberately so, since a typed base
        URL is how "Shopify" ends up pointing at somebody else's host with a valid-looking
        label. :meth:`ConnectorSpec.render_base_url` re-checks the account id against the
        connector's pattern on the way past.

        The ``ValueError`` becomes a ``NodeFailure`` here so it takes the drawn error path
        and names the connection, rather than surfacing as whatever the egress guard makes
        of an empty address.
        """
        try:
            return self.connector.render_base_url(self.connection)
        except ValueError as exc:
            raise NodeFailure(str(exc)) from exc

    @property
    def fingerprint(self) -> str:
        """The sha256 that goes on the step row. Half of the determinism claim — a replay
        producing a different hash is detectably not the same run."""
        return self.operation.fingerprint()

    @property
    def operation_label(self) -> str:
        return self.operation.label.strip() or self.operation.operation_id


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------


async def resolve_target(db, node_data: Mapping[str, Any], *, user_id: int) -> ResolvedTarget:  # noqa: ANN001
    """
    Turn a node's ``connection_uuid`` and ``operation_id`` into everything a call needs.

    **Scoped to the user in the query, not checked afterwards.** A workflow with a
    connection uuid pasted in by hand — or written by a language model that invented
    one — must not be able to reach a row its owner does not own, and the way to
    guarantee that is for the row never to be loaded.

    A connection that is not active is refused here rather than at the request, so the
    failure names the connection and says what to do about it instead of surfacing as a
    401 from a vendor.
    """
    connection = await connection_crud.get_by_uuid(
        db, _connection_uuid(node_data), extra_filters={"user_id": user_id}
    )
    if connection is None:
        raise NodeFailure(
            "The connection this step uses no longer exists. Open the step and choose "
            "one."
        )

    if not connection.is_active or connection.status != CONNECTION_ACTIVE:
        raise NodeFailure(
            f"'{connection.label}' is not usable — it is {connection.status.replace('_', ' ')}. "
            "Reconnect it from the Connections page and run this again."
        )

    try:
        connector, operation = await registry.resolve_operation(
            db, connection, str(node_data.get("operation_id") or "")
        )
    except IntegrationFailure as exc:
        # The registry speaks in the operator's words already; wrapping it as a
        # NodeFailure is what puts it on the drawn error path.
        raise NodeFailure(str(exc)) from exc

    header, query = await credential_service.auth_for(db, connection, connector)

    return ResolvedTarget(
        connection=connection,
        connector=connector,
        operation=operation,
        context=sender.SendContext(
            connection_key=str(connection.uuid),
            connection_label=connection.label,
            egress_policy=_policy_for(connection, connector),
            auth_header=header,
            auth_query=query,
            connector=connector,
            timeout=float(operation.timeout_seconds or http_client.DEFAULT_TIMEOUT_SECONDS),
        ),
    )


def _connection_uuid(node_data: Mapping[str, Any]) -> uuid_pkg.UUID:
    """
    The connection a node names, as a real uuid.

    ``graph_data`` is JSON, so the value arrives as a string however it got there — the
    canvas, an import, or a language model. Parsed here rather than passed through,
    because handing a string to a ``UUID`` column produces a database error naming
    ``.hex``, which is a sentence about SQLAlchemy in the middle of somebody's sync.

    A value that is not a uuid at all is the shape a hallucination takes — a model that
    writes ``"shopify-prod"`` where an identifier belongs — so the refusal says what the
    field is for rather than what the parser wanted.
    """
    raw = str(node_data.get("connection_uuid") or "").strip()
    if not raw:
        raise NodeFailure("This step does not say which connection to use.")

    try:
        return uuid_pkg.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        raise NodeFailure(
            f"'{raw}' is not a connection. Open this step and choose one from the list."
        )


def _policy_for(
    connection: IntegrationConnection, connector: ConnectorSpec
) -> outbound_http.EgressPolicy:
    """
    The egress rules for this connection.

    The private-host hatch needs **both** the connector to allow it and the connection to
    have been switched on by an administrator — and the ``allows_private_hosts`` check
    happens here rather than being trusted from the row, so a generic REST connection with
    ``allow_private_hosts`` set by hand in the database still cannot be aimed inside the
    network.
    """
    if not (connector.allows_private_hosts and connection.allow_private_hosts):
        return outbound_http.DEFAULT_POLICY

    allowlist = connection.private_host_allowlist or {}
    return outbound_http.EgressPolicy(
        require_https=True,
        allow_private=True,
        allowed_hosts=frozenset(allowlist.get("hosts") or ()),
        allowed_cidrs=tuple(allowlist.get("cidrs") or ()),
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def open_supply(
    target: ResolvedTarget,
    arguments: Mapping[str, Any],
    *,
    on_page: Optional[Any] = None,
) -> batching.PagedSupply:
    """
    A supply that fetches pages of this operation on demand.

    Nothing is requested here. The first call goes out when the ``batch`` node asks for
    its first batch, which is what makes a cancelled run stop having fetched one page
    rather than all of them — and what lets ``connector_read`` be a fast node that cannot
    time out.

    ``on_page`` is called with each :class:`~response_reader.ReadResponse` before the
    records are extracted. It is how the incremental cursor gets saved in Phase 2 without
    this function knowing anything about cursors.
    """
    first = request_builder.build_request(
        target.operation, arguments, base_url=target.base_url
    )
    walk = pagination.begin(target.operation.page_rule, first.url)

    async def fetch_page(current: pagination.PageWalk) -> batching.Page:
        request = request_builder.build_request(
            target.operation,
            # The walk's arguments last, so a cursor it worked out wins over a stale one
            # left in the node's own configuration. A node cannot usefully pin the cursor
            # anyway — that is the walk's job — and letting it would freeze the read on
            # page one.
            {**arguments, **(current.arguments or {})},
            base_url=target.base_url,
            extra_query=current.params or {},
        )
        if current.next_url:
            # A whole URL from the vendor — Shopify's `Link` header, SAP's
            # `@odata.nextLink`. Used verbatim, because rebuilding it from parsed
            # parameters drops `page_info` and Shopify 400s. It has already been checked
            # same-origin as page one by `pagination._step_url`.
            request = request.at_url(current.next_url)

        read = await sender.send(request, target.operation, target.context)

        if not read.ok:
            raise NodeFailure(
                response_reader.failure_message(read, label=target.connection.label),
                retryable=sender.is_retryable_status(read.status_code),
                permanent=sender.is_auth_status(read.status_code),
                status_code=read.status_code,
            )

        if on_page is not None:
            on_page(read)

        return batching.Page(
            records=paths.read_records(read.payload, target.operation.records_path),
            payload=read.payload,
            headers=read.headers,
        )

    return batching.PagedSupply(fetch_page, walk)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@dataclass
class WriteOutcome:
    """
    What one write node did with one batch.

    Counts are **deltas**, never totals — ``flow_state._accumulate`` sums them across
    every pass, and a runner returning a running total would make a fifty-thousand record
    run report the size of its last batch.
    """

    written: int = 0
    failed: int = 0
    skipped: int = 0
    entries: List[dict] = field(default_factory=list)
    sync_keys: List[Tuple[str, str]] = field(default_factory=list)


async def write_batch(
    db,  # noqa: ANN001
    target: ResolvedTarget,
    records: Sequence[Mapping[str, Any]],
    *,
    node_id: str,
    node_data: Mapping[str, Any],
    batch_index: int,
    dry_run: bool,
    semaphore: Optional[asyncio.Semaphore] = None,
    cancelled: Optional[Any] = None,
) -> WriteOutcome:
    """
    Send one batch, one outcome per record.

    The order of operations matters and is fixed: dedupe **before** anything is sent, so a
    record already written becomes an update rather than a second create; then chunk; then
    send the chunks concurrently under the run's semaphore; then record what each record
    became.

    ``cancelled`` is an awaitable predicate checked between chunks. That is the contract
    the UI states — cancel stops at the next record boundary, not mid-request — and
    checking between chunks rather than between records is what keeps it from costing a
    query per record.
    """
    if not target.operation.is_write:
        raise NodeFailure(
            f"'{target.operation_label}' is a read operation, so this step cannot write "
            "with it. Choose an operation that creates or updates records."
        )

    outcome = WriteOutcome()
    if not records:
        return outcome

    key = dedupe.load_natural_key(node_data.get("natural_key"))
    decisions = await dedupe.plan_writes(
        db,
        connection_id=target.connection.id,
        operation_id=target.operation.operation_id,
        records=list(records),
        key=key,
    )

    sendable: List[dedupe.WriteDecision] = []
    for decision in decisions:
        if decision.writes:
            sendable.append(decision)
            continue

        outcome.skipped += 1
        outcome.entries.append(
            record_log.entry(
                node_id=node_id,
                outcome=RECORD_SKIPPED,
                batch_index=batch_index,
                message=dedupe.duplicate_message(decision, key),
                source_key=decision.natural_key[:16] or None,
                payload=dict(decision.record),
            )
        )

    chunk_size = _chunk_size(node_data)
    limit = 1 if target.operation.ordered else _parallelism(node_data)
    gate = semaphore or asyncio.Semaphore(limit)

    attempted = 0
    for group in batching.chunks(sendable, chunk_size * limit):
        if cancelled is not None and await cancelled():
            # Everything still to go, not merely this group. A cancelled batch that
            # counted only the chunk it was holding would leave the rest unaccounted
            # for — the run would say "0 written, 1 skipped" out of five hundred, and
            # the operator would have no way to tell what happened to the other 499.
            outcome.skipped += len(sendable) - attempted
            break

        attempted += len(group)
        pieces = list(batching.chunks(group, chunk_size))
        results = await asyncio.gather(
            *(
                _send_chunk(
                    target, piece,
                    node_id=node_id, batch_index=batch_index,
                    dry_run=dry_run, gate=gate,
                )
                for piece in pieces
            )
        )
        for part in results:
            outcome.written += part.written
            outcome.failed += part.failed
            outcome.entries.extend(part.entries)
            outcome.sync_keys.extend(part.sync_keys)

    return outcome


async def _send_chunk(
    target: ResolvedTarget,
    decisions: Sequence[dedupe.WriteDecision],
    *,
    node_id: str,
    batch_index: int,
    dry_run: bool,
    gate: asyncio.Semaphore,
) -> WriteOutcome:
    """
    One request carrying one chunk, turned into one outcome per record in it.

    The per-record loop below is what stops a single rejected record failing forty-nine
    good ones. When the destination takes records one at a time — which the generic REST
    connector's operations usually do — the chunk *is* one record and this is trivially
    true; when it takes many, the caller is expected to have set ``chunk_size`` to match
    and the whole chunk shares the outcome, which is the honest reading of what the
    vendor told us.
    """
    outcome = WriteOutcome()

    async with gate:
        for decision in decisions:
            result = await _send_one(
                target, decision,
                node_id=node_id, batch_index=batch_index, dry_run=dry_run,
            )
            outcome.written += result.written
            outcome.failed += result.failed
            outcome.entries.extend(result.entries)
            outcome.sync_keys.extend(result.sync_keys)

    return outcome


async def _send_one(
    target: ResolvedTarget,
    decision: dedupe.WriteDecision,
    *,
    node_id: str,
    batch_index: int,
    dry_run: bool,
) -> WriteOutcome:
    outcome = WriteOutcome()
    arguments = dict(decision.record)

    if decision.action == dedupe.UPDATE and decision.target_record_id:
        # The destination's own id for a record we have written before. Supplied as an
        # input so the operation's path template can use it — `/contacts/{id}` — without
        # this layer knowing which vendor spells it which way.
        arguments.setdefault("id", decision.target_record_id)

    try:
        request = request_builder.build_request(
            target.operation, arguments, base_url=target.base_url
        )
    except ValueError as exc:
        # A template naming a field the operation does not declare, or a value that will
        # not coerce. The record is at fault or the mapping is; either way it is one
        # record, not the batch.
        outcome.failed += 1
        outcome.entries.append(
            record_log.entry(
                node_id=node_id, outcome=RECORD_FAILED, batch_index=batch_index,
                message=str(exc), source_key=decision.natural_key[:16] or None,
                payload=arguments, retryable=False,
            )
        )
        return outcome

    if dry_run:
        # Built, validated, and not sent. Recording what *would* have gone out is the
        # whole value of a dry run — and building the request first is why it catches a
        # broken template rather than only a broken mapping.
        outcome.entries.append(
            record_log.entry(
                node_id=node_id, outcome=RECORD_SAMPLE, batch_index=batch_index,
                message=f"{request.method} {request.url}",
                source_key=decision.natural_key[:16] or None,
                payload={"body": request.json_body, "query": dict(request.params or {})},
            )
        )
        return outcome

    try:
        read = await sender.send(request, target.operation, target.context)
    except NodeFailure as exc:
        # `exc.retryable` was decided by the sender, which is the code that made the
        # call and the only thing that knows whether the request could have arrived.
        # Re-deriving it here from the message is how a merchant ends up with two of
        # everything — see `idempotency.write_may_be_retried`.
        outcome.failed += 1
        outcome.entries.append(
            record_log.entry(
                node_id=node_id, outcome=RECORD_FAILED, batch_index=batch_index,
                message=str(exc),
                source_key=decision.natural_key[:16] or None,
                payload=arguments,
                retryable=bool(exc.retryable),
            )
        )
        return outcome

    if not read.ok:
        outcome.failed += 1
        outcome.entries.append(
            record_log.entry(
                node_id=node_id, outcome=RECORD_FAILED, batch_index=batch_index,
                message=response_reader.failure_message(
                    read, label=target.connection.label
                ),
                source_key=decision.natural_key[:16] or None,
                payload=arguments,
                retryable=sender.is_retryable_status(read.status_code),
            )
        )
        return outcome

    outcome.written += 1
    target_id = _target_id(read, target.operation)
    if decision.natural_key and target_id:
        outcome.sync_keys.append((decision.natural_key, target_id))

    return outcome


def _target_id(read: response_reader.ReadResponse, operation: OperationSpec) -> str:
    """
    What the destination called the record it just created.

    Read from the operation's declared output path when it has one, then from the obvious
    places. Empty when there is nothing to find, which means no sync key is written — and
    that is right: a key pointing at an id we guessed is worse than no key, because the
    next run turns a create into an update against something that may not exist.
    """
    for candidate in (operation.records_path, "id", "data.id", "resource_id"):
        if not candidate:
            continue
        found = paths.read(read.payload, candidate)
        if isinstance(found, (str, int)) and str(found).strip():
            return str(found)
    return ""


def _chunk_size(node_data: Mapping[str, Any]) -> int:
    raw = node_data.get("chunk_size") or DEFAULT_CHUNK_SIZE
    try:
        size = int(raw)
    except (TypeError, ValueError):
        size = DEFAULT_CHUNK_SIZE
    return max(1, min(size, 1000))


def _parallelism(node_data: Mapping[str, Any]) -> int:
    raw = node_data.get("parallelism") or DEFAULT_PARALLELISM
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        limit = DEFAULT_PARALLELISM
    return max(1, min(limit, 16))
