"""
Tests for app/utils/sql_guard.py.

This module answers one question — "is this a single, read-only statement?" — for
three callers: Ask AI before it shows a generated query, Tool Configs before it
stores a hand-written one, and the Deep Agents executor before it runs one. It is
therefore a security boundary, and the rejection cases are tested exhaustively.

The acceptance cases matter just as much, though, and are the reason the module
exists in this shape: a guard that refuses ordinary analytical SQL would push
people back to the query builder's subset, which is precisely the limitation
SQL-mode tool configs were added to remove. So "a column called ``created_at``",
"a literal containing the word delete" and "a CTE" are all asserted to pass.
"""

from __future__ import annotations

import pytest

from app.utils.sql_guard import (
    MAX_BUILT_SQL_LENGTH,
    MAX_SQL_LENGTH,
    PLACEHOLDER_LIST,
    PLACEHOLDER_SINGLE,
    bind_placeholders,
    forbidden_identifier,
    group_by_violation,
    missing_identifiers,
    normalised_sql,
    placeholder_shape,
    read_only_violation,
    spaced_placeholder,
    star_selection_violation,
    stripped_literals,
    suffixed_placeholders,
)


class TestNormalisedSql:
    @pytest.mark.parametrize("blank", ["", "   ", None, "\n\t "])
    def test_nothing_normalises_to_an_empty_string(self, blank) -> None:  # noqa: ANN001
        assert normalised_sql(blank) == ""

    def test_a_trailing_semicolon_is_dropped(self) -> None:
        """Typing one is a habit; dropping it is also what lets the
        single-statement check be a flat "no `;` anywhere"."""
        assert normalised_sql("SELECT 1 FROM t;") == "SELECT 1 FROM t"

    def test_several_trailing_semicolons_and_whitespace_are_dropped(self) -> None:
        assert normalised_sql("  SELECT 1 FROM t ;; \n ") == "SELECT 1 FROM t"

    def test_a_markdown_fence_is_removed(self) -> None:
        """Models fence regardless of being asked not to, and a fence is
        formatting rather than a reason to reject a good query."""
        assert normalised_sql("```sql\nSELECT 1 FROM t\n```") == "SELECT 1 FROM t"

    def test_a_fence_with_no_language_is_removed(self) -> None:
        assert normalised_sql("```\nSELECT 1 FROM t\n```") == "SELECT 1 FROM t"

    def test_an_empty_fence_yields_nothing(self) -> None:
        assert normalised_sql("```sql\n```") == ""

    def test_a_semicolon_inside_the_statement_survives_normalisation(self) -> None:
        """Only the trailing one is a habit. An interior one is a second
        statement, and it is read_only_violation's job to say so."""
        assert normalised_sql("SELECT 1; DROP TABLE t") == "SELECT 1; DROP TABLE t"


class TestStrippedLiterals:
    def test_single_quoted_strings_are_blanked(self) -> None:
        assert "delete" not in stripped_literals("WHERE action = 'delete'")

    def test_an_escaped_quote_does_not_end_the_string(self) -> None:
        bare = stripped_literals("WHERE note = 'it''s delete' AND id = 1")

        assert "delete" not in bare
        assert "id" in bare

    def test_double_quoted_identifiers_are_blanked(self) -> None:
        assert "create" not in stripped_literals('SELECT "create" FROM t')

    def test_backtick_identifiers_are_blanked(self) -> None:
        assert "update" not in stripped_literals("SELECT `update` FROM t")

    def test_line_comments_are_blanked(self) -> None:
        assert "drop" not in stripped_literals("SELECT 1 -- drop everything\nFROM t")

    def test_block_comments_are_blanked(self) -> None:
        assert "drop" not in stripped_literals("SELECT /* drop */ 1 FROM t")

    def test_a_comment_marker_inside_a_string_is_not_a_comment(self) -> None:
        """Ordered longest-first so `--` inside a literal stays inside it."""
        bare = stripped_literals("WHERE note = 'a -- b' AND id = 1")

        assert "id" in bare


