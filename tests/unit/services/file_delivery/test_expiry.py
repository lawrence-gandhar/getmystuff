"""
Tests for the expiry half of app/services/file_delivery/file_service.py.

Two rules, and they are separate on purpose:

* the **sweep** deletes bytes and marks the row ``expired``, keeping the row so a visitor
  coming back to a dead link can be told the file has expired rather than that it "could
  not be found" — which reads as though the application lost it;
* the **check** refuses a lapsed file whether or not the sweep has been round, which is
  what makes the window real in the minutes between sweeps.

``ttl_phrase`` is asserted because it is derived rather than written down: a hard-coded "24
hours" in a help page goes on saying so after somebody changes the TTL, which is the
mistake ``download_service.ttl_phrase`` exists to prevent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models.file_delivery import FILE_EXPIRED, FILE_READY, ORIGIN_GRAPH
from app.services.file_delivery import file_service
from app.services.file_delivery.row_source import Payload


@pytest.fixture
async def make_file(db, user, upload_root: Path):  # noqa: ANN001, ANN201
    """One ready file with real bytes behind it, owned by ``user``."""

    async def _make(**overrides):  # noqa: ANN202
        record = await file_service.create_file(
            db,
            user_id=user.id,
            payload=Payload(rows=[{"a": 1}, {"a": 2}]),
            file_format="csv",
            name_stem=overrides.pop("name_stem", "orders"),
            origin=ORIGIN_GRAPH,
        )

        if overrides:
            for key, value in overrides.items():
                setattr(record, key, value)
            await db.commit()
            await db.refresh(record)

        return record

    return _make


class TestTheWindow:
    async def test_a_new_file_is_inside_its_window(self, make_file) -> None:  # noqa: ANN001
        record = await make_file()

        assert record.status == FILE_READY
        assert not file_service.is_expired(record)

    async def test_a_naive_timestamp_is_read_as_utc_not_raised_on(
        self, make_file,  # noqa: ANN001
    ) -> None:
        """
        A column that lost its timezone must not make the comparison raise — this check is
        what stands between a lapsed link and somebody's data.
        """
        record = await make_file()
        record.expires_at = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).replace(tzinfo=None)

        assert file_service.is_expired(record) is True

    async def test_a_file_with_no_window_never_expires(self, make_file) -> None:  # noqa: ANN001
        record = await make_file(expires_at=None)

        assert file_service.is_expired(record) is False


class TestTheSweep:
    async def test_it_deletes_the_bytes_and_keeps_the_row(
        self, db, make_file,  # noqa: ANN001
    ) -> None:
        record = await make_file(
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        path = Path(record.file_path)
        assert path.is_file()

        assert await file_service.expire_lapsed_files(db) == 1

        await db.refresh(record)
        assert record.status == FILE_EXPIRED
        assert not path.exists()
        assert not path.parent.exists(), "the whole directory goes, not just the file"

    async def test_it_leaves_a_live_file_alone(self, db, make_file) -> None:  # noqa: ANN001
        record = await make_file()

        assert await file_service.expire_lapsed_files(db) == 0

        await db.refresh(record)
        assert record.status == FILE_READY
        assert Path(record.file_path).is_file()

    async def test_a_second_sweep_does_not_re_expire(self, db, make_file) -> None:  # noqa: ANN001
        """The query filters on ``ready``, so an already-swept row is not picked up again."""
        await make_file(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))

        assert await file_service.expire_lapsed_files(db) == 1
        assert await file_service.expire_lapsed_files(db) == 0


class TestTheCheck:
    async def test_a_lapsed_file_is_refused_while_its_bytes_are_still_there(
        self, make_file,  # noqa: ANN001
    ) -> None:
        """
        The sweep deletes bytes; this is what enforces the rule. Between sweeps the file is
        still on disk and must not be served.
        """
        from litestar.exceptions import HTTPException

        record = await make_file(
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        assert Path(record.file_path).is_file()

        with pytest.raises(HTTPException) as raised:
            file_service.assert_servable(record)

        assert raised.value.status_code == 410
        assert raised.value.detail == file_service.EXPIRED_MESSAGE

    async def test_a_row_whose_file_is_missing_is_a_404_not_a_410(
        self, make_file,  # noqa: ANN001
    ) -> None:
        """Different situations for the person holding the link, so different sentences."""
        from litestar.exceptions import HTTPException

        record = await make_file()
        Path(record.file_path).unlink()

        with pytest.raises(HTTPException) as raised:
            file_service.assert_servable(record)

        assert raised.value.status_code == 404
        assert raised.value.detail == file_service.NOT_FOUND_MESSAGE

    async def test_a_live_file_yields_its_path(self, make_file) -> None:  # noqa: ANN001
        record = await make_file()

        assert file_service.assert_servable(record) == Path(record.file_path)


class TestTtlPhrase:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (60, "1 minute"),
            (1800, "30 minutes"),
            (3600, "1 hour"),
            (7200, "2 hours"),
            (86400, "1 day"),
            (172800, "2 days"),
        ],
    )
    def test_it_follows_the_configured_ttl(
        self, monkeypatch: pytest.MonkeyPatch, seconds: int, expected: str,
    ) -> None:
        monkeypatch.setattr(file_service, "FILE_TTL_SECONDS", seconds)

        assert file_service.ttl_phrase() == expected

    def test_the_default_is_a_day(self) -> None:
        """
        Deliberately longer than the export queue's thirty minutes: a file an operator
        built into a flow and offered as a button is a deliverable, not a sample somebody
        can trivially ask for again.
        """
        assert file_service.FILE_TTL_SECONDS == 24 * 3600


class TestNaming:
    @pytest.mark.parametrize(
        ("stem", "file_format", "expected"),
        [
            ("orders", "csv", "orders.csv"),
            ("Sales Data 2026", "xlsx", "sales_data_2026.xlsx"),
            ("../../etc/passwd", "csv", "etcpasswd.csv"),
            ("", "parquet", "file.parquet"),
            ("   ", "txt", "file.txt"),
            # The extension is not doubled when the operator typed it.
            ("orders.csv", "csv", "orders.csv"),
            # …and a leftover from changing the format is replaced rather than kept, so
            # the name never carries a format that is not this file's.
            ("orders.csv", "parquet", "orders.parquet"),
        ],
    )
    def test_the_name_is_normalised_and_the_extension_comes_from_the_format(
        self, stem: str, file_format: str, expected: str,
    ) -> None:
        assert file_service.artifact_name(stem, file_format) == expected

    def test_the_directory_is_named_by_the_files_own_uuid(self, make_file) -> None:  # noqa: ANN001
        """
        Never from anything a visitor, an operator or a model supplied — the path is built
        here, and the operator's name only ever reaches the *filename*.
        """
        import uuid as uuid_pkg

        file_uuid = uuid_pkg.uuid4()

        assert file_service.file_dir(file_uuid).name == str(file_uuid)
