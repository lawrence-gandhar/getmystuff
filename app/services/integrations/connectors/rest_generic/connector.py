"""
The generic REST connector — the one every other connector is a special case of.

Built first, deliberately. Starting with Shopify would have produced two request
builders, two pagination implementations and two retry paths, and the user-facing one is
always the one that rots. Here the user-facing path *is* the path: this connector's
operations are ``integration_rest_operations`` rows, so it exercises exactly the code a
vendor connector's declared operations go through.

It declares almost nothing, and that is the whole shape of it:

*no operations*
    ``operations_are_user_defined = True``. The user writes them in a form and they land
    as rows; ``registry.resolve_operation`` fetches them and ``spec.load_operation``
    turns them into the same frozen dataclass a vendor connector's literals become.

*no base URL*
    ``base_url_is_user_supplied = True``. The connection carries it. This is the one
    connector where that is safe to allow, because it is the one whose entire purpose is
    "an API we have not heard of" — and it is also why the next line matters.

*no private hosts, ever*
    ``allows_private_hosts = False``, and not configurable. A user-supplied base URL
    combined with a private-host allowance would be a form field that reaches inside the
    network, which is a server-side request forgery with a Save button. The on-premise
    escape hatch belongs to ``sap_odata``, whose base URL is not typed by anybody.

**Auth is an API key in a header, and nothing else in Phase 1.** OAuth needs a
provider-specific authorisation URL, a callback and a token exchange; there is nothing
generic about it, and a form asking a user for six OAuth endpoints would be a worse
version of writing a connector. Basic and mTLS arrive with SAP.

``value_template`` is ``{api_key}`` rather than ``Bearer {api_key}`` because the header
name is the user's too: an API wanting ``X-Api-Key: abc`` and one wanting
``Authorization: Bearer abc`` are the same connection with different strings, and
hard-coding the prefix would make the second impossible to express.
"""

from app.models.integrations import AUTH_API_KEY
from app.services.integrations.connectors import registry
from app.services.integrations.connectors.spec import (
    AuthSpec,
    ConnectorSpec,
    PLACEMENT_HEADER,
    RateLimitSpec,
)

CONNECTOR_ID = "rest_generic"

# Conservative, because we know nothing about the far end. Four a second is slow enough
# not to trip a limit somebody forgot to tell us about and fast enough that a 50,000
# record sync at 500 per page is a hundred requests rather than an afternoon. A user who
# knows better can raise it on the connection.
DEFAULT_RATE_LIMIT = RateLimitSpec(requests_per_second=4.0, burst=8)


SPEC = ConnectorSpec(
    connector_id=CONNECTOR_ID,
    label="REST API",
    description=(
        "Any HTTP API that returns JSON. You supply the address, the key and what the "
        "calls look like."
    ),
    icon="las la-code",
    accent="#495057",
    auth=AuthSpec(
        kind=AUTH_API_KEY,
        placement=PLACEMENT_HEADER,
        name="Authorization",
        value_template="{api_key}",
    ),
    rate_limits=DEFAULT_RATE_LIMIT,
    operations=(),
    base_url_template="",
    base_url_is_user_supplied=True,
    operations_are_user_defined=True,
    # See the module docstring. Not a default — a fact about this connector.
    allows_private_hosts=False,
    requires_https=True,
    hooks=None,
)


registry.register(SPEC)
