"""
Reading a value out of a nested response, with a deliberately small grammar.

Four forms and nothing else::

    customer.email            a key, then a key
    line_items[0].sku         an index
    line_items[*].sku         every item, as a list
    ["@odata.nextLink"]       a key that contains a dot

The fourth exists because SAP sends ``@odata.nextLink`` as a literal key, dot included.
Without a quoted form that path means "the ``nextLink`` inside ``@odata``" and reads
nothing — silently, as a paged read that stops after page one.

**No filters, no expressions, no recursive descent, no functions.** The same posture
``node_runners._condition_holds`` and ``engine/transform.py`` take, for the same reason:
these strings are authored by a user in a form and, in Phase 1, sometimes by a language
model. Full JSONPath implementations differ from one another in what they accept, and
several of them *evaluate expressions* — which on user-authored input is a remote code
execution with a Save button.

What the narrowness costs is real and worth naming, because somebody will ask for each
of these:

===========================  ==========================================================
Asked for                    Why it is not here
===========================  ==========================================================
``items[?(@.qty > 1)]``      A predicate. Filtering is a ``filter`` node, which the
                             author can see on the canvas and the log can record.
``$..email``                 Recursive descent. "Every email anywhere in this document"
                             silently changes meaning when the vendor adds a field.
``items[-1]``                A negative index. Cheap to add and quietly wrong on an
                             empty list; ``last`` in the transform table is the
                             explicit version.
``concat(a, b)``             A function. Two mappings and a transform say the same
                             thing where the log can see both halves. Refused by name
                             rather than read as a field called ``concat(a``, because a
                             mapping that quietly matches nothing is worse than one that
                             will not save.
===========================  ==========================================================

**A missing path yields ``None``, and that is not the same as an error.** A record
without a shipping address is a fact about the record; required-ness is decided
afterwards by ``record_validation``. Conflating them would report every optional field
as a failure.

**A malformed path raises, and does so at save time.** ``validate_flow`` calls
:func:`parse` on every mapping, so ``customer..email`` is refused while the author is
looking at the canvas rather than at three in the morning.
"""

import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple, Union

#: A single segment: a key, an index, or the wildcard.
KEY = "key"
INDEX = "index"
WILDCARD = "wildcard"


@dataclass(frozen=True)
class Segment:
    kind: str
    value: Union[str, int, None] = None


# A key is anything but a dot or a bracket. Deliberately permissive about what a *key*
# may contain — vendors use dashes, colons and ``@`` — and strict about the punctuation
# that structures the path.
_SEGMENT = re.compile(r"[^.\[\]]+")

# Characters that never appear in a real JSON key and always mean somebody has written
# an expression: a function call, a JSONPath root, a predicate. Refused so the mapping
# fails at save time rather than matching nothing at run time.
_EXPRESSION_CHARS = frozenset("()$?")


class PathError(ValueError):
    """
    A path that cannot be read as one.

    A ``ValueError`` subclass so that every caller already catching malformed user input
    catches this too, and a distinct type so that ``validate_flow`` can say "this field
    path is not a field path" rather than reporting it alongside a type mismatch.
    """


def parse(path: str) -> Tuple[Segment, ...]:
    """
    Split a path into segments, refusing anything outside the grammar.

    Called by the validator at save time and by the reader at run time. One parser, so a
    path the canvas accepted cannot be one the runner chokes on.
    """
    text = str(path or "").strip()
    if not text:
        raise PathError("A field path cannot be empty.")

    segments: List[Segment] = []
    index = 0
    expecting_key = True

    while index < len(text):
        char = text[index]

        if char == ".":
            if expecting_key:
                raise PathError(
                    f"'{path}' has an empty step in it. Write 'customer.email', not "
                    "'customer..email'."
                )
            expecting_key = True
            index += 1
            continue

        if char == "[":
            close = text.find("]", index)
            if close == -1:
                raise PathError(f"'{path}' opens a bracket that is never closed.")

            inner = text[index + 1 : close].strip()
            quoted = _quoted(inner)

            if inner == "*":
                segments.append(Segment(WILDCARD))
            elif inner.isdigit():
                segments.append(Segment(INDEX, int(inner)))
            elif quoted is not None:
                # A key that contains a dot, or any other character the bare form
                # structures on. See the module docstring.
                segments.append(Segment(KEY, quoted))
            else:
                raise PathError(
                    f"'{inner or 'nothing'}' is not something that can go in brackets. "
                    "Use a number for one item, '*' for all of them, or a quoted name "
                    "for a field whose name contains a dot — there are no conditions "
                    "here; use a Filter step for that."
                )

            expecting_key = False
            index = close + 1
            continue

        match = _SEGMENT.match(text, index)
        if not match:
            raise PathError(f"'{path}' could not be read as a field path.")

        key = match.group()
        offending = _EXPRESSION_CHARS & set(key)
        if offending:
            raise PathError(
                f"'{path}' looks like an expression rather than a field path — it "
                f"contains {', '.join(sorted(offending))}. This reads fields only: "
                "'customer.email', 'line_items[0].sku', 'line_items[*].sku'. To pick "
                "records out, use a Filter step; to combine fields, use two mappings "
                "and a transform."
            )

        segments.append(Segment(KEY, key))
        expecting_key = False
        index = match.end()

    if expecting_key:
        raise PathError(f"'{path}' ends with a '.' and nothing after it.")

    return tuple(segments)