class TestReadOnlyViolationAccepts:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1 FROM t",
            "select 1 from t",
            "  \n SELECT 1 FROM t",
            "SELECT DISTINCT name FROM items",
            "SELECT name FROM items ORDER BY name LIMIT 10 OFFSET 5",
            "SELECT sku, COUNT(*) FROM sales GROUP BY sku HAVING COUNT(*) > 2",
            "WITH x AS (SELECT 1) SELECT * FROM x",
            "SELECT * FROM a UNION ALL SELECT * FROM b",
            "SELECT ROW_NUMBER() OVER (PARTITION BY sku ORDER BY d) FROM sales",
            "SELECT (SELECT MAX(id) FROM b) FROM a",
            "SELECT CASE WHEN q > 0 THEN 1 ELSE 0 END FROM s",
            "SELECT a.id, b.id FROM a LEFT JOIN b ON b.a_id = a.id",
        ],
    )
    def test_ordinary_analytical_sql_passes(self, sql: str) -> None:
        assert read_only_violation(sql) is None

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT created_at, updated_at FROM t",
            "SELECT deleted FROM t",
            "SELECT id FROM updates",
            "SELECT id FROM t WHERE status = 'deleted'",
            "SELECT id FROM t WHERE note = 'a;b'",
        ],
    )
    def test_write_words_that_are_not_writes_pass(self, sql: str) -> None:
        """Word boundaries and literal-stripping between them mean an ordinary
        column name or value is never mistaken for a verb."""
        assert read_only_violation(sql) is None

    def test_nothing_is_not_a_violation(self) -> None:
        """Whether a missing query is an error belongs to the caller — Ask AI
        treats an empty result as a real answer about the schema."""
        assert read_only_violation("") is None
        assert read_only_violation(None) is None

    def test_a_statement_at_exactly_the_length_limit_passes(self) -> None:
        sql = "SELECT " + "x" * (MAX_SQL_LENGTH - len("SELECT "))

        assert len(sql) == MAX_SQL_LENGTH
        assert read_only_violation(sql) is None


class TestReadOnlyViolationRejects:
    @pytest.mark.parametrize(
        "sql",
        [
            "DELETE FROM t",
            "UPDATE t SET a = 1",
            "INSERT INTO t VALUES (1)",
            "DROP TABLE t",
            "ALTER TABLE t ADD COLUMN a INT",
            "TRUNCATE t",
            "CREATE TABLE t (id INT)",
            "GRANT ALL ON t TO x",
            "PRAGMA table_info(t)",
            "COPY t FROM '/etc/passwd'",
            "SET search_path = x",
        ],
    )
    def test_a_statement_that_is_not_a_read_is_refused(self, sql: str) -> None:
        assert read_only_violation(sql) is not None

    def test_a_non_read_says_what_it_should_start_with(self) -> None:
        assert "SELECT or WITH" in read_only_violation("DELETE FROM t")

    @pytest.mark.parametrize(
        "sql",
        [
            "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
            "SELECT * INTO backup FROM t",
            "SELECT 1 FROM t WHERE EXISTS (SELECT 1 FROM u) /**/ DROP TABLE t",
        ],
    )
    def test_a_write_reached_from_a_read_position_is_refused(self, sql: str) -> None:
        """The reason the keyword scan exists at all: `WITH … INSERT` and
        `SELECT … INTO` both start like a read."""
        violation = read_only_violation(sql)

        assert violation is not None
        assert "change data" in violation

    def test_the_offending_keyword_is_named(self) -> None:
        """So the message can say which word put the query out of bounds."""
        assert "'INSERT'" in read_only_violation(
            "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x"
        )

    def test_two_statements_are_refused(self) -> None:
        assert read_only_violation("SELECT 1; SELECT 2") == (
            "contains more than one statement"
        )

    def test_an_over_long_statement_is_refused(self) -> None:
        violation = read_only_violation("SELECT " + "x" * MAX_SQL_LENGTH)

        assert violation == f"is longer than {MAX_SQL_LENGTH} characters"

    def test_length_is_checked_before_anything_else(self) -> None:
        """A megabyte of pasted text should not be regex-scanned first."""
        assert read_only_violation("DELETE " + "x" * MAX_SQL_LENGTH).startswith(
            "is longer than"
        )


