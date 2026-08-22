"""
``{{VARIABLE}}`` substitution for any node on the graph canvas.

A node may declare named variables bound to an earlier node's output, and use them in
its own text — a table name in a SQL statement, a figure in a Success message, a name
in the question a Human node asks. What a node produced has always been readable by
the node after it (``GraphState.outputs``); what was missing was a way to *write it
into* something.

**This is substitution, not evaluation, and that is the whole design.** There is no
expression language here, no filters, no conditionals, no ``eval`` behind a sandbox.
``{{NAME}}`` is replaced by a value that was resolved by a structured binding, and
anything else in the text is text. That rule is stated in three other places in this
codebase — ``email_dispatch/variable_sources.py``,
``email_dispatch/rendering.py`` and ``integrations/engine/transform.py`` — and this
module is the fourth. Anything that evaluates a string is a way to make this
application compute something nobody reviewed.

So the machinery is borrowed rather than rebuilt: ``rendering.render`` does the
substituting, ``variable_sources.resolve_bindings`` turns a binding into a value, and
``mapping.paths`` reads a dotted path. What is new here is only *which fields* on
*which node types* get substituted, and what a value is allowed to be once it lands in
a SQL statement.

Every refusal is an ``HTTPException(400)``. That is not laziness about error types — it
is what lets one function serve both callers. ``graph_service``'s validators already
speak it, and ``node_runners.run_node`` already has an ``except HTTPException`` branch
that writes the failed step row with the detail and re-raises it as a ``NodeFailure``.
Raising anything else would mean the run-time path lost the sentence to
``run_node``'s catch-all and reported "the reason has been logged" instead.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Set, Tuple

from litestar.exceptions import HTTPException

from app.models.email_dispatch import VARIABLE_NAME_PATTERN
from app.models.graph_designer import (
    MAX_NODE_VARIABLES,
    NODE_BRANCH,
    NODE_DO_UNTIL,
    NODE_EMAIL,
    NODE_FAILURE,
    NODE_FOR_EACH,
    NODE_HUMAN,
    NODE_SQL,
    NODE_SQL_UNION,
    NODE_START,
    NODE_SUCCESS,
    NODE_TIMER,
    NODE_TOOL_CONFIG,
    NODE_TYPE_VALUES,
    NODE_VALUE,
    NODE_WAIT,
)
from app.services.email_dispatch import rendering, variable_sources
from app.services.email_dispatch.errors import RenderError
from app.utils import sql_guard

#: Where a node's own variable declarations live in its ``data``.
#:
#: Deliberately **not** ``variable_bindings``, which an Email node already uses. There
#: the key means "values for the variables somebody else's template declared", and
#: ``variable_sources.assert_bindable`` refuses any name the template does not declare —
#: so folding graph-level variables into it would make every one of them an instant 400.
#: Two keys also means no migration: every ``graph_data`` row already stored reads back
#: byte-identically, because this one is simply absent.
NODE_VARIABLES_KEY = "variables"

#: The sources a graph can serve. The same frozenset the Email node is held to — a graph
#: has no chat session, no record in hand and no agent whose prompt variables it could
#: read, so offering those would build a form the runner cannot honour.
GRAPH_BINDING_SOURCES = frozenset({"node", "literal"})

_NAME_RE = re.compile(VARIABLE_NAME_PATTERN)


# --------------------------------------------------------------------------
# How a substituted value is treated, per field
# --------------------------------------------------------------------------

#: Substituted verbatim. The field is prose and its consumer renders it as text.
RENDER_TEXT = "text"

#: Substituted into SQL, where the value must first prove it is a name or a number.
#: See ``_sql_values`` for why this is the strictest of the three.
RENDER_SQL = "sql"

#: Substituted into a JSON document, so the value is escaped as a JSON string body
#: first — otherwise a value containing a quote produces a document that will not parse.
RENDER_JSON = "json"


@dataclass(frozen=True)
class FieldSpec:
    """
    One field on one node type that ``{{VARIABLE}}`` reaches.

    ``label`` is what the property panel calls the field, and it travels to the browser
    so the panel can say "use {{NAME}} in: SQL statement, Tables" without a second copy
    of this table in JavaScript.
    """

    key: str
    kind: str
    label: str
    is_list: bool = False


#: Which fields take variables, per node type. **Every** node type appears, including
#: the ones that take none — see the assertion below.
#:
#: What is deliberately absent from every entry:
#:
#: * any ``*_id`` — a picker's uuid, not prose. Substituting into one would let a run
#:   reach a row the save never checked.
#: * ``source_node`` / ``collect_from`` / ``timer_node`` — node ids the validator
#:   resolves against the drawing, before any state exists to substitute from.
#: * ``item_name`` / ``label_item_as`` — identifiers the loop machinery reads at compile
#:   time, which is also before any state exists.
#: * ``params`` and ``bindings`` — the typed parameter system. It is safer than string
#:   interpolation and this feature must not give anyone a reason to stop using it.
#: * ``recipients``, ``subject``, the email bodies — owned by the email renderer, which
#:   substitutes the *template's* declared variables into them. A pass here would eat
#:   ``{{CUSTOMER}}`` before that renderer ever saw it.
#:
#: A ``branch``'s and a ``do_until``'s condition values are absent for a sharper reason.
#: ``node_runners.branch_port`` and ``loop_continues`` are each called **twice** — once
#: by the runner and once by the compiler's router, after the node's own output has been
#: merged — and their docstrings say they are one function precisely so "the port
#: recorded in the log and the port the run actually takes cannot differ". A rendered
#: condition could resolve differently between those two calls, and the log would then
#: disagree with the route. A condition already has a typed way to read another node.
VARIABLE_FIELDS: Dict[str, Tuple[FieldSpec, ...]] = {
    NODE_START: (),
    NODE_SQL: (
        FieldSpec("sql_query", RENDER_SQL, "SQL statement"),
        FieldSpec("table_names", RENDER_SQL, "Tables", is_list=True),
    ),
    NODE_SQL_UNION: (
        FieldSpec("sql_query", RENDER_SQL, "SQL statement"),
        FieldSpec("table_names", RENDER_SQL, "Tables", is_list=True),
    ),
    NODE_VALUE: (FieldSpec("value_json", RENDER_JSON, "Value"),),
    NODE_TOOL_CONFIG: (),
    NODE_HUMAN: (
        FieldSpec("prompt", RENDER_TEXT, "Question"),
        FieldSpec("choices", RENDER_TEXT, "Choices", is_list=True),
    ),
    NODE_BRANCH: (),
    NODE_FOR_EACH: (),
    NODE_DO_UNTIL: (),
    NODE_EMAIL: (),
    NODE_TIMER: (),
    NODE_WAIT: (),
    NODE_SUCCESS: (FieldSpec("message", RENDER_TEXT, "Message"),),
    NODE_FAILURE: (FieldSpec("message", RENDER_TEXT, "Message"),),
}

# A node type with no entry here would silently take no variables, and the panel would
# offer a Variables section the runner ignored. Asserted at import so the mistake stops
# the application rather than one graph — the same call `variable_sources` makes about
# its resolvers, and `engine/node_runners` about its runners.
assert set(VARIABLE_FIELDS) == set(NODE_TYPE_VALUES), (
    "VARIABLE_FIELDS and the node vocabulary disagree: "
    f"{set(VARIABLE_FIELDS) ^ set(NODE_TYPE_VALUES)}"
)

#: A value that may be substituted into a statement: a dotted name, up to three parts
#: (``orders``, ``sales.orders``, ``warehouse.sales.orders``), or a whole number.
#:
#: This is the fence, and it is deliberately narrow. It refuses a space, a quote, a
#: semicolon, a parenthesis, a hyphen, an empty string, and the ``…`` that
#: ``variable_sources._stringify`` appends when it truncates an over-long value.
_SQL_VALUE_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*){0,2}|-?\d{1,18})$"
)


# --------------------------------------------------------------------------
# Reading a node
# --------------------------------------------------------------------------

def fields_for(node_type: str) -> Tuple[FieldSpec, ...]:
    """Which fields this node type substitutes into. Empty for most of them."""
    return VARIABLE_FIELDS.get(str(node_type or ""), ())


def variables_of(data: Optional[Mapping[str, Any]]) -> Dict[str, dict]:
    """
    A node's declarations, normalised to ``{NAME: binding}`` with the names upper-cased.

    Upper-cased because ``rendering.render`` upper-cases whatever it matched, so
    ``{{table}}`` in the text looks for ``TABLE``. A declaration stored in another case
    would validate, bind, and then fail at run time as unknown.
    """
    raw = (data or {}).get(NODE_VARIABLES_KEY)

    if not isinstance(raw, Mapping):
        return {}

    declared: Dict[str, dict] = {}

    for name, binding in raw.items():
        key = str(name).strip().upper()

        if key and isinstance(binding, Mapping):
            declared[key] = dict(binding)

    return declared


def source_nodes(data: Optional[Mapping[str, Any]]) -> Set[str]:
    """
    Every node id this node's variables read from.

    Reads **both** maps — a node's own ``variables`` and an Email node's
    ``variable_bindings``. The second was missing from ``referenced_nodes`` before this
    function existed, which made an Email node's upstream invisible to a selection run:
    it passed the dependency check and then failed inside the resolver claiming the node
    had been "deleted, or skipped by a branch", which is the wrong diagnosis for
    "you did not tick that box".
    """
    found: Set[str] = set()
    data = data or {}

    for key in (NODE_VARIABLES_KEY, "variable_bindings"):
        raw = data.get(key)

        if not isinstance(raw, Mapping):
            continue

        for binding in raw.values():
            if not isinstance(binding, Mapping):
                continue

            if str(binding.get("source") or "").strip().lower() != "node":
                continue

            node_id = str(binding.get("node_id") or "").strip()

            if node_id:
                found.add(node_id)

    return found


def placeholders_used(node: Optional[Mapping[str, Any]]) -> Set[str]:
    """Every ``{{NAME}}`` appearing in this node's substitutable fields, upper-cased."""
    data = (node or {}).get("data") or {}
    used: Set[str] = set()

    for spec in fields_for(str((node or {}).get("type") or "")):
        for text in _field_texts(data, spec):
            used |= rendering.placeholders_in(text)

    return used


