"""
Tests for the argument schema a tool advertises — app/services/deep_agents/tool_factory.py.

This schema is the contract between the model and a tool config: it is the only
thing that tells the model a parameter exists, what it is called and whether it may
be left out. A field that silently goes missing here is a tool the model can never
narrow; a field that appears where the operator did not open one is a filter the
operator thought was fixed.

The security property is asserted next door, in ``test_query_executor.py`` — that a
supplied value is *bound* and cannot reach a column, an operator or another filter.
What is asserted here is the shape of what the model is offered.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "langgraph", reason="langgraph is installed in the container only (see Dockerfile)",
)

from app.services.deep_agents.tool_factory import (  # noqa: E402
    _arguments_schema,
    _NoArguments,
)


def _open_filter(**overrides) -> dict:  # noqa: ANN003
    entry = {
        "column": "projects.created_at",
        "operator": ">",
        "agent_supplied": True,
        "required": True,
        "param": "created_after",
        "description": "ISO date; only projects created after it.",
    }
    entry.update(overrides)
    return entry


class TestAToolWithNoOpenFilters:
    def test_it_still_advertises_no_parameters(self) -> None:
        """
        The default, and the common case. An empty schema across all three
        providers' tool-calling formats is what stops a model inventing arguments.
        """
        schema = _arguments_schema("fetch_projects", {
            "filters": [{"column": "status", "operator": "=", "value": "active"}],
        })

        assert schema is _NoArguments

    def test_a_config_with_no_filters_at_all_is_the_same(self) -> None:
        assert _arguments_schema("fetch_projects", {}) is _NoArguments


class TestAToolWithOpenFilters:
    def test_a_required_filter_becomes_a_required_string_field(self) -> None:
        schema = _arguments_schema("fetch_projects", {"filters": [_open_filter()]})
        rendered = schema.model_json_schema()

        assert rendered["required"] == ["created_after"]
        assert rendered["properties"]["created_after"]["type"] == "string"

    def test_the_operators_sentence_is_what_the_model_is_shown(self) -> None:
        """
        The description is the operator's one chance to say what the value means.
        A model given "created_after: string" and nothing else guesses the format.
        """
        schema = _arguments_schema("fetch_projects", {"filters": [_open_filter()]})

        assert schema.model_json_schema()["properties"]["created_after"][
            "description"
        ] == "ISO date; only projects created after it."

    def test_an_optional_filter_is_not_required_and_defaults_to_none(self) -> None:
        """None is what ``_filter_conditions`` reads as "omit this clause"."""
        schema = _arguments_schema(
            "fetch_projects", {"filters": [_open_filter(required=False)]},
        )
        rendered = schema.model_json_schema()

        assert "created_after" not in rendered.get("required", [])
        assert rendered["properties"]["created_after"]["default"] is None

    def test_a_filter_with_no_description_still_gets_a_usable_one(self) -> None:
        """
        Derived from the column and the operator rather than left blank: a field
        with no description is one the model fills in from the name alone.
        """
        schema = _arguments_schema(
            "fetch_projects", {"filters": [_open_filter(description="")]},
        )
        described = schema.model_json_schema()["properties"]["created_after"][
            "description"
        ]

        assert "projects.created_at" in described
        assert ">" in described

    def test_only_the_opened_filters_appear(self) -> None:
        """
        The one that must not regress. A fixed filter beside an open one is still
        the operator's decision, and it must not show up as something the model can
        set.
        """
        schema = _arguments_schema("fetch_projects", {"filters": [
            {"column": "projects.status", "operator": "=", "value": "active"},
            _open_filter(),
            {"column": "projects.archived", "operator": "=", "value": "false"},
        ]})
        properties = schema.model_json_schema()["properties"]

        assert list(properties) == ["created_after"]

    def test_two_open_filters_become_two_fields(self) -> None:
        schema = _arguments_schema("fetch_projects", {"filters": [
            _open_filter(),
            _open_filter(
                column="projects.name", operator="LIKE", param="name_like",
                required=False,
            ),
        ]})
        rendered = schema.model_json_schema()

        assert sorted(rendered["properties"]) == ["created_after", "name_like"]
        assert rendered["required"] == ["created_after"]

    def test_a_filter_marked_open_with_no_parameter_name_is_skipped(self) -> None:
        """
        The validator always names one, so reaching here without a name means a row
        hand-edited into an invalid shape. Skipping it leaves a tool with no
        parameter — which fails loudly at call time — rather than a model with a
        nameless field it cannot address.
        """
        schema = _arguments_schema(
            "fetch_projects", {"filters": [_open_filter(param="")]},
        )

        assert schema is _NoArguments


class TestAToolThatDeclaresSqlValues:
    """
    A SQL-mode statement's declared parameters become the same kind of field.

    The two sources differ in what can be *said* about a field, not in what the
    model may do with it: builder mode knows the column and the operator, a
    statement knows neither, and in both cases what the model supplies is a value
    bound on the right of a comparison the operator wrote.
    """

    def test_each_declared_value_becomes_a_field(self) -> None:
        schema = _arguments_schema("fetch_projects", {}, [
            {"param": "department_id", "type": "number", "required": True,
             "description": "Which department to report on."},
        ])

        assert "department_id" in schema.model_fields
        assert schema.model_fields["department_id"].description == (
            "Which department to report on."
        )

    def test_an_optional_value_defaults_to_none(self) -> None:
        """Which is what the executor reads as "bind NULL" — the shape a statement
        written as ``(:x IS NULL OR col = :x)`` needs."""
        schema = _arguments_schema("fetch_projects", {}, [
            {"param": "since", "required": False},
        ])

        assert schema().since is None

    def test_a_declared_value_with_no_description_still_gets_one(self) -> None:
        """Nothing here knows what the statement compares it against, so the
        fallback names the field and claims nothing more."""
        schema = _arguments_schema("fetch_projects", {}, [{"param": "wanted"}])

        assert schema.model_fields["wanted"].description == "Value for 'wanted'."

    def test_declaring_nothing_leaves_the_tool_taking_no_arguments(self) -> None:
        assert _arguments_schema("fetch_projects", {}, []) is _NoArguments
        assert _arguments_schema("fetch_projects", {}, None) is _NoArguments

    def test_the_declared_type_does_not_reach_the_schema(self) -> None:
        """
        Every field is a string and the coercion happens at execution — against the
        reflected column for a filter, against the declared type for a statement.
        Two answers to "what type is this" is how they come to disagree.
        """
        schema = _arguments_schema("fetch_projects", {}, [
            {"param": "department_id", "type": "number", "required": True},
        ])

        assert schema.model_fields["department_id"].annotation is str
