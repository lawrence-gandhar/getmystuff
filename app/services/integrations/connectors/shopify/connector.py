"""
Shopify, through the Admin GraphQL API. Read-only.

The first *vendor* connector, and the first one that is not `rest_generic` — which means
it is the first to exercise the machinery built for exactly this: a computed base URL,
declared operations, and hooks. Three of those seams had never run once, and two of them
were broken until this landed.

**Read-only, and that is a property rather than a stage.** This connector declares no
write operations at all, so ``writable_operations()`` is empty and a ``connector_write``
node cannot select it — the palette is built from that list. The reason is Shopify's own:
its mutations take no idempotency key, so a create that times out *after reaching the
server* has already happened, and retrying duplicates the record while doing nothing wrong.
Getting that right needs `integration_sync_keys` doing natural-key lookups before every
create, and it is not what a first connector should be proving.

**Authentication is a custom app token.** The merchant creates a custom app in their own
Shopify admin and pastes the Admin API access token — a ``shpat_…`` string — which goes in
an ``X-Shopify-Access-Token`` header. Offline tokens do not expire, so nothing here needs
the refresh machinery. OAuth would make this an ``AuthSpec`` change plus an install flow;
see the connector documentation for what is and is not already in place.

**Everything about a request is data except the two things that cannot be.** The GraphQL
documents live in ``documents.py``, the shape is an ``OperationSpec`` like any other, and
``hooks.py`` carries only what declarative data genuinely cannot express: that Shopify
reports failure inside an HTTP 200, and that its rate limit is a shared points bucket whose
true state is in every response body.

**The three operations are structurally identical.** Same path, same method, same three
inputs, same paging rule shape — only the document, the record path and the outputs differ.
That is deliberate: it is what lets one page rule and one hook cover all of them, and what
makes adding a fourth resource a copy of a block rather than a new code path.
"""

from typing import Tuple

from app.models.integrations import AUTH_API_KEY, OPERATION_READ
from app.services.integrations.connectors import registry
from app.services.integrations.connectors.shopify import documents
from app.services.integrations.connectors.shopify.hooks import (
    SHOP_DOMAIN_PATTERN,
    ShopifyHooks,
)
from app.services.integrations.connectors.spec import (
    PAGE_INPUT_CURSOR,
    PLACEMENT_HEADER,
    AuthSpec,
    ConnectorSpec,
    FieldSpec,
    OperationSpec,
    PageRule,
    RateLimitSpec,
)

CONNECTOR_ID = "shopify"

#: The Admin API version every request goes to.
#:
#: A module constant and **not** an environment variable, deliberately. The version and the
#: query documents in ``documents.py`` are one thing: a field that exists in one version and
#: not the next is a changed document, so a version the operator can move without moving the
#: documents is a version that can silently stop matching them. Bumping it is a code change
#: with tests, which is the correct amount of ceremony.
#:
#: Shopify ships quarterly (January, April, July, October) and supports each release for
#: about twelve months. A deprecated field comes back as an error inside an HTTP 200 — which
#: `ShopifyHooks.after_response` now turns into a loud failure rather than an empty sync, so
#: falling behind is visible. There is no automatic warning before that point; the review is
#: manual and belongs on a quarterly rhythm.
API_VERSION = "2026-07"

#: Shopify's own maximum for a GraphQL connection. Asking for more is an error, not a clamp.
MAX_PAGE_SIZE = 250

#: Conservative, and the response corrects it. See ``hooks.py`` — the shop's points bucket
#: is shared with every other app the merchant installed, so a locally-computed rate is
#: always optimistic and the only real number arrives in ``extensions.cost``.
RATE_LIMITS = RateLimitSpec(requests_per_second=2.0, burst=4)


# ---------------------------------------------------------------------------
# The three inputs every operation takes
# ---------------------------------------------------------------------------
# None of them is required. `page_size` and `cursor` are supplied by the paging walk rather
# than by the node, and marking either required would make the very first `build_request` —
# the one `open_supply` makes only to work out the origin URL, before any walk exists —
# raise for a value nobody was supposed to provide yet.

_PAGE_SIZE = FieldSpec(
    name="page_size",
    label="Records per request",
    type="integer",
    description=f"How many to ask for at a time. Shopify's maximum is {MAX_PAGE_SIZE}.",
)

_CURSOR = FieldSpec(
    name="cursor",
    label="Page cursor",
    type="string",
    description=(
        "Supplied automatically while reading through the pages. Leave this alone."
    ),
)

_SEARCH = FieldSpec(
    name="search",
    label="Filter",
    type="string",
    description=(
        "Shopify's own search syntax, for example 'updated_at:>2026-08-01' or "
        "'financial_status:paid'. Leave it empty to read everything."
    ),
)