def _field_texts(data: Mapping[str, Any], spec: FieldSpec) -> Sequence[str]:
    """One field's text, or each entry of it when the field is a list."""
    raw = data.get(spec.key)

    if spec.is_list:
        return [str(entry) for entry in (raw or []) if isinstance(entry, (str, int, float))]

    return [str(raw)] if isinstance(raw, str) else []


# --------------------------------------------------------------------------
# Validation — offline, like every other validator on this canvas
# --------------------------------------------------------------------------

def assert_valid(
    node: Mapping[str, Any],
    label: str,
    node_by_id: Optional[Mapping[str, Any]] = None,
) -> None:
    """
    Refuse a node whose variables are wrong, before it is ever run.

    Synchronous and offline on purpose: ``validate_graph`` runs on save, on publish
    **and** on run, so a database read in here would slow all three and make the rules
    untestable without a session.

    What it cannot check is whether the *value* a binding will find is suitable — that
    needs a run. So the SQL rules come in two halves: where a placeholder may sit is
    settled here, and what its value may be is settled in ``_sql_values`` at run time.
    """
    data = node.get("data") or {}
    node_type = str(node.get("type") or "")
    specs = fields_for(node_type)
    declared = variables_of(data)

    _assert_declarations(data, declared, node_type, specs, label)

    for name, binding in declared.items():
        _assert_binding(name, binding, label, node_by_id)

    missing = sorted(placeholders_used(node) - set(declared))

    if missing:
        listed = ", ".join(f"{{{{{name}}}}}" for name in missing)
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' uses {listed} but nothing on it declares that. Add it "
                "under Variables, or remove it from the text."
            ),
        )

    # An unused declaration is deliberately allowed. The panel lets somebody add a row
    # before typing the name into the field, and refusing that would make the form
    # impossible to fill in in the order a person actually fills it in.

    for spec in specs:
        if spec.kind == RENDER_SQL:
            _assert_sql_placement(data, spec, label)