class TestStarSelection:
    """
    Ask AI refuses a generated ``SELECT *`` because ``*`` is the one selection whose
    column list the database decides at run time: a query approved today would start
    returning a column switched off tomorrow, without the query changing.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM orders",
            "select  *  from orders",
            "SELECT DISTINCT * FROM orders",
            "SELECT ALL * FROM orders",
            "SELECT o.* FROM orders o",
            "SELECT o . * FROM orders o",
            "SELECT id, o.* FROM orders o",
            "WITH x AS (SELECT * FROM orders) SELECT id FROM x",
        ],
    )
    def test_a_star_selection_is_reported(self, sql: str) -> None:
        assert star_selection_violation(sql) is not None

    def test_count_star_is_not_a_star_selection(self) -> None:
        """The false positive that would matter most: an aggregate over all rows
        names no columns, so refusing it would break every "how many" question."""
        assert star_selection_violation("SELECT COUNT(*) FROM orders") is None

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT COUNT( * ) FROM orders",
            "SELECT status, COUNT(*) FROM orders GROUP BY status",
            "SELECT orders.id, orders.total FROM orders",
            "SELECT total * 2 AS doubled FROM orders",
            "SELECT id FROM orders WHERE note = 'select * from x'",
        ],
    )
    def test_an_explicit_selection_passes(self, sql: str) -> None:
        assert star_selection_violation(sql) is None

    def test_the_offending_text_is_returned_for_the_message(self) -> None:
        assert star_selection_violation("SELECT   *  FROM orders") == "SELECT *"

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_nothing_to_check_passes(self, blank) -> None:  # noqa: ANN001
        assert star_selection_violation(blank) is None


class TestForbiddenIdentifier:
    def test_a_named_identifier_is_found(self) -> None:
        assert forbidden_identifier(
            "SELECT id, salary FROM staff", ["salary"],
        ) == "salary"

    def test_a_match_inside_a_literal_is_ignored(self) -> None:
        """Otherwise a perfectly ordinary filter value would be read as a reference
        to a column of the same name."""
        assert forbidden_identifier(
            "SELECT id FROM staff WHERE note = 'salary review'", ["salary"],
        ) is None

    def test_a_bare_name_does_not_match_a_qualified_one(self) -> None:
        """A caller that wants ``orders.id`` forbidden passes ``orders.id`` — so that
        forbidding ``id`` on one table cannot reject a query reading another's."""
        assert forbidden_identifier("SELECT orders.id FROM orders", ["id"]) is None

    def test_a_qualified_name_is_matched_as_written(self) -> None:
        assert forbidden_identifier(
            "SELECT orders.total FROM orders", ["orders.total"],
        ) == "orders.total"

    def test_a_longer_name_containing_it_is_not_a_match(self) -> None:
        assert forbidden_identifier("SELECT total_paid FROM orders", ["total"]) is None

    @pytest.mark.parametrize("forbidden", [None, [], ["", "  "]])
    def test_nothing_forbidden_passes(self, forbidden) -> None:  # noqa: ANN001
        assert forbidden_identifier("SELECT id FROM orders", forbidden) is None


class TestMissingIdentifiers:
    def test_the_absent_names_are_reported_in_order(self) -> None:
        assert missing_identifiers(
            "SELECT orders.id FROM orders",
            ["orders.id", "orders.total", "orders.note"],
        ) == ["orders.total", "orders.note"]

    def test_an_aliased_table_still_counts_as_present(self) -> None:
        """``orders.total`` written against a table aliased ``o`` is ``o.total`` and
        is not missing. Being advisory, the check errs towards not crying wolf."""
        assert missing_identifiers(
            "SELECT o.id, o.total FROM orders o", ["orders.id", "orders.total"],
        ) == []

    def test_a_name_only_inside_a_literal_is_still_missing(self) -> None:
        assert missing_identifiers(
            "SELECT id FROM orders WHERE note = 'total'", ["orders.total"],
        ) == ["orders.total"]


