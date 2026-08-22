"""
Where a template variable's value comes from.

This is the module that answers the product question "the dynamic variables can come from
the Agents section". A binding names a **source** and a **path**, and each caller supplies
whichever contexts it actually has:

===================  ==============================================  =====================
source               resolves from                                   available in
===================  ==============================================  =====================
``literal``          the binding's own ``value``                      everywhere
``agent``            ``chatbot_ai_settings_service.variables_map``    chat flows, graphs
``session``          ``ChatbotFlowSession.variables``                 Flow Builder
``node``             ``state["outputs"][node_id]``                    Graph Designer
``record``           the record in hand                              Integrations
``event``            the event or webhook payload                    triggers
===================  ==============================================  =====================

**No expression evaluator, ever.** A binding is a named source plus a dotted path read by
``mapping/paths.py``'s restricted reader. That is the same discipline
``engine/transform.py`` applies to record transforms and ``sql_guard`` to placeholders, and
the reason is that these bindings are authored by operators and rendered into mail sent to
customers: anything that evaluates a string is a way to make this application compute
something nobody reviewed.

**An unavailable source is refused by name, never resolved to an empty string.** A Flow
Builder node has session variables and no upstream node outputs; an integration node has
records and no chat session. Silently substituting blank would send somebody an email
addressed to "Dear ," — the failure is real either way, and a refusal at least says which
binding is wrong and where.

**A binding that finds nothing yields nothing, and the template decides what that means.**
This is the one piece of routing worth reading twice. When a path resolves to nothing, the
variable is *omitted* from the returned map rather than set to ``""`` — and
``rendering.render_message`` then fills it from the variable's declared default, or refuses
the whole send if it was declared required with no default.

That puts the strictness where the operator can see and set it, one variable at a time,
instead of hard-coding one answer for every case. A ``{{REASON}}`` that is naturally absent
when a run succeeded gets a default of "none given"; a ``{{CUSTOMER}}`` marked required
stops the email rather than addressing somebody as "Dear ,". Both are expressed in the
template, which is where somebody reading the template can see them.

A **malformed** path is different and is refused outright: ``customer..name`` is a typo in
the binding, not an absent field, and no default should paper over it. The same check runs
at save time, so it is normally caught at the keyboard.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from app.models.email_dispatch import (
    BINDING_AGENT,
    BINDING_EVENT,
    BINDING_LITERAL,
    BINDING_NODE,
    BINDING_RECORD,
    BINDING_SESSION,
    BINDING_SOURCE_VALUES,
    MAX_VARIABLE_VALUE_LENGTH,
)
from app.services.email_dispatch.errors import RenderError
from app.services.integrations.mapping import paths
from app.services.integrations.mapping.paths import PathError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VariableContext:
    """
    Whichever value sources the calling canvas can actually offer.

    A frozen dataclass with every field defaulted, so each caller fills in only what it has
    and the resolver decides what that makes available. That is what keeps one resolver
    serving three canvases plus two triggers without a flag saying which one it is: the
    *shape of the context* is the answer.

    ``node_outputs`` is the graph state's ``outputs`` map, keyed by node id — not by label,
    because two nodes can share a label.
    """

    agent_variables: Mapping[str, str] = field(default_factory=dict)
    session_variables: Mapping[str, Any] = field(default_factory=dict)
    node_outputs: Mapping[str, Any] = field(default_factory=dict)
    record: Optional[Mapping[str, Any]] = None
    event_payload: Optional[Mapping[str, Any]] = None

    def available(self) -> frozenset:
        """
        Which sources this context can serve.

        ``literal`` is always available. The rest are available when their container was
        supplied at all — an *empty* dict still counts, because "the chat session has no
        variables yet" is a different failure from "there is no chat session here", and the
        first deserves "PATH not found" rather than "this canvas cannot do that".
        """
        sources = {BINDING_LITERAL}
        if self.agent_variables is not None:
            sources.add(BINDING_AGENT)
        if self.session_variables is not None:
            sources.add(BINDING_SESSION)
        if self.node_outputs is not None:
            sources.add(BINDING_NODE)
        if self.record is not None:
            sources.add(BINDING_RECORD)
        if self.event_payload is not None:
            sources.add(BINDING_EVENT)
        return frozenset(sources)


#: What a canvas offers when it has nothing but literals — used by the validator to check a
#: binding's *shape* without a live run in front of it.
EMPTY_CONTEXT = VariableContext(
    agent_variables=None,  # type: ignore[arg-type]
    session_variables=None,  # type: ignore[arg-type]
    node_outputs=None,  # type: ignore[arg-type]
)


#: Returned by a source resolver that found nothing. Distinct from ``None``, which is a
#: value a payload can legitimately hold, and from ``""``, which is text. The caller omits a
#: MISSING variable from the resolved map so the template's own default or required flag
#: decides what happens — see the module docstring.
MISSING = object()


def _stringify(value: Any, *, name: str) -> str:
    """
    One resolved value as the text that goes in an email.

    ``None`` becomes empty rather than the word "None", which is the single most common way
    a templating layer embarrasses somebody. A list or dict is refused instead of being
    stringified: ``{'id': 3}`` in the middle of a sentence is never what the operator meant,
    and a path one level short of the field is the usual cause — so the message says so.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        # Before the int check: bool is a subclass of int, and "true" reads better than "1".
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        text = str(value)
    else:
        raise RenderError(
            f"{{{{{name}}}}} resolved to a whole {type(value).__name__} rather than a "
            "single value. Point the binding at one field inside it.",
            variable_name=name,
        )

    if len(text) > MAX_VARIABLE_VALUE_LENGTH:
        # Truncated rather than refused: an over-long value is usually a description or a
        # log line somebody wanted the beginning of, and refusing the whole email over it
        # would be the wrong trade. The ellipsis says it happened.
        return text[: MAX_VARIABLE_VALUE_LENGTH - 1] + "\u2026"
    return text


