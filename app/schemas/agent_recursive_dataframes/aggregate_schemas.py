"""
Schemas for Recursive DataFrame Agents — app/services/agent_recursive_dataframes/.

Three untrusted sources reach this feature and each gets a schema. The console
form is the ordinary one. The other two are less obvious and are the reason this
module exists at all:

* **the tool arguments a model supplies.** ``AggregateRecordsArgs`` is the agent
  tool's ``args_schema``, so a model asking for an aggregation is validated exactly
  as a browser posting a form is.
* **the plan a model produces.** ``AggregationPlan`` is the structured output of an
  LLM call, which makes it a *request* however it arrives — a model naming a column
  that does not exist is the expected case, not the exceptional one, and it is
  caught here and then again against the tool's real columns in
  ``aggregate_planner.validate_plan``.

The schema bounds the shape; the planner bounds the meaning. Neither is enough on
its own: pydantic can say "at most four group columns" but not "``regoin`` is not
a column this tool returns", and only one of those is the mistake a model actually
makes.
"""

import uuid as uuid_pkg
from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator

from app.schemas.base import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    AppBaseSchema,
    FormRequest,
    OptionalUUID,
    RequestSchema,
    ResponseSchema,
)
from app.services.agent_recursive_dataframes import filter_algebra, partial_algebra

#: The longest instruction accepted. The same order as a chat question — an
#: instruction is one sentence about what to group, not a document.
MAX_INSTRUCTION_LENGTH = 1000

#: How many columns may be grouped by at once. Past a handful the grouping stops
#: being a summary and starts being the table again, one row per record.
MAX_GROUP_BY_COLUMNS = 4

#: How many measures one plan may ask for.
MAX_PLAN_AGGREGATIONS = 8

#: How many filters one plan may carry. Filters are conjunctive, so past a handful the
#: plan is describing one specific record rather than narrowing a set — and a model
#: emitting twelve conditions has usually restated the same one several ways.
MAX_PLAN_FILTERS = 8


class PlannedAggregation(AppBaseSchema):
    """
    One measure: a function, the column it applies to, and what to call the result.

    ``column`` is optional for exactly one function. ``count`` without a column
    counts records; ``count`` with one counts the records that have a value there,
    and those are different questions with different answers whenever the column is
    nullable.

    ``alias`` is **assigned by the planner, never taken from the model**, and so it
    is optional here: a model that invents one would be choosing an output column
    name, which could collide with a group key and silently overwrite it.
    """

    type: str = Field(title="Function", max_length=32)
    column: str = Field(default="", title="Column", max_length=MAX_NAME_LENGTH)
    alias: str = Field(default="", title="Result name", max_length=MAX_NAME_LENGTH)

    @field_validator("type")
    @classmethod
    def _must_be_foldable(cls, value: str) -> str:
        """
        Refuse anything without an exact partial fold, in the model's own terms.

        Checked here as well as in the planner because this is where a shape
        arrives from an LLM, and the sooner ``median`` is refused the fewer places
        have to cope with it. The rule itself is not duplicated — it is asked of
        ``partial_algebra``, which is the only thing that knows.
        """
        refusal = partial_algebra.unsupported_function(value)

        if refusal:
            raise ValueError(refusal)

        return value.strip().lower()