# ---------------------------------------------------------------------------
# group_by_violation
#
# The check exists because MySQL's default sql_mode (ONLY_FULL_GROUP_BY) and
# PostgreSQL both refuse a grouped query that selects an ungrouped column, and a
# generated query that breaks that rule cannot run anywhere it will be used.
#
# It drives a regeneration and a warning, never a refusal, so the two directions
# are not equally costly: a missed violation ends in a clear message from the
# database, a false one sends the model off rewriting a query that was already
# right. Everything it cannot read honestly is therefore asserted to return None.
# ---------------------------------------------------------------------------
#: Reflected primary keys, as sql_assist passes them.
KEYS = {"orders": ["id"], "customers": ["id"], "line_items": ["order_id", "sku"]}


class TestGroupByViolationReports:
    def test_a_nonaggregated_column_outside_the_grouping_is_named(self) -> None:
        assert group_by_violation(
            "SELECT orders.customer_name, COUNT(*) FROM orders "
            "GROUP BY orders.status",
            KEYS,
        ) == "orders.customer_name"

    def test_a_bare_column_is_measured_against_a_qualified_grouping(self) -> None:
        assert group_by_violation(
            "SELECT customer_name, SUM(total) FROM orders GROUP BY orders.status",
            KEYS,
        ) == "customer_name"

    def test_a_second_table_s_column_is_not_covered_by_the_first_s_key(self) -> None:
        assert group_by_violation(
            "SELECT c.name, o.total, COUNT(*) FROM customers c "
            "JOIN orders o ON o.customer_id = c.id GROUP BY c.id",
            KEYS,
        ) == "o.total"

    def test_without_primary_keys_a_grouped_key_proves_nothing(self) -> None:
        """The functional dependency is only visible to a caller that passes the
        reflected keys; without them the column is reported."""
        assert group_by_violation(
            "SELECT orders.id, orders.customer_name FROM orders GROUP BY orders.id",
        ) == "orders.customer_name"

    def test_a_composite_key_has_to_be_grouped_in_full(self) -> None:
        """Half a key does not fix one row per group, so nothing depends on it."""
        assert group_by_violation(
            "SELECT line_items.order_id, line_items.note, COUNT(*) FROM line_items "
            "GROUP BY line_items.order_id",
            KEYS,
        ) == "line_items.note"

    def test_the_first_offender_is_the_one_returned(self) -> None:
        assert group_by_violation(
            "SELECT orders.status, orders.note, orders.total, COUNT(*) FROM orders "
            "GROUP BY orders.status",
            KEYS,
        ) == "orders.note"


class TestGroupByViolationAccepts:
    @pytest.mark.parametrize("sql", [
        "SELECT status, COUNT(*) FROM orders GROUP BY status",
        "SELECT orders.status, SUM(orders.total) FROM orders GROUP BY orders.status",
        "SELECT status, note, COUNT(*) FROM orders GROUP BY status, note",
        "SELECT DISTINCT status, COUNT(*) FROM orders GROUP BY status",
        "SELECT status AS state, COUNT(*) FROM orders GROUP BY status",
        "SELECT status, MIN(customer_name), COUNT(*) FROM orders GROUP BY status",
        "select status , sum(total) from orders group by status order by 2 desc limit 5",
    ])
    def test_a_properly_grouped_query_passes(self, sql: str) -> None:
        assert group_by_violation(sql, KEYS) is None

    def test_a_column_of_a_grouped_primary_key_is_functionally_dependent(self) -> None:
        """What both databases allow, and one of the most ordinary shapes there is:
        group by the key, select the row's other columns beside the aggregate."""
        assert group_by_violation(
            "SELECT orders.id, orders.customer_name, COUNT(*) FROM orders "
            "GROUP BY orders.id",
            KEYS,
        ) is None

    def test_an_alias_is_resolved_to_its_table_for_that_check(self) -> None:
        assert group_by_violation(
            "SELECT o.id, o.customer_name, COUNT(*) FROM orders o GROUP BY o.id",
            KEYS,
        ) is None

    def test_a_bare_column_takes_the_key_of_the_only_table(self) -> None:
        assert group_by_violation(
            "SELECT customer_name, COUNT(*) FROM orders GROUP BY id", KEYS,
        ) is None

    def test_a_word_that_is_a_value_and_not_a_column_is_ignored(self) -> None:
        assert group_by_violation(
            "SELECT status, NULL, CURRENT_DATE, COUNT(*) FROM orders GROUP BY status",
            KEYS,
        ) is None

    def test_a_literal_that_reads_like_a_clause_is_not_one(self) -> None:
        assert group_by_violation(
            "SELECT status, COUNT(*) FROM orders WHERE note = 'group by nothing' "
            "GROUP BY status",
            KEYS,
        ) is None

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_nothing_to_check_passes(self, blank) -> None:  # noqa: ANN001
        assert group_by_violation(blank, KEYS) is None


