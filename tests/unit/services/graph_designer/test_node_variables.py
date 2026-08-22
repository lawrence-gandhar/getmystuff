"""
Tests for ``graph_designer.node_variables`` — ``{{VARIABLE}}`` on any node.

Two halves, and the second is the one to read twice.

The first is ordinary: a node declares names, binds them to earlier nodes' outputs, and
its text fields come out substituted.

The second is the SQL guard. A ``{{VAR}}`` whose value came from ``source: "node"`` is a
value out of a database row that nobody reviewed, and putting one into statement *text*
is string-concatenated SQL by another name. Four things stand in the way and three of
them are here: a placeholder may not sit inside quotes or a comment (save time), a
substituted value must be a name or a whole number (run time), and the statement is
re-validated after substitution by ``_run_sql`` (covered in the compiler tests). Those
tests are the security boundary of this feature — treat a change that relaxes one as a
change to the threat model.

No LangGraph, no database. ``node_variables`` reads state as a plain mapping.
"""

from __future__ import annotations

import json

import pytest
from litestar.exceptions import HTTPException

from app.models.graph_designer import MAX_NODE_VARIABLES, NODE_TYPE_VALUES
from app.services.graph_designer import node_variables


def node(node_id: str, node_type: str, **data) -> dict:
    return {"id": node_id, "type": node_type, "data": data}


def sql_node(query: str, variables: dict | None = None, tables: list | None = None) -> dict:
    return node(
        "n5", "sql",
        label="Regional totals",
        sql_query=query,
        table_names=tables or [],
        variables=variables if variables is not None else {
            "TABLE": {"source": "node", "node_id": "n3"},
        },
    )


def outputs(**by_node) -> dict:
    return {"outputs": dict(by_node)}


DRAWING = {"n3": {}, "n5": {}, "s": {}}


class TestTheFieldTableCoversEveryNodeType:
    """
    Asserted in the module at import, and asserted again here so the failure reads as a
    test rather than as an ImportError halfway up a traceback.
    """

    def test_every_node_type_has_decided_what_it_substitutes(self) -> None:
        assert set(node_variables.VARIABLE_FIELDS) == set(NODE_TYPE_VALUES)

    def test_an_email_node_substitutes_nothing_itself(self) -> None:
        """
        Its subject and body are rendered by the email module against the *template's*
        declaration. A pass here would eat ``{{CUSTOMER}}`` before that renderer saw it.
        """
        assert node_variables.fields_for("email") == ()

    def test_a_branch_condition_is_not_substituted(self) -> None:
        """
        ``branch_port`` is called twice per visit — once by the runner, once by the
        router — so a rendered condition could resolve differently between them and the
        log would disagree with the route the run actually took.
        """
        assert node_variables.fields_for("branch") == ()


class TestSubstituting:
    """The ordinary case."""

    def test_a_value_from_an_earlier_node_lands_in_the_statement(self) -> None:
        prepared = node_variables.render_node(
            sql_node("SELECT * FROM {{TABLE}}"), outputs(n3="orders"),
        )

        assert prepared["data"]["sql_query"] == "SELECT * FROM orders"

    def test_a_dotted_path_reads_into_the_output(self) -> None:
        prepared = node_variables.render_node(
            sql_node(
                "SELECT * FROM {{TABLE}}",
                {"TABLE": {"source": "node", "node_id": "n3", "path": "rows[0].name"}},
            ),
            outputs(n3={"rows": [{"name": "orders"}]}),
        )

        assert prepared["data"]["sql_query"] == "SELECT * FROM orders"

    def test_a_fixed_value_needs_no_upstream_node(self) -> None:
        prepared = node_variables.render_node(
            sql_node("SELECT * FROM t LIMIT {{LIMIT}}",
                     {"LIMIT": {"source": "literal", "value": "50"}}),
            outputs(),
        )

        assert prepared["data"]["sql_query"] == "SELECT * FROM t LIMIT 50"

    def test_every_entry_of_a_list_field_is_substituted(self) -> None:
        prepared = node_variables.render_node(
            sql_node("SELECT 1", tables=["{{TABLE}}", "customers"]), outputs(n3="orders"),
        )

        assert prepared["data"]["table_names"] == ["orders", "customers"]

    def test_a_lowercase_placeholder_finds_its_declaration(self) -> None:
        """
        The renderer upper-cases whatever it matched, so ``{{table}}`` looks for ``TABLE``.
        Reading declarations the same way is what keeps the two from disagreeing.
        """
        prepared = node_variables.render_node(
            sql_node("SELECT * FROM {{table}}"), outputs(n3="orders"),
        )

        assert prepared["data"]["sql_query"] == "SELECT * FROM orders"

    def test_a_success_message_is_substituted(self) -> None:
        prepared = node_variables.render_node(
            node("f", "success", label="Done", message="Took {{DURATION}}.",
                 variables={"DURATION": {"source": "node", "node_id": "n3",
                                         "path": "elapsed_human"}}),
            outputs(n3={"elapsed_human": "1h 4m 12s"}),
        )

        assert prepared["data"]["message"] == "Took 1h 4m 12s."


