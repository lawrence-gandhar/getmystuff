"""
Brevo's eCommerce section: orders, products and categories.

Separate from ``connector.py`` for the reason ``shopify/documents.py`` is separate — six
operations with their field tables is most of a thousand lines, and the connector's own
shape (its address, its key, its allowances) should stay readable at the top of one
screen. Everything here is imported into ``SPEC.operations`` and is not otherwise reachable.

**This is the half of Brevo that a shop pushes into.** The contacts operations next door
read and write people; these read and write the things people bought. Brevo's marketing
features — abandoned-cart automations, product recommendations, revenue attribution — run
off this data and off nothing else, so a workflow that syncs a storefront here is what
turns the rest of the account on.

**Every write is an upsert on an id we supply, and that is what earns ``idempotent``.**
Three endpoints, two mechanisms: ``POST /orders/status`` upserts natively, and
``POST /products`` and ``POST /categories`` answer 400 for a duplicate id *unless*
``updateEnabled`` is set. Both are declared below as literal ``True`` in the body rather
than as inputs, because an unmapped input would silently buy the other behaviour — and the
other behaviour makes ``idempotent=True`` a lie in the one situation it exists for, a
write retried after a timeout that had already arrived.

**The allowances are per operation because Brevo's are.** Order writes get 5/second,
product writes 2/second, and everything else here shares one hundred **per hour** with the
rest of the account's miscellaneous traffic. See ``OperationSpec.rate_limits``; the group
is what makes the third case right, since a bucket each would be four hundred an hour
against a hundred-an-hour limit.

**Money is text.** ``amount`` and ``price`` are declared ``string`` rather than ``number``
for the reason Shopify's connector declares them so: a merchant's revenue must not
round-trip through a float on its way between two systems that both got it right.
"""

from typing import Tuple

from app.models.integrations import OPERATION_READ, OPERATION_WRITE
from app.services.integrations.connectors.spec import (
    PAGE_OFFSET,
    FieldSpec,
    OperationSpec,
    PageRule,
    RateLimitSpec,
)

# ---------------------------------------------------------------------------
# What Brevo will tolerate, per family
# ---------------------------------------------------------------------------
# Published figures, and they differ by 180× across the four endpoints this file and its
# neighbour reach. One connector-wide number cannot serve them: the slowest would throttle
# an order sync to one record every 36 seconds, and the fastest would exhaust the retry
# engine's three attempts against the hourly ceiling and fail the run.

#: ``POST /orders/status`` — 18,000/hour.
ORDER_LIMITS = RateLimitSpec(requests_per_second=5.0, burst=10)

#: ``POST /products`` — 7,200/hour.
PRODUCT_LIMITS = RateLimitSpec(requests_per_second=2.0, burst=4)

#: Everything else Brevo does not name individually: 100 requests **per hour**, shared.
#:
#: A leaky bucket says that exactly, and more usefully than a fixed window would. It
#: refills at one token every 36 seconds and holds an hour's worth, and
#: ``RateLimiter.bucket`` starts a bucket full — so a read of a few pages goes straight out
#: and only a sustained one waits. Which is precisely the allowance: a hundred whenever you
#: like, then the drip.
OTHER_LIMITS = RateLimitSpec(requests_per_second=100 / 3600, burst=100)

#: The group name the three reads and the category write share. The quota is one pool, so
#: the bucket has to be one bucket. See ``sender._bucket_key``.
OTHER_GROUP = "other"

#: Brevo's own page ceilings. Asking for more is a 400, not a clamp.
MAX_ORDER_PAGE = 100
MAX_PRODUCT_PAGE = 1000
MAX_CATEGORY_PAGE = 100


# ---------------------------------------------------------------------------
# Shared read inputs
# ---------------------------------------------------------------------------
# None of them required, and that is load-bearing rather than lenient. `test_connection`
# builds its one real call with `pagination.first_page_arguments`, which is empty for
# offset paging — so a required read input would make every Test button press fail with a
# sentence about a missing value instead of telling the owner whether their key works.
#
# `limit` and `offset` are not here either. Offset paging supplies both as request
# parameters (`pagination.first_page_params`), and declaring them as well would put the
# page size in the query string twice — once from the walk, once from a node that thought
# it was in charge of it.

