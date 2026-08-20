"""
One field of a source record becoming one field of a destination record.

A mapping is seven pieces of data and no code::

    {"source": "customer.email", "target": "email", "type": "string",
     "required": true, "transform": ["trim", "lower"]}
    {"target": "source_system", "const": "shopify"}

**Contradictions are refused at load, not resolved by precedence.** A mapping with both
a ``source`` and a ``const``, or a ``const`` and a ``transform``, does not get a
documented winner — it fails to load, naming both halves. Precedence rules are how you
get a canvas where two people disagree about what a node does and both have read the
docs; the rule that ``const`` beats ``source`` is invisible on screen, and the mapping
that silently ignores half of what its author wrote is a mapping nobody can debug.

The same argument makes a ``default`` with no ``source`` a refusal rather than a synonym
for ``const``. With nothing to read the value is always absent, so the default always
applies — it *is* a fixed value, entered in the wrong column, and the refusal says so.

**The order the pieces run in, and why each step is where it is.**

1. ``const``, or the value at ``source`` read by ``mapping/paths.py``.
2. ``transform`` — the chain, left to right, from ``engine/transform.py``'s fixed table.
3. ``default`` — applied only if the value is still ``None``.
4. ``type`` — coerced by ``app/utils/type_coercion.py``.
5. ``required`` — refused if the value is ``None`` after all of that.

Default comes *after* transforms because of one shape that happens constantly:
``line_items[*].sku`` then ``first`` over a record with no line items yields ``None``,
and that record wants the default. Defaulting first would hand ``first`` a literal and
produce something nobody asked for. Required comes last because a default satisfies it —
that is what a default is for.

A ``const`` skips steps 2 and 3 entirely, which is exactly why declaring either of them
alongside it is refused rather than ignored. It still goes through step 4: a ``const`` of
``"5"`` on an integer field is a mistake worth catching, and catching it at load means
catching it while the author is looking at the canvas.

**Nothing here guesses, and every problem is collected.** A record with three unmappable
fields reports three, in one :class:`RecordOutcome`, with the field names separate from
the sentences. The alternative is an operator fixing one mapping, re-running fifty
thousand records, and meeting the second problem tomorrow.
"""

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from app.services.integrations.connectors.spec import FieldSpec
from app.services.integrations.engine import transform as transform_table
from app.services.integrations.mapping import paths
from app.services.integrations.mapping.record_validation import FieldProblem, RecordOutcome
from app.utils import type_coercion