class TestTheDrawingIsNeverEdited:
    """
    Load-bearing rather than tidy. The compiler captures each node in a closure once per
    run and a loop body re-enters the *same* closure on every pass — so writing rendered
    text back would bake pass one's values in, and every later pass would substitute into
    text that had already been substituted.
    """

    def test_the_node_it_was_given_is_unchanged_afterwards(self) -> None:
        original = sql_node("SELECT * FROM {{TABLE}}")

        node_variables.render_node(original, outputs(n3="orders"))

        assert original["data"]["sql_query"] == "SELECT * FROM {{TABLE}}"

    def test_a_list_field_is_a_new_list(self) -> None:
        original = sql_node("SELECT 1", tables=["{{TABLE}}"])

        node_variables.render_node(original, outputs(n3="orders"))

        assert original["data"]["table_names"] == ["{{TABLE}}"]

    def test_two_passes_substitute_from_scratch_each_time(self) -> None:
        """The regression the copying exists to prevent, run as a loop body would run it."""
        original = sql_node("SELECT * FROM {{TABLE}}")

        first = node_variables.render_node(original, outputs(n3="orders"))
        second = node_variables.render_node(original, outputs(n3="customers"))

        assert first["data"]["sql_query"] == "SELECT * FROM orders"
        assert second["data"]["sql_query"] == "SELECT * FROM customers"

    def test_a_node_with_nothing_to_substitute_costs_nothing(self) -> None:
        plain = node("f", "success", label="Done", message="Finished.")

        assert node_variables.render_node(plain, outputs())["data"]["message"] == "Finished."


class TestWhenAValueIsNotThere:
    """A binding that finds nothing: the author says per variable what that means."""

    def test_without_a_default_the_node_refuses(self) -> None:
        with pytest.raises(HTTPException) as caught:
            node_variables.render_node(sql_node("SELECT * FROM {{TABLE}}"), outputs())

        assert "TABLE" in str(caught.value.detail)

    def test_a_default_covers_a_node_that_never_ran(self) -> None:
        """
        The case an author actually reaches for a default to handle — the branch that
        fills this in was not taken.
        """
        prepared = node_variables.render_node(
            sql_node("SELECT * FROM {{TABLE}}",
                     {"TABLE": {"source": "node", "node_id": "n3", "default": "orders"}}),
            outputs(),
        )

        assert prepared["data"]["sql_query"] == "SELECT * FROM orders"

    def test_a_default_covers_a_path_that_found_nothing(self) -> None:
        prepared = node_variables.render_node(
            sql_node("SELECT * FROM {{TABLE}}",
                     {"TABLE": {"source": "node", "node_id": "n3",
                                "path": "missing.key", "default": "orders"}}),
            outputs(n3={"other": 1}),
        )

        assert prepared["data"]["sql_query"] == "SELECT * FROM orders"

    def test_a_real_value_still_beats_the_default(self) -> None:
        prepared = node_variables.render_node(
            sql_node("SELECT * FROM {{TABLE}}",
                     {"TABLE": {"source": "node", "node_id": "n3", "default": "orders"}}),
            outputs(n3="customers"),
        )

        assert prepared["data"]["sql_query"] == "SELECT * FROM customers"

    def test_a_declaration_nothing_uses_is_never_resolved(self) -> None:
        """
        An unused row is allowed to exist, so a broken binding on one must not fail a node
        whose text never mentions it.
        """
        prepared = node_variables.render_node(
            sql_node("SELECT 1", {"UNUSED": {"source": "node", "node_id": "ghost"}}),
            outputs(),
        )

        assert prepared["data"]["sql_query"] == "SELECT 1"