def _assert_declarations(
    data: Mapping[str, Any],
    declared: Mapping[str, Any],
    node_type: str,
    specs: Tuple[FieldSpec, ...],
    label: str,
) -> None:
    """That the ``variables`` map is readable, allowed on this type, and within the cap."""
    raw = data.get(NODE_VARIABLES_KEY)

    if raw is not None and not isinstance(raw, Mapping):
        raise HTTPException(
            status_code=400,
            detail=f"The variables on '{label}' could not be read.",
        )

    if declared and not specs:
        raise HTTPException(status_code=400, detail=_no_variables_here(node_type, label))

    if len(declared) > MAX_NODE_VARIABLES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' declares {len(declared)} variables, which is more than the "
                f"{MAX_NODE_VARIABLES} a node may have."
            ),
        )


def _no_variables_here(node_type: str, label: str) -> str:
    """Why this particular node type has no Variables section."""
    if node_type == NODE_EMAIL:
        return (
            f"'{label}' cannot declare its own variables — an Email node's variables "
            "come from the template it sends. Choose the template, then fill in the "
            "rows underneath it."
        )

    return (
        f"'{label}' has no fields that use variables, so it cannot declare any. "
        "Remove them."
    )


def _assert_binding(
    name: str,
    binding: Mapping[str, Any],
    label: str,
    node_by_id: Optional[Mapping[str, Any]],
) -> None:
    """One declaration: a legal name, a source a graph can serve, and a readable path."""
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' declares a variable called '{name}'. A variable name must "
                "start with a letter and use capitals, digits and underscores only."
            ),
        )

    source = str(binding.get("source") or "").strip().lower()

    if source not in GRAPH_BINDING_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{{{{{name}}}}} on '{label}' is bound to '{source}', which is not "
                "available in a graph. Use an earlier node's output or a fixed value."
            ),
        )

    if source == "node":
        node_id = str(binding.get("node_id") or "").strip()

        if not node_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{{{{{name}}}}} on '{label}' reads an earlier node's output but "
                    "no node was chosen."
                ),
            )

        # Checked here and deliberately not retro-applied to an Email node's
        # `variable_bindings`: graphs already saved may name a node that has since been
        # deleted, and refusing them on load would make them uneditable.
        if node_by_id is not None and node_id not in node_by_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{{{{{name}}}}} on '{label}' reads a node that is no longer in "
                    "this graph. Choose it again."
                ),
            )

    path = str(binding.get("path") or "").strip()

    if path:
        try:
            variable_sources.assert_path(path, name=name)
        except RenderError as exc:
            raise HTTPException(
                status_code=400, detail=f"On '{label}': {exc.message}",
            ) from exc

    if "default" in binding and not isinstance(
        binding.get("default"), (str, int, float, type(None))
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"The default for {{{{{name}}}}} on '{label}' must be a single value, "
                "not a list or an object."
            ),
        )


