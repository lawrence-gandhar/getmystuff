"""
The public webhook endpoint: an external system POSTs, and an email goes out.

**This controller is unauthenticated on purpose**, which makes it the highest-risk surface
in the module. It carries no ``dependencies = {"user": require_auth}``, following
``public_chatbot_routes`` and ``PublicDownloadController`` — and everything below exists
because of that.

Five defences, in the order they run:

1. **Endpoint id** — a uuid in the path that is *not* the trigger row's own uuid, so it can
   be rotated without rebuilding callers. Unknown, disabled, or the wrong kind all answer
   the same 404. A different status for "exists but disabled" would tell an unauthenticated
   caller which endpoint ids are real.
2. **Body size** — read and capped before anything parses it. An unbounded read on a public
   endpoint is a way to spend this application's memory from outside.
3. **Signature** — HMAC-SHA256 over the exact bytes received, compared with
   ``hmac.compare_digest``. Not ``==``: a byte-by-byte comparison leaks, through its timing,
   how much of a guessed signature was right, which is enough to construct a valid one.
4. **Timestamp** — a signed timestamp header outside the replay window is refused, so a
   captured request cannot be sent again tomorrow. The timestamp is inside the signed
   material, so it cannot be edited without invalidating the signature.
5. **Throttle** — ``min_interval_seconds`` since ``last_fired_at``. Without it the URL is a
   way to make this application send mail as fast as somebody can post.

**The signature covers the timestamp and the body together.** ``"{timestamp}.{body}"``,
which is what stops the two being recombined: signing them separately would let an attacker
pair yesterday's body with today's timestamp, and each part would verify on its own.

**A valid call returns 202, not 200.** The email is queued, not sent — the worker sends it —
and answering 200 would tell the caller something this endpoint does not know yet.
"""

import hashlib
import hmac
import logging
import time
import uuid
from datetime import datetime, timezone

from litestar import Controller, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.email_dispatch.queries import mark_trigger_fired, trigger_by_endpoint
from app.models.email_dispatch import SOURCE_WEBHOOK, TRIGGER_WEBHOOK
from app.services.email_dispatch import dispatch_service, queue
from app.services.email_dispatch.errors import EmailFailure, RenderError
from app.services.email_dispatch.variable_sources import (
    VariableContext,
    resolve_bindings,
)
from app.utils.crypto import decrypt_secret

logger = logging.getLogger(__name__)

#: Largest body accepted. A webhook payload is a handful of fields; anything larger is
#: either a mistake or an attempt to make this process allocate.
MAX_BODY_BYTES = 64 * 1024

#: How far out of date a signed timestamp may be. Five minutes each way covers ordinary
#: clock skew between two hosts without leaving a captured request replayable for long.
REPLAY_WINDOW_SECONDS = 300

#: Where the caller puts the signature and the timestamp it signed.
SIGNATURE_HEADER = "X-GetMyStuff-Signature"
TIMESTAMP_HEADER = "X-GetMyStuff-Timestamp"

#: The prefix the signature carries, so the algorithm is stated rather than assumed and a
#: future second one can be told apart from this one.
SIGNATURE_PREFIX = "sha256="


def _expected_signature(secret: str, timestamp: str, body: bytes) -> str:
    """
    The signature the caller should have sent.

    Signs ``"{timestamp}.{body}"`` as one string — see the module docstring on why the two
    cannot be signed separately.
    """
    material = timestamp.encode("utf-8") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def _assert_fresh(timestamp: str) -> None:
    """Refuse a timestamp outside the replay window, or one that is not a number."""
    try:
        sent_at = int(float(timestamp))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"{TIMESTAMP_HEADER} must be a Unix timestamp in seconds.",
        ) from None

    drift = abs(int(time.time()) - sent_at)
    if drift > REPLAY_WINDOW_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=(
                "This request's timestamp is outside the accepted window. Check the "
                "sending system's clock, and do not replay old requests."
            ),
        )


