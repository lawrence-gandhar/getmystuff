"""
What a connector is, as frozen data.

**Operations are declarative rows, not Python functions**, and that is the single most
load-bearing decision in the connector layer. Three reasons, in order of how much they
cost to give up:

1. **Determinism is a property of data.** Every run step records
   ``operation_hash = sha256(canonical_json(op))``. A replay that produces a different
   hash is *detectably* not the same run. A Python operation can only record a module
   path and a commit, so a hotfix silently changes what "replay" means — and the audit
   trail keeps claiming otherwise.

2. **Generic REST has to be data anyway.** The user authors it in a form, so it arrives
   as ``integration_rest_operations`` rows whatever we do. Two request builders means the
   well-exercised vendor path and the rotting user-facing one, and it is always the
   user-facing one that rots.

3. **It makes ``build_request`` pure.** No HTTP, no database, no credential. Every
   URL-escaping and injection question then lives in one function that a table-driven
   unit test can exhaust.

:func:`load_operation` returns the same frozen :class:`OperationSpec` from a database row
and from a vendor connector's own declaration. One code path, by construction.

**What data cannot express gets a hook, and the hook is fenced.** Shopify's GraphQL cost
accounting, SAP's ``X-CSRF-Token: Fetch`` handshake and GoHighLevel's company→location
exchange are genuinely procedural. :class:`ConnectorHooks` is the named, optional escape,
and it is fenced in both directions. On the way out, :func:`assert_hook_kept_the_target`
refuses a hook that changed the method, host, path or body — that check is what keeps the
audit hash honest, because without it "operations are data" would be true of the
declaration and false of what actually went out. On the way back, ``after_response`` may
only *raise*; its return value is ignored, so a hook cannot rewrite what the vendor said
into something the recorded audit no longer matches.

Everything here is frozen and hashable. Nothing in this module does I/O, reads a clock,
or imports a database or an HTTP library.
"""

import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import urlsplit

from app.models.integrations import (
    AUTH_API_KEY,
    AUTH_BASIC,
    AUTH_KIND_VALUES,
    AUTH_NONE,
    AUTH_OAUTH2,
    OPERATION_KINDS,
    OPERATION_READ,
    OPERATION_WRITE,
)
from app.services.integrations.engine.idempotency import operation_hash
from app.utils.type_coercion import TYPES

# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
# Six kinds, and every real API in scope is one of them. Declarative because "read the
# next page" is the step most likely to be got subtly wrong by hand — and a paging bug
# does not fail, it silently returns the first page and reports success.

#: One page. The whole response is the result.
PAGE_NONE = "none"

#: ``?page=2``. The oldest and least reliable — a page number over a list that is being
#: written to skips and repeats records, which is why an incremental read prefers a
#: cursor where the vendor offers one.
PAGE_NUMBER = "page_number"

#: ``?offset=500&limit=500``. Same hazard as page numbers, stated in records.
PAGE_OFFSET = "offset"

#: A token read out of the response and sent back as a parameter. Stable under
#: concurrent writes, which is why it is the preferred kind wherever it exists.
PAGE_CURSOR = "cursor"

#: A complete URL in the response body — SAP's ``@odata.nextLink``. **Re-validated and
#: asserted same-origin as page one** before it is followed: a next URL is chosen by the
#: server being read, so following it unchecked hands the choice of destination to
#: whoever controls the response.
PAGE_NEXT_URL = "next_url"

#: RFC 5988 ``Link: <…>; rel="next"`` — Shopify REST. **Used verbatim.** Rebuilding it
#: from parsed parameters drops ``page_info``, and Shopify rejects ``page_info``
#: combined with filters, so the reconstruction fails in exactly the case that matters.
PAGE_LINK_HEADER = "link_header"

#: A token read out of the response and sent back as one of the operation's **declared
#: inputs** rather than as a query parameter.
#:
#: The difference from :data:`PAGE_CURSOR` is where the cursor lands, and it is the whole
#: reason this kind exists. ``cursor`` puts the value in the query string, which is right
#: for a REST API and useless for Shopify's Admin GraphQL, where ``after:`` belongs in the
#: POST body's ``variables``. Feeding the cursor in as an *input* hands that choice back
#: to the operation: whatever its templates do with an input, the cursor now does too —
#: body, header or query — and pagination stays ignorant of the request shape.
#:
#: ``param`` and ``size_param`` therefore name inputs, not parameters, and
#: :meth:`OperationSpec.validated` refuses names the operation never declared.
PAGE_INPUT_CURSOR = "input_cursor"

PAGE_KINDS = (
    PAGE_NONE,
    PAGE_NUMBER,
    PAGE_OFFSET,
    PAGE_CURSOR,
    PAGE_INPUT_CURSOR,
    PAGE_NEXT_URL,
    PAGE_LINK_HEADER,
)

#: The kinds whose ``param``/``size_param`` name declared inputs rather than request
#: parameters. A tuple rather than a single comparison because a future vendor may need a
#: second input-shaped kind, and the checks that read this should not each grow an ``or``.
PAGE_INPUT_KINDS = (PAGE_INPUT_CURSOR,)