def _assert_sql_placement(data: Mapping[str, Any], spec: FieldSpec, label: str) -> None:
    """
    Refuse a ``{{VARIABLE}}`` sitting inside a quoted string or a comment.

    This is the rule that gives the feature a reason to exist *and* takes away its
    reason to be dangerous. A bind parameter — ``:region``, wired through ``bindings`` —
    already expresses every **value**, and the driver binds it so it cannot change what
    the statement does. The one thing a bind parameter cannot express is an
    **identifier**: no driver will let ``FROM :table`` name a table.

    So ``{{VAR}}`` is for identifiers, and a placeholder found inside quotes is somebody
    reaching for it to do a value's job — where it would be string-concatenated SQL, with
    the string coming from a database row nobody reviewed.
    """
    for text in _field_texts(data, spec):
        outside = rendering.placeholders_in(sql_guard.stripped_literals(text))
        inside = sorted(rendering.placeholders_in(text) - outside)

        if not inside:
            continue

        listed = ", ".join(f"{{{{{name}}}}}" for name in inside)
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' uses {listed} inside quotes or a comment in its "
                f"{spec.label.lower()}. A value that goes inside a string belongs in a "
                ":parameter with a binding — the driver binds those, so they cannot "
                "change what the statement does. Use a variable only where a table or "
                "column name goes."
            ),
        )


# --------------------------------------------------------------------------
# Rendering, at run time
# --------------------------------------------------------------------------

def render_node(node: Mapping[str, Any], state: Mapping[str, Any]) -> dict:
    """
    A copy of ``node`` with its ``{{VARIABLE}}``s filled in from what earlier nodes produced.

    Returns the node **unchanged** when nothing needs substituting, which is the common
    case and costs nothing.

    Otherwise it returns a *new* dict and never edits the one it was given. That is
    load-bearing rather than tidy: the compiler captures each node in a closure once per
    run, and a loop body re-enters the same closure on every pass. Writing the rendered
    text back would bake the first pass's values into the drawing, and every later pass
    would substitute into text that had already been substituted.
    """
    node_type = str(node.get("type") or "")
    specs = fields_for(node_type)

    if not specs:
        return dict(node)

    data = node.get("data") or {}
    used = placeholders_used(node)

    if not used:
        return dict(node)

    label = _label_of(node)
    values = _resolved(node, state, used, label)

    rendered = dict(data)

    for spec in specs:
        raw = data.get(spec.key)
        substituted = _values_for(spec.kind, values, label, spec)

        if spec.is_list:
            if not isinstance(raw, (list, tuple)):
                continue
            rendered[spec.key] = [
                _render_one(entry, substituted, spec, label)
                if isinstance(entry, str) else entry
                for entry in raw
            ]
            continue

        if isinstance(raw, str):
            rendered[spec.key] = _render_one(raw, substituted, spec, label)

    return {**node, "data": rendered}


def _label_of(node: Mapping[str, Any]) -> str:
    """This node's name, for a sentence. Not substituted — a label is not a field."""
    data = node.get("data") or {}

    return str(data.get("label") or "").strip() or str(node.get("type") or "node")