class TestSqlPlacement:
    """
    Save time. A ``{{VAR}}`` is for an **identifier** — the one thing a bind parameter
    cannot express. Inside quotes it is doing a value's job, where a ``:parameter`` is
    both available and safe.
    """

    @pytest.mark.parametrize(
        "statement",
        [
            "SELECT * FROM t WHERE region = '{{TABLE}}'",
            'SELECT * FROM t WHERE region = "{{TABLE}}"',
            "SELECT * FROM t -- note about {{TABLE}}",
        ],
    )
    def test_a_placeholder_inside_a_literal_or_comment_is_refused(
        self, statement: str,
    ) -> None:
        with pytest.raises(HTTPException) as caught:
            node_variables.assert_valid(sql_node(statement), "Regional totals", DRAWING)

        assert ":parameter" in str(caught.value.detail)

    def test_the_same_name_outside_quotes_is_fine(self) -> None:
        node_variables.assert_valid(
            sql_node("SELECT * FROM {{TABLE}}"), "Regional totals", DRAWING,
        )

    def test_a_placeholder_in_a_table_name_is_fine(self) -> None:
        node_variables.assert_valid(
            sql_node("SELECT 1", tables=["{{TABLE}}"]), "Regional totals", DRAWING,
        )


class TestSqlValues:
    """
    Run time. Whatever a binding found has to prove it is a name or a whole number before
    it is allowed anywhere near statement text.
    """

    @pytest.mark.parametrize(
        "value", ["orders", "sales.orders", "warehouse.sales.orders", "orders_2024", "42", "-1"],
    )
    def test_a_name_or_a_number_is_allowed(self, value: str) -> None:
        prepared = node_variables.render_node(
            sql_node("SELECT * FROM {{TABLE}}"), outputs(n3=value),
        )

        assert prepared["data"]["sql_query"] == f"SELECT * FROM {value}"

    @pytest.mark.parametrize(
        "value, why",
        [
            ("orders; DROP TABLE users", "a second statement"),
            ("orders WHERE 1=1", "a space"),
            ("o'reilly", "a quote"),
            ('o"r', "a double quote"),
            ("(SELECT 1)", "a subquery"),
            ("", "nothing at all"),
            ("orders--", "a comment"),
            ("a" * 600, "an over-long value the resolver truncates"),
        ],
    )
    def test_anything_else_is_refused(self, value: str, why: str) -> None:
        with pytest.raises(HTTPException) as caught:
            node_variables.render_node(sql_node("SELECT * FROM {{TABLE}}"), outputs(n3=value))

        assert "only be a name or a whole number" in str(caught.value.detail), why

    def test_a_fixed_value_is_held_to_the_same_rule(self) -> None:
        """One rule is easier to reason about than two, and the author can type SQL directly."""
        with pytest.raises(HTTPException):
            node_variables.render_node(
                sql_node("SELECT * FROM {{TABLE}}",
                         {"TABLE": {"source": "literal", "value": "a; DROP TABLE t"}}),
                outputs(),
            )

    def test_the_refusal_names_the_node_and_shows_the_value(self) -> None:
        with pytest.raises(HTTPException) as caught:
            node_variables.render_node(
                sql_node("SELECT * FROM {{TABLE}}"), outputs(n3="bad value"),
            )

        detail = str(caught.value.detail)
        assert "Regional totals" in detail
        assert "bad value" in detail


class TestJsonValues:
    """A value with a quote in it must not produce a document that will not parse."""

    def test_a_quoted_value_is_escaped(self) -> None:
        prepared = node_variables.render_node(
            node("v", "value", label="V", value_kind="dict",
                 value_json='{"name": "{{NAME}}"}',
                 variables={"NAME": {"source": "node", "node_id": "n3"}}),
            outputs(n3='say "hi"'),
        )

        assert json.loads(prepared["data"]["value_json"]) == {"name": 'say "hi"'}

    def test_a_newline_does_not_break_the_document(self) -> None:
        prepared = node_variables.render_node(
            node("v", "value", label="V", value_kind="dict",
                 value_json='{"name": "{{NAME}}"}',
                 variables={"NAME": {"source": "node", "node_id": "n3"}}),
            outputs(n3="one\ntwo"),
        )

        assert json.loads(prepared["data"]["value_json"]) == {"name": "one\ntwo"}


