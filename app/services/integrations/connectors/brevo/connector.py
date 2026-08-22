"""
Brevo (formerly Sendinblue), through the v3 REST API. Contacts and eCommerce.

The second *vendor* connector, and the first one that writes. Where Shopify proved the
computed-base-URL and declared-operation seams, this one proves the pair of seams a write
needs: a body built from mapped fields, and an operation honest about whether sending it
twice is safe.

**Two sections of one API, on one connection.** The contacts operations are here; orders,
products and categories are in ``ecommerce.py``. They are one connector rather than two
because they are one account behind one key — splitting them would make an operator create
the same connection twice and keep two copies of the same credential in step. What that
costs is a single connector-wide send rate covering endpoints whose published limits differ
by 180×, and the answer to that is ``OperationSpec.rate_limits``: every operation below and
next door declares its own allowance and the family it shares one with.

**Its address is fixed, not computed and not typed.** Brevo is one multi-tenant API at one
hostname for every account — there is no shop domain, no region and no instance — so
``base_url_template`` is a literal and ``base_url_is_user_supplied`` is False. That makes
this the simplest possible connection to add: a name and a key, nothing else. It is also
the reason there is no ``account_id``; the key identifies the account.

**Authentication is an account API key in an ``api-key`` header.** Generated in the Brevo
dashboard under SMTP & API → API keys. The keys do not expire, so nothing here needs the
refresh machinery. A key that has been deleted in the dashboard answers 401, which
``sender`` raises and the connections page shows next to a Reconnect button.

**Why ``create_contact`` may be retried and ``send_email`` is absent.** The create sends
``updateEnabled: true``, which makes Brevo treat an address it already holds as an update
rather than a duplicate — so a create that times out *after* reaching Brevo and is retried
lands on the same contact, and the operation can honestly declare ``idempotent=True``. A
transactional send has no such property: an email that went out cannot be un-sent by a
retry, and every retry is a second copy in somebody's inbox. Sending mail is what the
Email module is for; a workflow step that quietly duplicated a customer's receipt is not
worth the convenience of having it here too.

**Paging is by offset because Brevo offers nothing better.** Both reads step ``offset`` by
the number of records actually returned — see ``pagination.advance`` for why that is
computed from the response rather than from the requested page size. Offset paging over a
list being written to can skip or repeat a record; for a contact list that is a
re-processed contact rather than a lost one, and ``modified_since`` is the field to narrow
a sync with when it matters.
"""

from typing import Tuple

from app.models.integrations import AUTH_API_KEY, OPERATION_READ, OPERATION_WRITE
from app.services.integrations.connectors import registry
from app.services.integrations.connectors.brevo import ecommerce
from app.services.integrations.connectors.spec import (
    PAGE_OFFSET,
    PLACEMENT_HEADER,
    AuthSpec,
    ConnectorSpec,
    FieldSpec,
    OperationSpec,
    PageRule,
    RateLimitSpec,
)

CONNECTOR_ID = "brevo"

#: The one address every Brevo account is reached at. A module constant rather than a
#: setting, for the same reason Shopify's API version is one: the paths and the response
#: shapes below belong to *this* version of *this* API, and an operator who could move the
#: base URL could move it somewhere the declarations no longer describe.
BASE_URL = "https://api.brevo.com/v3"

#: Brevo's own ceilings, per endpoint. ``GET /contacts`` accepts up to 1000 at a time and
#: ``GET /contacts/lists`` up to 50; asking for more is a 400, not a clamp.
MAX_CONTACT_PAGE = 500
MAX_LIST_PAGE = 50

#: The contacts endpoints: 36,000/hour, so 10/second.
CONTACT_LIMITS = RateLimitSpec(requests_per_second=10.0, burst=20)

#: The group the four contacts operations share, because Brevo's quota is one pool across
#: them rather than one each. See ``sender._bucket_key``.
CONTACT_GROUP = "contacts"

#: The connector-wide figure, which nothing below actually uses — every operation declares
#: its own. It is Brevo's slowest published quota rather than a comfortable middle on
#: purpose: an operation added later without an allowance falls back to this, and the
#: failure that costs is a slow sync, whereas the other default's failure is a suspended
#: account. Uncorrected, too — Brevo's ``x-sib-ratelimit-*`` headers are not among the
#: shapes ``rate_limiter.read_vendor_view`` reads, so nothing lowers this from a response.
RATE_LIMITS = ecommerce.OTHER_LIMITS


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
# Neither read declares `limit` or `offset` as an input. Offset paging supplies both as
# request parameters — see `pagination.first_page_params` — and declaring them as well
# would put the page size in the query string twice, once from the walk and once from a
# node that thought it was in charge of it.

