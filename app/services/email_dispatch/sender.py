"""
Handing one finished message to one SMTP server.

**This is the only module that imports aiosmtplib, and ``send_message`` is the only
function that touches a socket.** Both facts are load-bearing. ``tests/conftest.py``
installs an autouse ``block_network`` fixture that raises on any non-loopback TCP
connection, so every test of the worker, the queue, the retry logic and the routes
monkeypatches this one name. If the transport were reachable from two places, one of them
would be untestable without a real mail server.

**The egress policy is checked before the socket, not after.** An SMTP host is
user-supplied text, and the same class of hazard applies to it as to an integration
connection's base URL: ``smtp.internal``, ``localhost``, ``169.254.169.254`` and anything
resolving into a private range are ways to make this application connect somewhere on the
operator's behalf that they did not intend. ``assert_public_host`` is the check the
integrations module already uses; reusing it means one place decides what "reachable" means
and one place gets fixed when that answer changes.

**Plaintext SMTP is only allowed to a host that has been allow-listed.** Sending
``AUTH LOGIN`` over an unencrypted connection to a public host puts a base64'd password on
the wire, which is a credential leak dressed as a configuration choice. ``security = none``
is refused unless the policy already permits that private host — the same coupling
``EgressPolicy`` enforces between ``require_https=False`` and ``allow_private``.

**Every failure leaves through ``SendError`` carrying flags, never a driver exception.**
``retry.classify_code`` decides retryable/permanent from the reply code once, here, at the
moment of failure. Nothing downstream re-reads the message text to work it out again — the
rule ``NodeFailure`` states, and the case that decides it is a timeout after ``DATA``,
where the server may have accepted the mail and only the code holding the socket knows how
far the conversation got.
"""

import logging
import os
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, make_msgid
from typing import List, Optional, Tuple

import aiosmtplib

from app.models.email_dispatch import SECURITY_NONE, SECURITY_SSL, SECURITY_STARTTLS
from app.services.email_dispatch import retry
from app.services.email_dispatch.errors import DispatchError, SendError
from app.utils.outbound_http import EgressError, EgressPolicy, assert_public_host

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmtpTarget:
    """
    Everything needed to open one connection, with the password already decrypted.

    A frozen dataclass rather than the ORM row, for the same reason
    ``credential_service.auth_for`` returns a finished pair rather than a token: what
    crosses into the transport is exactly what the transport needs, and a caller cannot
    accidentally hand the whole config — with its owner, its workspace and its test history
    — to something that logs its arguments.

    The password is in memory here and nowhere else. It is never put on the message, never
    logged, and never included in a ``SendResult``.
    """

    host: str
    port: int
    security: str
    username: Optional[str]
    password: Optional[str]
    timeout_seconds: int


@dataclass(frozen=True)
class SendResult:
    """
    What the server said when it accepted the message.

    ``response`` is the final reply verbatim, for the log. ``rejected`` names recipients the
    server refused *while accepting the others* — a partial success SMTP reports without
    raising, which would otherwise be recorded as an unqualified "sent" while somebody
    never received it.
    """

    response: str
    message_id: str
    rejected: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Reaching a relay inside a private network
# ---------------------------------------------------------------------------
# A great many real deployments send through a relay on a private address — a corporate
# smarthost, a sidecar, `localhost:1025` in development. Refusing all of them would make
# the feature unusable for exactly the organisations most likely to insist on it.
#
# But it cannot be a form field. A form field that grants itself permission to reach
# internal addresses is an SSRF hole with a label on it: anybody who can create an SMTP
# config could point it at the cloud metadata endpoint and read the reply out of the test
# button's error message. So the allow-list lives in the environment, where changing it is
# a deployment decision made by whoever operates the host — the same conclusion
# `connection_service.set_private_host_access` reaches by gating on an admin flag.
#
#   EMAIL_ALLOWED_PRIVATE_HOSTS=localhost:1025,smtp.internal:587
#   EMAIL_ALLOWED_PRIVATE_CIDRS=127.0.0.0/8,10.0.0.0/8
#
# Both are required together, which `EgressPolicy.validated()` enforces: the hostname has
# to be one somebody wrote down *and* the address it resolved to has to be inside a range
# somebody wrote down. Either alone is too loose — a hostname check falls to a DNS answer
# the operator does not control, and a CIDR check permits any hostname that resolves into
# the range.


