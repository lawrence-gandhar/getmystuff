"""
Turning a template plus a map of values into the finished text of one email.

``{{VARIABLE}}`` substitution and nothing else. No conditionals, no loops, no filters, no
expression evaluator — the same discipline ``engine/transform.py`` applies to record
transforms and ``mapping/paths.py`` to path reads. What a template can do is put a value
somewhere; deciding *which* value is the binding's job, and deciding whether to send at
all is the trigger's.

**This renderer is strict, and it is the third one in the codebase with different
semantics on purpose.** The other two disagree with each other already:

* ``chatbot_ai_settings_service.render_system_prompt`` leaves an unknown placeholder
  standing, because a visible ``{{THING}}`` in a system prompt is safer than silently
  changing what the prompt asked the model to do.
* ``chatbot_action_service._render`` raises, because a half-built URL must not be sent.

Email follows the second and goes further: an unknown placeholder, an unbound required
variable, or a value that would break a header refuses the **whole** send. A prompt with a
stray placeholder can be re-run; a URL can be retried; an email that has gone to a
customer with "Dear {{CUSTOMER}}" in it cannot be recalled. Nothing is enqueued until
every placeholder in every field has resolved, because a queued row is a row a worker will
eventually send.

**Substitution is ``re.sub`` with a replacement function, never ``str.format``.** The same
rule ``credential_service._rendered`` states: ``format`` treats ``{0}`` and ``{a.b}`` as
instructions, and an operator-authored template is the last place to allow that. A literal
brace in a template is just a brace.

**The HTML body escapes its values; the text body and subject do not.** A customer name of
``Bob & Sons <bob@example.com>`` has to arrive intact in the plain-text part and as
``Bob &amp; Sons &lt;bob@example.com&gt;`` in the HTML one, or the HTML mail renders as
half a tag. The cost is that a variable cannot carry markup into the HTML body — an
operator who wants a bold name puts the ``<strong>`` in the template, where it belongs and
where it is visible to whoever reviews the template. Letting values carry markup would
make every template an injection point for whatever an upstream agent produced.

**Names are upper-case and matched case-insensitively**, so ``{{company}}`` and
``{{COMPANY}}`` cannot become two different variables. Straight from
``chatbot_ai_settings_service``; an operator who has learned the rule in the Agents section
has learned it here.
"""

import html
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from app.models.email_dispatch import (
    MAX_RECIPIENTS,
    MAX_TEMPLATE_VARIABLES,
    MAX_VARIABLE_VALUE_LENGTH,
    VARIABLE_NAME_PATTERN,
)
from app.services.email_dispatch.errors import RenderError

# ``{{ NAME }}`` with optional inner whitespace. Matches the Agents section's pattern
# exactly, including accepting a lower-case name at match time so that `{{company}}` can be
# recognised and *then* upper-cased — rejecting it at the regex would report "unknown
# placeholder" for what is really a case mistake.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

_VARIABLE_NAME_RE = re.compile(VARIABLE_NAME_PATTERN)

# A deliberately conservative address check. Not RFC 5322 — that grammar admits quoted
# strings, comments and domain literals, and a validator that accepts everything the RFC
# does accepts a great deal an SMTP server will reject anyway. What this refuses is the set
# of things that would either break the envelope or smuggle a second header: whitespace,
# angle brackets, commas, semicolons, and anything without exactly one `@` and a dotted
# domain. Over-strict by design; the failure mode is a clear refusal at save time rather
# than a mystery bounce at three in the morning.
_ADDRESS_RE = re.compile(r"^[^\s<>,;:\\\"()\[\]@]+@[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}$")

#: Longest address accepted. The RFC's own ceiling, and the column width.
MAX_ADDRESS_LENGTH = 320

# The fields a value can be substituted into. Named because the escaping and the
# line-break rule differ per field, and a RenderError says which one it happened in.
FIELD_SUBJECT = "subject"
FIELD_BODY_HTML = "body_html"
FIELD_BODY_TEXT = "body_text"
FIELD_RECIPIENTS = "recipients"


# ---------------------------------------------------------------------------
# Reading a template
# ---------------------------------------------------------------------------


def placeholders_in(text: Optional[str]) -> Set[str]:
    """
    Every variable name the text references, upper-cased.

    Used by the validator to check a template against its own declaration, and by the
    property panel to work out which rows to offer. Returns names, not occurrences — a
    variable used four times is one thing to bind.
    """
    return {match.group(1).upper() for match in _PLACEHOLDER_RE.finditer(text or "")}


def declared_names(variables: Optional[Sequence[Mapping[str, Any]]]) -> List[str]:
    """
    The declared variable names, in declaration order.

    Order is preserved rather than sorted: it is the display order in the settings UI and
    in a node's property panel, and the operator's grouping is information. The same rule
    ``SCHEMAS.md`` states for a multi-select whose order carries meaning.
    """
    names: List[str] = []
    for item in variables or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip().upper()
        if name and name not in names:
            names.append(name)
    return names