class EmailWebhookController(Controller):
    """
    Inbound webhooks that send email.

    Path is under ``/public`` so it reads as unauthenticated wherever it appears in a route
    list, matching ``/public/chatbot`` and ``/public/downloads``.
    """

    path = "/public/emails"

    @post("/webhooks/{endpoint_id:uuid}")
    async def receive(
        self, endpoint_id: uuid.UUID, request: Request, db: AsyncSession
    ) -> Response:
        """
        Verify, then queue. 202 on success.

        Every refusal is a plain JSON body with a sentence — the caller is a machine and its
        operator is reading a log, so there is no template here and no HTML.
        """
        trigger = await trigger_by_endpoint(db, endpoint_id)

        # One answer for "no such endpoint", "disabled" and "not a webhook trigger". A
        # different status for any of them tells an unauthenticated caller which endpoint
        # ids exist.
        if (
            trigger is None
            or trigger.kind != TRIGGER_WEBHOOK
            or not trigger.is_enabled
            or not trigger.webhook_secret_encrypted
        ):
            return Response(
                {"status": "error", "message": "Unknown webhook endpoint."},
                status_code=404,
            )

        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return Response(
                {
                    "status": "error",
                    "message": (
                        f"The request body is larger than {MAX_BODY_BYTES} bytes."
                    ),
                },
                status_code=413,
            )

        signature = request.headers.get(SIGNATURE_HEADER, "")
        timestamp = request.headers.get(TIMESTAMP_HEADER, "")
        if not signature or not timestamp:
            return Response(
                {
                    "status": "error",
                    "message": (
                        f"Both {SIGNATURE_HEADER} and {TIMESTAMP_HEADER} are required."
                    ),
                },
                status_code=401,
            )

        try:
            _assert_fresh(timestamp)
        except HTTPException as exc:
            return Response(
                {"status": "error", "message": str(exc.detail)},
                status_code=exc.status_code,
            )

        expected = _expected_signature(
            decrypt_secret(trigger.webhook_secret_encrypted), timestamp, body
        )
        # compare_digest, never ==. See the module docstring.
        if not hmac.compare_digest(signature, expected):
            logger.warning(
                "Rejected a webhook call to endpoint %s: bad signature.", endpoint_id
            )
            return Response(
                {"status": "error", "message": "The signature does not match."},
                status_code=401,
            )

        # --- throttle ---------------------------------------------------------
        # After the signature, deliberately: an unauthenticated caller must not be able to
        # learn when a trigger last fired by watching for a 429.
        if trigger.last_fired_at is not None:
            last = trigger.last_fired_at
            # `DateTime(timezone=True)` is naive on SQLite and aware on PostgreSQL, so a
            # value off a row is normalised before any arithmetic. Same helper reasoning as
            # `scheduler._aware`.
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            if elapsed < trigger.min_interval_seconds:
                return Response(
                    {
                        "status": "error",
                        "message": (
                            "This webhook was called too recently. It accepts one call "
                            f"every {trigger.min_interval_seconds} seconds."
                        ),
                    },
                    status_code=429,
                    headers={
                        "Retry-After": str(
                            max(1, int(trigger.min_interval_seconds - elapsed))
                        )
                    },
                )

        payload = await self._parsed(request, body)

        try:
            values = resolve_bindings(
                trigger.variable_bindings,
                VariableContext(
                    event_payload=payload,
                    agent_variables=None,  # type: ignore[arg-type]
                    session_variables=None,  # type: ignore[arg-type]
                    node_outputs=None,  # type: ignore[arg-type]
                ),
            )
            await dispatch_service.enqueue_email(
                db,
                user_id=trigger.user_id,
                template=trigger.template,
                config=trigger.smtp_config,
                recipients=trigger.recipients,
                values=values,
                source=SOURCE_WEBHOOK,
                source_ref=f"webhook {endpoint_id}",
                trigger_id=trigger.id,
                trigger_kind=trigger.kind,
                workspace_id=trigger.workspace_id,
                # Keyed on the signature: a caller that did not see our 202 and retried
                # sends byte-identical material, so it produces one email. A caller sending
                # genuinely new data signs differently and gets a second.
                idempotency=dispatch_service.idempotency_key(
                    "webhook", str(trigger.uuid), signature
                ),
            )
            await db.commit()
        except IntegrityError:
            # The idempotency key exists: this exact call has already been accepted. 202,
            # because from the caller's point of view it succeeded — and answering anything
            # else would make a well-behaved retrying client escalate.
            await db.rollback()
            logger.info("Webhook %s redelivered; already queued.", endpoint_id)
            return Response(
                {"status": "success", "message": "Already queued."}, status_code=202
            )
        except (RenderError, EmailFailure) as exc:
            await db.rollback()
            # 422, not 500: the request was authentic and well-formed, but its payload does
            # not satisfy the template's bindings — a field is missing or has the wrong
            # shape. That is something the *caller* can fix, so it must not read as our
            # fault.
            logger.warning(
                "Webhook %s accepted but could not be rendered: %s",
                endpoint_id,
                exc.message,
            )
            return Response(
                {"status": "error", "message": exc.message}, status_code=422
            )

        await mark_trigger_fired(db, trigger.id)
        queue.wake()

        return Response(
            {"status": "success", "message": "Email queued."}, status_code=202
        )

    async def _parsed(self, request: Request, body: bytes) -> dict:
        """
        The body as a mapping, whatever it arrived as.

        A non-object JSON body — a bare list or string — is wrapped under ``value`` rather
        than refused, so a binding can still reach it with a path. A body that is not JSON
        at all becomes ``{"raw": "..."}`` for the same reason: the signature already proved
        the caller is who they say they are, and refusing them over a content type is less
        useful than letting a binding decide whether the payload has what it needs.
        """
        if not body:
            return {}
        try:
            parsed = await request.json()
        except Exception:  # noqa: BLE001
            return {"raw": body.decode("utf-8", errors="replace")[:10_000]}

        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