def _env_list(name: str) -> tuple:
    """A comma-separated environment list, trimmed, without empties."""
    return tuple(
        part.strip() for part in (os.getenv(name) or "").split(",") if part.strip()
    )


def _build_policy() -> EgressPolicy:
    """
    The policy every send and every test is judged against.

    Built once at import and **validated there**, so a malformed allow-list stops the
    application at startup with a sentence rather than failing the first send at three in
    the morning. Half a policy — hosts without ranges — is refused rather than quietly
    treated as "no private access", because an administrator who configured one of the two
    believes they have granted access.
    """
    hosts = _env_list("EMAIL_ALLOWED_PRIVATE_HOSTS")
    cidrs = _env_list("EMAIL_ALLOWED_PRIVATE_CIDRS")

    if not hosts and not cidrs:
        # The ordinary case: nothing private is reachable.
        return EgressPolicy().validated()

    return EgressPolicy(
        allow_private=True,
        allowed_hosts=frozenset(hosts),
        allowed_cidrs=cidrs,
    ).validated()


#: Module-level, so the refusal for an incoherent allow-list happens at import.
POLICY = _build_policy()


def _policy_for(target: SmtpTarget) -> EgressPolicy:
    """
    The policy for one target.

    A function rather than the constant used directly, so a per-config exception has one
    obvious place to go if this ever needs one — and so a test can patch a single name.
    """
    return POLICY


def _address(email: str, name: Optional[str]) -> str:
    """
    ``Name <email>``, or bare ``email``.

    ``formataddr`` quotes and RFC 2047-encodes the display name, which matters because the
    name can come from a template variable: a comma in it would otherwise be read as an
    address separator and split one recipient into two.
    """
    return formataddr((name, email)) if name else email


def build_mime(
    *,
    subject: str,
    body_html: str,
    body_text: Optional[str],
    from_email: str,
    from_name: Optional[str],
    to_addresses: List[str],
    cc_addresses: Optional[List[str]] = None,
    reply_to: Optional[str] = None,
) -> MimeMessage:
    """
    Assemble the MIME message.

    Split out from ``send_message`` and pure, so a test can assert the envelope — headers,
    parts, encodings — without a socket or a monkeypatch.

    **Bcc is deliberately absent from the headers.** It is passed to the transport as an
    envelope recipient only. A ``Bcc`` header would be delivered to everyone on the
    message, which is the exact opposite of what bcc means and is a privacy breach rather
    than a formatting mistake.

    Text first, then HTML: ``multipart/alternative`` is ordered least-preferred to
    most-preferred, so a client picking the last part it understands lands on the HTML. Put
    the other way round every graphical client shows the plain text.

    ``Message-ID`` is generated here rather than left to the server, so the value we log is
    the value that went out and a bounce can be traced back to a row.
    """
    mime = MimeMessage()
    mime["Subject"] = subject
    mime["From"] = _address(from_email, from_name)
    mime["To"] = ", ".join(to_addresses)
    if cc_addresses:
        mime["Cc"] = ", ".join(cc_addresses)
    if reply_to:
        mime["Reply-To"] = reply_to
    mime["Message-ID"] = make_msgid()

    if body_text:
        mime.set_content(body_text, subtype="plain", charset="utf-8")
        mime.add_alternative(body_html, subtype="html", charset="utf-8")
    else:
        # No text alternative: a single-part text/html message rather than a multipart with
        # one branch, which some older clients render as an empty message with an
        # attachment.
        mime.set_content(body_html, subtype="html", charset="utf-8")

    return mime


