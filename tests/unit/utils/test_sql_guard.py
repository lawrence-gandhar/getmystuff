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
    MAX_SQL_LENGTH,
    normalised_sql,
    read_only_violation,
    stripped_literals,
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
