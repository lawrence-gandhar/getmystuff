"""
Tests for app/utils/csv_to_parquet.py — streaming CSV to Parquet conversion.

This module was invisible to coverage before these tests existed: nothing in the
application imports it, and coverage cannot discover never-imported files inside
app/utils (no __init__.py, so its source scan skips the directory). It was
neither measured nor reported — see documentations/TESTING.md.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from app.utils.csv_to_parquet import csv_to_parquet_stream


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    path = tmp_path / "products.csv"
    path.write_text("id,name,price\n1,Widget,9.99\n2,Gadget,19.50\n")
    return path


class TestConversion:
    def test_writes_a_readable_parquet_file(self, tmp_path: Path, csv_file: Path) -> None:
        target = tmp_path / "products.parquet"
        csv_to_parquet_stream(csv_file, target)

        assert target.is_file()
        table = pq.read_table(target)
        assert table.num_rows == 2
        assert table.column_names == ["id", "name", "price"]

    def test_values_survive_the_round_trip(self, tmp_path: Path, csv_file: Path) -> None:
        target = tmp_path / "products.parquet"
        csv_to_parquet_stream(csv_file, target)

        rows = pq.read_table(target).to_pylist()
        assert rows == [
            {"id": 1, "name": "Widget", "price": 9.99},
            {"id": 2, "name": "Gadget", "price": 19.50},
        ]

    def test_types_are_inferred_not_stringified(self, tmp_path: Path, csv_file: Path) -> None:
        target = tmp_path / "products.parquet"
        csv_to_parquet_stream(csv_file, target)

        schema = pq.read_table(target).schema
        assert schema.field("id").type == "int64"
        assert schema.field("price").type == "double"

    def test_a_header_only_csv_produces_an_empty_table(self, tmp_path: Path) -> None:
        source = tmp_path / "empty.csv"
        source.write_text("id,name\n")
        target = tmp_path / "empty.parquet"

        csv_to_parquet_stream(source, target)

        table = pq.read_table(target)
        assert table.num_rows == 0
        assert table.column_names == ["id", "name"]

    def test_accepts_path_objects_and_strings(self, tmp_path: Path, csv_file: Path) -> None:
        target = tmp_path / "as_str.parquet"
        csv_to_parquet_stream(str(csv_file), str(target))
        assert target.is_file()

    def test_a_larger_file_streams_every_row(self, tmp_path: Path) -> None:
        """The point of the module is constant memory over many batches."""
        source = tmp_path / "big.csv"
        rows = "\n".join(f"{i},name{i}" for i in range(5000))
        source.write_text(f"id,name\n{rows}\n")
        target = tmp_path / "big.parquet"

        csv_to_parquet_stream(source, target)

        assert pq.read_table(target).num_rows == 5000


class TestValidation:
    @pytest.mark.parametrize("empty", ["", None])
    def test_a_blank_source_path_is_rejected(self, tmp_path: Path, empty) -> None:
        with pytest.raises(ValueError, match="csv_path cannot be empty"):
            csv_to_parquet_stream(empty, tmp_path / "out.parquet")

    @pytest.mark.parametrize("empty", ["", None])
    def test_a_blank_target_path_is_rejected(self, csv_file: Path, empty) -> None:
        with pytest.raises(ValueError, match="parquet_path cannot be empty"):
            csv_to_parquet_stream(csv_file, empty)

    def test_a_missing_source_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="CSV file not found"):
            csv_to_parquet_stream(tmp_path / "nope.csv", tmp_path / "out.parquet")

    def test_validation_errors_are_not_wrapped_in_runtimeerror(self, tmp_path: Path) -> None:
        """
        FileNotFoundError and ValueError are re-raised as themselves so a caller
        can tell a bad argument from a genuine conversion failure.
        """
        with pytest.raises(FileNotFoundError):
            csv_to_parquet_stream(tmp_path / "nope.csv", tmp_path / "out.parquet")

    def test_an_unreadable_source_becomes_a_runtimeerror(self, tmp_path: Path) -> None:
        """A file that exists but is not CSV fails during the read, not the check."""
        source = tmp_path / "binary.csv"
        source.write_bytes(b"\x00\x01\x02\x03")

        with pytest.raises(RuntimeError) as exc:
            csv_to_parquet_stream(source, tmp_path / "out.parquet")

        # The specific "Unable to read CSV file: ..." message is raised inside
        # the function but never reaches the caller — see the test below.
        assert "Unable to read CSV file" in str(exc.value.__cause__)

    def test_an_unwritable_target_becomes_a_runtimeerror(self, csv_file: Path) -> None:
        with pytest.raises(RuntimeError) as exc:
            csv_to_parquet_stream(csv_file, "/nonexistent-dir/out.parquet")

        assert "Error occurred while writing Parquet file" in str(exc.value.__cause__)

    @pytest.mark.parametrize(
        "scenario",
        ["unreadable-source", "unwritable-target"],
    )
    def test_the_specific_failure_message_never_reaches_the_caller(
        self, tmp_path: Path, csv_file: Path, scenario
    ) -> None:
        """
        Documents a real defect rather than the intended design.

        The function raises a precise, actionable RuntimeError for each failure
        mode ("Unable to read CSV file: <path>", "Error occurred while writing
        Parquet file: <path>"), but the outer ``except Exception`` catches its
        own RuntimeError and re-wraps it in the generic "Unexpected failure
        occurred during CSV to Parquet conversion." So the caller — and any user
        shown the message — never learns which end failed or which file. Only
        FileNotFoundError and ValueError are allowed through unwrapped.

        Fixing it means adding RuntimeError to the pass-through clause. Until
        then this test pins the behaviour so the swallowing is visible.
        """
        if scenario == "unreadable-source":
            source = tmp_path / "binary.csv"
            source.write_bytes(b"\x00\x01\x02\x03")
            args = (source, tmp_path / "out.parquet")
        else:
            args = (csv_file, "/nonexistent-dir/out.parquet")

        with pytest.raises(RuntimeError) as exc:
            csv_to_parquet_stream(*args)

        assert str(exc.value) == (
            "Unexpected failure occurred during CSV to Parquet conversion."
        )
