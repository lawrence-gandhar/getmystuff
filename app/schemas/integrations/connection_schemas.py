"""
app/schemas/integrations/connection_schemas.py

Pydantic schemas for connections, their credentials and their operations.

**Secrets travel in one direction only.** ``api_key``, ``password`` and ``client_secret``
are fields on *request* schemas and appear on no response schema in this package. That is
the whole shape of the rule: a request carries a secret inward, ``store_credential``
encrypts it, and nothing sends one back out — not masked, not truncated, not at all. The
connections page shows a mask built by ``credential_service.mask_secret`` from the value
the browser already has in hand, never one round-tripped from the database.

**A blank secret is not a cleared secret.** These fields are ``OptionalText``, so an
untouched input arrives as ``None`` rather than as ``""``, and ``update_connection``
reads that as "leave it alone". The edit form shows a mask, so the field arrives empty on
every save where somebody only fixed a typo in the label — treating that as a deletion
would break a working connection with nothing on screen saying so.

**An operation is validated by being loaded, not by being re-described here.**
``connection_service`` turns a submitted operation into an ``OperationSpec`` through the
same ``load_operation`` the runtime calls, so an operation that saves is one that loads.
This layer bounds the JSON fields' *shape* and stops there; a second set of rules written
for the form is how the form and the runtime come to disagree, and the disagreement
surfaces as a workflow that saved fine and fails at 3am.
"""

from typing import Any, ClassVar, Dict, List, Mapping, Optional

from pydantic import Field, field_validator

from app.models.integrations import (
    OPERATION_KINDS,
    OPERATION_READ,
)
from app.schemas.base import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    MAX_URL_LENGTH,
    CheckboxBool,
    FormRequest,
    JsonArrayField,
    JsonObjectField,
    OptionalText,
    QueryRequest,
    RequiredText,
    ResponseSchema,
)

#: How long an operation id may be. The column's width, and the reason it is bounded here
#: as well is that this value ends up in a step's ``operation_id``, in log lines and in the
#: AI catalogue — three places where an over-long identifier is somebody else's problem.
MAX_OPERATION_ID_LENGTH = 64

#: A path is joined onto the connection's base URL. Bounded well under the column so a
#: refusal is a sentence rather than a truncation.
MAX_PATH_LENGTH = 512

#: How many host and network entries the on-premise escape hatch accepts. Mirrors
#: ``connection_service.MAX_ALLOWLIST_ENTRIES`` — restated rather than imported, which is
#: the direction every other schema in this application takes with a service's cap, so the
#: schema layer does not depend on a service.
MAX_ALLOWLIST_ENTRIES = 10

#: How many fields one operation may declare on each side. Every input is a column in the
#: mapping grid and a line in the AI catalogue; past this the panel is unusable and the
#: prompt is over budget.
MAX_OPERATION_FIELDS = 100

#: The verbs an operation may use. A closed set rather than free text, because the verb
#: decides whether the retry rules treat a call as a write — and a lower-case ``post`` that
#: failed that comparison would be retried after a timeout, which is how a timed-out order
#: becomes two orders. Upper-cased before the comparison, here and in the service.
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

# Labels used on more than one schema. Named so a request field and the response field it
# round-trips to cannot drift apart on screen — somebody who typed into "API address"
# should read "API address" back, not "Base URL".
_API_ADDRESS = "API address"
_OPERATION_TYPE = "Operation type"
_INPUTS = "Fields it accepts"
_OUTPUTS = "Fields it returns"


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class ConnectionCreateRequest(FormRequest):
    """
    The Add connection form.

    ``connector_id`` decides the auth kind, the operations and the egress rules, so it is
    fixed at creation and absent from :class:`ConnectionUpdateRequest` — changing it would
    keep a credential issued by one vendor pointed at another's endpoints.
    """

    connector_id: RequiredText = Field(title="Connector", max_length=64)
    label: RequiredText = Field(title="Name", max_length=MAX_NAME_LENGTH)
    base_url: OptionalText = Field(
        default=None, title=_API_ADDRESS, max_length=MAX_URL_LENGTH
    )
    external_account_id: OptionalText = Field(
        default=None, title="Account", max_length=MAX_NAME_LENGTH
    )

    # Inward only. See the module docstring.
    api_key: OptionalText = Field(default=None, title="API key", max_length=4096)
    username: OptionalText = Field(default=None, title="Username", max_length=MAX_NAME_LENGTH)
    password: OptionalText = Field(default=None, title="Password", max_length=4096)


class ConnectionUpdateRequest(FormRequest):
    """
    The Edit connection form.

    A secret left blank means "leave it alone" — the module docstring says why that is not
    the same as clearing it, and why clearing is its own action with its own name.
    """

    label: RequiredText = Field(title="Name", max_length=MAX_NAME_LENGTH)
    base_url: OptionalText = Field(
        default=None, title=_API_ADDRESS, max_length=MAX_URL_LENGTH
    )

    api_key: OptionalText = Field(default=None, title="API key", max_length=4096)
    username: OptionalText = Field(default=None, title="Username", max_length=MAX_NAME_LENGTH)
    password: OptionalText = Field(default=None, title="Password", max_length=4096)