def required_names(variables: Optional[Sequence[Mapping[str, Any]]]) -> List[str]:
    """
    The declared variables that must have a value, in declaration order.

    A variable with a non-empty ``default`` is not required even if it says it is: the
    default *is* a value, so demanding one as well would refuse a send that could
    obviously have gone out.
    """
    required: List[str] = []
    for item in variables or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip().upper()
        if not name or name in required:
            continue
        if item.get("required") and not str(item.get("default") or "").strip():
            required.append(name)
    return required


def defaults_for(
    variables: Optional[Sequence[Mapping[str, Any]]],
) -> Dict[str, str]:
    """The declared defaults, as a substitution map to fall back on."""
    defaults: Dict[str, str] = {}
    for item in variables or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip().upper()
        default = str(item.get("default") or "")
        if name and default:
            defaults[name] = default
    return defaults


def parse_declaration(raw: Any) -> List[Dict[str, Any]]:
    """
    Validate and normalise a template's declared variable list.

    Accepts the already-decoded list — the schema layer owns turning the form's single
    hidden JSON field into it, the same way ``variables_json`` works in the Agents
    section. One JSON field rather than repeated form inputs so there is exactly one place
    to validate the shape.

    Raises :class:`RenderError` naming the offending entry. Every refusal here is a
    save-time refusal, which is the point: a template that cannot be rendered must not be
    storable, or the failure moves to whenever the trigger next fires.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RenderError(
            "The variable list must be a list of variables. "
            "Add them one at a time under Variables."
        )
    if len(raw) > MAX_TEMPLATE_VARIABLES:
        raise RenderError(
            f"A template may declare at most {MAX_TEMPLATE_VARIABLES} variables, "
            f"and this one declares {len(raw)}."
        )

    parsed: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise RenderError(f"Variable {index} is not filled in correctly.")

        name = str(item.get("name") or "").strip().upper()
        if not name:
            raise RenderError(f"Variable {index} needs a name.")
        if not _VARIABLE_NAME_RE.match(name):
            raise RenderError(
                f"'{name}' is not a usable variable name. Use capital letters, numbers "
                "and underscores, starting with a letter — for example CUSTOMER_NAME.",
                variable_name=name,
            )
        if name in seen:
            raise RenderError(
                f"{{{{{name}}}}} is declared twice. Remove one of them.",
                variable_name=name,
            )
        seen.add(name)

        default = str(item.get("default") or "")
        if len(default) > MAX_VARIABLE_VALUE_LENGTH:
            raise RenderError(
                f"The default for {{{{{name}}}}} is longer than "
                f"{MAX_VARIABLE_VALUE_LENGTH} characters.",
                variable_name=name,
            )

        parsed.append(
            {
                "name": name,
                "label": str(item.get("label") or "").strip()[:255] or name.replace("_", " ").title(),
                "required": bool(item.get("required")),
                "default": default,
            }
        )

    return parsed


def assert_declared(
    *,
    subject_template: str,
    body_html_template: str,
    body_text_template: Optional[str],
    variables: Sequence[Mapping[str, Any]],
) -> None:
    """
    Refuse a template that references a variable it does not declare.

    The same refusal ``_validate_prompt`` makes in the Agents section, and the same
    sentence shape, because it is the same mistake: the operator typed a placeholder and
    did not add the row. Checked across all three fields at once so the message can list
    every missing one rather than making them fix them one save at a time.
    """
    known = set(declared_names(variables))
    used = (
        placeholders_in(subject_template)
        | placeholders_in(body_html_template)
        | placeholders_in(body_text_template)
    )
    missing = sorted(used - known)
    if missing:
        listed = ", ".join(f"{{{{{name}}}}}" for name in missing)
        raise RenderError(
            f"This template uses {listed} but no matching variable is declared. "
            "Add each one under Variables, or remove it from the template."
        )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(
    text: Optional[str],
    values: Mapping[str, str],
    *,
    field: str,
    escape_html: bool = False,
    single_line: bool = False,
) -> str:
    """
    Substitute every ``{{VARIABLE}}`` in ``text``, or raise.

    ``escape_html`` HTML-escapes each substituted value — on for the HTML body, off for
    the text body and the subject. See the module docstring for why this is per-field
    rather than per-template.

    ``single_line`` refuses a value containing CR or LF. On for the subject and for
    recipients, because both become mail headers and a newline in a header value is header
    injection: everything after it is read by the server as a new header, which is how an
    attacker-supplied name turns into an extra ``Bcc``. ``chatbot_action_service`` makes
    the identical check for the same reason.

    An unknown placeholder raises rather than being left in place or emptied. Both
    alternatives send a real email to a real person with something wrong in it.
    """
    if not text:
        return ""

    def _replace(match: "re.Match[str]") -> str:
        name = match.group(1).upper()
        if name not in values:
            raise RenderError(
                f"{{{{{name}}}}} has no value. Bind it before sending.",
                variable_name=name,
                field=field,
            )
        value = str(values[name])

        if single_line and ("\r" in value or "\n" in value):
            raise RenderError(
                f"The value for {{{{{name}}}}} contains a line break, which is not "
                f"allowed in the {field.replace('_', ' ')}.",
                variable_name=name,
                field=field,
            )

        return html.escape(value, quote=True) if escape_html else value

    return _PLACEHOLDER_RE.sub(_replace, text)


def render_message(
    *,
    subject_template: str,
    body_html_template: str,
    body_text_template: Optional[str],
    variables: Sequence[Mapping[str, Any]],
    values: Mapping[str, str],
) -> Tuple[str, str, Optional[str]]:
    """
    The finished ``(subject, body_html, body_text)``.

    Declared defaults fill in for anything ``values`` does not carry, and a declared
    required variable with neither is refused **before** any substitution starts — so the
    error names the missing binding rather than whichever placeholder happened to appear
    first in the subject.

    ``body_text`` comes back ``None`` when the template has no text version, which is what
    tells the sender to build a single-part HTML message instead of ``multipart/
    alternative``.
    """
    resolved: Dict[str, str] = dict(defaults_for(variables))
    for name, value in (values or {}).items():
        resolved[str(name).upper()] = "" if value is None else str(value)

    unbound = [
        name
        for name in required_names(variables)
        if not str(resolved.get(name, "")).strip()
    ]
    if unbound:
        listed = ", ".join(f"{{{{{name}}}}}" for name in unbound)
        raise RenderError(
            f"{listed} is required by this template but has no value. "
            "Bind it, or give it a default."
            if len(unbound) == 1
            else f"{listed} are required by this template but have no values. "
            "Bind them, or give them defaults."
        )

    subject = render(
        subject_template, resolved, field=FIELD_SUBJECT, single_line=True
    ).strip()
    if not subject:
        raise RenderError(
            "The subject came out empty. Check the template and the values bound to it.",
            field=FIELD_SUBJECT,
        )

    body_html = render(
        body_html_template, resolved, field=FIELD_BODY_HTML, escape_html=True
    )
    if not body_html.strip():
        raise RenderError(
            "The message body came out empty. Check the template and the values bound "
            "to it.",
            field=FIELD_BODY_HTML,
        )

    body_text = (
        render(body_text_template, resolved, field=FIELD_BODY_TEXT)
        if body_text_template
        else None
    )

    return subject, body_html, body_text


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------


def validated_address(value: Any, *, field: str = FIELD_RECIPIENTS) -> str:
    """
    One address, or a refusal naming it.

    Deliberately over-strict — see ``_ADDRESS_RE``. The failure mode of being too strict
    is a clear sentence at save time; the failure mode of being too loose is a bounce
    nobody sees.
    """
    address = str(value or "").strip()
    if not address:
        raise RenderError("An email address is blank.", field=field)
    if len(address) > MAX_ADDRESS_LENGTH:
        raise RenderError(
            f"'{address[:60]}…' is too long to be an email address.", field=field
        )
    if not _ADDRESS_RE.match(address):
        raise RenderError(
            f"'{address}' does not look like an email address.", field=field
        )
    return address


def render_recipients(
    recipients: Optional[Mapping[str, Any]],
    values: Mapping[str, str],
) -> Dict[str, List[str]]:
    """
    Resolve ``{"to": [...], "cc": [...], "bcc": [...]}`` into checked addresses.

    Entries may contain ``{{VARIABLE}}``, which is how "email whoever this event was
    about" is expressed without a rule engine. Each rendered entry is then split on commas
    — an upstream value very often arrives as ``"a@x.com, b@y.com"`` and refusing that
    would push string-splitting into every binding.

    Duplicates are dropped while order is kept, and an address already in ``to`` is not
    also added to ``cc``: the same person receiving one email twice is a bug the operator
    did not write, usually created by two bindings resolving to the same mailbox.

    At least one ``to`` address is required. A message with only ``bcc`` is legal SMTP but
    is nearly always a mistake in this context, and one that is invisible afterwards.
    """
    resolved: Dict[str, List[str]] = {"to": [], "cc": [], "bcc": []}
    seen: Set[str] = set()

    for key in ("to", "cc", "bcc"):
        entries = (recipients or {}).get(key) or []
        if isinstance(entries, str):
            entries = [entries]
        if not isinstance(entries, (list, tuple)):
            raise RenderError(
                f"The {key.upper()} list is not filled in correctly.",
                field=FIELD_RECIPIENTS,
            )

        for entry in entries:
            rendered = render(
                str(entry), values, field=FIELD_RECIPIENTS, single_line=True
            )
            for part in rendered.split(","):
                if not part.strip():
                    continue
                address = validated_address(part)
                lowered = address.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                resolved[key].append(address)

    if not resolved["to"]:
        raise RenderError(
            "This email has nobody to go to. Add at least one TO address.",
            field=FIELD_RECIPIENTS,
        )

    total = sum(len(addresses) for addresses in resolved.values())
    if total > MAX_RECIPIENTS:
        raise RenderError(
            f"This email would go to {total} addresses, and the limit is "
            f"{MAX_RECIPIENTS}. Send to a distribution list instead.",
            field=FIELD_RECIPIENTS,
        )

    return resolved