# Everything that is not a letter or a digit, for name matching. See :func:`match_by_name`.
_NOT_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class FieldMapping:
    """
    One row of the mapping grid.

    Frozen because a mapping is read once per record across a fifty-thousand record run
    and mutated never. ``transform`` is a tuple for the same reason a frozen dataclass is
    hashable: the compiled form of a node's mapping list is cached per run, and a list
    would make that cache a place where one batch can change what the next batch does.

    A ``const`` of ``None`` means "no const" — there is no way to express "put null in
    this field", and there does not need to be, because a field nothing is mapped into is
    already absent.
    """

    target: str
    source: str = ""
    const: Any = None
    type: str = "string"
    required: bool = False
    default: Any = None
    transform: Tuple[str, ...] = ()

    #: Set by :meth:`validated`. The parsed source path, so a run reads it fifty
    #: thousand times without re-parsing it fifty thousand times.
    segments: Tuple[paths.Segment, ...] = ()

    @property
    def has_const(self) -> bool:
        return self.const is not None

    def validated(self) -> "FieldMapping":
        """
        Every refusal this shape can make, made once, at load.

        Returns a copy carrying the parsed source path. Called by ``load_mapping`` and by
        ``validate_flow`` at save time, so a mapping the canvas accepted cannot be one the
        runner chokes on halfway through a batch.
        """
        target = str(self.target or "").strip()
        if not target:
            raise ValueError(
                "does not say which field it fills in — every mapping needs a "
                "destination."
            )

        source = str(self.source or "").strip()
        self._refuse_contradictions(target, source)

        if self.type not in type_coercion.TYPES:
            raise ValueError(
                f"The mapping for '{target}' declares a '{self.type}', which is not a "
                f"kind of value. Available: {', '.join(type_coercion.TYPES)}."
            )

        for name in self.transform:
            if not transform_table.is_known(str(name)):
                raise ValueError(
                    f"The mapping for '{target}' uses a transform called '{name}', "
                    "which does not exist. Available: "
                    f"{', '.join(transform_table.TRANSFORM_NAMES)}."
                )

        # A malformed path is refused here, while the author is looking at the canvas,
        # rather than at three in the morning. `paths.PathError` is a ValueError, so a
        # caller catching this function's refusals catches it without a second except.
        segments = paths.parse(source) if source else ()

        # The const is coerced now as well as per-record. Same rule, two moments: this
        # one catches `{"const": "5", "type": "integer"}` at save time, which is the only
        # moment anybody is in a position to fix it.
        if self.has_const:
            type_coercion.coerce_value(self.const, self.type)

        return FieldMapping(
            target=target,
            source=source,
            const=self.const,
            type=self.type,
            required=bool(self.required),
            default=self.default,
            transform=tuple(str(name) for name in self.transform),
            segments=segments,
        )

    def _refuse_contradictions(self, target: str, source: str) -> None:
        """
        The four ways a mapping can say two things at once, each refused by name.

        Separate from :meth:`validated` because these are one rule stated four times —
        *no piece of a mapping may be silently ignored* — and reading them together is
        how somebody adding a fifth column notices they owe it an entry here.
        """
        if not source and not self.has_const:
            # A `default` with no source is not a third way of filling a field: with
            # nothing to read the value is always absent, so the default always applies,
            # which is a fixed value written in the wrong column. Refused rather than
            # quietly treated as one, because a grid where two columns mean the same
            # thing depending on whether a third is blank is a grid nobody can read.
            hint = (
                " A default with nothing to fall back from is a fixed value — put it in "
                "the fixed value column."
                if self.default is not None
                else ""
            )
            raise ValueError(
                f"The mapping for '{target}' has neither a source field nor a fixed "
                f"value, so there is nothing to put in it.{hint}"
            )

        if source and self.has_const:
            raise ValueError(
                f"The mapping for '{target}' has both a source field ('{source}') and a "
                f"fixed value ({self.const!r}). Pick one — a mapping that quietly "
                "ignores half of what it was given is one nobody can debug."
            )

        if self.has_const and self.transform:
            raise ValueError(
                f"The mapping for '{target}' applies {', '.join(self.transform)} to a "
                "fixed value. Write the value you want instead; a transform on a "
                "constant is a transform that runs fifty thousand times to produce the "
                "same answer."
            )

        if self.has_const and self.default is not None:
            raise ValueError(
                f"The mapping for '{target}' has both a fixed value and a default. A "
                "fixed value is never absent, so the default could never apply."
            )

    def read(self, record: Any) -> Any:
        """
        This mapping's value for one record. Raises ``ValueError`` naming the reason.

        The steps in the order the module docstring gives them. The refusal is thrown
        rather than returned because every one of them means the same thing to the
        caller — this record cannot be mapped — and :func:`apply_mappings` is the one
        place that decides what to do about it.
        """
        if self.has_const:
            return type_coercion.coerce_value(self.const, self.type)

        value = paths.read_parsed(record, self.segments)
        value = transform_table.apply_all(self.transform, value)

        if value is None and self.default is not None:
            value = self.default

        value = type_coercion.coerce_value(value, self.type)

        if self.required and value is None:
            raise ValueError(
                f"nothing was found at '{self.source}' and this field is required"
            )

        return value


