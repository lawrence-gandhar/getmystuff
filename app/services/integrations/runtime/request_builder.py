"""
Turning an operation and some values into one request — as a pure function.

``build_request`` does no HTTP, touches no database and never sees a credential. That is
the payoff the whole "operations are data" decision was for: every URL-escaping and
injection question in this module lives in this one file, and a table-driven unit test
can exhaust it.

**The body is assembled and serialised, never templated as a string.** The chatbot's
actions template JSON because a language model supplies strings and there is nothing else
to do — hence ``chatbot_action_service``'s two escaping modes. Here the values arrive
already typed, from a field mapping, so producing invalid JSON is not a failure mode that
has to exist. ``{"qty": "{quantity}"}`` with a quantity of 3 produces ``{"qty": 3}``, and
there is no way to write a template that breaks the document.

**Path parameters are escaped with ``quote(safe="")``.** A path parameter is the shortest
route from a record's contents to a request for a different URL: an order id of
``../../admin/users`` in an unescaped path is a request to somewhere nobody drew. The
same call ``chatbot_action_service._render(mode="url")`` already makes.

**Header names and values are refused if they contain CR or LF.** Response splitting,
and it is worth checking here rather than trusting the HTTP library, because the value
comes from a record and the record comes from somebody else's system.
"""

import json
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote, urlsplit

from app.services.integrations.connectors.spec import OperationSpec, PreparedRequest
from app.utils import type_coercion

# A `{name}` in a template. Deliberately not a general expression: the only thing that
# may appear between the braces is the name of a declared input.
_PLACEHOLDER_START = "{"
_PLACEHOLDER_END = "}"

_CRLF = ("\r", "\n")


def build_request(
    operation: OperationSpec,
    arguments: Optional[Mapping[str, Any]] = None,
    *,
    base_url: str,
    extra_query: Optional[Mapping[str, Any]] = None,
    extra_headers: Optional[Mapping[str, str]] = None,
) -> PreparedRequest:
    """
    One request, from an operation and the values a node supplied.

    ``extra_query`` is how pagination adds its cursor without the operation having to
    know it is being paged, and ``extra_headers`` is how an idempotency key is attached.
    Both are applied *after* the template, so a page parameter cannot be silently
    overridden by a stale one in the operation's own query.

    Raises ``ValueError`` with a sentence naming the field. The node runner turns it into
    a ``NodeFailure`` — this function knows nothing about nodes, which is what keeps it
    testable without one.
    """
    values = _typed_arguments(operation, arguments or {})

    # Every name the operation says it takes. A placeholder naming one of these with no
    # value supplied is an optional field that was not sent; a placeholder naming
    # anything else is a typo in the operation, and the two must not be confused —
    # dropping the second silently is how `{sinse}` becomes a sync with no date filter.
    declared = frozenset(spec.name for spec in operation.inputs)

    path = _render_path(operation, values, declared)
    url = _join(base_url, path)

    query = _render_query(operation, values, declared)
    query.update(extra_query or {})

    headers = _render_headers(operation, values, declared)
    headers.update(_checked_headers(extra_headers or {}))

    body = _render_body(operation, values, declared)

    host = urlsplit(url).hostname or ""

    return PreparedRequest(
        method=operation.method.upper(),
        url=url,
        headers=headers,
        params=query,
        json_body=body,
        host=host,
        path=path,
    )


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def _typed_arguments(
    operation: OperationSpec, arguments: Mapping[str, Any]
) -> Dict[str, Any]:
    """
    Every declared input, coerced to its declared type, with the undeclared ones dropped.

    Dropped rather than passed through: an argument the operation does not declare is
    either a mapping mistake or a stale node, and forwarding it would send a field the
    author did not write to a system they do not control.

    A required input that is absent raises here rather than at the far end, because "400
    Bad Request" from a vendor is a worse sentence than the one this can compose.
    """
    values: Dict[str, Any] = {}

    for spec in operation.inputs:
        raw = arguments.get(spec.name)

        if raw is None:
            if spec.required:
                raise ValueError(
                    f"'{operation.label or operation.operation_id}' needs a value for "
                    f"'{spec.display_label()}' and nothing supplied one."
                )
            continue

        try:
            values[spec.name] = type_coercion.coerce_value(raw, spec.type)
        except ValueError as exc:
            raise ValueError(
                f"'{spec.display_label()}' {exc}"
                if str(exc).startswith(("expected", "must"))
                else f"'{spec.display_label()}': {exc}"
            ) from exc

    return values


