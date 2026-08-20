"""
Checking a record against the fields a destination actually declares.

**The field list is the schema.** There is no ``jsonschema`` dependency here and there
will not be one. A connector operation already carries a tuple of :class:`FieldSpec` —
name, type, required, label — because the mapping panel needs it to draw a field picker
and the workflow generator needs it to know what a destination accepts. Adding a second
description of the same fields, in a different vocabulary, buys nothing and creates a
pair that can disagree; the day they disagree is the day a record passes validation and
is refused by the vendor with a message nobody can map back to a step.

**Two rules run, in this order, and the order is the point.**

1. *Required-present.* A field the destination insists on, with nothing in it.
2. *Coercion.* The value honours the type the field declared, via
   ``app/utils/type_coercion.py``.

Required first because a missing value is a different fact from a malformed one, and
running coercion first would report ``None`` for a required integer as a type error —
sending the operator to look at a transform when the actual problem is upstream, in
whatever was supposed to supply the field.

**Never coerce past a failed coercion.** ``"abc"`` for a number field is a refusal, not
``0``. That rule lives in ``type_coercion`` and this module simply does not work around
it. A record written into somebody's CRM with a silently-zeroed amount is a wrong record
with nothing in the log to find it by, which is strictly worse than a record that failed
loudly and sits in the dead-letter list with its payload intact.

**Every problem with a record is collected, not the first one.** A record with three bad
fields reports three. The alternative is an operator fixing one mapping, re-running fifty
thousand records, and discovering the second problem — three times, over three days.

**A refusal names the alternatives.** ``frame_ops._require_column``'s sentence shape
("This does not have a field called 'x' … It has: …"), mirrored rather than imported:
importing it would drag polars and a deep-agents exception into a module whose value is
having neither.
"""

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.services.integrations.connectors.spec import FieldSpec
from app.utils import type_coercion

#: How many field problems one record's summary sentence carries before it says "and n
#: more". The full list stays on the outcome; this only bounds the *sentence*, which
#: lands in a ``integration_run_records.message`` column and in a table cell.
MAX_PROBLEMS_IN_SENTENCE = 3


@dataclass(frozen=True)
class FieldProblem:
    """
    One thing wrong with one field of one record.

    Carries the field name separately from the message so the UI can render the two in
    different columns and the AI triage layer can group ten thousand failures by field —
    which is the question an operator actually has ("which mapping is broken?"), and it
    is unanswerable if the field name is only ever embedded in a sentence.
    """

    field: str
    message: str

    def sentence(self) -> str:
        return f"{self.field}: {self.message}"