class TestGroupByViolationStaysQuiet:
    """The statements this check cannot read honestly. Each one is a shape it would
    need a parser to judge, so it says nothing rather than guessing."""

    def test_an_aggregate_with_no_grouping_at_all_is_left_alone(self) -> None:
        """The database refuses this too, but with no GROUP BY there is nothing to
        compare the SELECT list against without parsing it."""
        assert group_by_violation(
            "SELECT customer_name, COUNT(*) FROM orders", KEYS,
        ) is None

    def test_a_cte_has_a_second_scope(self) -> None:
        assert group_by_violation(
            "WITH recent AS (SELECT id, total FROM orders) "
            "SELECT note, COUNT(*) FROM recent GROUP BY id",
            KEYS,
        ) is None

    def test_a_subquery_has_a_second_scope(self) -> None:
        assert group_by_violation(
            "SELECT note, COUNT(*) FROM orders "
            "WHERE id IN (SELECT order_id FROM line_items) GROUP BY status",
            KEYS,
        ) is None

    def test_grouping_by_an_ordinal_cannot_be_matched_to_a_column(self) -> None:
        assert group_by_violation(
            "SELECT customer_name, COUNT(*) FROM orders GROUP BY 1", KEYS,
        ) is None

    def test_grouping_by_an_expression_cannot_be_matched_to_a_column(self) -> None:
        assert group_by_violation(
            "SELECT note, COUNT(*) FROM orders GROUP BY DATE(created_at)", KEYS,
        ) is None

    def test_a_selected_expression_is_passed_over(self) -> None:
        assert group_by_violation(
            "SELECT UPPER(customer_name), COUNT(*) FROM orders GROUP BY status", KEYS,
        ) is None

    def test_an_alias_written_without_as_is_passed_over(self) -> None:
        assert group_by_violation(
            "SELECT customer_name cname, COUNT(*) FROM orders GROUP BY status", KEYS,
        ) is None


class TestBindPlaceholders:
    """
    The ``:name`` parameters a statement uses. Read off the statement with literals blanked,
    which is what keeps a time inside a string from being mistaken for a parameter.

    Two callers had a private copy of this each, and could not share one because neither
    module could import the other; it lives here now and both delegate.
    """

    def test_finds_every_placeholder(self) -> None:
        assert bind_placeholders(
            "SELECT id FROM t WHERE a = :one AND b IN :two",
        ) == {"one", "two"}

    def test_ignores_a_time_inside_a_string(self) -> None:
        """The reason literals are blanked first: '12:30' is not a parameter called 30."""
        assert bind_placeholders("SELECT id FROM t WHERE note = '12:30'") == set()

    def test_ignores_a_postgres_cast(self) -> None:
        assert bind_placeholders("SELECT id::text FROM t") == set()

    def test_a_cast_of_a_placeholder_still_finds_the_placeholder(self) -> None:
        assert bind_placeholders("SELECT id FROM t WHERE a = :one::text") == {"one"}

    def test_ignores_a_comment(self) -> None:
        assert bind_placeholders("SELECT id FROM t -- :nope") == set()

    def test_nothing_in_nothing(self) -> None:
        assert bind_placeholders("") == set()
        assert bind_placeholders(None) == set()