# ---------------------------------------------------------------------------
# Path
# ---------------------------------------------------------------------------


def _render_path(
    operation: OperationSpec, values: Mapping[str, Any], declared: frozenset
) -> str:
    """
    The path with its placeholders filled and escaped.

    ``quote(safe="")`` escapes ``/`` as well, which is the point: a value containing a
    slash must become one path segment, not several. See the module docstring.
    """
    def _escape(name: str, value: Any) -> str:
        return quote(_as_text(value), safe="")

    # A path placeholder is the one case where "declared but absent" is still fatal:
    # a URL with a hole in it is not a URL. `_substitute` refuses any name it cannot
    # fill, which is the behaviour a path needs and a query does not.
    return _substitute(operation.path, values, declared, _escape, where="path")


def _join(base_url: str, path: str) -> str:
    """
    Base plus path, with exactly one slash between them.

    The base URL's own path is kept — ``https://x.example.com/api/v2`` plus ``/orders``
    is ``/api/v2/orders``, not ``/orders``. A vendor whose API lives under a prefix is
    the ordinary case, and ``urljoin`` would discard it.
    """
    return f"{str(base_url).rstrip('/')}{path}"


# ---------------------------------------------------------------------------
# Query and headers
# ---------------------------------------------------------------------------


def _render_query(
    operation: OperationSpec, values: Mapping[str, Any], declared: frozenset
) -> Dict[str, Any]:
    """
    The query template, filled.

    Values are **not** URL-escaped here: they are handed to the HTTP library as a
    parameter mapping, and it encodes them. Escaping first would double-encode, which
    turns ``a b`` into ``a%2520b`` and produces a filter that matches nothing — the kind
    of bug that reads as "there were no results".

    A parameter whose value is absent is dropped rather than sent empty. ``?since=`` and
    no ``since`` at all mean different things to several APIs in scope, and the one we
    can be sure of is the one we did not send.
    """
    rendered: Dict[str, Any] = {}

    for name, template in (operation.query_template or {}).items():
        value = _fill(template, values, declared)
        if value is None:
            continue
        rendered[str(name)] = value

    return rendered


def _render_headers(
    operation: OperationSpec, values: Mapping[str, Any], declared: frozenset
) -> Dict[str, str]:
    rendered: Dict[str, str] = {}

    for name, template in (operation.header_template or {}).items():
        value = _fill(template, values, declared)
        if value is None:
            continue
        rendered[str(name)] = _as_text(value)

    return _checked_headers(rendered)


def _checked_headers(headers: Mapping[str, Any]) -> Dict[str, str]:
    """
    Refuse a header carrying a line break, in the name or the value.

    Response splitting. Checked here rather than trusted to the HTTP library, because the
    value can come from a record and the record comes from somebody else's system.
    """
    checked: Dict[str, str] = {}

    for name, value in headers.items():
        text = _as_text(value)
        if any(char in str(name) or char in text for char in _CRLF):
            raise ValueError(
                f"The '{name}' header contains a line break, which is not something a "
                "header may carry. Check the value being mapped into it."
            )
        checked[str(name)] = text

    return checked


# ---------------------------------------------------------------------------
# Body
# ---------------------------------------------------------------------------


def _render_body(
    operation: OperationSpec, values: Mapping[str, Any], declared: frozenset
) -> Optional[Any]:
    """
    The body, built as a structure.

    A leaf that is *exactly* a placeholder becomes the typed value — a number stays a
    number, a boolean stays a boolean, a nested object stays an object. A leaf with a
    placeholder among other text becomes a string, which is the only reading that makes
    sense for ``"Order {order_id} from the shop"``.

    A top-level key named in ``body_literals`` is copied across untouched, braces and all.
    That is how a GraphQL document survives: it is full of ``{``, and every one of them
    would otherwise be read as the start of an input name. The exemption is per key rather
    than per operation so that the sibling keys — ``variables``, where the cursor and the
    page size arrive — still substitute normally.

    Nothing here concatenates JSON. See the module docstring.
    """
    if operation.body_template is None:
        return None

    literals = frozenset(operation.body_literals or ())
    if not literals:
        return _fill_structure(operation.body_template, values, declared)

    filled: dict = {}
    for key, item in operation.body_template.items():
        if key in literals:
            filled[str(key)] = item
            continue

        value = _fill_structure(item, values, declared)
        if value is _ABSENT:
            continue
        filled[str(key)] = value

    return filled