#: Ceilings that apply to every kind, so a malformed vendor response cannot become an
#: unattended infinite loop burning an API quota. The third guard — a repeated cursor —
#: is not expressible as a number and lives in ``runtime/pagination.py``.
DEFAULT_MAX_PAGES = 1000
DEFAULT_MAX_RECORDS = 1_000_000


# ---------------------------------------------------------------------------
# Where a credential goes
# ---------------------------------------------------------------------------
PLACEMENT_HEADER = "header"
PLACEMENT_QUERY = "query"

PLACEMENTS = (PLACEMENT_HEADER, PLACEMENT_QUERY)


# ---------------------------------------------------------------------------
# Where a connection's own identity goes
# ---------------------------------------------------------------------------
#: The one substitution ``base_url_template`` understands, filled from the connection's
#: ``external_account_id``. Deliberately not ``{shop}`` even though Shopify is the first
#: user of it: the field is generic, a second vendor would want a second name for the same
#: slot, and two names for one placeholder is the drift this module exists to avoid.
ACCOUNT_PLACEHOLDER = "{account}"


@dataclass(frozen=True)
class AuthSpec:
    """
    How a connection proves who it is, and where the proof is put.

    ``value_template`` is the one field with a placeholder: ``"Bearer {token}"``,
    ``"{api_key}"``, ``"Basic {basic}"``. The credential itself is substituted at send
    time and **never** appears in anything hashed, logged or previewed — which is why
    this spec holds the shape and not the secret.

    ``placement = query`` exists because some APIs still want the key in the URL. It is
    supported and quietly worse: a query string reaches access logs, proxies and
    ``Referer`` headers. Nothing here can fix that, but recording which placement a
    connection uses at least makes it answerable.
    """

    kind: str = AUTH_NONE
    placement: str = PLACEMENT_HEADER
    name: str = "Authorization"
    value_template: str = "Bearer {token}"

    # OAuth only. Plain strings rather than URLs so this stays data; the runtime
    # validates them through the same egress guard as any other outbound call.
    authorize_url: str = ""
    token_url: str = ""
    scopes: Tuple[str, ...] = ()

    # Whether the provider issues a new refresh token on every use. GoHighLevel and
    # Shopify online tokens do, and that single boolean is what makes the difference
    # between "write the new token" and "the connection is permanently locked out".
    rotates_refresh_token: bool = False

    def validated(self) -> "AuthSpec":
        if self.kind not in AUTH_KIND_VALUES:
            raise ValueError(
                f"'{self.kind}' is not a way of authenticating. Available: "
                f"{', '.join(sorted(AUTH_KIND_VALUES))}."
            )
        if self.placement not in PLACEMENTS:
            raise ValueError(
                f"A credential goes in a {' or a '.join(PLACEMENTS)}, not in "
                f"'{self.placement}'."
            )
        if self.kind == AUTH_OAUTH2 and not self.token_url:
            raise ValueError(
                "An OAuth connector needs a token URL — without one there is nothing to "
                "exchange the authorisation code at."
            )
        return self


@dataclass(frozen=True)
class RateLimitSpec:
    """
    What the far end will tolerate.

    ``requests_per_second`` and ``burst`` seed an in-memory leaky bucket, which is
    **corrected from the response** where the vendor reports its own state. That
    correction is not a refinement: Shopify's bucket is per shop and shared with every
    other app the merchant has installed, so a locally-computed bucket is sending into
    one somebody else has already drained.

    ``daily_limit`` is persisted in ``integration_rate_counters`` rather than held in
    memory, because an in-process counter resets on every deploy and a marketplace
    application that blows its daily cap gets suspended. It is the most
    account-endangering number in the module and the only one that survives a restart.
    """

    requests_per_second: float = 4.0
    burst: int = 8
    daily_limit: Optional[int] = None

    #: Stop locally at this fraction of the daily cap. Under rather than at, so a
    #: concurrent worker's in-flight requests cannot carry it over.
    daily_soft_limit: float = 0.95

    #: The response header carrying the vendor's own view of the bucket, if it has one.
    usage_header: str = ""

    def validated(self) -> "RateLimitSpec":
        if self.requests_per_second <= 0:
            raise ValueError("A rate limit of zero requests per second sends nothing.")
        if self.daily_limit is not None and self.daily_limit <= 0:
            raise ValueError("A daily limit of zero or fewer requests sends nothing.")
        return self


@dataclass(frozen=True)
class FieldSpec:
    """
    One field an operation accepts or returns.

    **The field list is the schema.** No ``jsonschema`` dependency: this same list draws
    the mapping panel's field picker and tells the workflow generator what a destination
    will accept, and one description of a field is worth more than two that can disagree.

    ``path`` is where the value lives in a response — ``customer.email``,
    ``line_items[*].sku`` — read by the restricted reader in ``mapping/paths.py``. Empty
    for an input, whose ``name`` is its whole identity.
    """

    name: str
    label: str = ""
    type: str = "string"
    required: bool = False
    description: str = ""
    path: str = ""
    example: Any = None

    def validated(self) -> "FieldSpec":
        if not str(self.name).strip():
            raise ValueError("A field needs a name.")
        if self.type not in TYPES:
            raise ValueError(
                f"'{self.name}' is declared as a '{self.type}', which is not a kind of "
                f"value. Available: {', '.join(TYPES)}."
            )
        return self

    def display_label(self) -> str:
        return self.label.strip() or self.name