_INPUTS: Tuple[FieldSpec, ...] = (_PAGE_SIZE, _CURSOR, _SEARCH)


def _page_rule(resource: str) -> PageRule:
    """
    The paging rule for one resource.

    ``input_cursor`` rather than ``cursor``: the cursor has to reach the POST body's
    ``variables``, and the query-string kind cannot get it there. Naming an input instead
    lets the operation's own template decide where it lands, and
    ``OperationSpec.validated`` refuses a name the operation never declared — so a typo
    here is an import-time error rather than a sync that silently re-reads page one.

    ``has_more_path`` is Shopify's ``hasNextPage``, checked before the cursor is read.
    Shopify keeps sending an ``endCursor`` on the last page, so believing the cursor alone
    would mean one extra request per read, every read.
    """
    return PageRule(
        kind=PAGE_INPUT_CURSOR,
        param="cursor",
        size_param="page_size",
        size=MAX_PAGE_SIZE,
        cursor_path=f"data.{resource}.pageInfo.endCursor",
        has_more_path=f"data.{resource}.pageInfo.hasNextPage",
    )


def _operation(
    *,
    operation_id: str,
    resource: str,
    label: str,
    description: str,
    document: str,
    outputs: Tuple[FieldSpec, ...],
) -> OperationSpec:
    """
    One read, assembled.

    ``body_literals=("query",)`` is what lets the document through. Without it the request
    builder reads the first ``{`` in the GraphQL as the start of an input name and refuses
    the operation before anything is sent. ``variables`` beside it substitutes normally —
    that is where the cursor, the page size and the filter arrive.
    """
    return OperationSpec(
        operation_id=operation_id,
        label=label,
        description=description,
        kind=OPERATION_READ,
        method="POST",
        path="/graphql.json",
        body_template={
            "query": document,
            "variables": {
                "first": "{page_size}",
                "after": "{cursor}",
                "search": "{search}",
            },
        },
        body_literals=("query",),
        inputs=_INPUTS,
        outputs=outputs,
        records_path=f"data.{resource}.edges[*].node",
        page_rule=_page_rule(resource),
    )


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
# Money is a connection in Shopify's schema rather than a scalar, so an amount arrives at
# `…Set.shopMoney.amount` and every field below says where to read it. Amounts are typed
# `string`, not `number`: Shopify sends them as decimal strings on purpose, and parsing
# them to a float here would introduce a rounding error into somebody's revenue figures
# before the mapping layer ever sees them.

_ORDER_OUTPUTS: Tuple[FieldSpec, ...] = (
    FieldSpec(name="id", label="Order ID", type="string"),
    FieldSpec(name="name", label="Order number", type="string"),
    FieldSpec(name="email", label="Email", type="string"),
    FieldSpec(name="phone", label="Phone", type="string"),
    FieldSpec(name="created_at", label="Placed at", type="datetime", path="createdAt"),
    FieldSpec(name="updated_at", label="Updated at", type="datetime", path="updatedAt"),
    FieldSpec(name="processed_at", label="Processed at", type="datetime", path="processedAt"),
    FieldSpec(name="cancelled_at", label="Cancelled at", type="datetime", path="cancelledAt"),
    FieldSpec(name="financial_status", label="Payment status", type="string", path="displayFinancialStatus"),
    FieldSpec(name="fulfillment_status", label="Fulfilment status", type="string", path="displayFulfillmentStatus"),
    FieldSpec(name="note", label="Note", type="string"),
    FieldSpec(name="tags", label="Tags", type="json"),
    FieldSpec(name="total_price", label="Total", type="string", path="currentTotalPriceSet.shopMoney.amount"),
    FieldSpec(name="currency", label="Currency", type="string", path="currentTotalPriceSet.shopMoney.currencyCode"),
    FieldSpec(name="subtotal_price", label="Subtotal", type="string", path="currentSubtotalPriceSet.shopMoney.amount"),
    FieldSpec(name="shipping_price", label="Shipping", type="string", path="totalShippingPriceSet.shopMoney.amount"),
    FieldSpec(name="total_tax", label="Tax", type="string", path="currentTotalTaxSet.shopMoney.amount"),
    FieldSpec(name="customer_id", label="Customer ID", type="string", path="customer.id"),
    FieldSpec(name="customer_email", label="Customer email", type="string", path="customer.email"),
    FieldSpec(name="shipping_city", label="Ship-to city", type="string", path="shippingAddress.city"),
    FieldSpec(name="shipping_country", label="Ship-to country", type="string", path="shippingAddress.country"),
    FieldSpec(name="shipping_zip", label="Ship-to postcode", type="string", path="shippingAddress.zip"),
)

ORDERS = _operation(
    operation_id="orders",
    resource="orders",
    label="Orders",
    description="Orders in the store, most recently updated first.",
    document=documents.ORDERS,
    outputs=_ORDER_OUTPUTS,
)


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

