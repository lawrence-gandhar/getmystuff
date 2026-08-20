"""
The JSON schema an LLM provider is handed for a plan, and why it has to be dull.

``AggregationPlan`` is a structured-output schema: ``model.with_structured_output(...)``
sends it to the provider as ``response_format``, and a **strict** validator — Cerebras's,
in the case that produced this file — refuses shapes that pydantic emits perfectly happily.

    400 Unsupported JSON schema fields in schema with keys: dict_keys([])
        param: response_format, code: wrong_api_format

That was every planning call failing the moment filters existed. A field typed ``Any``
renders as an **empty** schema ``{}``, which is what "keys: dict_keys([])" describes.
The visible symptom was the same sentence the whole feature exists to remove — the agent
reporting that it could not filter by month — arriving one layer further in, which is
exactly why a test at this level is worth having: the failure is invisible from the
outside and identical to the bug it was meant to fix.

So the properties here are about the *shape of the schema*, not about planning:

* **no empty property schema** — an ``Any`` anywhere;
* **no ``anyOf`` / ``oneOf``** — which is what a union or an ``Optional`` renders as, and
  which several strict validators also refuse;
* every property has a concrete, single ``type``.

None of that is checkable against the real provider from a test, which is the point: these
assert the shape the provider was refusing, so the next `Any` fails here rather than in
somebody's conversation.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from app.schemas.agent_recursive_dataframes import AggregationPlan, PlannedFilter


def json_schema() -> Dict[str, Any]:
    return AggregationPlan.model_json_schema()


def every_schema_node(node: Any, path: str = "") -> List[tuple]:
    """
    Every ``(path, subschema)`` in the document, definitions included.

    Walked rather than spot-checked because the offending field was two levels down — a
    property of an item of a list — and a test that only looked at the top level would
    have passed while the provider refused every call.
    """
    found: List[tuple] = []

    if isinstance(node, dict):
        if "type" in node or "anyOf" in node or "oneOf" in node or not node:
            found.append((path, node))

        for key, value in node.items():
            found.extend(every_schema_node(value, f"{path}/{key}"))

    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(every_schema_node(value, f"{path}/{index}"))

    return found


def properties_of(schema: Dict[str, Any]) -> List[tuple]:
    """Every ``(owner, name, subschema)`` under any ``properties`` block."""
    found: List[tuple] = []

    def walk(node: Any, owner: str) -> None:
        if not isinstance(node, dict):
            return

        for name, subschema in (node.get("properties") or {}).items():
            found.append((owner, name, subschema))

        for key, value in node.items():
            if isinstance(value, dict):
                walk(value, name_of(key, owner))

    def name_of(key: str, owner: str) -> str:
        return key if key not in ("properties", "$defs") else owner

    walk(schema, "AggregationPlan")

    for name, definition in (schema.get("$defs") or {}).items():
        walk(definition, name)

    return found


class TestNoPropertyIsUntyped:
    """
    The one that failed. ``Optional[Any]`` and ``List[Any]`` on ``PlannedFilter`` rendered
    as ``{}`` and ``{"items": {}}``, and the provider rejected the request outright.
    """

    def test_no_property_schema_is_empty(self) -> None:
        empty = [
            (owner, name) for owner, name, subschema in properties_of(json_schema())
            if not subschema
        ]

        assert empty == [], f"untyped properties (an `Any`?): {empty}"

    def test_no_list_has_untyped_items(self) -> None:
        untyped = [
            (owner, name)
            for owner, name, subschema in properties_of(json_schema())
            if subschema.get("type") == "array" and not subschema.get("items")
        ]

        assert untyped == [], f"lists of `Any`: {untyped}"

    def test_the_filter_value_fields_are_strings(self) -> None:
        """
        Named explicitly as well, because these two are the fields that broke it and the
        reason they are strings is not obvious from reading them — the coercion that makes
        it safe lives in ``frame_ops._coerced``, a long way away.
        """
        schema = PlannedFilter.model_json_schema()

        assert schema["properties"]["value"]["type"] == "string"
        assert schema["properties"]["values"]["type"] == "array"
        assert schema["properties"]["values"]["items"]["type"] == "string"


class TestNothingIsAUnion:
    """
    ``anyOf`` is what pydantic emits for a union and for ``Optional[X]``. Some strict
    validators refuse it, so the schema stays single-typed throughout — which also means
    a model never has to choose between two representations of the same value.
    """

    def test_no_subschema_offers_a_choice_of_types(self) -> None:
        offenders = [
            path for path, node in every_schema_node(json_schema())
            if "anyOf" in node or "oneOf" in node
        ]

        assert offenders == [], f"union-typed schema nodes: {offenders}"

    def test_no_property_declares_a_list_of_types(self) -> None:
        """The other spelling of a union — ``{"type": ["string", "null"]}``."""
        offenders = [
            (owner, name)
            for owner, name, subschema in properties_of(json_schema())
            if isinstance(subschema.get("type"), list)
        ]

        assert offenders == []


class TestThePlanStillMeansWhatItMeant:
    """
    The schema being provider-safe is worth nothing if it stopped expressing a plan. These
    are the fields the planner and the pipeline read by name.
    """

    @pytest.mark.parametrize(
        "field", ["group_by", "aggregations", "filters", "unsupported", "reason", "mode"],
    )
    def test_the_field_is_present(self, field: str) -> None:
        assert field in json_schema()["properties"]

    def test_a_filter_round_trips_through_the_schema(self) -> None:
        plan = AggregationPlan.parse({
            "group_by": ["department"],
            "aggregations": [{"type": "sum", "column": "revenue"}],
            "filters": [
                {"column": "created_at", "part": "month", "operator": "==", "value": "8"},
                {"column": "department", "operator": "in", "values": ["Python", "Go"]},
            ],
        })

        assert plan.filters[0].part == "month"
        assert plan.filters[0].value == "8"
        assert plan.filters[1].values == ["Python", "Go"]

    def test_a_number_is_refused_rather_than_silently_accepted(self) -> None:
        """
        Strict on the way in too. A model sending ``8`` rather than ``"8"`` is a validation
        error the provider layer retries on, which is better than a value whose type
        depends on which model answered.
        """
        with pytest.raises(Exception):
            PlannedFilter(column="created_at", operator="==", value=8)