@dataclass(frozen=True)
class PageRule:
    """
    How to get the next page, and when to stop.

    The stop conditions are on the rule rather than on the caller because "stop" is part
    of the contract with the vendor. A read that keeps going until the response is empty
    trusts the far end to eventually say so, and a malformed response that always returns
    the same page would loop forever, unattended, at whatever the rate limit allows.
    """

    kind: str = PAGE_NONE

    #: The request parameter carrying the page number, offset or cursor.
    param: str = ""

    #: How many records to ask for at a time, and the parameter that says so.
    size_param: str = ""
    size: Optional[int] = None

    #: Where the next cursor lives in the response — ``meta.next_cursor``. For
    #: ``next_url`` this is where the whole URL lives, e.g. ``@odata.nextLink``.
    cursor_path: str = ""

    #: Where the response says whether there is more. Optional: absence of a cursor is
    #: already a stop condition, and a vendor that lies about ``has_more`` while still
    #: returning a cursor should be believed about the cursor.
    has_more_path: str = ""

    #: The first value of ``param``. 1 for a page number, 0 for an offset.
    start_at: int = 1

    max_pages: int = DEFAULT_MAX_PAGES
    max_records: int = DEFAULT_MAX_RECORDS

    def validated(self) -> "PageRule":
        if self.kind not in PAGE_KINDS:
            raise ValueError(
                f"'{self.kind}' is not a way of paging through results. Available: "
                f"{', '.join(PAGE_KINDS)}."
            )

        if (
            self.kind in (PAGE_NUMBER, PAGE_OFFSET, PAGE_CURSOR, PAGE_INPUT_CURSOR)
            and not self.param
        ):
            raise ValueError(
                f"Paging by {self.kind.replace('_', ' ')} needs the name of the request "
                "parameter that carries it."
            )

        if (
            self.kind in (PAGE_CURSOR, PAGE_INPUT_CURSOR, PAGE_NEXT_URL)
            and not self.cursor_path
        ):
            raise ValueError(
                f"Paging by {self.kind.replace('_', ' ')} needs to know where in the "
                "response to find it."
            )

        if self.max_pages < 1 or self.max_records < 1:
            raise ValueError("A paging limit of zero would read nothing.")

        return self

    @property
    def is_paged(self) -> bool:
        return self.kind != PAGE_NONE