def load_mapping(raw: Mapping[str, Any]) -> FieldMapping:
    """
    One mapping out of the JSON a canvas or a language model wrote.

    ``transform`` accepts a bare string as well as a list, because "trim" is what a
    single-transform row looks like in every form this will ever be posted from and
    demanding ``["trim"]`` would be pedantry enforced with a 400.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("A field mapping has to be an object.")

    transforms = raw.get("transform")
    if transforms is None:
        transforms = ()
    elif isinstance(transforms, str):
        transforms = (transforms,)
    elif isinstance(transforms, (list, tuple)):
        transforms = tuple(transforms)
    else:
        raise ValueError(
            f"The transforms on '{raw.get('target')}' could not be read — expected a "
            "name or a list of names."
        )

    return FieldMapping(
        target=str(raw.get("target") or ""),
        source=str(raw.get("source") or ""),
        const=raw.get("const"),
        type=str(raw.get("type") or "string"),
        required=bool(raw.get("required")),
        default=raw.get("default"),
        transform=transforms,
    ).validated()


def load_mappings(raw: Any) -> Tuple[FieldMapping, ...]:
    """
    A whole mapping list, refusing a duplicate target.

    Two mappings into one field is not an edge case to resolve — one of them would win
    by list order, which is invisible on the canvas. The refusal names the field.
    """
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError("The field mappings could not be read — expected a list.")

    loaded: List[FieldMapping] = []
    seen: Dict[str, int] = {}

    for position, item in enumerate(raw, start=1):
        try:
            mapping = load_mapping(item)
        except ValueError as exc:
            raise ValueError(f"Field mapping {position}: {exc}") from exc

        if mapping.target in seen:
            raise ValueError(
                f"maps two things into '{mapping.target}' — mappings "
                f"{seen[mapping.target]} and {position}. One of them would silently "
                "win, so say which."
            )

        seen[mapping.target] = position
        loaded.append(mapping)

    return tuple(loaded)


def targets_of(mappings: Iterable[FieldMapping]) -> Tuple[str, ...]:
    """The destination fields a mapping list fills in — for the publish-time
    unmapped-required check in ``record_validation``."""
    return tuple(mapping.target for mapping in mappings)


def apply_mappings(
    mappings: Sequence[FieldMapping], record: Any
) -> RecordOutcome:
    """
    Every mapping against one record, collecting problems rather than stopping.

    Returns the same :class:`RecordOutcome` the ``validate`` node produces, so a
    transform failure and a validation failure land in ``integration_run_records`` in one
    shape and the dead-letter page has one renderer. A field that failed is **absent**
    from the result rather than present as ``None`` — a half-built record with holes in it
    is the thing that gets written to a CRM by mistake, and an absent key at least fails
    the destination's own required check.
    """
    built: Dict[str, Any] = {}
    problems: List[FieldProblem] = []

    for mapping in mappings:
        try:
            value = mapping.read(record)
        except ValueError as exc:
            problems.append(FieldProblem(field=mapping.target, message=str(exc)))
            continue

        if value is not None:
            built[mapping.target] = value

    return RecordOutcome(record=built, problems=tuple(problems))


def apply_to_batch(
    mappings: Sequence[FieldMapping], records: Iterable[Any]
) -> List[RecordOutcome]:
    """:func:`apply_mappings` over a batch, in order. Order is preserved because the
    record log stores a batch index and a position within it."""
    return [apply_mappings(mappings, record) for record in records]


# ---------------------------------------------------------------------------
# The "map matching names" button
# ---------------------------------------------------------------------------


def match_by_name(
    source_fields: Sequence[FieldSpec], target_fields: Sequence[FieldSpec]
) -> Tuple[FieldMapping, ...]:
    """
    Mappings for the fields whose names match once punctuation and case are ignored.

    ``customer_email``, ``customerEmail`` and ``Customer Email`` are the same field name
    written by three teams, and refusing to see that makes the operator retype forty rows.

    **Exact after normalisation is not fuzzy matching**, and the distinction is the whole
    safety argument. There is no edit distance here and no scoring: two names either
    reduce to the same string or they do not. ``email`` never matches ``emails``,
    ``billing_email`` never matches ``email``. A near-match that resolves silently writes
    somebody's data into the wrong field and reports success, which is the failure the
    AI field mapper in Phase 2 has to declare a confidence for and this one simply cannot
    have.

    The result is a **suggestion**, presented as "matched by name" and never as an AI
    suggestion — the user reviews it in the grid before anything is saved. Fields with no
    match are left for them, because a destination field this cannot fill in is one it has
    nothing honest to say about.
    """
    by_normalised: Dict[str, FieldSpec] = {}
    for spec in source_fields:
        # First writer wins, so a source list with two fields normalising the same way
        # picks the earlier one rather than depending on iteration luck.
        by_normalised.setdefault(_normalise(spec.name), spec)

    matched: List[FieldMapping] = []
    for target in target_fields:
        source = by_normalised.get(_normalise(target.name))
        if source is None:
            continue
        matched.append(
            FieldMapping(
                target=target.name,
                source=source.path or source.name,
                type=target.type,
                required=target.required,
            ).validated()
        )

    return tuple(matched)


def _normalise(name: str) -> str:
    return _NOT_ALPHANUMERIC.sub("", str(name).strip().lower())