def _resolved(
    node: Mapping[str, Any],
    state: Mapping[str, Any],
    used: Set[str],
    label: str,
) -> Dict[str, str]:
    """
    A value for every variable the node's text actually uses.

    Only the used ones are resolved. A declaration nobody references is allowed to exist
    (see ``assert_valid``), and resolving it would mean a broken binding on an unused row
    could fail a node whose text never mentions it.
    """
    declared = variables_of(node.get("data") or {})
    wanted = {name: binding for name, binding in declared.items() if name in used}

    context = variable_sources.VariableContext(
        node_outputs=(state or {}).get("outputs") or {},
        # Explicitly unavailable, not merely empty. A graph has no chat session and no
        # agent, and the dataclass's defaults would otherwise report both as available
        # — which would turn "this canvas cannot do that" into "that variable was empty".
        agent_variables=None,  # type: ignore[arg-type]
        session_variables=None,  # type: ignore[arg-type]
    )

    # Resolved one at a time rather than in one `resolve_bindings` call, because a
    # variable's default has to be able to cover **every** way it can come up empty.
    # The batch call cannot do that: it omits a binding whose path found nothing, but
    # *raises* for one whose node never ran — and "the branch that fills this in was not
    # taken" is exactly the case an author reaches for a default to handle. Resolving per
    # variable is what makes "if it has no value, use this" mean what the panel says.
    return {
        name: _one_value(name, binding, context, label)
        for name, binding in wanted.items()
    }


def _one_value(
    name: str,
    binding: Mapping[str, Any],
    context: "variable_sources.VariableContext",
    label: str,
) -> str:
    """One variable's value: what the binding found, else its default, else a refusal."""
    has_default = "default" in binding
    fallback = str(binding.get("default") or "")

    try:
        found = variable_sources.resolve_bindings({name: binding}, context)
    except RenderError as exc:
        if has_default:
            return fallback

        raise HTTPException(
            status_code=400, detail=f"On '{label}': {exc.message}",
        ) from exc

    if name in found:
        return found[name]

    if has_default:
        return fallback

    raise HTTPException(
        status_code=400,
        detail=(
            f"'{label}' needs a value for {{{{{name}}}}}, and nothing produced one on "
            "this path. Give it a default, or point it at a field that has a value."
        ),
    )


def _values_for(
    kind: str, values: Mapping[str, str], label: str, spec: FieldSpec
) -> Dict[str, str]:
    """The value map as this kind of field needs it."""
    if kind == RENDER_SQL:
        return _sql_values(values, label, spec)

    if kind == RENDER_JSON:
        return _json_values(values)

    return dict(values)


def _sql_values(
    values: Mapping[str, str], label: str, spec: FieldSpec
) -> Dict[str, str]:
    """
    The values, each having proved it is a name or a whole number.

    The second of the four things standing between this feature and string-concatenated
    SQL. The first is ``_assert_sql_placement``, which keeps a placeholder out of quotes.
    The third is ``_run_sql``, which re-runs ``validated_tool_sql`` over the *substituted*
    statement, so a second statement or a write verb is still refused. The fourth is the
    ``table_names`` allow-list, which a substituted table name must still appear in.

    Applied to a ``literal`` binding as well as a ``node`` one. One rule is easier to
    reason about than two, and an author who needs something a name cannot express can
    type it into the statement, where a reviewer can see it.
    """
    checked: Dict[str, str] = {}

    for name, value in values.items():
        text = str(value)

        if not _SQL_VALUE_RE.match(text):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{label}' puts {{{{{name}}}}} into its {spec.label.lower()}, and "
                    f"it resolved to '{text[:60]}'. A variable used in SQL may only be "
                    "a name or a whole number."
                ),
            )

        checked[name] = text

    return checked


def _json_values(values: Mapping[str, str]) -> Dict[str, str]:
    """
    The values, escaped so each is safe *inside* a JSON string.

    ``json.dumps("a\\"b")`` is ``"a\\"b"``; trimming the quotes leaves the body, which is
    what belongs between the quotes already in the document. Done to the values rather
    than to the text so ``rendering.render`` needs no JSON mode — that module is the one
    whose docstring commits to being strict, and this is a graph concern.
    """
    return {name: json.dumps(str(value))[1:-1] for name, value in values.items()}


def _render_one(
    text: str, values: Mapping[str, str], spec: FieldSpec, label: str
) -> str:
    """Substitute one field, turning the renderer's refusal into this module's."""
    try:
        return rendering.render(text, values, field=spec.label)
    except RenderError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' uses {{{{{exc.variable_name or '?'}}}}} in its "
                f"{spec.label.lower()}, which has no value here."
            ),
        ) from exc