_SINCE_INPUTS: Tuple[FieldSpec, ...] = (
    FieldSpec(
        name="modified_since",
        label="Changed since",
        type="datetime",
        description=(
            "Only records changed at or after this moment. On an hourly quota this is the "
            "difference between a sync that fits and one that does not."
        ),
    ),
    FieldSpec(
        name="created_since",
        label="Added since",
        type="datetime",
        description="Only records created at or after this moment.",
    ),
)

_SORT_INPUT = FieldSpec(
    name="sort",
    label="Order",
    type="string",
    description="'desc' for newest first, which is Brevo's default, or 'asc' for oldest.",
)

_SINCE_QUERY = {
    "modifiedSince": "{modified_since}",
    "createdSince": "{created_since}",
    "sort": "{sort}",
}


def _offset_page(size: int) -> PageRule:
    """
    Offset paging, counted from zero.

    Brevo counts from zero and ``PageRule``'s default ``start_at`` is 1, so every rule here
    sets it explicitly. Left at the default it would skip the first record of every sync —
    the shape of paging bug that returns plausible data forever and is never noticed.
    """
    return PageRule(
        kind=PAGE_OFFSET,
        param="offset",
        size_param="limit",
        size=size,
        start_at=0,
    )


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

_ORDER_OUTPUTS: Tuple[FieldSpec, ...] = (
    FieldSpec(name="id", label="Order ID", type="string"),
    # Text, not a number. See the module docstring: this is somebody's revenue.
    FieldSpec(name="amount", label="Total", type="string"),
    FieldSpec(name="status", label="Status", type="string"),
    FieldSpec(name="email", label="Email", type="string"),
    FieldSpec(name="contact_id", label="Contact ID", type="integer"),
    FieldSpec(name="store_id", label="Store", type="string", path="storeId"),
    FieldSpec(name="coupons", label="Coupons", type="json"),
    FieldSpec(name="created_at", label="Placed at", type="datetime", path="createdAt"),
    FieldSpec(name="updated_at", label="Changed at", type="datetime", path="updatedAt"),
    FieldSpec(name="billing_address", label="Billing address", type="string", path="billing.address"),
    FieldSpec(name="billing_city", label="Billing city", type="string", path="billing.city"),
    FieldSpec(name="billing_country", label="Billing country", type="string", path="billing.country"),
    FieldSpec(
        name="billing_country_code",
        label="Billing country code",
        type="string",
        path="billing.countryCode",
    ),
    FieldSpec(name="billing_post_code", label="Billing post code", type="string", path="billing.postCode"),
    FieldSpec(name="billing_region", label="Billing region", type="string", path="billing.region"),
    FieldSpec(
        name="payment_method",
        label="Payment method",
        type="string",
        path="billing.paymentMethod",
    ),
    # The whole line-item array, and each field across it. Both, deliberately: the array is
    # what a write step maps straight back out, and the flattened columns are what a
    # `transform` or a `filter` can actually reach without a path of its own.
    FieldSpec(name="products", label="Line items", type="json"),
    FieldSpec(name="product_ids", label="Product IDs", type="json", path="products[*].productId"),
    FieldSpec(name="quantities", label="Quantities", type="json", path="products[*].quantity"),
    FieldSpec(name="prices", label="Line prices", type="json", path="products[*].price"),
    FieldSpec(name="identifiers", label="Identifiers", type="json"),
)

ORDERS = OperationSpec(
    operation_id="orders",
    label="Orders",
    description="Orders Brevo holds for the account, with their line items and billing.",
    kind=OPERATION_READ,
    method="GET",
    path="/orders",
    query_template=_SINCE_QUERY,
    inputs=(*_SINCE_INPUTS, _SORT_INPUT),
    outputs=_ORDER_OUTPUTS,
    records_path="orders",
    page_rule=_offset_page(MAX_ORDER_PAGE),
    rate_limits=OTHER_LIMITS,
    rate_limit_group=OTHER_GROUP,
)