class ConnectionTestQuery(QueryRequest):
    """
    Which call to make when somebody presses Test.

    Blank means "the connector's first read operation", chosen by the service. A read,
    never a write: a write test would create a record in somebody's production system to
    prove that it could.
    """

    operation_id: str = Field(
        default="", title="Operation", max_length=MAX_OPERATION_ID_LENGTH
    )


class PrivateHostRequest(FormRequest):
    """
    The on-premise escape hatch.

    **The administrator check is not here.** It is in the service, because a business rule
    a second route could skip is not a rule — and this is the one setting in the module
    that lets a request reach an address inside the network. What this schema bounds is the
    list: explicit entries, each one short, and no more than ten of them, because an
    allow-list long enough to be convenient is one nobody audits.
    """

    multi_fields: ClassVar[tuple] = ("hosts", "cidrs")

    allow: CheckboxBool = Field(default=False, title="Allow private addresses")
    hosts: List[str] = Field(
        default_factory=list, title="Allowed hosts", max_length=MAX_ALLOWLIST_ENTRIES
    )
    cidrs: List[str] = Field(
        default_factory=list, title="Allowed network ranges", max_length=MAX_ALLOWLIST_ENTRIES
    )

    @field_validator("hosts", "cidrs")
    @classmethod
    def _clean(cls, values: List[str]) -> List[str]:
        return [str(v).strip() for v in values if str(v or "").strip()]


class OperationSaveRequest(FormRequest):
    """
    One user-authored REST operation, as the form posts it.

    The template fields arrive as JSON in hidden inputs, which is why they are
    ``JsonObjectField``/``JsonArrayField`` rather than plain dicts: a browser that posts a
    malformed one is refused with a sentence about that field, instead of having the user's
    work silently discarded and being told the save succeeded.

    Everything past shape — that the path's placeholders name declared inputs, that the
    page rule is coherent — is decided by ``load_operation``, which is the function the
    runtime uses. See the module docstring.
    """

    operation_id: RequiredText = Field(
        title="Operation id", max_length=MAX_OPERATION_ID_LENGTH
    )
    label: RequiredText = Field(title="Name", max_length=MAX_NAME_LENGTH)
    description: OptionalText = Field(
        default=None, title="Description", max_length=MAX_DESCRIPTION_LENGTH
    )
    kind: str = Field(default=OPERATION_READ, title=_OPERATION_TYPE)
    method: str = Field(default="GET", title="Request method")
    path: str = Field(default="", title="Path", max_length=MAX_PATH_LENGTH)

    # These four titles match their field names on purpose. ``schemas/base`` builds a
    # refusal from ``ValidationInfo``, which exposes a field's *name* and not its
    # ``title`` — so a field labelled "Headers" and named ``header_template`` would put
    # "Headers" on the form and "Header template" in the error about it. Same word in both
    # places, and the name is the one that has to read as English.
    query_template: JsonObjectField = Field(
        default_factory=dict, title="Query template"
    )
    header_template: JsonObjectField = Field(default_factory=dict, title="Header template")
    body_template: JsonObjectField = Field(default_factory=dict, title="Body template")

    inputs: JsonArrayField = Field(
        default_factory=list, title=_INPUTS, max_length=MAX_OPERATION_FIELDS
    )
    outputs: JsonArrayField = Field(
        default_factory=list, title=_OUTPUTS, max_length=MAX_OPERATION_FIELDS
    )

    records_path: str = Field(
        default="", title="Where the records are", max_length=MAX_PATH_LENGTH
    )
    page_rule: JsonObjectField = Field(default_factory=dict, title="Page rule")

    idempotent: CheckboxBool = Field(default=False, title="Safe to send twice")
    idempotency_header: str = Field(
        default="", title="Idempotency header", max_length=64
    )
    ordered: CheckboxBool = Field(default=False, title="Order matters")
    timeout_seconds: Optional[int] = Field(
        default=None, title="Timeout", ge=1, le=3600
    )

    @field_validator("inputs", "outputs")
    @classmethod
    def _fields(cls, values: List[Any], info: Any) -> List[Any]:
        """
        Every entry is an object with a name.

        ``JsonArrayField`` guarantees a *list*; that a list of field descriptors contains
        descriptors is this schema's job. It matters because the alternative is silence:
        ``connection_service._fields_or_none`` used to skip an entry that was not a
        mapping, so a form with one malformed row saved green and lost that field — and
        the first anybody heard of it was a workflow that could not map to it.

        The entry is numbered from one, because somebody counting rows on a screen does.
        """
        label = _INPUTS if info.field_name == "inputs" else _OUTPUTS

        for index, entry in enumerate(values, start=1):
            if not isinstance(entry, Mapping):
                raise ValueError(f"{label} (entry {index}) is not in the expected format")
            if not str(entry.get("name") or "").strip():
                raise ValueError(f"{label} (entry {index}) needs a field name")

        return values

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        text = str(value or "").strip()
        if text not in OPERATION_KINDS:
            raise ValueError(
                f"{_OPERATION_TYPE} is not one of the allowed values: "
                f"{', '.join(sorted(OPERATION_KINDS))}"
            )
        return text

    @field_validator("method")
    @classmethod
    def _method(cls, value: str) -> str:
        text = str(value or "").strip().upper()
        if text not in HTTP_METHODS:
            raise ValueError(
                f"Request method is not one of the allowed values: {', '.join(HTTP_METHODS)}"
            )
        return text

    def operation(self) -> Dict[str, Any]:
        """
        This form as the mapping ``connection_service.save_operation`` takes.

        The service takes one mapping rather than twenty keyword arguments, because that is
        what an operation is — ``integration_rest_operations``' columns *are*
        ``OperationSpec``'s fields — and a call site enumerating them would be a third
        place that list is written down.
        """
        return self.payload()


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class ConnectionView(ResponseSchema):
    """
    One connection as the list page reads it.

    **There is no credential field of any kind**, and there is no bigint ``id``. Both are
    enforced by the class rather than observed: ``ResponseSchema`` is ``extra="ignore"``,
    so a view function that started emitting either would have it dropped here rather than
    serialised.
    """

    uuid: str = Field(title="Connection")
    label: str = Field(title="Name")
    connector_id: str = Field(title="Connector")
    connector_label: str = Field(default="", title="Connector name")
    auth_kind: str = Field(default="", title="Authentication")
    base_url: str = Field(default="", title=_API_ADDRESS)
    external_account_id: str = Field(default="", title="Account")
    status: str = Field(title="Status")
    is_active: bool = Field(default=True, title="Switched on")
    needs_reauth: bool = Field(default=False, title="Needs reconnecting")
    allow_private_hosts: bool = Field(default=False, title="Reaches private addresses")
    created_at: Optional[Any] = Field(default=None, title="Added")