_PRODUCT_OUTPUTS: Tuple[FieldSpec, ...] = (
    FieldSpec(name="id", label="Product ID", type="string"),
    FieldSpec(name="title", label="Title", type="string"),
    FieldSpec(name="handle", label="Handle", type="string"),
    FieldSpec(name="description", label="Description", type="string"),
    FieldSpec(name="product_type", label="Type", type="string", path="productType"),
    FieldSpec(name="vendor", label="Vendor", type="string"),
    FieldSpec(name="status", label="Status", type="string"),
    FieldSpec(name="tags", label="Tags", type="json"),
    FieldSpec(name="total_inventory", label="Inventory", type="integer", path="totalInventory"),
    FieldSpec(name="created_at", label="Created at", type="datetime", path="createdAt"),
    FieldSpec(name="updated_at", label="Updated at", type="datetime", path="updatedAt"),
    FieldSpec(name="published_at", label="Published at", type="datetime", path="publishedAt"),
    FieldSpec(name="image_url", label="Image", type="string", path="featuredImage.url"),
    FieldSpec(name="min_price", label="Lowest price", type="string", path="priceRangeV2.minVariantPrice.amount"),
    FieldSpec(name="max_price", label="Highest price", type="string", path="priceRangeV2.maxVariantPrice.amount"),
    FieldSpec(name="currency", label="Currency", type="string", path="priceRangeV2.minVariantPrice.currencyCode"),
)

PRODUCTS = _operation(
    operation_id="products",
    resource="products",
    label="Products",
    description="Products in the store, most recently updated first.",
    document=documents.PRODUCTS,
    outputs=_PRODUCT_OUTPUTS,
)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

_CUSTOMER_OUTPUTS: Tuple[FieldSpec, ...] = (
    FieldSpec(name="id", label="Customer ID", type="string"),
    FieldSpec(name="first_name", label="First name", type="string", path="firstName"),
    FieldSpec(name="last_name", label="Last name", type="string", path="lastName"),
    FieldSpec(name="display_name", label="Name", type="string", path="displayName"),
    FieldSpec(name="email", label="Email", type="string"),
    FieldSpec(name="phone", label="Phone", type="string"),
    FieldSpec(name="note", label="Note", type="string"),
    FieldSpec(name="tags", label="Tags", type="json"),
    FieldSpec(name="state", label="Account state", type="string"),
    FieldSpec(name="verified_email", label="Email verified", type="boolean", path="verifiedEmail"),
    FieldSpec(name="number_of_orders", label="Orders placed", type="integer", path="numberOfOrders"),
    FieldSpec(name="created_at", label="Created at", type="datetime", path="createdAt"),
    FieldSpec(name="updated_at", label="Updated at", type="datetime", path="updatedAt"),
    FieldSpec(name="amount_spent", label="Total spent", type="string", path="amountSpent.amount"),
    FieldSpec(name="currency", label="Currency", type="string", path="amountSpent.currencyCode"),
    FieldSpec(name="address_city", label="City", type="string", path="defaultAddress.city"),
    FieldSpec(name="address_country", label="Country", type="string", path="defaultAddress.country"),
    FieldSpec(name="address_zip", label="Postcode", type="string", path="defaultAddress.zip"),
)

CUSTOMERS = _operation(
    operation_id="customers",
    resource="customers",
    label="Customers",
    description="Customers of the store, most recently updated first.",
    document=documents.CUSTOMERS,
    outputs=_CUSTOMER_OUTPUTS,
)


# ---------------------------------------------------------------------------
# The connector
# ---------------------------------------------------------------------------

SPEC = ConnectorSpec(
    connector_id=CONNECTOR_ID,
    label="Shopify",
    description=(
        "Read orders, products and customers from a Shopify store using an Admin API "
        "access token from a custom app."
    ),
    auth=AuthSpec(
        kind=AUTH_API_KEY,
        placement=PLACEMENT_HEADER,
        name="X-Shopify-Access-Token",
        value_template="{api_key}",
    ),
    rate_limits=RATE_LIMITS,
    operations=(ORDERS, PRODUCTS, CUSTOMERS),
    base_url_template=f"https://{{account}}/admin/api/{API_VERSION}",
    # The address is computed, never typed. A vendor connector that let the user supply a
    # base URL would be a way to point something labelled "Shopify" — and carrying a
    # Shopify token — at any host at all.
    base_url_is_user_supplied=False,
    account_id_pattern=SHOP_DOMAIN_PATTERN,
    account_id_label="Shop domain",
    account_id_help="your-store.myshopify.com",
    account_id_required=True,
    operations_are_user_defined=False,
    allows_private_hosts=False,
    requires_https=True,
    hooks=ShopifyHooks(),
)

registry.register(SPEC)