_ORDER_WRITE_INPUTS: Tuple[FieldSpec, ...] = (
    FieldSpec(
        name="id",
        label="Order ID",
        type="string",
        required=True,
        description=(
            "Your own id for the order. Brevo keys on it, so sending the same one again "
            "updates that order rather than creating a second."
        ),
    ),
    FieldSpec(
        name="created_at",
        label="Placed at",
        type="datetime",
        required=True,
        description="When the order was placed, as a UTC timestamp.",
    ),
    FieldSpec(
        name="updated_at",
        label="Changed at",
        type="datetime",
        required=True,
        description="When the order last changed, as a UTC timestamp.",
    ),
    FieldSpec(
        name="status",
        label="Status",
        type="string",
        required=True,
        description=(
            "What state the order is in — 'completed', 'cancelled', or whatever the shop "
            "calls it. Brevo stores the word and automations match on it."
        ),
    ),
    FieldSpec(
        name="amount",
        label="Total",
        type="string",
        required=True,
        description="The order total including shipping and tax.",
    ),
    FieldSpec(
        name="products",
        label="Line items",
        type="json",
        required=True,
        description=(
            "An array of line items. Each needs productId, price and either quantity or "
            'quantityFloat, for example [{"productId": "SKU-1", "price": 9.99, '
            '"quantity": 2}]. Brevo rejects the whole order if one line is short a field, '
            "so validate the array before this step rather than after."
        ),
    ),
    FieldSpec(
        name="email",
        label="Email",
        type="string",
        description="The buyer's address. This is what ties the order to a contact.",
    ),
    FieldSpec(
        name="billing",
        label="Billing",
        type="json",
        description=(
            "An object of address, city, countryCode, postCode, region, phone and "
            "paymentMethod. Any subset."
        ),
    ),
    FieldSpec(name="coupons", label="Coupons", type="json", description='An array, for example ["SAVE10"].'),
    FieldSpec(
        name="identifiers",
        label="Identifiers",
        type="json",
        description="Brevo's alternative ways to match the buyer — ext_id, email_id, phone_id.",
    ),
    FieldSpec(name="meta_info", label="Extra fields", type="json"),
    FieldSpec(name="store_id", label="Store", type="string", description="For a multi-store account."),
    FieldSpec(
        name="historical",
        label="Backfill",
        type="boolean",
        description=(
            "True — Brevo's own default — imports the order without firing automations. "
            "Set it false for live orders, or an abandoned-cart flow never runs."
        ),
    ),
)

UPSERT_ORDER = OperationSpec(
    operation_id="upsert_order",
    label="Create or update an order",
    description=(
        "Sends one order to Brevo, or updates the one already held under that id. This is "
        "what puts a shop's revenue behind Brevo's automations and reporting."
    ),
    kind=OPERATION_WRITE,
    method="POST",
    path="/orders/status",
    body_template={
        "id": "{id}",
        "createdAt": "{created_at}",
        "updatedAt": "{updated_at}",
        "status": "{status}",
        "amount": "{amount}",
        "products": "{products}",
        "email": "{email}",
        "billing": "{billing}",
        "coupons": "{coupons}",
        "identifiers": "{identifiers}",
        "metaInfo": "{meta_info}",
        "storeId": "{store_id}",
        "historical": "{historical}",
    },
    inputs=_ORDER_WRITE_INPUTS,
    outputs=(),
    # No `updateEnabled` needed and none sent: this endpoint upserts on `id` by itself,
    # which is the difference between it and the two below. The retry is therefore safe
    # for the same reason but by a different mechanism — worth knowing before anybody
    # "fixes" the inconsistency by adding a flag Brevo would ignore.
    idempotent=True,
    rate_limits=ORDER_LIMITS,
    rate_limit_group="orders",
)


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