def _quoted(inner: str) -> Optional[str]:
    """The text inside ``["..."]`` or ``['...']``, or ``None`` when it is not quoted."""
    for quote in ('"', "'"):
        if len(inner) >= 2 and inner.startswith(quote) and inner.endswith(quote):
            return inner[1:-1]
    return None


def is_valid(path: str) -> bool:
    """Whether :func:`parse` would accept this. For a validator that wants a sentence
    of its own rather than this module's."""
    try:
        parse(path)
    except PathError:
        return False
    return True


def read(value: Any, path: str) -> Any:
    """
    The value at ``path``, or ``None`` if it is not there.

    A wildcard anywhere makes the result a list — ``line_items[*].sku`` over three items
    gives three skus. A wildcard over something that is not a list gives ``[]`` rather
    than raising: a vendor that returns ``null`` for an empty collection is common
    enough that treating it as a fault would fail records that are fine.
    """
    return read_parsed(value, parse(path))


def read_parsed(value: Any, segments: Sequence[Segment]) -> Any:
    """
    :func:`read` with the parsing already done.

    Worth having separately: a mapping applied to fifty thousand records parses its
    paths once and reads them fifty thousand times.
    """
    current: Any = value

    for position, segment in enumerate(segments):
        if segment.kind == WILDCARD:
            if not isinstance(current, (list, tuple)):
                return []
            rest = segments[position + 1 :]
            return [read_parsed(item, rest) for item in current]

        if segment.kind == INDEX:
            if not isinstance(current, (list, tuple)) or segment.value >= len(current):
                return None
            current = current[segment.value]
            continue

        if isinstance(current, Mapping):
            current = current.get(segment.value)
        else:
            # Not a mapping, so there is nothing here by this name. `None` rather than a
            # refusal: a field that is sometimes an object and sometimes a scalar is an
            # ordinary shape in a third-party API, and the record is not at fault.
            return None

        if current is None:
            return None

    return current


def read_records(payload: Any, records_path: str = "") -> List[Any]:
    """
    The list of records in a response body.

    An empty ``records_path`` means the body *is* the list, which is what an API
    returning a bare array does.

    **A path that resolves to something other than a list raises**, and that is the
    difference between this and :func:`read`. Reading one field off a record can
    legitimately find nothing; being handed a single object where a page of records was
    promised means the path is wrong, and treating it as "one record" would sync one row
    per page and report success.
    """
    found = read(payload, records_path) if records_path else payload

    if found is None:
        # A page with no records is a real page — it is how most APIs say "that is all".
        return []

    if isinstance(found, (list, tuple)):
        return list(found)

    where = f"'{records_path}'" if records_path else "the response"
    raise PathError(
        f"{where} does not hold a list of records — it holds "
        f"{_describe(found)}. Check where the records are in this API's response."
    )


def _describe(value: Any) -> str:
    if isinstance(value, Mapping):
        keys = ", ".join(list(value)[:5])
        return f"a single object (with {keys})" if keys else "a single object"
    return f"a {type(value).__name__}"


def first_present(value: Any, paths: Sequence[str]) -> Optional[Any]:
    """
    The first of several paths that yields something.

    For a natural key that may be an ``id`` or an ``external_id`` depending on which
    endpoint returned the record. Stops at the first non-``None``, so the order the
    caller gives is a preference rather than a fallback chain to be read backwards.
    """
    for path in paths:
        found = read(value, path)
        if found is not None:
            return found
    return None