_CONTACT_INPUTS: Tuple[FieldSpec, ...] = (
    FieldSpec(
        name="modified_since",
        label="Changed since",
        type="datetime",
        description=(
            "Only contacts changed at or after this moment. This is how an incremental "
            "sync stays a few hundred records rather than the whole list every night."
        ),
    ),
    FieldSpec(
        name="created_since",
        label="Added since",
        type="datetime",
        description="Only contacts created at or after this moment.",
    ),
)

# `attributes` is Brevo's own bag of per-account fields, and its keys are whatever the
# account defined — FIRSTNAME on one, PRENOM on another. The three below are the ones
# Brevo creates on every account, named individually so the mapping grid can offer them;
# the whole object comes through as `attributes` so a field nobody predicted is still
# reachable by path.
_CONTACT_OUTPUTS: Tuple[FieldSpec, ...] = (
    FieldSpec(name="id", label="Contact ID", type="integer"),
    FieldSpec(name="email", label="Email", type="string"),
    FieldSpec(name="first_name", label="First name", type="string", path="attributes.FIRSTNAME"),
    FieldSpec(name="last_name", label="Last name", type="string", path="attributes.LASTNAME"),
    FieldSpec(name="sms", label="Phone", type="string", path="attributes.SMS"),
    FieldSpec(name="attributes", label="All attributes", type="json"),
    FieldSpec(name="list_ids", label="Lists", type="json", path="listIds"),
    FieldSpec(
        name="email_blacklisted",
        label="Unsubscribed from email",
        type="boolean",
        path="emailBlacklisted",
    ),
    FieldSpec(
        name="sms_blacklisted",
        label="Unsubscribed from SMS",
        type="boolean",
        path="smsBlacklisted",
    ),
    FieldSpec(name="created_at", label="Added at", type="datetime", path="createdAt"),
    FieldSpec(name="modified_at", label="Changed at", type="datetime", path="modifiedAt"),
)

CONTACTS = OperationSpec(
    operation_id="contacts",
    label="Contacts",
    description="Contacts in the account, with their attributes and list membership.",
    kind=OPERATION_READ,
    method="GET",
    path="/contacts",
    query_template={
        "modifiedSince": "{modified_since}",
        "createdSince": "{created_since}",
    },
    inputs=_CONTACT_INPUTS,
    outputs=_CONTACT_OUTPUTS,
    records_path="contacts",
    page_rule=PageRule(
        kind=PAGE_OFFSET,
        param="offset",
        size_param="limit",
        size=MAX_CONTACT_PAGE,
        # Brevo counts from zero. The default of 1 would silently skip the first contact
        # of every sync, which is the shape of paging bug that never gets noticed.
        start_at=0,
    ),
    rate_limits=CONTACT_LIMITS,
    rate_limit_group=CONTACT_GROUP,
)


_LIST_OUTPUTS: Tuple[FieldSpec, ...] = (
    FieldSpec(name="id", label="List ID", type="integer"),
    FieldSpec(name="name", label="Name", type="string"),
    FieldSpec(name="folder_id", label="Folder", type="integer", path="folderId"),
    FieldSpec(
        name="total_subscribers",
        label="Contacts",
        type="integer",
        path="totalSubscribers",
    ),
    FieldSpec(
        name="unique_subscribers",
        label="Unique contacts",
        type="integer",
        path="uniqueSubscribers",
    ),
    FieldSpec(
        name="total_blacklisted",
        label="Unsubscribed",
        type="integer",
        path="totalBlacklisted",
    ),
    FieldSpec(name="created_at", label="Created at", type="datetime", path="createdAt"),
)

LISTS = OperationSpec(
    operation_id="lists",
    label="Contact lists",
    description=(
        "The contact lists in the account. Read this to find the list id a contact should "
        "be added to."
    ),
    kind=OPERATION_READ,
    method="GET",
    path="/contacts/lists",
    inputs=(),
    outputs=_LIST_OUTPUTS,
    records_path="lists",
    page_rule=PageRule(
        kind=PAGE_OFFSET,
        param="offset",
        size_param="limit",
        size=MAX_LIST_PAGE,
        start_at=0,
    ),
    rate_limits=CONTACT_LIMITS,
    rate_limit_group=CONTACT_GROUP,
)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

_CREATE_CONTACT_INPUTS: Tuple[FieldSpec, ...] = (
    FieldSpec(
        name="email",
        label="Email",
        type="string",
        required=True,
        description="The address Brevo identifies the contact by.",
    ),
    FieldSpec(
        name="attributes",
        label="Attributes",
        type="json",
        description=(
            "An object of the account's own contact fields, for example "
            '{"FIRSTNAME": "Ada", "LASTNAME": "Lovelace"}. A key the account has not '
            "defined is refused by Brevo, so map to fields that exist."
        ),
    ),
    FieldSpec(
        name="list_ids",
        label="Lists to add to",
        type="json",
        description="An array of list ids, for example [3, 7]. Read them from 'Contact lists'.",
    ),
    FieldSpec(
        name="email_blacklisted",
        label="Unsubscribed from email",
        type="boolean",
        description="Set this when the source system says the person opted out.",
    ),
    FieldSpec(
        name="sms_blacklisted",
        label="Unsubscribed from SMS",
        type="boolean",
    ),
)