class PlannedFilter(AppBaseSchema):
    """
    One condition a record must satisfy to be counted.

    ``part`` is the field that makes "in March" expressible. Left blank the filter
    compares the column itself; set to ``month``/``year``/``quarter``/``day`` it
    compares that part of a date column — which keeps month-boundary arithmetic out
    of a model's hands, for the reason ``filter_algebra`` states.

    ``value`` and ``values`` are two fields rather than one because a model handed
    one field for both will put a single-element list where a scalar belongs about as
    often as not, and then ``in: 5`` and ``==: [5]`` cannot be told from typos. Which
    of the two an operator wants is ``filter_algebra.needs_values``' answer, asked in
    one place so the schema and the planner cannot disagree.

    **Both are strings, and that is a constraint imposed from outside.** This class is an
    LLM's structured output, so its JSON schema is sent to the provider as
    ``response_format`` — and a field typed ``Any`` renders as an **empty** schema ``{}``,
    which a strict validator rejects outright:

        400 Unsupported JSON schema fields in schema with keys: dict_keys([])

    That is not a hypothetical. It was Cerebras refusing every planning call the moment
    filters existed, so the tool failed on each attempt and the agent reported that it
    could not filter — the same apology, one layer further in. A union of
    ``str | float | bool`` would render as ``anyOf``, which several strict validators also
    refuse, so one concrete type is the only shape that travels everywhere.

    A string is safe here because **the value is coerced against the column's real type**
    before it is compared — ``frame_ops._coerced`` — and refused with the column named when
    it cannot be. That is strictly better than trusting the model's own JSON types: a model
    sending ``"1000"`` where the column holds integers was previously a polars type error,
    and is now a comparison against 1000.

    The consequence worth knowing: ``value=""`` means *no value given*, not "equals the
    empty string". Emptiness is asked for with ``is_null`` / ``is_not_null``, which is the
    question somebody actually means, and a column of empty strings that are not NULL is
    rare enough to be worth the trade.

    Every column here is re-resolved against the columns the source really returns —
    see ``aggregate_planner.validate_plan`` — so what this schema guarantees is the
    *shape*, exactly as it does for a measure.
    """

    column: str = Field(title="Column", max_length=MAX_NAME_LENGTH)
    part: str = Field(default="", title="Part of a date", max_length=16)
    operator: str = Field(title="Comparison", max_length=16)
    value: str = Field(default="", title="Value", max_length=MAX_NAME_LENGTH)
    values: List[str] = Field(
        default_factory=list,
        title="Values",
        max_length=filter_algebra.MAX_IN_VALUES,
    )

    @field_validator("operator")
    @classmethod
    def _must_be_a_known_comparison(cls, value: str) -> str:
        """
        Refuse an invented operator in the model's own terms, here rather than only
        in the planner, for the reason ``PlannedAggregation`` gives about ``median``:
        the sooner ``like`` is refused the fewer places have to cope with it.
        """
        refusal = filter_algebra.unsupported_operator(value)

        if refusal:
            raise ValueError(refusal)

        return value.strip()

    @field_validator("part")
    @classmethod
    def _must_be_a_known_date_part(cls, value: str) -> str:
        refusal = filter_algebra.unsupported_part(value)

        if refusal:
            raise ValueError(refusal)

        return value.strip().lower()


class AggregationPlan(RequestSchema):
    """
    What to keep, what to group by and what to measure — an LLM's structured output.

    ``unsupported`` is how the model declines rather than guesses. Given a request
    it cannot express with the five foldable functions, a model with no way to say
    so will produce a plan that is *shaped* right and answers a different question,
    which is the worst available outcome. So it is given the words.

    **``aggregations`` may be empty when ``filters`` is not.** A plan with conditions
    and no measures is a request for the matching records themselves, which is the
    ordinary shape of "show me the Python department's March figures" — the planner
    records that as the plan's ``mode`` so nothing downstream has to infer it from an
    empty list. Empty on both counts is still nothing to do.
    """

    group_by: List[str] = Field(
        default_factory=list, title="Group by", max_length=MAX_GROUP_BY_COLUMNS,
    )
    aggregations: List[PlannedAggregation] = Field(
        default_factory=list, title="Measures", max_length=MAX_PLAN_AGGREGATIONS,
    )
    filters: List[PlannedFilter] = Field(
        default_factory=list, title="Conditions", max_length=MAX_PLAN_FILTERS,
    )
    #: Which shape of answer this plan asks for. **Assigned by the planner, never taken
    #: from the model** — the same rule an alias follows, and for the same kind of
    #: reason: a model that said "groups" while asking for no measure would describe a
    #: fold that cannot happen. Defaulted to what every plan meant before filters
    #: existed, so a plan built by anything that has not been updated still folds.
    mode: str = Field(
        default=filter_algebra.MODE_GROUPS, title="Kind of answer", max_length=16,
    )
    unsupported: bool = Field(
        default=False, title="Cannot be expressed as a grouping",
    )
    reason: str = Field(
        default="", title="Why not", max_length=MAX_DESCRIPTION_LENGTH,
    )

    @field_validator("group_by")
    @classmethod
    def _no_blank_or_repeated_columns(cls, value: List[str]) -> List[str]:
        """
        Grouping by the same column twice is not an error a person makes, but it is
        one a model makes, and it would produce a plan whose sort order and whose
        carried names both depend on which copy is read.
        """
        columns = [str(item).strip() for item in value if str(item).strip()]

        if len(columns) != len(set(columns)):
            raise ValueError("The same column cannot be grouped by twice")

        return columns


