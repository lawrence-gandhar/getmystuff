"""
Which connectors exist, and how a node gets from a stored id to a runnable operation.

A dictionary rather than a plugin discovery mechanism. There will be four connectors by
the end of Phase 4, all of them in this repository, and anything that scans directories
or reads entry points would be machinery in place of a literal — with the added property
that a typo becomes "no such connector" at run time instead of an import error at start
up.

**Registration is explicit and at import.** ``get()`` returning ``None`` for a connector
somebody genuinely wrote is the failure mode a lazy registry has, and it appears as an
empty picker rather than as an error.

:func:`resolve_operation` is the one function the rest of the module calls. It takes a
connection and an operation id and returns an :class:`OperationSpec`, going to
``integration_rest_operations`` when the connector's operations are user-defined and to
the connector's own declaration when they are not. **The caller never learns which** —
that branch existing in one place is what keeps the vendor path and the generic path
from drifting, and it is the same argument ``load_operation`` makes one level down.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.services.integrations.connectors.spec import (
    ConnectorSpec,
    OperationSpec,
    describe_operation,
    load_operation,
)
from app.services.integrations.errors import IntegrationFailure

logger = logging.getLogger(__name__)


_REGISTRY: Dict[str, ConnectorSpec] = {}


def register(spec: ConnectorSpec) -> ConnectorSpec:
    """
    Add a connector. Validated here rather than at first use.

    A malformed spec is a programming error in this repository, and the moment to find
    out is at import — not when somebody's scheduled sync fires at three in the morning
    and discovers that an operation declares a body on a GET.
    """
    validated = spec.validated()

    if validated.connector_id in _REGISTRY:
        raise ValueError(
            f"Two connectors are registered as '{validated.connector_id}'. A connection "
            "stores that id and nothing else, so a duplicate would make it ambiguous."
        )

    _REGISTRY[validated.connector_id] = validated
    return validated


def get(connector_id: str) -> Optional[ConnectorSpec]:
    return _REGISTRY.get(str(connector_id or "").strip())


def require(connector_id: str) -> ConnectorSpec:
    """
    The connector, or a sentence naming what is available.

    ``IntegrationFailure`` rather than ``KeyError``: a connection can outlive the
    connector it names — a spec removed in a later version, a database restored from
    somewhere else — and the person reading this needs to know which connection to fix.
    """
    spec = get(connector_id)
    if spec is not None:
        return spec

    raise IntegrationFailure(
        f"This connection uses a connector called '{connector_id}', which is not "
        "available in this version. The connections it belongs to cannot run until it "
        f"is. Available: {', '.join(connector_ids()) or 'none'}."
    )


def connector_ids() -> List[str]:
    return sorted(_REGISTRY)


def all_connectors() -> List[ConnectorSpec]:
    return [_REGISTRY[key] for key in connector_ids()]


def describe_connectors() -> List[Dict[str, Any]]:
    """
    Every connector as the connections page and the AI catalogue see it.

    No URLs, no auth templates, no operation paths — see ``describe_operation``. This
    payload reaches a browser, and a base URL template in it is an internal endpoint in
    somebody's devtools.
    """
    return [
        {
            "connector_id": spec.connector_id,
            "label": spec.label,
            "description": spec.description,
            "auth_kind": spec.auth.kind,
            "asks_for_base_url": spec.base_url_is_user_supplied,
            # What the form should ask for instead of a base URL, when the connector
            # computes its own. The pattern goes out too — the browser uses it as the
            # `pattern` attribute so a bad shop domain is caught before the round trip,
            # and it is not a secret: it describes the shape of a public hostname, and the
            # server checks it again regardless.
            "asks_for_account_id": spec.account_id_required,
            "account_id_label": spec.account_id_label,
            "account_id_help": spec.account_id_help,
            "account_id_pattern": spec.account_id_pattern,
            "operations_are_user_defined": spec.operations_are_user_defined,
            "allows_private_hosts": spec.allows_private_hosts,
            "operations": [describe_operation(op) for op in spec.operations],
        }
        for spec in all_connectors()
    ]


# ---------------------------------------------------------------------------
# From a connection to an operation
# ---------------------------------------------------------------------------


async def resolve_operation(
    db: Any, connection: Any, operation_id: str
) -> Tuple[ConnectorSpec, OperationSpec]:
    """
    The connector and the operation a node named, whichever kind of connector it is.

    The one branch in the module. See the module docstring for why it is only here.
    """
    spec = require(getattr(connection, "connector_id", ""))
    wanted = str(operation_id or "").strip()

    if not wanted:
        raise IntegrationFailure(
            "This step does not say which operation to run on its connection."
        )

    if spec.operations_are_user_defined:
        operation = await _load_user_defined_operation(db, connection, wanted)
    else:
        operation = spec.operation(wanted)

    if operation is None:
        raise IntegrationFailure(
            f"'{spec.label}' has no operation called '{wanted}'. It may have been "
            "renamed or removed since this workflow was drawn — open the step and "
            f"choose again. Available: {_available(spec, wanted)}."
        )

    return spec, operation


def _available(spec: ConnectorSpec, wanted: str) -> str:
    """
    What the connector does offer.

    Only meaningful for a declared connector; a user-defined one's list lives in the
    database and saying "none" would be wrong rather than merely unhelpful.
    """
    if spec.operations_are_user_defined:
        return "the operations set up on this connection"
    return ", ".join(op.operation_id for op in spec.operations) or "none"


async def _load_user_defined_operation(
    db: Any, connection: Any, operation_id: str
) -> Optional[OperationSpec]:
    """
    One ``integration_rest_operations`` row, as a spec.

    Imported inside the function, the same call ``graph_run_service`` makes for its
    compiler: this module is imported by the AI catalogue and the routes, neither of
    which should drag the query layer in behind it, and the connectors package stays
    importable without a database.
    """
    from app.db.integrations import queries

    row = await queries.get_rest_operation(db, connection.id, operation_id)
    return load_operation(row) if row is not None else None


# ---------------------------------------------------------------------------
# Built-in connectors
# ---------------------------------------------------------------------------
# Imported for the side effect of registering. Last, so a connector module can import
# anything above it without a cycle — the same reason `app/db/models.py` imports every
# model at the bottom of the dependency graph rather than the top.
from app.services.integrations.connectors.rest_generic import connector as _rest_generic  # noqa: E402,F401
from app.services.integrations.connectors.shopify import connector as _shopify  # noqa: E402,F401