_PRODUCT_OUTPUTS: Tuple[FieldSpec, ...] = (
    FieldSpec(name="id", label="Product ID", type="string"),
    FieldSpec(name="name", label="Name", type="string"),
    FieldSpec(name="sku", label="SKU", type="string"),
    FieldSpec(name="price", label="Price", type="string"),
    FieldSpec(name="alternative_price", label="Alternative price", type="string", path="alternativePrice"),
    FieldSpec(name="url", label="Product page", type="string"),
    FieldSpec(name="image_url", label="Image", type="string", path="imageUrl"),
    FieldSpec(name="categories", label="Categories", type="json"),
    FieldSpec(name="parent_id", label="Parent product", type="string", path="parentId"),
    FieldSpec(name="description", label="Description", type="string"),
    FieldSpec(name="brand", label="Brand", type="string"),
    FieldSpec(name="stock", label="Stock", type="number"),
    FieldSpec(name="is_deleted", label="Deleted", type="boolean", path="isDeleted"),
    FieldSpec(name="meta_info", label="Extra fields", type="json", path="metaInfo"),
    FieldSpec(name="created_at", label="Added at", type="datetime", path="createdAt"),
    FieldSpec(name="modified_at", label="Changed at", type="datetime", path="modifiedAt"),
)

PRODUCTS = OperationSpec(
    operation_id="products",
    label="Products",
    description="The product catalogue Brevo holds for the account.",
    kind=OPERATION_READ,
    method="GET",
    path="/products",
    query_template={
        **_SINCE_QUERY,
        "ids": "{ids}",
        "search": "{search}",
        "categories": "{categories}",
    },
    inputs=(
        *_SINCE_INPUTS,
        _SORT_INPUT,
        FieldSpec(
            name="ids",
            label="Product IDs",
            type="json",
            description='Only these products, for example ["SKU-1", "SKU-2"].',
        ),
        FieldSpec(
            name="search",
            label="Search",
            type="string",
            description="Matches across name, SKU and id.",
        ),
        FieldSpec(
            name="categories",
            label="In categories",
            type="json",
            description="Only products in these category ids.",
        ),
    ),
    outputs=_PRODUCT_OUTPUTS,
    records_path="products",
    page_rule=_offset_page(MAX_PRODUCT_PAGE),
    rate_limits=OTHER_LIMITS,
    rate_limit_group=OTHER_GROUP,
)


UPSERT_PRODUCT = OperationSpec(
    operation_id="upsert_product",
    label="Create or update a product",
    description=(
        "Sends one product to Brevo's catalogue, or updates the one already held under "
        "that id. Fields that are not mapped are left as they are."
    ),
    kind=OPERATION_WRITE,
    method="POST",
    path="/products",
    body_template={
        "id": "{id}",
        "name": "{name}",
        "sku": "{sku}",
        "price": "{price}",
        "alternativePrice": "{alternative_price}",
        "url": "{url}",
        "imageUrl": "{image_url}",
        "categories": "{categories}",
        "parentId": "{parent_id}",
        "description": "{description}",
        "brand": "{brand}",
        "stock": "{stock}",
        "isDeleted": "{is_deleted}",
        "metaInfo": "{meta_info}",
        # A literal, and the value that makes `idempotent` below true rather than
        # aspirational. Without it Brevo answers 400 for an id it already holds, so the
        # second night of a nightly sync fails on every product it sent the first night —
        # and a write retried after a timeout that had already arrived fails too, which is
        # the case `idempotent` exists for. Not an input: a node that left it unmapped
        # would buy the other behaviour with nothing on screen saying so.
        "updateEnabled": True,
    },
    inputs=(
        FieldSpec(
            name="id",
            label="Product ID",
            type="string",
            required=True,
            description="Your own id for the product. Brevo keys on it.",
        ),
        FieldSpec(
            name="name",
            label="Name",
            type="string",
            required=True,
            description="Required when the product is new; kept as-is on an update.",
        ),
        FieldSpec(name="sku", label="SKU", type="string"),
        FieldSpec(name="price", label="Price", type="string"),
        FieldSpec(name="alternative_price", label="Alternative price", type="string"),
        FieldSpec(name="url", label="Product page", type="string"),
        FieldSpec(name="image_url", label="Image", type="string"),
        FieldSpec(
            name="categories",
            label="Categories",
            type="json",
            description=(
                'An array of category ids, for example ["shoes"]. Create the categories '
                "first — Brevo does not make them up from a product that mentions one."
            ),
        ),
        FieldSpec(name="parent_id", label="Parent product", type="string"),
        FieldSpec(name="description", label="Description", type="string"),
        FieldSpec(name="brand", label="Brand", type="string"),
        FieldSpec(name="stock", label="Stock", type="number"),
        FieldSpec(name="is_deleted", label="Deleted", type="boolean"),
        FieldSpec(name="meta_info", label="Extra fields", type="json"),
    ),
    outputs=(FieldSpec(name="id", label="Product ID", type="string"),),
    idempotent=True,
    rate_limits=PRODUCT_LIMITS,
    rate_limit_group="products",
)


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