class TestSuffixedPlaceholders:
    """
    Renaming a statement's placeholders, which is what makes one copy of a fragment
    distinguishable from another when several are joined into one union.

    The property under test throughout is that **text is preserved and only placeholders
    move**. Every other reader in this module blanks literals because it only needs to look;
    this one rewrites, so a literal that happens to contain a colon has to come out the far
    side untouched — and the LIKE pattern in the case this was built for does exactly that.
    """

    def test_renames_a_placeholder(self) -> None:
        assert suffixed_placeholders(
            "SELECT id FROM t WHERE a = :one", "__p3",
        ) == "SELECT id FROM t WHERE a = :one__p3"

    def test_renames_every_occurrence_of_the_same_name(self) -> None:
        """Both mentions must move together, or half the fragment reads another pass."""
        assert suffixed_placeholders(
            "SELECT id FROM t WHERE a = :one OR b = :one", "__p1",
        ) == "SELECT id FROM t WHERE a = :one__p1 OR b = :one__p1"

    def test_a_literal_containing_a_colon_survives(self) -> None:
        """
        The case this was written for. ``'%s:departs:'`` is a serialised-field pattern, and
        blanking or rewriting inside it would corrupt the query while renaming exactly the
        placeholder that was wanted.
        """
        statement = (
            "SELECT pd.id FROM project_details pd "
            "WHERE pd.departments LIKE concat('%s:departs:', :id, '%')"
        )

        assert suffixed_placeholders(statement, "__p7") == (
            "SELECT pd.id FROM project_details pd "
            "WHERE pd.departments LIKE concat('%s:departs:', :id__p7, '%')"
        )

    def test_a_comment_containing_a_colon_survives(self) -> None:
        assert suffixed_placeholders(
            "SELECT id FROM t WHERE a = :one -- todo: check :two", "__p2",
        ) == "SELECT id FROM t WHERE a = :one__p2 -- todo: check :two"

    def test_a_postgres_cast_is_not_renamed(self) -> None:
        assert suffixed_placeholders("SELECT id::text FROM t", "__p1") == (
            "SELECT id::text FROM t"
        )

    def test_a_cast_of_a_placeholder_renames_only_the_placeholder(self) -> None:
        assert suffixed_placeholders(
            "SELECT id FROM t WHERE a = :one::text", "__p4",
        ) == "SELECT id FROM t WHERE a = :one__p4::text"

    def test_a_statement_with_no_placeholders_is_unchanged(self) -> None:
        assert suffixed_placeholders("SELECT 1 FROM t", "__p1") == "SELECT 1 FROM t"

    def test_an_empty_suffix_changes_nothing(self) -> None:
        """Guarded rather than allowed: renaming to the same name would bind pass over pass."""
        assert suffixed_placeholders("SELECT 1 WHERE a = :one", "") == (
            "SELECT 1 WHERE a = :one"
        )

    def test_nothing_in_nothing(self) -> None:
        assert suffixed_placeholders("", "__p1") == ""
        assert suffixed_placeholders(None, "__p1") == ""


class TestReadOnlyViolationLength:
    """
    The length rule, and the one caller allowed a different one.

    ``MAX_BUILT_SQL_LENGTH`` exists because 8,000 is guarding against a pasted dump — text
    whose length is itself evidence nobody wrote it on purpose — and a union this
    application composed from an already-checked fragment is not that. Everything *else*
    the guard asks has to keep applying to it, which is the second half of this class.
    """

    def test_the_default_still_refuses_at_the_ordinary_ceiling(self) -> None:
        violation = read_only_violation("SELECT " + "a" * MAX_SQL_LENGTH)

        assert violation is not None
        assert str(MAX_SQL_LENGTH) in violation

    def test_a_raised_ceiling_admits_a_longer_statement(self) -> None:
        assert read_only_violation(
            "SELECT " + "a" * MAX_SQL_LENGTH, max_length=MAX_BUILT_SQL_LENGTH,
        ) is None

    def test_a_raised_ceiling_still_refuses_a_write_verb(self) -> None:
        """The only thing relaxed is the length. A built statement is still one read."""
        violation = read_only_violation(
            "SELECT 1 UNION SELECT 2; DROP TABLE clients",
            max_length=MAX_BUILT_SQL_LENGTH,
        )

        assert violation is not None

    def test_a_raised_ceiling_still_has_a_ceiling(self) -> None:
        violation = read_only_violation(
            "SELECT " + "a" * MAX_BUILT_SQL_LENGTH, max_length=MAX_BUILT_SQL_LENGTH,
        )

        assert violation is not None
        assert str(MAX_BUILT_SQL_LENGTH) in violation