@dataclass(frozen=True)
class OperationSpec:
    """
    One thing a connector can do — a request shape, its inputs and its outputs.

    Every field here is a column on ``integration_rest_operations``, deliberately: a
    generic REST operation is a row, a vendor operation is a literal in that connector's
    module, and :func:`load_operation` turns either into this. If the two ever diverge,
    the row is the one that is right, because it is the one a user can edit.

    ``idempotent`` defaults to **False** and that default is the safe one. A write
    retried after a read timeout may already have happened; creating a second order is
    not something a backoff can undo. An operation earns the right to be retried by
    saying so, or by naming an ``idempotency_header`` the destination honours.

    ``ordered`` forces parallelism to one. ``asyncio.gather`` preserves the order of its
    *results* and not of the wire, which is invisible everywhere except an
    order-sensitive destination — SAP IDoc sequences — where it is a correctness bug.
    """

    operation_id: str
    label: str = ""
    description: str = ""
    kind: str = OPERATION_READ
    method: str = "GET"
    path: str = ""

    query_template: Mapping[str, Any] = field(default_factory=dict)
    header_template: Mapping[str, Any] = field(default_factory=dict)
    body_template: Optional[Mapping[str, Any]] = None

    #: Top-level ``body_template`` keys whose value is taken **verbatim**, with no
    #: ``{name}`` substitution anywhere inside it.
    #:
    #: This exists for GraphQL. A query document is nothing but braces, and the
    #: substituter reads the first ``{…}`` span in any string as an input name, so a
    #: document in a template is refused before a request is ever built.
    #:
    #: Declared key names rather than ``{{``-escaping, deliberately: doubling every brace
    #: in a thirty-line document is unreadable, and one brace missed is not an error but a
    #: silently different query. A key name is checked once, here, at import time.
    #:
    #: The literal is part of :meth:`canonical`, so the document is inside the operation's
    #: fingerprint. It has to be — the document *is* the operation, and a hash that did
    #: not cover it would let a replay run different GraphQL and still claim to match.
    body_literals: Tuple[str, ...] = ()

    inputs: Tuple[FieldSpec, ...] = ()
    outputs: Tuple[FieldSpec, ...] = ()

    records_path: str = ""
    page_rule: PageRule = field(default_factory=PageRule)

    idempotent: bool = False
    idempotency_header: str = ""
    ordered: bool = False
    timeout_seconds: Optional[int] = None

    #: This operation's own send rate, when the connector's single figure would be wrong
    #: for it. ``None`` — the usual case — means it spends the connector's allowance.
    #:
    #: Brevo is why this exists. It publishes limits that differ by 180× across the
    #: endpoints one connection reaches: 10/second for contacts, 5/second for order
    #: writes, 2/second for product writes, and 100 **per hour** for everything else. A
    #: single connector-wide figure has to be one of those, and both ends are wrong — the
    #: slowest throttles order writes to one every 36 seconds, and the fastest exhausts
    #: the retry engine's three attempts against the hourly ceiling and fails the run.
    #:
    #: **Vendor-declared only.** ``load_operation`` does not read it and no column backs
    #: it, so an ``integration_rest_operations`` row a user typed cannot raise its own
    #: send rate. That is deliberate rather than unfinished: a rate limit a user can set
    #: is a way to hammer somebody else's API from our egress address.
    rate_limits: Optional[RateLimitSpec] = None
    rate_limit_group: str = ""

    def validated(self) -> "OperationSpec":
        if not str(self.operation_id).strip():
            raise ValueError("An operation needs an id.")

        if self.kind not in OPERATION_KINDS:
            raise ValueError(
                f"'{self.operation_id}' is neither a read nor a write — it says "
                f"'{self.kind}'."
            )

        method = self.method.upper()
        if method not in _METHODS:
            raise ValueError(
                f"'{self.operation_id}' uses the method '{self.method}', which is not "
                f"one this can send. Available: {', '.join(sorted(_METHODS))}."
            )

        if not self.path.startswith("/"):
            raise ValueError(
                f"The path on '{self.operation_id}' must start with '/' — it is joined "
                "onto the connection's base URL, not used on its own."
            )

        if self.kind == OPERATION_WRITE and method in _SAFE_METHODS:
            # A write declared as a GET would slip past every rule that keys off
            # `kind` — the dry-run suppression, the retry restriction, the node-type
            # check — while still not writing anything.
            raise ValueError(
                f"'{self.operation_id}' is declared as a write but uses {method}, which "
                "does not change anything at the far end."
            )

        for spec in (*self.inputs, *self.outputs):
            spec.validated()

        self.page_rule.validated()

        if self.page_rule.is_paged and self.kind != OPERATION_READ:
            raise ValueError(
                f"'{self.operation_id}' is a write, so there are no pages to read."
            )

        self._check_body_literals()
        self._check_input_paging()
        self._check_rate_limits()

        return self

    def _check_rate_limits(self) -> None:
        """
        A group has to name an allowance, and the allowance has to be a usable one.

        The group without the limits is the failure worth catching. It reads exactly like
        an operation with its own budget, and it silently has not got one — the call falls
        back to the connector's figure, which is the number this field exists because it
        was wrong. A run then sends at somebody else's rate and only the vendor's 429s say
        so.
        """
        if self.rate_limits is not None:
            self.rate_limits.validated()
            return

        if self.rate_limit_group:
            raise ValueError(
                f"'{self.operation_id}' names the rate limit group "
                f"'{self.rate_limit_group}' but declares no limits of its own, so it "
                "would quietly spend the connector's allowance instead. Give it a "
                "RateLimitSpec or drop the group."
            )

    def _check_body_literals(self) -> None:
        """
        Every declared literal must actually be a key of the body.

        A name that matches nothing is the failure this catches: the key it was meant to
        protect goes through substitution as usual and raises somewhere far away, or —
        worse, if the value happens to parse as a placeholder — quietly sends something
        else. Refusing at import turns both into a startup error naming the typo.
        """
        if not self.body_literals:
            return

        body = self.body_template or {}
        for name in self.body_literals:
            if name not in body:
                raise ValueError(
                    f"'{self.operation_id}' says the body value '{name}' is a literal, "
                    "but its body has no such key. Its keys are: "
                    f"{', '.join(sorted(str(k) for k in body)) or 'none'}."
                )

    def _check_input_paging(self) -> None:
        """
        For an input-shaped page kind, ``param`` and ``size_param`` must name declared
        inputs.

        Without this a typo is not an error. The cursor would be passed as an argument the
        operation never declared, ``build_request`` drops undeclared arguments silently,
        and every page would be a fresh request for page one — read as new records each
        time, until the repeated-cursor guard eventually trips several thousand records
        into a sync that duplicated all of them.
        """
        if self.page_rule.kind not in PAGE_INPUT_KINDS:
            return

        declared = {spec.name for spec in self.inputs}

        for role, name in (
            ("cursor", self.page_rule.param),
            ("page size", self.page_rule.size_param),
        ):
            if name and name not in declared:
                raise ValueError(
                    f"'{self.operation_id}' pages by input and names '{name}' as its "
                    f"{role}, which is not one of its inputs. Its inputs are: "
                    f"{', '.join(sorted(declared)) or 'none'}."
                )

    @property
    def is_read(self) -> bool:
        return self.kind == OPERATION_READ

    @property
    def is_write(self) -> bool:
        return self.kind == OPERATION_WRITE

    @property
    def required_inputs(self) -> Tuple[str, ...]:
        return tuple(spec.name for spec in self.inputs if spec.required)

    def input_named(self, name: str) -> Optional[FieldSpec]:
        return next((spec for spec in self.inputs if spec.name == name), None)

    def canonical(self) -> Dict[str, Any]:
        """
        The form that gets hashed onto every step row.

        ``label`` and ``description`` are excluded on purpose: renaming an operation in
        the UI does not change what it sends, and a hash that moved for a typo fix would
        make every replay look like a different run. Everything that reaches the wire is
        in here.

        ``rate_limits`` and ``rate_limit_group`` are excluded for the same reason
        ``has_more_path`` is: they decide *when* a request leaves, never what it says.
        Retuning a limit after a vendor publishes new figures would otherwise move every
        fingerprint at once and make every prior run look like it ran something else.
        """
        return {
            "operation_id": self.operation_id,
            "kind": self.kind,
            "method": self.method.upper(),
            "path": self.path,
            "query": dict(self.query_template or {}),
            "headers": dict(self.header_template or {}),
            "body": dict(self.body_template) if self.body_template else None,
            # In the hash because a literal body value is the request. For GraphQL it is
            # the entire query document, and an operation whose fingerprint moved when the
            # label changed but not when the query did would make the replay claim false.
            "body_literals": list(self.body_literals),
            "inputs": [
                {"name": s.name, "type": s.type, "required": s.required}
                for s in self.inputs
            ],
            "records_path": self.records_path,
            "page": {
                "kind": self.page_rule.kind,
                "param": self.page_rule.param,
                # In the hash for an input-shaped kind this names an input whose value
                # goes out on every request, so it is as much a part of what was sent as
                # the size itself. `has_more_path` stays out: it reads the response and
                # never reaches the wire.
                "size_param": self.page_rule.size_param,
                "size": self.page_rule.size,
                "cursor_path": self.page_rule.cursor_path,
            },
            "idempotent": self.idempotent,
            "idempotency_header": self.idempotency_header,
            "ordered": self.ordered,
        }

    def fingerprint(self) -> str:
        """sha256 of :meth:`canonical`. What ``integration_run_steps.operation_hash``
        holds, and half of the determinism claim."""
        return operation_hash(self.canonical())