async def send_message(
    *,
    target: SmtpTarget,
    subject: str,
    body_html: str,
    body_text: Optional[str],
    from_email: str,
    from_name: Optional[str],
    to_addresses: List[str],
    cc_addresses: Optional[List[str]] = None,
    bcc_addresses: Optional[List[str]] = None,
    reply_to: Optional[str] = None,
) -> SendResult:
    """
    Send one message, or raise :class:`SendError` / :class:`DispatchError`.

    **The seam.** Monkeypatch this name in tests; do not reach past it.

    ``DispatchError`` for something a person has to fix before any send can work (a host we
    refuse to reach, plaintext auth to a public server). ``SendError`` for the server
    declining this message, carrying the retryable/permanent flags the worker acts on.
    """
    recipients = list(to_addresses) + list(cc_addresses or []) + list(bcc_addresses or [])
    if not recipients:
        raise DispatchError(
            "This email has no recipients, so there is nothing to send it to."
        )

    policy = _policy_for(target)

    # Refuse before opening anything. The check resolves the hostname, so it also catches a
    # name that does not resolve at all — which is a configuration mistake worth a clear
    # sentence rather than a connection timeout thirty seconds later.
    try:
        await assert_public_host(target.host, target.port, policy=policy)
    except EgressError as exc:
        raise DispatchError(
            f"This application will not connect to {target.host}: {exc}"
        ) from exc

    if target.security == SECURITY_NONE and target.password:
        raise DispatchError(
            f"Sending through {target.host} with no encryption would put the password "
            "on the network in readable form. Use STARTTLS or SSL/TLS, or remove the "
            "credentials if the relay authenticates by address."
        )

    mime = build_mime(
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        from_email=from_email,
        from_name=from_name,
        to_addresses=to_addresses,
        cc_addresses=cc_addresses,
        reply_to=reply_to,
    )

    try:
        rejected, response = await aiosmtplib.send(
            mime,
            sender=from_email,
            recipients=recipients,
            hostname=target.host,
            port=target.port,
            username=target.username or None,
            password=target.password or None,
            timeout=float(target.timeout_seconds),
            # aiosmtplib's two switches are mutually exclusive: `use_tls` wraps the socket
            # from the first byte (the 465 style), `start_tls` upgrades a plaintext
            # connection (the 587 style). Passing both raises before connecting.
            use_tls=target.security == SECURITY_SSL,
            start_tls=True if target.security == SECURITY_STARTTLS else None,
        )
    except aiosmtplib.SMTPAuthenticationError as exc:
        # Its own branch above the general response error: the code is a 5xx that
        # `classify_code` correctly calls permanent, but the *sentence* has to point at the
        # credentials rather than at the message, because that is where the fix is.
        raise SendError(
            f"{target.host} rejected the username and password. Check the credentials "
            "on this SMTP server's settings.",
            retryable=False,
            permanent=True,
            smtp_code=getattr(exc, "code", None),
            smtp_response=str(exc),
        ) from exc
    except (
        aiosmtplib.SMTPRecipientsRefused,
        aiosmtplib.SMTPRecipientRefused,
    ) as exc:
        raise SendError(
            f"{target.host} refused every recipient address on this email. "
            "Check the addresses it was sent to.",
            retryable=False,
            permanent=True,
            smtp_code=getattr(exc, "code", None),
            smtp_response=str(exc),
        ) from exc
    except (
        aiosmtplib.SMTPConnectTimeoutError,
        aiosmtplib.SMTPReadTimeoutError,
        aiosmtplib.SMTPTimeoutError,
    ) as exc:
        # Retryable, and knowingly so: a timeout during DATA may mean the server took the
        # message. See the module docstring. The alternative — treating a timeout as
        # permanent — loses mail every time a relay is merely slow, which is far more
        # common than the duplicate.
        raise SendError(
            f"{target.host} did not respond within {target.timeout_seconds} seconds. "
            "It will be tried again.",
            retryable=True,
            permanent=False,
            smtp_response=str(exc),
        ) from exc
    except (
        aiosmtplib.SMTPConnectError,
        aiosmtplib.SMTPServerDisconnected,
    ) as exc:
        raise SendError(
            f"Could not reach {target.host} on port {target.port}. It will be tried "
            "again.",
            retryable=True,
            permanent=False,
            smtp_response=str(exc),
        ) from exc
    except aiosmtplib.SMTPResponseException as exc:
        code = getattr(exc, "code", None)
        retryable, permanent = retry.classify_code(code)
        raise SendError(
            f"{target.host} would not accept this email (SMTP {code}). "
            + (
                "It will be tried again."
                if retryable
                else "It will not be tried again automatically."
            ),
            retryable=retryable,
            permanent=permanent,
            smtp_code=code,
            smtp_response=str(exc),
        ) from exc
    except aiosmtplib.SMTPException as exc:
        # The catch-all for the driver's own errors, kept last so the specific branches
        # above still get their better sentences. Retryable by the asymmetric-cost argument
        # in `retry`: an unrecognised transport error is more often a bad moment than a bad
        # message.
        raise SendError(
            f"Sending through {target.host} failed: {exc}. It will be tried again.",
            retryable=True,
            permanent=False,
            smtp_response=str(exc),
        ) from exc

    # `rejected` is a dict of address -> SMTPResponse for recipients the server refused
    # while accepting the rest. aiosmtplib does not raise for this, and treating it as an
    # unqualified success would record "sent" for an email somebody never got.
    refused = tuple(str(address) for address in (rejected or {}))
    if refused:
        logger.warning(
            "%s accepted the message but refused %d recipient(s): %s",
            target.host,
            len(refused),
            ", ".join(refused),
        )

    return SendResult(
        response=str(response or "").strip()[:2000],
        message_id=str(mime["Message-ID"] or ""),
        rejected=refused,
    )