class TestSpacedPlaceholder:
    """
    A ``:`` that has come adrift from its name — ``= : item``.

    Its own check because the space makes the placeholder invisible to
    ``bind_placeholders``, so nothing else here notices it and the statement reaches the
    database with a stray colon in it.
    """

    def test_finds_a_colon_with_a_space_after_it(self) -> None:
        assert spaced_placeholder(
            "SELECT id, name FROM departments WHERE id = : item",
        ) == "item"

    def test_a_tab_counts_too(self) -> None:
        assert spaced_placeholder("SELECT 1 WHERE a = :\titem") == "item"

    def test_a_correctly_written_placeholder_is_not_reported(self) -> None:
        assert spaced_placeholder("SELECT 1 WHERE a = :item AND b IN :ids") is None

    def test_a_postgres_cast_with_a_space_is_not_reported(self) -> None:
        """
        The lookbehind is ``bind_placeholders``' own, so the second colon of a ``::`` is
        never read as a colon adrift from the type name after it.
        """
        assert spaced_placeholder("SELECT id :: text FROM t") is None

    def test_a_colon_inside_a_literal_is_not_reported(self) -> None:
        assert spaced_placeholder("SELECT id FROM t WHERE note = 'due: soon'") is None

    def test_a_colon_inside_a_comment_is_not_reported(self) -> None:
        assert spaced_placeholder("SELECT id FROM t -- todo: fix") is None

    def test_a_colon_at_the_end_of_a_line_is_not_reported(self) -> None:
        """
        Same-line whitespace only. A trailing colon is more likely to be something else
        than a mistyped placeholder, and being sure is worth more than being exhaustive.
        """
        assert spaced_placeholder("SELECT id FROM t WHERE a = :\nitem") is None

    def test_nothing_in_nothing(self) -> None:
        assert spaced_placeholder("") is None
        assert spaced_placeholder(None) is None


class TestPlaceholderShape:
    """
    Whether a placeholder is written where a list belongs or where one value does.

    An expanding bind parameter always renders parenthesised, so the two are not
    interchangeable — ``= :x`` given a list is ``= (?, ?, ?)`` and ``IN :x`` given one value
    is ``IN ?``. Both are syntax errors the *database* reports, which is why this is checked
    when a tool or a graph is saved rather than when it runs.
    """

    def test_an_in_comparison_wants_a_list(self) -> None:
        assert placeholder_shape(
            "SELECT id FROM t WHERE a IN :ids", "ids",
        ) == PLACEHOLDER_LIST

    def test_in_is_matched_regardless_of_case_or_spacing(self) -> None:
        assert placeholder_shape("SELECT 1 WHERE a In:ids", "ids") == PLACEHOLDER_LIST

    def test_an_equality_wants_one_value(self) -> None:
        assert placeholder_shape(
            "SELECT id FROM t WHERE a = :one", "one",
        ) == PLACEHOLDER_SINGLE

    def test_a_greater_than_wants_one_value(self) -> None:
        assert placeholder_shape("SELECT 1 WHERE a > :one", "one") == PLACEHOLDER_SINGLE

    def test_a_placeholder_in_neither_shape_concludes_nothing(self) -> None:
        """
        A function argument, say. Guessing here would refuse a statement that works, so the
        caller is told nothing rather than something.
        """
        assert placeholder_shape(
            "SELECT id FROM t WHERE upper(a) LIKE upper(:one)", "one",
        ) is None

    def test_a_shape_inside_a_literal_is_not_seen(self) -> None:
        assert placeholder_shape("SELECT id FROM t WHERE a = 'b IN :ids'", "ids") is None

    def test_a_longer_name_starting_the_same_is_not_confused(self) -> None:
        """``:ids_extra`` is not ``:ids``, and a prefix match would read the wrong shape."""
        assert placeholder_shape("SELECT 1 WHERE a = :ids_extra", "ids") is None

    def test_nothing_in_nothing(self) -> None:
        assert placeholder_shape("", "one") is None
        assert placeholder_shape("SELECT 1", "") is None