_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})
_SAFE_METHODS = frozenset({"GET", "HEAD"})


# ---------------------------------------------------------------------------
# The request, as data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedRequest:
    """
    Everything about one outbound call except the credential.

    Lives here rather than in ``runtime/`` so that :class:`ConnectorHooks` can be typed
    against it without the spec module depending on anything that does I/O. It is data,
    and keeping it data is what lets :func:`assert_hook_kept_the_target` compare a
    request before and after a hook touched it.

    The credential is **not** on this object. It is applied at the socket, from the
    :class:`AuthSpec`, so nothing that gets logged, previewed or hashed has ever held it.
    """

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    params: Mapping[str, Any] = field(default_factory=dict)
    json_body: Optional[Any] = None

    #: The host and path the egress guard resolved and approved, kept so the hook fence
    #: below can compare against what was checked rather than re-parsing.
    host: str = ""
    path: str = ""

    def with_headers(self, headers: Mapping[str, str]) -> "PreparedRequest":
        merged = dict(self.headers)
        merged.update(headers)
        return replace(self, headers=merged)

    def at_url(self, url: str) -> "PreparedRequest":
        """
        The same request aimed at a whole URL the vendor supplied.

        For Shopify's ``Link: rel="next"`` and SAP's ``@odata.nextLink``, which are used
        **verbatim** — rebuilding them from parsed parameters drops ``page_info`` and
        Shopify answers 400. The parameters are dropped for the same reason: the URL
        already carries everything the vendor wants, and re-appending page one's query
        is how a cursor ends up combined with filters it may not be combined with.

        ``host`` and ``path`` are recomputed rather than carried over, because they are
        what the egress fence compares against — leaving page one's host on a request
        aimed somewhere else would be a fence that checks the wrong thing.
        """
        split = urlsplit(url)
        return replace(
            self,
            url=url,
            params={},
            host=split.hostname or "",
            path=split.path or "",
        )

    def target(self) -> Tuple[str, str, str]:
        return (self.method.upper(), self.host.lower(), self.path)


class HookViolation(Exception):
    """
    A connector hook changed something it may not change.

    A programming error in a connector rather than anything a user did, so the message
    is written for whoever is editing that connector. It is raised rather than logged
    because the alternative is sending a request whose recorded ``operation_hash``
    describes a different request — an audit trail that is quietly false is worse than a
    run that failed.
    """


def assert_hook_kept_the_target(
    before: PreparedRequest, after: PreparedRequest, *, connector_id: str
) -> None:
    """
    The fence around :class:`ConnectorHooks`.

    A hook may add or replace headers and report throttle state. It may **not** change
    the method, the host or the path — those are what the egress guard approved and what
    the operation hash claims. Enforced by comparison rather than by convention, because
    a convention is only as good as the last person who read it, and the failure it
    prevents is silent.
    """
    if before.target() != after.target():
        raise HookViolation(
            f"The '{connector_id}' connector's before_request hook changed where the "
            f"request goes: {before.target()} became {after.target()}. A hook may add "
            "headers and nothing else — the destination is checked before it runs and "
            "recorded on the run step afterwards, and both would now be wrong."
        )

    if before.json_body != after.json_body:
        raise HookViolation(
            f"The '{connector_id}' connector's before_request hook changed the request "
            "body. The body comes from the operation and its mapped values; a hook that "
            "rewrites it makes the recorded operation a description of something else."
        )