class OperationFieldView(ResponseSchema):
    """One field an operation accepts or returns, for the mapping grid."""

    name: str = Field(title="Field")
    label: str = Field(default="", title="Name")
    type: str = Field(default="string", title="Type")
    required: bool = Field(default=False, title="Required")
    description: str = Field(default="", title="Description")
    path: str = Field(default="", title="Path")


class OperationView(ResponseSchema):
    """
    One operation as the picker reads it.

    Built from ``describe_operation``, which deliberately omits the URL, the auth template
    and the path: this payload reaches a browser, and a base URL template in it is an
    internal endpoint in somebody's devtools.
    """

    operation_id: str = Field(title="Operation")
    label: str = Field(default="", title="Name")
    description: str = Field(default="", title="Description")
    kind: str = Field(default=OPERATION_READ, title=_OPERATION_TYPE)
    inputs: List[OperationFieldView] = Field(
        default_factory=list, title=_INPUTS
    )
    outputs: List[OperationFieldView] = Field(
        default_factory=list, title=_OUTPUTS
    )


class OperationSchemaView(ResponseSchema):
    """What one operation reads and will accept, for the mapping grid."""

    connection_uuid: str = Field(title="Connection")
    operation_id: str = Field(title="Operation")
    operation_label: str = Field(default="", title="Operation name")
    kind: str = Field(default=OPERATION_READ, title=_OPERATION_TYPE)
    inputs: List[OperationFieldView] = Field(
        default_factory=list, title=_INPUTS
    )
    outputs: List[OperationFieldView] = Field(
        default_factory=list, title=_OUTPUTS
    )
    required: List[str] = Field(default_factory=list, title="Required fields")


class ConnectionTestView(ResponseSchema):
    """
    What one Test press came back with.

    A status and a sentence, and **never the response body**: a vendor's error text
    frequently echoes the request that caused it, credential included. The sentence has
    already been through ``sender.scrubbed``, which removes this connection's own
    credential by value — the key-name deny-list cannot see a secret embedded in prose.
    """

    ok: bool = Field(title="Worked")
    message: str = Field(title="Result")
    operation_id: str = Field(default="", title="Operation")
    operation_label: str = Field(default="", title="Operation name")
    status_code: Optional[int] = Field(default=None, title="Response status")
    record_count: Optional[int] = Field(default=None, title="Records returned")


class ConnectorView(ResponseSchema):
    """One connector the user may add a connection for."""

    connector_id: str = Field(title="Connector")
    label: str = Field(title="Name")
    description: str = Field(default="", title="Description")
    auth_kind: str = Field(default="", title="Authentication")
    asks_for_base_url: bool = Field(default=False, title="Needs an address")

    # What a connector that computes its own address asks for instead — Shopify's shop
    # domain. The pattern reaches the browser as the form's `pattern` attribute, which is
    # the frontend half of the validation; the service checks it again, and again at the
    # moment it becomes a hostname.
    asks_for_account_id: bool = Field(default=False, title="Asks for account id")
    account_id_label: str = Field(default="", title="Account id label")
    account_id_help: str = Field(default="", title="Account id help")
    account_id_pattern: str = Field(default="", title="Account id pattern")

    operations_are_user_defined: bool = Field(
        default=False, title="You write its operations"
    )
    allows_private_hosts: bool = Field(default=False, title="Can reach private addresses")
