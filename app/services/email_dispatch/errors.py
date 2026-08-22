"""
What can go wrong sending an email, as three exceptions.

The module has three levels of failure and they are never conflated:

*rendering failed* — :class:`RenderError`
    The template and the values it was given do not make a whole email: an unknown
    placeholder, a required variable nobody bound, a newline in a subject. **Nothing is
    enqueued.** This is caught at save time by the validator and again at enqueue, and it
    is the one failure that never produces a message row — a half-rendered email must not
    exist, because a queued row is a row a worker will eventually send.

*sending failed* — :class:`SendError`
    The message is fine; the server would not take it. Produces an ``email_messages`` row
    in ``failed`` (or back in ``queued`` with a later ``next_attempt_at``, if it is
    retryable) and an ``email_message_attempts`` row either way. This is the only one of
    the three that is ever retried.

*the configuration is wrong* — :class:`DispatchError`
    Something an operator has to go and fix before anything can be sent at all: no SMTP
    config, a config pointing at a host we refuse to connect to, a template that has been
    deleted. Not retryable by definition — trying again changes nothing until a person
    changes something.

**``retryable`` is decided by the code that made the call**, never re-derived later from
the message text. The case that decides it: a timeout after ``DATA`` has been sent. The
server may well have accepted and queued the message, so trying again could deliver it
twice — and only the code holding the socket knows how far the conversation got. A stored
string cannot answer that, and guessing wrong sends somebody the same email twice. This is
the same rule ``IntegrationFailure`` states, for the same reason.

**Every message here is written for the person who configured the email**, in the register
``IntegrationFailure`` and ``ToolQueryError`` use: what happened, and what they can do
about it. None of these carry a stack trace to the browser — the route layer turns them
into an ``HTTPException`` with the sentence, and the traceback goes to the log.
"""

from typing import Optional


class EmailFailure(Exception):
    """
    Base for everything in this module.

    Not raised directly. Its job is to let a caller that genuinely does not care which of
    the three it was — the event-bus subscriber, which logs and moves on regardless —
    write one ``except`` instead of three.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RenderError(EmailFailure):
    """
    The template could not be turned into an email.

    ``variable_name`` is set when the failure is about one specific placeholder, so the
    property panel can highlight that row rather than showing a banner about a template
    the operator has to re-read. ``field`` names which part of the template it happened in
    — ``subject``, ``body_html``, ``body_text`` or ``recipients`` — because the same
    variable can be fine in a body and illegal in a subject.

    Deliberately **not** retryable and not carrying a ``retryable`` flag at all: there is
    no version of "try that again" that helps. The template or the binding has to change.
    """

    def __init__(
        self,
        message: str,
        *,
        variable_name: str = "",
        field: str = "",
    ) -> None:
        super().__init__(message)
        self.variable_name = variable_name
        self.field = field


class SendError(EmailFailure):
    """
    The SMTP server would not accept the message.

    ``retryable`` says whether the worker should try again after a backoff. See the module
    docstring for why this is passed in rather than inferred.

    ``permanent`` is stronger than ``not retryable``: it means no later attempt will
    succeed either, so the message is failed immediately without burning the remaining
    attempts. A rejected recipient address is permanent; a 421 "too many connections" is
    not. It is what stops the queue spending five retries and twenty minutes on a mailbox
    that does not exist.

    ``smtp_code`` and ``smtp_response`` are what the server actually said, kept verbatim
    alongside the human sentence rather than instead of it — the operator needs the
    sentence, and whoever they escalate to needs the code.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        permanent: bool = False,
        smtp_code: Optional[int] = None,
        smtp_response: str = "",
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.permanent = permanent
        self.smtp_code = smtp_code
        self.smtp_response = smtp_response


class DispatchError(EmailFailure):
    """
    The setup is wrong, so there is nothing to try.

    A template that was deleted out from under a trigger, an SMTP config marked inactive,
    a recipient list that came out empty after rendering, a host the egress policy
    refuses. All of them need a person, which is why this is separate from
    :class:`SendError` and why nothing here is ever retried: a queue that retries a
    misconfiguration just writes the same failure five times.
    """