class ConnectorHooks(Protocol):
    """
    The narrow, typed escape from "operations are data".

    Every method is optional, and each exists because a specific vendor cannot be
    expressed declaratively:

    ``resolve_base_url``
        Shopify derives its base URL from the shop domain rather than storing one, so a
        user cannot point a trusted connector somewhere untrusted. Falls back to
        :meth:`ConnectorSpec.render_base_url`, which covers every connector whose URL is
        just a template.

    ``before_request``
        SAP's ``X-CSRF-Token: Fetch`` handshake. Fenced by
        :func:`assert_hook_kept_the_target`: it may add or replace headers and nothing
        else.

    ``after_response``
        **The seam for an API that reports failure inside a success.** Shopify's Admin
        GraphQL answers a permissions error with HTTP 200 and an ``errors`` array, and
        without somewhere to look at that, the run reads no records, stops because "the
        last page was empty" and ends green. A sync of an empty store and a sync that was
        refused are indistinguishable, which is the worst shape a failure can take.

        Called after the body is parsed and before the status is judged. **Its return
        value is ignored; it may only raise.** Same reasoning as the ``before_request``
        fence — a hook that could rewrite a response is a hook that could make the
        recorded audit disagree with what the vendor actually said. Correcting the rate
        limiter from the body belongs here too: it is a side effect on the bucket, not a
        change to the response.

    ``classify_error``
        Turning a vendor-specific error body into a retry verdict. GoHighLevel returns
        401 for both an expired token and a revoked install, and only one of those is
        worth retrying. **Declared and not yet wired** — no connector implements it and
        ``sender`` does not call it. Left in place because GoHighLevel is a named later
        phase; do not implement it expecting it to fire.
    """

    def resolve_base_url(self, connection: Any) -> str: ...

    def before_request(
        self, request: PreparedRequest, context: Any
    ) -> PreparedRequest: ...

    def after_response(
        self, read: Any, operation: "OperationSpec", context: Any
    ) -> None: ...

    def classify_error(self, error: BaseException, response: Any) -> Optional[Any]: ...


