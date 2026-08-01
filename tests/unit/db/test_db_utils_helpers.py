"""
Tests for the pure helpers in app/db/db_utils.py.

These build connection URLs, quote identifiers and resolve user-supplied file
paths — the points where a user-controlled value becomes part of a connection
string, a SQL identifier or a filesystem read. They are pure, so they are cheap
to test exhaustively, and several are security boundaries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db import db_utils


class TestBuildRdbmsUrl:
    def test_postgres_uses_the_asyncpg_driver(self) -> None:
        url = db_utils.build_rdbms_url("postgres", "localhost", "5432", "shop", "bob", "pw")
        assert url == "postgresql+asyncpg://bob:pw@localhost:5432/shop"

    def test_mysql_uses_the_aiomysql_driver(self) -> None:
        url = db_utils.build_rdbms_url("mysql", "localhost", "3306", "shop", "bob", "pw")
        assert url == "mysql+aiomysql://bob:pw@localhost:3306/shop"

    def test_sqlite_ignores_host_port_and_credentials(self) -> None:
        url = db_utils.build_rdbms_url("sqlite", "ignored", "0", "/data/app.db", "u", "p")
        assert url == "sqlite+aiosqlite:////data/app.db"

    @pytest.mark.parametrize(
        "username,password,expected",
        [
            ("bo b", "pw", "bo+b:pw"),
            ("bob", "p@ss:word/1", "bob:p%40ss%3Aword%2F1"),
            ("us:er", "p#w", "us%3Aer:p%23w"),
        ],
    )
    def test_credentials_are_percent_encoded(self, username, password, expected) -> None:
        """
        A password containing '@' or '/' would otherwise terminate the userinfo
        section early and point the connection at a different host.
        """
        url = db_utils.build_rdbms_url("postgres", "h", "1", "d", username, password)
        assert url == f"postgresql+asyncpg://{expected}@h:1/d"

    @pytest.mark.parametrize("username,password", [(None, None), ("", "")])
    def test_missing_credentials_become_empty_strings(self, username, password) -> None:
        url = db_utils.build_rdbms_url("postgres", "h", "1", "d", username, password)
        assert url == "postgresql+asyncpg://:@h:1/d"

    def test_oracle_is_rejected_with_a_readable_message(self) -> None:
        with pytest.raises(ValueError, match="Oracle is not yet supported"):
            db_utils.build_rdbms_url("oracle", "h", "1", "d", "u", "p")

    @pytest.mark.parametrize("db_type", ["mongodb", "mssql", "", "POSTGRES"])
    def test_unsupported_types_are_rejected(self, db_type) -> None:
        """Matching is exact and case-sensitive, so 'POSTGRES' is not accepted."""
        with pytest.raises(ValueError, match="Unsupported database type"):
            db_utils.build_rdbms_url(db_type, "h", "1", "d", "u", "p")


class TestBuildMongoUri:
    def test_builds_a_mongodb_uri(self) -> None:
        assert (
            db_utils.build_mongo_uri("localhost", "27017", "bob", "pw")
            == "mongodb://bob:pw@localhost:27017"
        )

    def test_credentials_are_not_encoded(self) -> None:
        """
        Documents a real inconsistency with build_rdbms_url, which does encode:
        a Mongo password containing '@' or '/' will produce a malformed URI.
        """
        uri = db_utils.build_mongo_uri("h", "1", "bob", "p@ss")
        assert uri == "mongodb://bob:p@ss@h:1"


class TestQuoteIdentifier:
    @pytest.mark.parametrize("db_type", ["postgres", "sqlite"])
    def test_uses_double_quotes(self, db_type) -> None:
        assert db_utils._quote_identifier(db_type, "users") == '"users"'

    def test_mysql_uses_backticks(self) -> None:
        assert db_utils._quote_identifier("mysql", "users") == "`users`"

    @pytest.mark.parametrize(
        "name",
        [
            'users"; DROP TABLE users; --',
            "users`",
            "users' OR '1'='1",
            "user s",
            "users;",
            "",
            "users)",
        ],
    )
    def test_rejects_anything_that_could_break_out_of_the_quoting(self, name) -> None:
        """The quoting is only safe because the name is validated first."""
        with pytest.raises(ValueError, match="Invalid table identifier"):
            db_utils._quote_identifier("postgres", name)


class TestResolveSafePath:
    def test_returns_the_absolute_path_of_a_real_file(self, tmp_path: Path) -> None:
        target = tmp_path / "data.csv"
        target.write_text("a,b\n1,2\n")
        assert db_utils._resolve_safe_path(str(target)) == str(target.resolve())

    @pytest.mark.parametrize(
        "raw",
        ["../etc/passwd", "/tmp/../etc/passwd", "data/../../secret.csv"],
    )
    def test_rejects_traversal_components(self, raw) -> None:
        with pytest.raises(ValueError, match="Path traversal detected"):
            db_utils._resolve_safe_path(raw)

    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            db_utils._resolve_safe_path(str(tmp_path / "nope.csv"))

    def test_a_directory_is_not_a_regular_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            db_utils._resolve_safe_path(str(tmp_path))

    def test_a_symlink_to_a_real_file_resolves(self, tmp_path: Path) -> None:
        target = tmp_path / "real.csv"
        target.write_text("x")
        link = tmp_path / "link.csv"
        link.symlink_to(target)
        assert db_utils._resolve_safe_path(str(link)) == str(target.resolve())


class TestNormaliseFileType:
    @pytest.mark.parametrize("raw", ["csv", "CSV", "  csv  "])
    def test_is_case_and_whitespace_insensitive(self, raw) -> None:
        assert db_utils._normalise_file_type(raw, "/x/data.csv") == "csv"

    @pytest.mark.parametrize("alias", ["xls", "xlsx", "XLSX"])
    def test_excel_aliases_collapse(self, alias) -> None:
        assert db_utils._normalise_file_type(alias, "/x/data.xlsx") == "excel"

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/x/data.csv", "csv"),
            ("/x/data.parquet", "parquet"),
            ("/x/data.json", "json"),
            ("/x/data.xlsx", "excel"),
            ("/x/data.avro", "avro"),
        ],
    )
    def test_auto_infers_from_the_extension(self, path, expected) -> None:
        assert db_utils._normalise_file_type("auto", path) == expected

    def test_auto_on_an_unknown_extension_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unsupported file type"):
            db_utils._normalise_file_type("auto", "/x/data.txt")

    def test_auto_with_no_extension_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unsupported file type"):
            db_utils._normalise_file_type("auto", "/x/data")

    def test_the_error_names_the_accepted_values(self) -> None:
        with pytest.raises(ValueError, match="Accepted values"):
            db_utils._normalise_file_type("docx", "/x/data.docx")


class TestIsJsonl:
    def test_object_per_line_is_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "d.json"
        path.write_text('{"a": 1}\n{"a": 2}\n')
        assert db_utils._is_jsonl(str(path)) is True

    def test_a_json_array_is_not_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "d.json"
        path.write_text('[\n  {"a": 1}\n]\n')
        assert db_utils._is_jsonl(str(path)) is False

    def test_leading_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "d.json"
        path.write_text('\n\n   \n{"a": 1}\n')
        assert db_utils._is_jsonl(str(path)) is True

    def test_an_empty_file_is_not_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "d.json"
        path.write_text("")
        assert db_utils._is_jsonl(str(path)) is False

    def test_a_missing_file_returns_false_rather_than_raising(self, tmp_path: Path) -> None:
        """OSError is swallowed on purpose — callers treat this as a hint only."""
        assert db_utils._is_jsonl(str(tmp_path / "missing.json")) is False

    def test_undecodable_bytes_are_dropped_rather_than_raising(self, tmp_path: Path) -> None:
        """
        The reader opens with errors="ignore", so a stray BOM or corrupt prefix
        is stripped and detection still succeeds on the remaining text.
        """
        path = tmp_path / "d.json"
        path.write_bytes(b'\xff\xfe{"a": 1}\n')
        assert db_utils._is_jsonl(str(path)) is True


class TestParseAvroSchema:
    def test_none_yields_no_columns(self) -> None:
        assert db_utils._parse_avro_schema(None) == []

    def test_maps_fields_to_column_and_type(self) -> None:
        schema = {
            "type": "record",
            "fields": [
                {"name": "id", "type": "long"},
                {"name": "name", "type": "string"},
            ],
        }
        assert db_utils._parse_avro_schema(schema) == [
            {"column": "id", "type": "long"},
            {"column": "name", "type": "string"},
        ]

    def test_a_union_collapses_to_the_first_non_null_member(self) -> None:
        schema = {"fields": [{"name": "email", "type": ["null", "string"]}]}
        assert db_utils._parse_avro_schema(schema) == [
            {"column": "email", "type": "string"}
        ]

    def test_a_schema_without_fields_yields_no_columns(self) -> None:
        assert db_utils._parse_avro_schema({"type": "record"}) == []
