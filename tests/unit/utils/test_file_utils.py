"""
Tests for app/utils/file_utils.py — filename normalization, versioning,
checksums and the per-owner upload directory layout.

``normalize_filename`` is the path-traversal guard for every uploaded file, so
its behaviour on hostile input is asserted explicitly rather than implied.
Directory tests use the ``upload_root`` fixture, which repoints all three base
paths at tmp_path — without it they would create real directories under the
repo.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.utils import file_utils
from app.utils.file_utils import (
    ACCEPT_ATTRS,
    ALLOWED_EXTENSIONS,
    ALLOWED_IMAGE_EXTENSIONS,
    ALLOWED_KB_EXTENSIONS,
    FILE_BASED_TYPES,
    MAX_IMAGE_SIZE_BYTES,
    MAX_KB_FILE_SIZE_BYTES,
    compute_checksum,
    ensure_knowledge_base_upload_dir,
    ensure_upload_dir,
    ensure_widget_upload_dir,
    normalize_filename,
    versioned_filename,
)


class TestNormalizeFilename:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Sales Data 2024.csv", "sales_data_2024.csv"),
            ("  My File!!.xlsx ", "my_file.xlsx"),
            ("already_fine.csv", "already_fine.csv"),
            ("UPPER.CSV", "upper.csv"),
            ("multi   space.csv", "multi_space.csv"),
            ("dots...everywhere.csv", "dots_everywhere.csv"),
            ("dash--dash.csv", "dash_dash.csv"),
            ("under__score.csv", "under_score.csv"),
        ],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        assert normalize_filename(raw) == expected

    @pytest.mark.parametrize(
        ("hostile", "expected"),
        [
            ("../../etc/passwd", "etcpasswd"),
            ("/absolute/path.csv", "absolutepath.csv"),
            ("..\\..\\windows\\system32", "windowssystem32"),
            ("file;rm -rf /.csv", "filerm_rf_csv"),
            ("nul\x00byte.csv", "nulbyte.csv"),
        ],
    )
    def test_strips_path_separators_and_shell_characters(
        self, hostile: str, expected: str
    ) -> None:
        """The separator characters are removed outright, so the result can
        never escape the directory it is joined onto."""
        assert normalize_filename(hostile) == expected
        assert "/" not in normalize_filename(hostile)
        assert "\\" not in normalize_filename(hostile)

    @pytest.mark.parametrize("empty", ["", "   ", "!!!", "...", "___", "///"])
    def test_falls_back_to_file_when_nothing_survives(self, empty: str) -> None:
        """An all-punctuation name must not normalize to the empty string —
        that would produce a directory path ending in a bare separator."""
        assert normalize_filename(empty) == "file"

    def test_result_is_never_empty(self) -> None:
        assert normalize_filename("@@@@") != ""

    def test_unicode_word_characters_are_kept(self) -> None:
        """\\w is unicode-aware in Python 3, so accented names survive rather
        than being mangled to 'file'."""
        assert normalize_filename("Ünïcodé.csv") == "ünïcodé.csv"


class TestVersionedFilename:
    @pytest.mark.parametrize("version", [1, 0, -5])
    def test_version_one_or_lower_returns_the_base_name(self, version: int) -> None:
        assert versioned_filename("sales_data.csv", version) == "sales_data.csv"

    @pytest.mark.parametrize(
        ("base", "version", "expected"),
        [
            ("sales_data.csv", 2, "sales_data_v2.csv"),
            ("sales_data.csv", 3, "sales_data_v3.csv"),
            ("report.xlsx", 10, "report_v10.xlsx"),
            ("noextension", 2, "noextension_v2"),
            ("archive.tar.gz", 2, "archive.tar_v2.gz"),
        ],
    )
    def test_inserts_the_version_before_the_extension(
        self, base: str, version: int, expected: str
    ) -> None:
        assert versioned_filename(base, version) == expected

    def test_versions_do_not_collide(self) -> None:
        names = {versioned_filename("data.csv", v) for v in range(1, 6)}
        assert len(names) == 5


class TestComputeChecksum:
    async def test_matches_hashlib(self, tmp_path: Path) -> None:
        path = tmp_path / "data.csv"
        payload = b"id,name\n1,Widget\n"
        path.write_bytes(payload)

        assert await compute_checksum(path) == hashlib.sha256(payload).hexdigest()

    async def test_empty_file_hashes_to_the_empty_digest(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.csv"
        path.write_bytes(b"")

        assert await compute_checksum(path) == hashlib.sha256(b"").hexdigest()

    async def test_reads_files_larger_than_one_chunk(self, tmp_path: Path) -> None:
        """The 64 KiB read loop is only exercised by a file bigger than the
        buffer — a smaller file would pass even if the loop were broken."""
        path = tmp_path / "big.bin"
        payload = b"x" * (65536 * 3 + 17)
        path.write_bytes(payload)

        assert await compute_checksum(path) == hashlib.sha256(payload).hexdigest()

    async def test_different_content_gives_a_different_digest(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        a.write_bytes(b"one")
        b.write_bytes(b"two")

        assert await compute_checksum(a) != await compute_checksum(b)

    async def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            await compute_checksum(tmp_path / "absent.csv")


class TestUploadDirectories:
    def test_ensure_upload_dir_creates_a_per_datasource_folder(
        self, upload_root: Path
    ) -> None:
        path = ensure_upload_dir("abc-123")

        assert path.is_dir()
        assert path == file_utils.UPLOAD_BASE / "abc-123"

    def test_ensure_widget_upload_dir_creates_a_per_widget_folder(
        self, upload_root: Path
    ) -> None:
        path = ensure_widget_upload_dir("widget-1")

        assert path.is_dir()
        assert path == file_utils.WIDGET_UPLOAD_BASE / "widget-1"

    def test_ensure_knowledge_base_upload_dir_creates_a_per_kb_folder(
        self, upload_root: Path
    ) -> None:
        path = ensure_knowledge_base_upload_dir("kb-1")

        assert path.is_dir()
        assert path == file_utils.KNOWLEDGE_BASE_UPLOAD_BASE / "kb-1"

    @pytest.mark.parametrize(
        "factory",
        [ensure_upload_dir, ensure_widget_upload_dir, ensure_knowledge_base_upload_dir],
    )
    def test_is_idempotent(self, factory, upload_root: Path) -> None:  # noqa: ANN001
        """exist_ok=True — a second upload for the same owner must not raise."""
        first = factory("same-id")
        second = factory("same-id")

        assert first == second
        assert second.is_dir()

    @pytest.mark.parametrize(
        "factory",
        [ensure_upload_dir, ensure_widget_upload_dir, ensure_knowledge_base_upload_dir],
    )
    def test_accepts_a_uuid_object(self, factory, upload_root: Path) -> None:  # noqa: ANN001
        """Callers pass the model's ``uuid`` attribute, which is a UUID object,
        not a string — str() inside the helper is what makes that work."""
        import uuid as uuid_pkg

        identifier = uuid_pkg.uuid4()
        path = factory(identifier)

        assert path.name == str(identifier)
        assert path.is_dir()

    def test_creates_missing_parent_directories(self, upload_root: Path) -> None:
        """parents=True — the base directory does not exist on a fresh
        deployment, so the first upload has to create the whole chain."""
        assert not file_utils.KNOWLEDGE_BASE_UPLOAD_BASE.exists()

        ensure_knowledge_base_upload_dir("kb-9")

        assert file_utils.KNOWLEDGE_BASE_UPLOAD_BASE.is_dir()


class TestConstants:
    def test_every_file_based_type_has_extensions_and_an_accept_attr(self) -> None:
        """These three tables are indexed by the same db_type key from the
        upload form; a type missing from any one of them breaks the form."""
        assert set(ALLOWED_EXTENSIONS) == FILE_BASED_TYPES
        assert set(ACCEPT_ATTRS) == FILE_BASED_TYPES

    def test_accept_attrs_agree_with_allowed_extensions(self) -> None:
        for db_type, accept in ACCEPT_ATTRS.items():
            declared = {part.lstrip(".") for part in accept.split(",")}
            assert declared == ALLOWED_EXTENSIONS[db_type]

    def test_extensions_are_lowercase_and_dotless(self) -> None:
        every = (
            set().union(*ALLOWED_EXTENSIONS.values())
            | set(ALLOWED_IMAGE_EXTENSIONS)
            | set(ALLOWED_KB_EXTENSIONS)
        )
        assert all(e == e.lower() and not e.startswith(".") for e in every)

    def test_size_limits_are_positive(self) -> None:
        assert MAX_IMAGE_SIZE_BYTES == 2 * 1024 * 1024
        assert MAX_KB_FILE_SIZE_BYTES == 10 * 1024 * 1024