@dataclass(frozen=True)
class ConnectorSpec:
    """
    One connector: how to authenticate, what it will tolerate, and what it can do.

    ``allows_private_hosts`` is the gate on the on-premise escape hatch, and it is set on
    the *connector* rather than only on the connection for a reason: it means a generic
    REST connection can never be aimed inside the network, whatever an administrator
    ticks. In Phase 1 nothing sets it; ``sap_odata`` will be the only connector that
    does.
    """

    connector_id: str
    label: str
    description: str = ""
    auth: AuthSpec = field(default_factory=AuthSpec)
    rate_limits: RateLimitSpec = field(default_factory=RateLimitSpec)
    operations: Tuple[OperationSpec, ...] = ()

    #: How this connector appears on the Apps page — a Line Awesome class and a brand
    #: colour for the tile behind it.
    #:
    #: Here rather than in a mapping in the template for the same reason ``label`` and
    #: ``description`` are here: a connector is added by writing one module and
    #: registering it, and a second place listing every connector by id is a place that
    #: falls behind — which shows up as a new app rendering with no icon at all. Both have
    #: defaults, so a connector that says nothing gets a generic plug rather than a blank
    #: square.
    icon: str = "las la-plug"
    accent: str = "#6c757d"

    #: ``https://{account}/admin/api/2026-07``. ``{account}`` is substituted from the
    #: connection's ``external_account_id``. Empty when the connection supplies its own
    #: base URL, which is what generic REST does.
    base_url_template: str = ""

    #: Whether the user types the base URL. True only for generic REST — a vendor
    #: connector computing its own is what stops "Shopify" pointing at an attacker's
    #: host with a valid-looking label.
    base_url_is_user_supplied: bool = False

    #: What the connection's ``external_account_id`` has to look like, and what to call
    #: it in the form. For Shopify that is the shop domain, and the pattern is the
    #: security control that makes the whole vendor-connector shape safe: the value is
    #: user-supplied text that becomes **the host of an outbound request carrying a
    #: credential**. An unconstrained one is a way to aim a trusted connector, and its
    #: stored token, at any host on the internet.
    #:
    #: Matched with ``re.fullmatch``, so a pattern does not need anchors and cannot be
    #: defeated by one that forgot them.
    account_id_pattern: str = ""
    account_id_label: str = ""
    account_id_help: str = ""
    account_id_required: bool = False

    allows_private_hosts: bool = False
    requires_https: bool = True

    #: Whether operations come from ``integration_rest_operations`` rows rather than
    #: from this spec. Generic REST only.
    operations_are_user_defined: bool = False

    hooks: Optional[ConnectorHooks] = None

    def validated(self) -> "ConnectorSpec":
        if not str(self.connector_id).strip():
            raise ValueError("A connector needs an id.")

        self.auth.validated()
        self.rate_limits.validated()

        seen = set()
        for operation in self.operations:
            operation.validated()
            if operation.operation_id in seen:
                raise ValueError(
                    f"The '{self.connector_id}' connector declares two operations "
                    f"called '{operation.operation_id}'."
                )
            seen.add(operation.operation_id)

        if not self.base_url_template and not self.base_url_is_user_supplied:
            raise ValueError(
                f"The '{self.connector_id}' connector has no base URL and does not ask "
                "the user for one, so there is nowhere to send a request."
            )

        self._check_account_id()

        if self.allows_private_hosts and not self.requires_https:
            # Both together is the widest possible posture. It is what an on-premise
            # SAP gateway actually needs, so it is permitted — but only deliberately,
            # and the connection still needs an admin, an explicit host:port and a CIDR.
            pass

        return self

    def _check_account_id(self) -> None:
        """
        The account id has to be usable before anything is sent, not after.

        Two separate demands, both at import: the pattern has to compile, and a connector
        that *builds its address* from the account id has to both require one and say what
        a valid one looks like. Without the second, an empty value renders
        ``https:///admin/...`` and an unconstrained one aims the connector — and the
        credential it carries — at whatever host was typed.
        """
        if self.account_id_pattern:
            try:
                re.compile(self.account_id_pattern)
            except re.error as exc:
                raise ValueError(
                    f"The '{self.connector_id}' connector's account pattern is not a "
                    f"valid expression: {exc}."
                ) from exc

        if ACCOUNT_PLACEHOLDER not in self.base_url_template:
            return

        if not self.account_id_required:
            raise ValueError(
                f"The '{self.connector_id}' connector builds its address from the "
                "account id, so the account id has to be required."
            )

        if not self.account_id_pattern:
            raise ValueError(
                f"The '{self.connector_id}' connector puts the account id into its "
                "address, so it needs a pattern saying what a valid one looks like."
            )

    @property
    def account_id_name(self) -> str:
        """What to call the account id in a sentence — 'Shop domain', or a fallback."""
        return self.account_id_label.strip() or "account id"

    def validated_account_id(self, value: Any) -> str:
        """
        One connection's ``external_account_id``, checked against this connector.

        Raises :class:`ValueError` with a sentence the owner can act on. Called from two
        places on purpose — once in ``connection_service`` so a bad value is refused at
        the form, and again in :meth:`render_base_url` immediately before it becomes a
        hostname. The second is not redundant: the first protects the person typing, the
        second protects the request, and only the second is still true if some future code
        path writes the column without going through the form.
        """
        text = str(value or "").strip()

        if not text:
            if self.account_id_required:
                raise ValueError(
                    f"This connection needs a {self.account_id_name.lower()}"
                    + (f" — for example {self.account_id_help}." if self.account_id_help else ".")
                )
            return ""

        if self.account_id_pattern and not re.fullmatch(self.account_id_pattern, text):
            raise ValueError(
                f"'{text}' is not a valid {self.account_id_name.lower()} for "
                f"{self.label}."
                + (f" It should look like {self.account_id_help}." if self.account_id_help else "")
            )

        return text

    def render_base_url(self, connection: Any) -> str:
        """
        Where this connection's requests go.

        Three sources, in this order: the connector's own hook, then its template with the
        account id substituted, then whatever the connection stored. The order is the
        point — a vendor connector's address is computed and a generic one's is typed, and
        putting the stored value last is what stops a typed URL overriding a computed one.

        Raises :class:`ValueError` rather than returning an empty string. An empty base URL
        does not fail; it produces a request to a relative path that the egress guard then
        refuses with a message about the URL's shape, which tells the owner nothing about
        the connection that actually caused it.
        """
        hook = getattr(self.hooks, "resolve_base_url", None)
        if hook is not None:
            resolved = str(hook(connection) or "").strip()
            if resolved:
                return resolved.rstrip("/")

        if self.base_url_template:
            account = self.validated_account_id(
                getattr(connection, "external_account_id", "")
            )
            return self.base_url_template.replace(ACCOUNT_PLACEHOLDER, account).rstrip("/")

        stored = str(getattr(connection, "base_url", "") or "").strip()
        if stored:
            return stored.rstrip("/")

        raise ValueError(
            f"'{getattr(connection, 'label', self.label)}' has no address to send "
            "requests to. Open it on the Connections page and supply one."
        )

    def operation(self, operation_id: str) -> Optional[OperationSpec]:
        return next(
            (op for op in self.operations if op.operation_id == operation_id), None
        )

    def readable_operations(self) -> Tuple[OperationSpec, ...]:
        return tuple(op for op in self.operations if op.is_read)

    def writable_operations(self) -> Tuple[OperationSpec, ...]:
        return tuple(op for op in self.operations if op.is_write)


# ---------------------------------------------------------------------------
# One code path from a row or a declaration
# ---------------------------------------------------------------------------


def load_operation(source: Any) -> OperationSpec:
    """
    Build an :class:`OperationSpec` from a database row, a plain mapping, or another
    spec.

    The whole point of the function: ``connector_api`` calls it once and never learns
    whether this operation came from a vendor module or from a form somebody filled in.
    A branch there would be the seam through which the two paths drift.

    Validated on the way out rather than trusted. A row is user input — it was typed into
    a form — and a spec that was never checked is a request nobody predicted.
    """
    if isinstance(source, OperationSpec):
        return source.validated()

    data = _as_mapping(source)

    return OperationSpec(
        operation_id=str(data.get("operation_id") or "").strip(),
        label=str(data.get("label") or "").strip(),
        description=str(data.get("description") or "").strip(),
        kind=str(data.get("kind") or OPERATION_READ).strip(),
        method=str(data.get("method") or "GET").strip().upper(),
        path=str(data.get("path") or "").strip(),
        query_template=dict(data.get("query_template") or {}),
        header_template=dict(data.get("header_template") or {}),
        body_template=(
            dict(data["body_template"]) if data.get("body_template") else None
        ),
        body_literals=tuple(str(name) for name in (data.get("body_literals") or ())),
        inputs=load_fields(data.get("inputs")),
        outputs=load_fields(data.get("outputs")),
        records_path=str(data.get("records_path") or "").strip(),
        page_rule=load_page_rule(data.get("page_rule")),
        idempotent=bool(data.get("idempotent")),
        idempotency_header=str(data.get("idempotency_header") or "").strip(),
        ordered=bool(data.get("ordered")),
        timeout_seconds=data.get("timeout_seconds"),
    ).validated()