def assert_path(path: str, *, name: str) -> None:
    """
    Refuse a malformed path.

    ``paths.parse`` raises ``PathError`` for something like ``customer..name`` or an
    unclosed bracket. That is a typo in the binding rather than an absent field, so no
    default should paper over it — and this same function runs at save time, which is where
    it is normally caught.
    """
    try:
        paths.parse(path)
    except PathError as exc:
        raise RenderError(
            f"'{path}' is not a valid field path for {{{{{name}}}}}: {exc}",
            variable_name=name,
        ) from exc


def _read(container: Any, path: str, *, name: str) -> Any:
    """
    A dotted path into a payload, or :data:`MISSING`.

    Uses ``mapping/paths.py`` — the reader the integrations module already trusts with
    vendor payloads — rather than a fresh attribute walk. It understands ``a.b`` and
    ``items[0]`` and refuses anything that would reach into Python internals.

    ``paths.read`` answers ``None`` both for "absent" and for "present and null", and this
    does not try to separate them: for an email the two mean the same thing — there is
    nothing to put in the sentence — and the template's default is the right place to say
    what to do about it.
    """
    assert_path(path, name=name)
    found = paths.read(container, path)
    if found is None or found == []:
        return MISSING
    return found


# ---------------------------------------------------------------------------
# One resolver per source
# ---------------------------------------------------------------------------
# Split out and dispatched through a dict rather than an if/elif chain, the same shape
# `node_runners._RUNNERS` uses: adding a source is a function plus an entry, and each one
# stays short enough to read whole.
#
# Every resolver returns the raw value or MISSING, and never a string — stringifying is one
# concern in one place, so the truncation rule and the "that is a whole dict" refusal cannot
# drift between sources.


def _from_literal(binding: Mapping[str, Any], context: "VariableContext", name: str) -> Any:
    value = binding.get("value")
    # A literal that was left blank is genuinely blank, not missing: the operator typed
    # nothing on purpose, and a default overriding that would ignore them.
    return "" if value is None else value


def _from_agent(binding: Mapping[str, Any], context: "VariableContext", name: str) -> Any:
    """
    A prompt variable from the Agents section.

    A flat name lookup, not a path: an agent's variables are a flat string->string map by
    construction, so treating "COMPANY" as a path would make a name containing a dot
    unreachable for no gain.
    """
    key = (str(binding.get("path") or "").strip() or name).upper()
    return context.agent_variables.get(key, MISSING)


def _from_session(binding: Mapping[str, Any], context: "VariableContext", name: str) -> Any:
    """A value the conversation collected. Also a flat map — `ChatbotFlowSession.variables`
    is string->string."""
    key = str(binding.get("path") or "").strip() or name
    return context.session_variables.get(key, MISSING)


def _from_node(binding: Mapping[str, Any], context: "VariableContext", name: str) -> Any:
    """
    The output of an earlier node, optionally a field inside it.

    A node id that is not in the outputs map is refused rather than treated as missing, and
    that distinction is deliberate: it means the referenced node was deleted or was skipped
    by a branch, which is a broken drawing rather than an absent value, and a default would
    hide it. Every reader of a node id inside a JSONB drawing has to survive this — the id
    is not a foreign key and nothing enforces it.
    """
    node_id = str(binding.get("node_id") or "").strip()
    if not node_id:
        raise RenderError(
            f"{{{{{name}}}}} is bound to an earlier node but no node was chosen.",
            variable_name=name,
        )
    if node_id not in context.node_outputs:
        raise RenderError(
            f"{{{{{name}}}}} is bound to a node that did not produce anything on this "
            "path. It may have been deleted, or skipped by a branch.",
            variable_name=name,
        )

    output = context.node_outputs[node_id]
    path = str(binding.get("path") or "").strip()
    if not path:
        return MISSING if output is None else output
    return _read(output, path, name=name)