async def verify_target(target: SmtpTarget) -> str:
    """
    Open a connection, authenticate, and hang up without sending anything.

    Behind the Send-test button. Deliberately does **not** send a probe email: a test that
    delivers mail to whoever the operator happened to have in the form is a test with a
    side effect, and operators press test buttons repeatedly.

    Returns the greeting for the log. Raises the same two exception types as
    ``send_message`` so the route has one thing to catch.
    """
    try:
        await assert_public_host(target.host, target.port, policy=_policy_for(target))
    except EgressError as exc:
        raise DispatchError(
            f"This application will not connect to {target.host}: {exc}"
        ) from exc

    client = aiosmtplib.SMTP(
        hostname=target.host,
        port=target.port,
        timeout=float(target.timeout_seconds),
        use_tls=target.security == SECURITY_SSL,
        start_tls=True if target.security == SECURITY_STARTTLS else None,
    )

    try:
        await client.connect()
        if target.username:
            await client.login(target.username, target.password or "")
        return "Connected and signed in successfully."
    except aiosmtplib.SMTPAuthenticationError as exc:
        raise SendError(
            f"{target.host} accepted the connection but rejected the username and "
            "password.",
            retryable=False,
            permanent=True,
            smtp_code=getattr(exc, "code", None),
            smtp_response=str(exc),
        ) from exc
    except aiosmtplib.SMTPException as exc:
        raise SendError(
            f"Could not connect to {target.host} on port {target.port}: {exc}",
            retryable=True,
            permanent=False,
            smtp_response=str(exc),
        ) from exc
    finally:
        # QUIT can itself fail on a connection that has already gone; that must not replace
        # the real error above with a less useful one.
        try:
            await client.quit()
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring error while closing SMTP test connection.", exc_info=True)