_CATEGORY_OUTPUTS: Tuple[FieldSpec, ...] = (
    FieldSpec(name="id", label="Category ID", type="string"),
    FieldSpec(name="name", label="Name", type="string"),
    FieldSpec(name="url", label="Category page", type="string"),
    FieldSpec(name="is_deleted", label="Deleted", type="boolean", path="isDeleted"),
    FieldSpec(name="created_at", label="Added at", type="datetime", path="createdAt"),
    FieldSpec(name="modified_at", label="Changed at", type="datetime", path="modifiedAt"),
)

CATEGORIES = OperationSpec(
    operation_id="categories",
    label="Product categories",
    description=(
        "The categories the catalogue is organised by. Read this to find the ids a "
        "product should be filed under."
    ),
    kind=OPERATION_READ,
    method="GET",
    path="/categories",
    query_template={**_SINCE_QUERY, "ids": "{ids}"},
    inputs=(
        *_SINCE_INPUTS,
        _SORT_INPUT,
        FieldSpec(name="ids", label="Category IDs", type="json", description="Only these categories."),
    ),
    outputs=_CATEGORY_OUTPUTS,
    records_path="categories",
    page_rule=_offset_page(MAX_CATEGORY_PAGE),
    rate_limits=OTHER_LIMITS,
    rate_limit_group=OTHER_GROUP,
)


UPSERT_CATEGORY = OperationSpec(
    operation_id="upsert_category",
    label="Create or update a category",
    description=(
        "Sends one category to Brevo, or updates the one already held under that id. Run "
        "this before syncing products, so the ids a product refers to already exist."
    ),
    kind=OPERATION_WRITE,
    method="POST",
    path="/categories",
    body_template={
        "id": "{id}",
        "name": "{name}",
        "url": "{url}",
        "isDeleted": "{is_deleted}",
        # Same literal and the same reason as the product write above.
        "updateEnabled": True,
    },
    inputs=(
        FieldSpec(
            name="id",
            label="Category ID",
            type="string",
            required=True,
            description="Your own id for the category. Brevo keys on it.",
        ),
        FieldSpec(name="name", label="Name", type="string"),
        FieldSpec(name="url", label="Category page", type="string"),
        FieldSpec(name="is_deleted", label="Deleted", type="boolean"),
    ),
    outputs=(FieldSpec(name="id", label="Category ID", type="string"),),
    idempotent=True,
    # Brevo names no figure for categories, so they come out of the same hourly hundred as
    # the reads — and out of the same bucket, because it is the same hundred.
    rate_limits=OTHER_LIMITS,
    rate_limit_group=OTHER_GROUP,
)


#: Everything this module contributes to ``SPEC.operations``, in the order the palette
#: shows them: read then write, orders then products then categories.
OPERATIONS: Tuple[OperationSpec, ...] = (
    ORDERS,
    PRODUCTS,
    CATEGORIES,
    UPSERT_ORDER,
    UPSERT_PRODUCT,
    UPSERT_CATEGORY,
)