def _from_record(binding: Mapping[str, Any], context: "VariableContext", name: str) -> Any:
    path = str(binding.get("path") or "").strip()
    if not path:
        raise RenderError(
            f"{{{{{name}}}}} is bound to the current record but no field was chosen.",
            variable_name=name,
        )
    return _read(context.record, path, name=name)


def _from_event(binding: Mapping[str, Any], context: "VariableContext", name: str) -> Any:
    path = str(binding.get("path") or "").strip()
    if not path:
        raise RenderError(
            f"{{{{{name}}}}} is bound to the incoming payload but no field was chosen.",
            variable_name=name,
        )
    return _read(context.event_payload, path, name=name)


_RESOLVERS = {
    BINDING_LITERAL: _from_literal,
    BINDING_AGENT: _from_agent,
    BINDING_SESSION: _from_session,
    BINDING_NODE: _from_node,
    BINDING_RECORD: _from_record,
    BINDING_EVENT: _from_event,
}

# The two lists must not drift: a source named in the model vocabulary with no resolver here
# would be offered by the form and refused at run time. Asserted at import, the same way
# `engine/node_runners` pins its runners against `IMPLEMENTED_NODE_TYPES` — so the mistake
# stops the application rather than one email.
assert set(_RESOLVERS) == set(BINDING_SOURCE_VALUES), (
    "the binding resolvers and BINDING_SOURCES disagree: "
    f"{set(_RESOLVERS) ^ set(BINDING_SOURCE_VALUES)}"
)


def resolve_bindings(
    bindings: Optional[Mapping[str, Any]],
    context: VariableContext,
) -> Dict[str, str]:
    """
    Turn ``{"CUSTOMER": {"source": "event", "path": "customer.name"}}`` into
    ``{"CUSTOMER": "Jane"}``.

    A binding that finds nothing is **left out** of the result rather than set to ``""``, so
    the template's declared default or required flag decides what happens next. See the
    module docstring — this is the piece worth reading twice.

    Every refusal is a :class:`RenderError` naming the variable, so the caller can put it in
    front of whoever authored the binding. Nothing is enqueued when one fails.
    """
    available = context.available()
    resolved: Dict[str, str] = {}

    for raw_name, binding in (bindings or {}).items():
        name = str(raw_name).strip().upper()
        if not name:
            continue

        if not isinstance(binding, Mapping):
            raise RenderError(
                f"The binding for {{{{{name}}}}} is not filled in correctly.",
                variable_name=name,
            )

        source = str(binding.get("source") or "").strip().lower()
        if source not in _RESOLVERS:
            raise RenderError(
                f"{{{{{name}}}}} has no value source chosen.", variable_name=name
            )
        if source not in available:
            # The refusal that makes a per-canvas binding honest. See the module docstring.
            raise RenderError(
                f"{{{{{name}}}}} is bound to '{source}', which is not available here. "
                "Choose a different source.",
                variable_name=name,
            )

        found = _RESOLVERS[source](binding, context, name)
        if found is MISSING:
            continue
        resolved[name] = _stringify(found, name=name)

    return resolved


def assert_bindable(
    bindings: Optional[Mapping[str, Any]],
    *,
    declared: Any,
    available: frozenset,
) -> None:
    """
    Check a set of bindings against a template's declaration, without running anything.

    Called at **save** time by every canvas's node validator and by the trigger service, so
    a graph can never execute a binding looser than one that could be saved — the rule
    ``graph_service.validate_graph`` states, applied here.

    Three things are refused: a binding for a variable the template does not declare (the
    template was edited underneath it), a *required* declared variable with no binding, and a
    source this canvas cannot serve. A declared optional variable with no binding is fine —
    its default fills in.
    """
    from app.services.email_dispatch.rendering import declared_names, required_names

    known = set(declared_names(declared))
    bound = {str(name).strip().upper() for name in (bindings or {})}

    stray = sorted(bound - known)
    if stray:
        listed = ", ".join(f"{{{{{name}}}}}" for name in stray)
        raise RenderError(
            f"{listed} is bound here but the template no longer declares it. "
            "Remove the binding, or add the variable back to the template."
        )

    missing = [name for name in required_names(declared) if name not in bound]
    if missing:
        listed = ", ".join(f"{{{{{name}}}}}" for name in missing)
        raise RenderError(
            f"{listed} is required by this template and has nothing bound to it."
        )

    for raw_name, binding in (bindings or {}).items():
        if not isinstance(binding, Mapping):
            raise RenderError(
                f"The binding for {{{{{str(raw_name).upper()}}}}} is not filled in "
                "correctly."
            )
        source = str(binding.get("source") or "").strip().lower()
        if source not in BINDING_SOURCE_VALUES:
            raise RenderError(
                f"{{{{{str(raw_name).upper()}}}}} has no value source chosen."
            )
        if source not in available:
            raise RenderError(
                f"{{{{{str(raw_name).upper()}}}}} is bound to '{source}', which is not "
                "available here. Choose a different source."
            )