@dataclass(frozen=True)
class RecordOutcome:
    """
    A record after validation: what it became, and what was wrong with it.

    ``record`` holds the **coerced** values for every field that coerced cleanly, even
    when the outcome is invalid. That is deliberate — the dead-letter row stores the
    payload so the record can be replayed after the mapping is fixed, and storing the
    half-converted form is more use than storing the raw one when the question is "how
    far did this get".
    """

    record: Dict[str, Any]
    problems: Tuple[FieldProblem, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def message(self) -> str:
        """
        One sentence for the log row, bounded in length.

        A record with forty bad fields would otherwise write a paragraph into every one
        of forty thousand log rows.
        """
        if not self.problems:
            return ""

        shown = [problem.sentence() for problem in self.problems[:MAX_PROBLEMS_IN_SENTENCE]]
        remaining = len(self.problems) - len(shown)
        if remaining > 0:
            shown.append(f"and {remaining} more")
        return "; ".join(shown)

    def fields_at_fault(self) -> Tuple[str, ...]:
        """The field names, de-duplicated, in the order they were reported."""
        return tuple(dict.fromkeys(problem.field for problem in self.problems))


@dataclass
class Partition:
    """
    A batch split the way the ``validate`` node's two ports split it.

    ``valid`` is a list of plain dicts because that is what goes downstream and nothing
    downstream cares how it got there. ``invalid`` keeps whole outcomes because the
    reason is the only useful thing about a record that did not make it, and the
    ``invalid`` port exists precisely so an author can route those somewhere.
    """

    valid: List[Dict[str, Any]] = dataclass_field(default_factory=list)
    invalid: List[RecordOutcome] = dataclass_field(default_factory=list)

    def counts(self) -> Dict[str, int]:
        """The delta ``run_node`` merges into ``state["counts"]``. See ``flow_state``."""
        return {"valid": len(self.valid), "invalid": len(self.invalid)}


# ---------------------------------------------------------------------------
# Validating one record
# ---------------------------------------------------------------------------


def validate_record(
    record: Mapping[str, Any],
    fields: Sequence[FieldSpec],
    *,
    keep_unknown: bool = True,
) -> RecordOutcome:
    """
    One record against one field list.

    ``keep_unknown`` decides what happens to a key the field list does not mention. The
    default keeps it, and that is the right default for the ``validate`` node, which sits
    in the middle of a flow and must not quietly discard fields a later step maps. A
    ``connector_write`` passes ``False``, because sending a vendor a key it never declared
    is at best ignored and at worst a 400 naming a field the author did not write.

    An empty ``fields`` list validates nothing and reports nothing. That is not a hole:
    the node that has no field list has nothing to check against, and inventing rules
    would mean guessing at somebody else's API.
    """
    declared = {spec.name: spec for spec in fields}
    problems: List[FieldProblem] = []

    result: Dict[str, Any] = {}
    if keep_unknown:
        result.update(dict(record))
    else:
        result.update(
            {name: record[name] for name in record if name in declared}
        )

    for name, spec in declared.items():
        present = name in record
        value = record.get(name)

        # Rule 1. An empty string is absent for this purpose: a form field somebody
        # cleared and an API that sends "" for "not set" are the same fact, and a
        # destination that requires a value does not want "" either.
        if spec.required and (not present or value is None or value == ""):
            problems.append(
                FieldProblem(
                    field=name,
                    message=(
                        f"{spec.display_label()} is required by this destination and "
                        "nothing was mapped into it"
                    ),
                )
            )
            continue

        if not present or value is None:
            # Optional and absent. Not an error, and deliberately not defaulted either —
            # a default is a mapping's decision, made in ``field_map``, where the author
            # can see it.
            continue

        # Rule 2.
        try:
            result[name] = type_coercion.coerce_value(value, spec.type)
        except ValueError as exc:
            # The raw value is *removed*, not left in place. With ``keep_unknown`` the
            # record was copied wholesale, so leaving it would put the uncoerced value —
            # the text "abc" in a numeric field — into a record the dead-letter page
            # renders and a replay resubmits. A field that would not coerce is a field
            # this record does not have.
            result.pop(name, None)
            problems.append(FieldProblem(field=name, message=str(exc)))

    return RecordOutcome(record=result, problems=tuple(problems))


def validate_records(
    records: Iterable[Mapping[str, Any]],
    fields: Sequence[FieldSpec],
    *,
    keep_unknown: bool = True,
) -> List[RecordOutcome]:
    """:func:`validate_record` over a batch, in order. Order is preserved because the
    record log records a batch index and a position, and a shuffled batch makes those
    two numbers point at the wrong row."""
    return [
        validate_record(record, fields, keep_unknown=keep_unknown) for record in records
    ]


def partition(
    records: Iterable[Mapping[str, Any]],
    fields: Sequence[FieldSpec],
    *,
    keep_unknown: bool = True,
) -> Partition:
    """
    The ``validate`` node's whole job: split a batch into two recordsets.

    Both halves are always returned, even when one is empty, because the node writes a
    handle for each port and a missing handle downstream is an error rather than an empty
    batch.
    """
    split = Partition()
    for outcome in validate_records(records, fields, keep_unknown=keep_unknown):
        if outcome.ok:
            split.valid.append(outcome.record)
        else:
            split.invalid.append(outcome)
    return split


# ---------------------------------------------------------------------------
# Checking a *mapping* against a field list — save time, not run time
# ---------------------------------------------------------------------------


def field_names(fields: Sequence[FieldSpec]) -> Tuple[str, ...]:
    return tuple(spec.name for spec in fields)


def find_field(fields: Sequence[FieldSpec], name: str) -> Optional[FieldSpec]:
    """
    The spec for a field, matched exactly and then case-insensitively.

    Two steps and no further. Exact first so a destination with both ``Email`` and
    ``email`` behaves predictably; case-insensitive second because a user typing into a
    form and a model writing JSON both get capitalisation wrong constantly and refusing
    over it helps nobody. **No fuzzy matching** — the reasoning is the same one that
    forbids it for connection names: ``customer_email`` silently resolving to
    ``customer_emails`` writes data into the wrong field and reports success.
    """
    for spec in fields:
        if spec.name == name:
            return spec

    lowered = str(name).strip().lower()
    for spec in fields:
        if spec.name.lower() == lowered:
            return spec
    return None


def require_field(fields: Sequence[FieldSpec], name: str, *, destination: str = "") -> FieldSpec:
    """
    :func:`find_field`, or a refusal that lists what the destination does accept.

    This is the check that catches the most damaging AI hallucination there is: a
    mapping into ``customer_email`` when the operation takes ``email``. Both are
    plausible names, the flow validates, the run goes green, and every customer is
    created without an address. The sentence names the real fields because the operator
    or the model retrying is one word away from being right.
    """
    found = find_field(fields, name)
    if found is not None:
        return found

    where = f"'{destination}'" if destination else "This destination"
    available = ", ".join(field_names(fields)) or "no fields at all"
    raise ValueError(
        f"{where} does not accept a field called '{name}'. It accepts: {available}."
    )


def unmapped_required(
    fields: Sequence[FieldSpec], mapped_targets: Iterable[str]
) -> Tuple[str, ...]:
    """
    Required destination fields that nothing fills in.

    **A warning while drawing, a refusal at publish.** Both matter and they are not the
    same rule. Refusing to *save* a half-built flow throws away work somebody is in the
    middle of, and refusing an AI draft that is ninety per cent right throws away the
    draft. Publishing it means it runs on a schedule at three in the morning and creates
    records the vendor rejects, at whatever the rate limit allows, with nobody watching —
    so publish is where the line goes, and without this check the mapping panel's red
    warning is decorative.
    """
    filled = {str(target).strip().lower() for target in mapped_targets}
    return tuple(
        spec.name
        for spec in fields
        if spec.required and spec.name.lower() not in filled
    )
