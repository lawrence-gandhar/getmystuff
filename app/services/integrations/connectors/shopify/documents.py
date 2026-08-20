"""
The GraphQL documents this connector sends, one per operation.

Separate from ``connector.py`` on purpose. Shopify ships a new API version every quarter
and supports each one for about a year, so these strings are the part of the connector
that has to be re-read against the vendor's changelog on a schedule. Keeping them in their
own module makes a version bump a diff somebody can actually review — the alternative is
thirty lines of query text wrapped around every ``OperationSpec`` field, where a changed
field name looks exactly like a changed page size.

**Every document is a literal.** ``OperationSpec.body_literals`` names the ``query`` key so
the request builder copies it across untouched; without that, the first ``{`` in
``{ orders(first: $first) …`` is read as the start of an input name and the operation is
refused before a request is built. The ``variables`` sibling still substitutes normally,
which is how the cursor and the page size get in.

**The variables are the same three everywhere**, and that is what makes the paging rule
identical for all three operations:

``$first``
    Page size. Shopify's maximum is 250 and asking for more is an error, not a clamp.

``$after``
    The cursor. Absent on page one — the template drops an input with no value rather than
    sending null — which is exactly what an unqualified first request should look like.

``$search``
    Shopify's own search syntax, e.g. ``updated_at:>2026-08-01``. Named ``search`` rather
    than ``query`` because ``query`` is already the body key holding the document, and two
    different things called ``query`` in one operation is a trap for whoever edits it next.
    This is also the field an incremental read will set once cursors are wired, so it is
    declared now even though nothing fills it automatically yet.

Fields were chosen to stay inside the AI catalogue's 25-field ceiling per operation while
covering what a sync actually maps. Money is a connection in Shopify's schema, not a
scalar, so amounts arrive as ``…Set.shopMoney.amount`` and the ``FieldSpec.path`` on each
output says so.
"""

# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
ORDERS = """
query GetOrders($first: Int!, $after: String, $search: String) {
  orders(first: $first, after: $after, query: $search, sortKey: UPDATED_AT) {
    edges {
      node {
        id
        name
        email
        phone
        createdAt
        updatedAt
        processedAt
        cancelledAt
        displayFinancialStatus
        displayFulfillmentStatus
        note
        tags
        currentTotalPriceSet { shopMoney { amount currencyCode } }
        currentSubtotalPriceSet { shopMoney { amount } }
        totalShippingPriceSet { shopMoney { amount } }
        currentTotalTaxSet { shopMoney { amount } }
        customer { id email firstName lastName }
        shippingAddress { address1 address2 city province country zip }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""".strip()


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
PRODUCTS = """
query GetProducts($first: Int!, $after: String, $search: String) {
  products(first: $first, after: $after, query: $search, sortKey: UPDATED_AT) {
    edges {
      node {
        id
        title
        handle
        description
        productType
        vendor
        status
        tags
        totalInventory
        createdAt
        updatedAt
        publishedAt
        featuredImage { url altText }
        priceRangeV2 {
          minVariantPrice { amount currencyCode }
          maxVariantPrice { amount }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""".strip()


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------
CUSTOMERS = """
query GetCustomers($first: Int!, $after: String, $search: String) {
  customers(first: $first, after: $after, query: $search, sortKey: UPDATED_AT) {
    edges {
      node {
        id
        firstName
        lastName
        displayName
        email
        phone
        note
        tags
        state
        verifiedEmail
        numberOfOrders
        createdAt
        updatedAt
        amountSpent { amount currencyCode }
        defaultAddress { address1 address2 city province country zip }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""".strip()