class TestDeclarations:
    """What the save refuses, each naming the node."""

    def test_a_placeholder_nothing_declares_is_refused(self) -> None:
        with pytest.raises(HTTPException) as caught:
            node_variables.assert_valid(
                sql_node("SELECT * FROM {{NOPE}}", {}), "Regional totals", DRAWING,
            )

        assert "NOPE" in str(caught.value.detail)

    def test_a_declaration_nothing_uses_is_allowed(self) -> None:
        """
        The panel lets somebody add a row before typing the name into the field, and
        refusing that would make the form impossible to fill in in a natural order.
        """
        node_variables.assert_valid(
            sql_node("SELECT 1", {"SPARE": {"source": "literal", "value": "x"}}),
            "Regional totals", DRAWING,
        )

    def test_a_name_that_is_not_a_name_is_refused(self) -> None:
        with pytest.raises(HTTPException) as caught:
            node_variables.assert_valid(
                sql_node("SELECT 1", {"has-a-dash": {"source": "literal", "value": "x"}}),
                "Regional totals", DRAWING,
            )

        assert "Regional totals" in str(caught.value.detail)

    def test_a_source_a_graph_cannot_serve_is_refused_by_name(self) -> None:
        """A graph has no chat session, so ``session`` is refused rather than left empty."""
        with pytest.raises(HTTPException) as caught:
            node_variables.assert_valid(
                sql_node("SELECT 1", {"X": {"source": "session", "path": "a"}}),
                "Regional totals", DRAWING,
            )

        assert "session" in str(caught.value.detail)

    def test_a_node_binding_with_no_node_chosen_is_refused(self) -> None:
        with pytest.raises(HTTPException):
            node_variables.assert_valid(
                sql_node("SELECT 1", {"X": {"source": "node"}}), "Regional totals", DRAWING,
            )

    def test_a_binding_to_a_deleted_node_is_refused(self) -> None:
        with pytest.raises(HTTPException) as caught:
            node_variables.assert_valid(
                sql_node("SELECT 1", {"X": {"source": "node", "node_id": "ghost"}}),
                "Regional totals", DRAWING,
            )

        assert "no longer in this graph" in str(caught.value.detail)

    @pytest.mark.parametrize("path", ["a..b", "a[", ".."])
    def test_a_malformed_path_is_refused(self, path: str) -> None:
        """
        Shape only — ``assert_path`` checks that the path parses, not where it points.
        Refusing a read that reaches into Python internals is ``paths.read``'s job and
        happens when the value is actually fetched.
        """
        with pytest.raises(HTTPException) as caught:
            node_variables.assert_valid(
                sql_node("SELECT 1",
                         {"X": {"source": "node", "node_id": "n3", "path": path}}),
                "Regional totals", DRAWING,
            )

        assert "Regional totals" in str(caught.value.detail)

    def test_more_than_the_cap_is_refused(self) -> None:
        too_many = {
            f"V{index}": {"source": "literal", "value": "x"}
            for index in range(MAX_NODE_VARIABLES + 1)
        }

        with pytest.raises(HTTPException) as caught:
            node_variables.assert_valid(sql_node("SELECT 1", too_many), "Q", DRAWING)

        assert str(MAX_NODE_VARIABLES) in str(caught.value.detail)

    def test_variables_that_are_not_a_mapping_are_refused(self) -> None:
        with pytest.raises(HTTPException):
            node_variables.assert_valid(
                node("n5", "sql", label="Q", sql_query="SELECT 1", variables=["nope"]),
                "Q", DRAWING,
            )

    def test_an_email_node_cannot_declare_its_own(self) -> None:
        """Its variables come from the template, and the message says so."""
        with pytest.raises(HTTPException) as caught:
            node_variables.assert_valid(
                node("e", "email", label="Notify",
                     variables={"X": {"source": "literal", "value": "1"}}),
                "Notify", DRAWING,
            )

        assert "template" in str(caught.value.detail)


class TestSourceNodes:
    """
    What ``referenced_nodes`` reads through, and therefore what makes a tested selection
    refuse honestly instead of failing obscurely halfway through.
    """

    def test_a_nodes_own_variables_are_counted(self) -> None:
        assert node_variables.source_nodes(sql_node("SELECT 1")["data"]) == {"n3"}

    def test_an_email_nodes_bindings_are_counted(self) -> None:
        """
        The gap this function was written to close. Without it an Email node's upstream was
        invisible to a selection run, which then failed claiming the node had been
        "deleted, or skipped by a branch" — the wrong thing to tell somebody who simply
        did not tick that box.
        """
        data = {"variable_bindings": {"CUSTOMER": {"source": "node", "node_id": "n3"}}}

        assert node_variables.source_nodes(data) == {"n3"}

    def test_both_maps_are_read_at_once(self) -> None:
        data = {
            "variables": {"A": {"source": "node", "node_id": "n1"}},
            "variable_bindings": {"B": {"source": "node", "node_id": "n2"}},
        }

        assert node_variables.source_nodes(data) == {"n1", "n2"}

    def test_a_fixed_value_references_nothing(self) -> None:
        data = {"variables": {"A": {"source": "literal", "value": "x"}}}

        assert node_variables.source_nodes(data) == set()
