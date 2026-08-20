"""
What can go wrong in an integration run, as three exceptions and one refusal.

The module has three levels of failure and they are never conflated — see
``documentations/INTEGRATIONS.md``. Only two of them are exceptions, and that is the
point:

*a record failed*
    **Not an exception.** It is a counter and a row in ``integration_run_records``. The
    node keeps going. Raising here is how "3 of 50,000 records had a bad email address"
    becomes "the sync failed", and once that happens nobody can tell the two apart.

*a node failed* — :class:`NodeFailure`
    This node could not do its job. The run takes the node's ``error`` edge if one was
    drawn, and ends if not. Raised by a runner, caught by ``run_node``.

*the run failed* — :class:`IntegrationFailure`
    Something the drawing cannot route around: the version will not compile, a
    connection has been revoked, the buffer is gone. The run ends.

:class:`RunCancelled` is neither. It is a request that was honoured, and the run ends
``cancelled`` rather than ``failed`` — a distinction an operator cares about, because
one of them is their own doing.

**Every message here is written for the person who owns the workflow**, in the same
register as ``ToolQueryError``'s: what happened, and what they can do about it. None of
these carry a stack trace to the browser; the route layer turns them into an
``HTTPException`` with the sentence, and the traceback goes to the log.
"""

from typing import Optional


class IntegrationFailure(Exception):
    """
    The run cannot continue.

    Base class for everything in this module, and used directly for a failure that no
    drawn error path can catch — a version that will not compile, a buffer that has
    been released, a connection that no longer exists. Those are not conditions a
    workflow author could have anticipated with an extra node.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NodeFailure(IntegrationFailure):
    """
    One node could not do its job.

    Carries the node it happened at, so ``run_node`` can write ``errors[node_id]``
    without the runner having to know how routing works, and so the step row names the
    node even when the message does not.

    ``retryable`` is decided **here, by the code that made the call**, and never
    re-derived later from the message. The case that matters is a read timeout on a
    write that is not idempotent: the request may well have reached the destination, so
    re-sending it could create a second order, and only the caller knows whether the
    operation declared itself safe to repeat. A stored string cannot answer that
    question, and guessing it wrong duplicates somebody's data.

    ``permanent`` is stronger than ``not retryable``: it means trying again later, in a
    new run, will also fail. A 401 is permanent until somebody reconnects; a 503 is not.
    It is what stops an automatic requeue from hammering a door that is locked.
    """

    def __init__(
        self,
        message: str,
        *,
        node_id: str = "",
        retryable: bool = False,
        permanent: bool = False,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.node_id = node_id
        self.retryable = retryable
        self.permanent = permanent
        self.status_code = status_code


class RunCancelled(IntegrationFailure):
    """
    The run was asked to stop and did.

    Not a failure, and kept distinct from one so the dock can say "you stopped this"
    rather than showing a red badge for something nobody needs to investigate.

    Raised at a record boundary, never mid-request — the contract stated in the UI.
    Cancellation is polled rather than pushed, so a node already waiting on somebody
    else's server finishes that call before it notices.
    """


class FlowValidationError(IntegrationFailure):
    """
    The drawing is not a workflow that can be run.

    Separate from :class:`IntegrationFailure` because the audience is different: this
    one is answered by editing the canvas, and the sentence has to say *which node* and
    *what to do*, not merely what was wrong. ``validate_flow`` raises it identically for
    save, publish and run — a run that validated more loosely than the save would be a
    run of a flow its author could not have stored.

    ``node_id`` lets the canvas highlight the offending node instead of showing a
    banner about a graph the user has to search by hand.
    """

    def __init__(self, message: str, *, node_id: str = "", edge_id: str = "") -> None:
        super().__init__(message)
        self.node_id = node_id
        self.edge_id = edge_id