CREATE_CONTACT = OperationSpec(
    operation_id="create_contact",
    label="Create or update a contact",
    description=(
        "Adds the contact, or updates the one Brevo already holds for that address. "
        "Existing attributes that are not sent are left alone."
    ),
    kind=OPERATION_WRITE,
    method="POST",
    path="/contacts",
    body_template={
        "email": "{email}",
        "attributes": "{attributes}",
        "listIds": "{list_ids}",
        "emailBlacklisted": "{email_blacklisted}",
        "smsBlacklisted": "{sms_blacklisted}",
        # A literal, and the single most consequential value in this module. Without it
        # Brevo answers 400 `duplicate_parameter` for an address it already holds, so a
        # re-run of yesterday's sync fails on every contact it already sent — and, worse,
        # `idempotent` below would be a lie. It is not an input because a node that left
        # it unmapped would get the other behaviour with nothing on screen saying so.
        "updateEnabled": True,
    },
    inputs=_CREATE_CONTACT_INPUTS,
    outputs=(FieldSpec(name="id", label="Contact ID", type="integer"),),
    # Earned by `updateEnabled`, not assumed. See the module docstring: a create that
    # times out after reaching Brevo has already happened, and the retry has to land on
    # the same contact rather than on a second one.
    idempotent=True,
    rate_limits=CONTACT_LIMITS,
    rate_limit_group=CONTACT_GROUP,
)


ADD_TO_LIST = OperationSpec(
    operation_id="add_to_list",
    label="Add contacts to a list",
    description=(
        "Adds addresses that already exist as contacts to one list. Addresses Brevo does "
        "not hold are reported back as failures rather than created."
    ),
    kind=OPERATION_WRITE,
    method="POST",
    # The one path in this connector with a placeholder in it. `_render_path` escapes the
    # value and refuses to build a URL with a hole in it, so a step that failed to map a
    # list id is a refusal here rather than a POST to `/contacts/lists//contacts/add`.
    path="/contacts/lists/{list_id}/contacts/add",
    body_template={"emails": "{emails}"},
    inputs=(
        FieldSpec(
            name="list_id",
            label="List",
            type="integer",
            required=True,
            description="The id of the list to add to. Read it from 'Contact lists'.",
        ),
        FieldSpec(
            name="emails",
            label="Email addresses",
            type="json",
            required=True,
            description='An array of addresses, for example ["ada@example.com"].',
        ),
    ),
    outputs=(
        FieldSpec(
            name="added",
            label="Added",
            type="json",
            path="contacts.success",
        ),
        FieldSpec(
            name="failed",
            label="Not added",
            type="json",
            path="contacts.failure",
        ),
    ),
    # Adding a contact already on the list is not an error and not a second membership,
    # so a retried request leaves the same state behind.
    idempotent=True,
    rate_limits=CONTACT_LIMITS,
    rate_limit_group=CONTACT_GROUP,
)


# ---------------------------------------------------------------------------
# The connector
# ---------------------------------------------------------------------------

SPEC = ConnectorSpec(
    connector_id=CONNECTOR_ID,
    label="Brevo",
    description=(
        "Read and write contacts, lists, orders, products and categories in a Brevo "
        "account using an account API key. Sending email is the Email module's job, not "
        "this connector's."
    ),
    # No brand glyph exists in the icon set, so the tile carries a mail icon on Brevo's
    # green rather than a wrong logo.
    icon="las la-paper-plane",
    accent="#0b996e",
    auth=AuthSpec(
        kind=AUTH_API_KEY,
        placement=PLACEMENT_HEADER,
        name="api-key",
        # No `Bearer` prefix: Brevo reads the header value as the key itself, and a prefix
        # would be sent as part of it and answered with a 401 that reads like a bad key.
        value_template="{api_key}",
    ),
    rate_limits=RATE_LIMITS,
    # Contacts first, and `contacts` first of those — `connection_service._operation_to_test`
    # picks the first *read* positionally, so this decides what the Test button on the
    # connections page actually calls. Contacts is the right one: every Brevo account has
    # the endpoint, whereas the eCommerce endpoints answer 403 until the account enables
    # the eCommerce app, and a Test that failed for that reason would read as a bad key.
    operations=(CONTACTS, LISTS, CREATE_CONTACT, ADD_TO_LIST, *ecommerce.OPERATIONS),
    base_url_template=BASE_URL,
    # One hostname for every account. Nothing to compute and nothing to ask for — see the
    # module docstring.
    base_url_is_user_supplied=False,
    account_id_required=False,
    operations_are_user_defined=False,
    allows_private_hosts=False,
    requires_https=True,
    hooks=None,
)

registry.register(SPEC)