class AggregateRecordsArgs(AppBaseSchema):
    """
    The arguments the agent tool takes. A model fills these in.

    ``tool_name`` is optional and, when it is given and resolves, is what makes the
    common case cost no LLM call at all — the routing prompt already lists every
    tool by name, so the model naming one is the expected path rather than a hint.
    """

    instruction: str = Field(
        title="What to group and measure",
        min_length=1,
        max_length=MAX_INSTRUCTION_LENGTH,
        description=(
            "What to group the records by and what to measure, in one sentence — "
            "for example 'total and average amount by region'."
        ),
    )
    tool_name: str = Field(
        default="",
        title="Tool",
        max_length=MAX_NAME_LENGTH,
        description=(
            "The name of the tool whose records should be grouped. Give this "
            "whenever you know which tool holds the data."
        ),
    )


class AggregateRunRequest(FormRequest):
    """The console form: pick an agent, optionally a tool, and say what to group."""

    agent_id: OptionalUUID = Field(default=None, title="Data agent")
    tool_id: OptionalUUID = Field(default=None, title="Tool")
    instruction: str = Field(
        title="Instruction", min_length=1, max_length=MAX_INSTRUCTION_LENGTH,
    )


class AggregationResultView(ResponseSchema):
    """
    One finished run, as the console renders it and the tool describes it.

    ``group_count`` is separate from ``len(rows)`` on purpose and is the number
    that must be reported: the rows are capped at the tool row limit, the group
    count is not, so a result of 200 rows out of 4,821 groups says so rather than
    reading as the whole answer.

    ``mode`` says which of two questions was answered, and the three counts mean
    slightly different things in each:

    | | ``groups`` | ``rows`` |
    |---|---|---|
    | ``rows`` | one per group | the matching records, capped |
    | ``group_count`` | how many groups there were | how many records **matched** |
    | ``total_records`` | how many records were read | the same — read, not matched |

    So ``group_count`` is the "out of" number in both, which is what lets one
    ``describe_result`` call render either honestly.
    """

    tool_name: str = Field(default="", title="Tool")
    tool_id: Optional[uuid_pkg.UUID] = Field(default=None, title="Tool id")
    datasource_name: str = Field(default="", title="Datasource")
    summary: str = Field(default="", title="What was calculated")
    mode: str = Field(default="groups", title="Kind of answer")
    columns: List[str] = Field(default_factory=list, title="Columns")
    rows: List[Dict[str, Any]] = Field(default_factory=list, title="Rows")
    group_count: int = Field(default=0, title="Groups")
    records_read: int = Field(default=0, title="Records read")
    total_records: int = Field(default=0, title="Records matched")

    @property
    def is_capped(self) -> bool:
        """Whether more groups (or matching records) were found than are being shown."""
        return self.group_count > len(self.rows)