def load_fields(raw: Any) -> Tuple[FieldSpec, ...]:
    """A JSONB list of field dictionaries, as specs."""
    if not raw:
        return ()

    if not isinstance(raw, (list, tuple)):
        raise ValueError("An operation's field list must be a list.")

    return tuple(
        FieldSpec(
            name=str(item.get("name") or "").strip(),
            label=str(item.get("label") or "").strip(),
            type=str(item.get("type") or "string").strip(),
            required=bool(item.get("required")),
            description=str(item.get("description") or "").strip(),
            path=str(item.get("path") or "").strip(),
            example=item.get("example"),
        ).validated()
        for item in raw
        if isinstance(item, Mapping)
    )


def load_page_rule(raw: Any) -> PageRule:
    """A JSONB paging dictionary, as a rule. Absent means one page."""
    if not raw:
        return PageRule()

    if isinstance(raw, PageRule):
        return raw.validated()

    data = _as_mapping(raw)

    return PageRule(
        kind=str(data.get("kind") or PAGE_NONE).strip(),
        param=str(data.get("param") or "").strip(),
        size_param=str(data.get("size_param") or "").strip(),
        size=data.get("size"),
        cursor_path=str(data.get("cursor_path") or "").strip(),
        has_more_path=str(data.get("has_more_path") or "").strip(),
        start_at=int(data.get("start_at") or 1),
        max_pages=int(data.get("max_pages") or DEFAULT_MAX_PAGES),
        max_records=int(data.get("max_records") or DEFAULT_MAX_RECORDS),
    ).validated()


def _as_mapping(source: Any) -> Mapping[str, Any]:
    """
    A mapping of columns, from a mapping or from an ORM row.

    Reading attributes off a row rather than requiring the caller to convert it, so
    ``load_operation(row)`` is what the call site says — the alternative is a
    dict-comprehension repeated at every call, each free to forget a column.
    """
    if isinstance(source, Mapping):
        return source

    return {
        name: getattr(source, name, None)
        for name in (
            "operation_id", "label", "description", "kind", "method", "path",
            "query_template", "header_template", "body_template", "inputs", "outputs",
            "records_path", "page_rule", "idempotent", "idempotency_header", "ordered",
            "timeout_seconds",
            # No column backs this one yet: `body_literals` is declared by vendor
            # connectors in Python and there is no migration adding it to
            # `integration_rest_operations`. `getattr` returns None for a row, which
            # becomes an empty tuple — so a user-authored REST operation behaves exactly
            # as it did before. Listed here so that adding the column later is a
            # migration and nothing else.
            "body_literals",
            # `rate_limits` and `rate_limit_group` are deliberately **not** here, and
            # adding them later should not be a migration. A row is a form somebody filled
            # in; an operation that could declare its own send rate would let that form set
            # how fast we hammer a third party from our egress address. Vendor connectors
            # declare them in Python, where a reviewer sees the number.
        )
    }


def describe_operation(operation: OperationSpec) -> Dict[str, Any]:
    """
    One operation as the canvas and the AI catalogue see it.

    Never includes the request shape. A field picker needs to know what a destination
    accepts; it does not need the URL, and putting one in a payload the browser receives
    is how an internal endpoint ends up in somebody's devtools.
    """
    return {
        "operation_id": operation.operation_id,
        "label": operation.label or operation.operation_id,
        "description": operation.description,
        "kind": operation.kind,
        "paged": operation.page_rule.is_paged,
        "idempotent": operation.idempotent,
        "inputs": [
            {
                "name": s.name,
                "label": s.display_label(),
                "type": s.type,
                "required": s.required,
                "description": s.description,
            }
            for s in operation.inputs
        ],
        "outputs": [
            {
                "name": s.name,
                "label": s.display_label(),
                "type": s.type,
                "path": s.path,
                "description": s.description,
            }
            for s in operation.outputs
        ],
    }


__all__ = [
    "ACCOUNT_PLACEHOLDER",
    "AUTH_API_KEY",
    "AUTH_BASIC",
    "AUTH_NONE",
    "AUTH_OAUTH2",
    "AuthSpec",
    "ConnectorHooks",
    "ConnectorSpec",
    "FieldSpec",
    "HookViolation",
    "OperationSpec",
    "PAGE_CURSOR",
    "PAGE_INPUT_CURSOR",
    "PAGE_INPUT_KINDS",
    "PAGE_KINDS",
    "PAGE_LINK_HEADER",
    "PAGE_NEXT_URL",
    "PAGE_NONE",
    "PAGE_NUMBER",
    "PAGE_OFFSET",
    "PLACEMENT_HEADER",
    "PLACEMENT_QUERY",
    "PageRule",
    "PreparedRequest",
    "RateLimitSpec",
    "assert_hook_kept_the_target",
    "describe_operation",
    "load_fields",
    "load_operation",
    "load_page_rule",
]