def _fill_structure(template: Any, values: Mapping[str, Any], declared: frozenset) -> Any:
    if isinstance(template, Mapping):
        filled = {}
        for key, item in template.items():
            value = _fill_structure(item, values, declared)
            # A key whose value resolved to nothing is omitted rather than sent as null.
            # Several APIs in scope treat an explicit null as "clear this field", which
            # is a destructive reading of a field that was simply not mapped.
            if value is _ABSENT:
                continue
            filled[str(key)] = value
        return filled

    if isinstance(template, (list, tuple)):
        return [
            item
            for item in (_fill_structure(entry, values, declared) for entry in template)
            if item is not _ABSENT
        ]

    if isinstance(template, str):
        return _fill(template, values, declared, absent=_ABSENT)

    return template


class _Absent:
    """A leaf whose placeholder had no value. Distinct from ``None``, which is a value
    somebody deliberately mapped."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<absent>"


_ABSENT = _Absent()


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------


def _fill(
    template: Any,
    values: Mapping[str, Any],
    declared: frozenset,
    absent: Any = None,
) -> Any:
    """
    One template value.

    Whole-placeholder templates keep their type; mixed ones become text. Non-strings pass
    through untouched — a template of ``{"limit": 250}`` is a literal, and coercing it to
    ``"250"`` would send a string where the API documented a number.

    **A name the operation never declared raises; a declared name with no value is
    absent.** The distinction matters: the second is an optional field somebody did not
    map, and the first is a typo in the operation. Treating them alike would let
    ``{sinse}`` become a sync with no date filter that reads every record every time and
    reports success.
    """
    if not isinstance(template, str):
        return template

    name = _sole_placeholder(template)
    if name is not None:
        _require_declared(name, declared, where="value")
        return values.get(name, absent) if name in values else absent

    if _PLACEHOLDER_START not in template:
        return template

    return _substitute(
        template, values, declared, lambda _n, v: _as_text(v), where="value"
    )


def _require_declared(name: str, declared: frozenset, *, where: str) -> None:
    if name not in declared:
        raise ValueError(
            f"This operation's {where} refers to '{name}', which is not one of its "
            f"inputs. Its inputs are: {', '.join(sorted(declared)) or 'none'}."
        )


def _sole_placeholder(template: str) -> Optional[str]:
    """The name, when the template is exactly ``{name}`` and nothing else."""
    text = template.strip()
    if (
        len(text) > 2
        and text.startswith(_PLACEHOLDER_START)
        and text.endswith(_PLACEHOLDER_END)
        and _PLACEHOLDER_START not in text[1:-1]
        and _PLACEHOLDER_END not in text[1:-1]
    ):
        return text[1:-1].strip()
    return None


def _substitute(
    template: str, values: Mapping[str, Any], declared: frozenset, render, *, where: str
) -> str:
    """
    Replace every ``{name}`` in a string, with ``render`` deciding the escaping.

    Hand-written rather than ``str.format`` on purpose. ``format`` would treat ``{0}``,
    ``{a.b}`` and ``{a!r}`` as instructions, and every one of those is a way to read
    something the author did not intend from an object they do not control.
    """
    out: List[str] = []
    index = 0

    while index < len(template):
        start = template.find(_PLACEHOLDER_START, index)
        if start == -1:
            out.append(template[index:])
            break

        out.append(template[index:start])
        end = template.find(_PLACEHOLDER_END, start)

        if end == -1:
            raise ValueError(
                f"A {where} in this operation opens a '{{' that is never closed: "
                f"'{template}'."
            )

        name = template[start + 1 : end].strip()
        _require_declared(name, declared, where=where)

        if name not in values:
            raise ValueError(
                f"This operation's {where} needs a value for '{name}' and nothing "
                "supplied one."
            )

        out.append(render(name, values[name]))
        index = end + 1

    return "".join(out)


def _as_text(value: Any) -> str:
    """
    A value as text, going through the shared coercion so a dict becomes JSON rather
    than a Python repr — ``str({"a": True})`` is ``{'a': True}``, which is not JSON and
    not what any API accepts.
    """
    return type_coercion.coerce_value(value, "string")


def serialise_body(body: Any) -> Optional[bytes]:
    """
    The body on the wire.

    Separate from building it so a caller can log or preview the structure and send the
    bytes, without the two being produced by different code and able to disagree.
    """
    if body is None:
        return None
    return json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
