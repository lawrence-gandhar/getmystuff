"""
Tests for app/schemas/downloader_agents/downloader_agent_schemas.py.

The tool-argument schemas carry most of the weight here, because their input comes from a
language model rather than from a form. So the cases are the ones a model actually
produces: an empty object, "latest", "xlsx" when it means "xls", "excel", and a format that
does not exist. The last one must be refused **by name** — silently writing a CSV for
someone who asked for Parquet is worse than saying no.

``PublicDownloadQuery`` is the only request schema on an unauthenticated route, and the
thing worth asserting about it is that a missing token is not silently treated as an empty
one: the route requires both, and a schema that defaulted them to "" would push that
decision somewhere less obvious.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from litestar.exceptions import HTTPException

from app.models.downloader_agents import EXPORT_READY
from app.schemas.downloader_agents import (
    ConfirmDownloadArgs,
    DownloadExportView,
    DownloadProgressEvent,
    DownloadStatusArgs,
    PublicDownloadQuery,
)


# ---- Tool arguments ----

class TestConfirmDownloadArgs:
    def test_an_empty_object_is_a_valid_bare_yes(self) -> None:
        """
        The common case: the user said "yes" and the model called the tool with nothing.
        """
        args = ConfirmDownloadArgs.parse({})

        assert args.export_id is None
        assert args.file_format == "csv"
        assert args.wants_latest() is True

    @pytest.mark.parametrize(
        "token", ["", "latest", "LATEST", " last ", "previous", "recent", "none"],
    )
    def test_the_stand_ins_for_the_last_offer_are_recognised(self, token: str) -> None:
        """
        A model that did not keep the id says so in words. Refusing those would turn a
        working "yes" into an apology.
        """
        assert ConfirmDownloadArgs.parse({"export_id": token}).wants_latest() is True

    def test_a_real_uuid_is_not_a_stand_in(self) -> None:
        args = ConfirmDownloadArgs.parse({"export_id": str(uuid_pkg.uuid4())})

        assert args.wants_latest() is False

    @pytest.mark.parametrize(
        ("given", "stored"),
        [
            ("csv", "csv"),
            ("CSV", "csv"),
            (".csv", "csv"),
            ("xls", "xls"),
            ("xlsx", "xls"),
            ("excel", "xls"),
            ("Excel", "xls"),
            ("spreadsheet", "xls"),
            ("sheet", "xls"),
            ("parquet", "parquet"),
            ("pq", "parquet"),
            ("txt", "csv"),
        ],
    )
    def test_the_formats_a_model_proposes_map_to_the_three_we_write(
        self, given: str, stored: str,
    ) -> None:
        assert ConfirmDownloadArgs.parse({"file_format": given}).file_format == stored

    @pytest.mark.parametrize("given", ["pdf", "docx", "json", "avro", "zip"])
    def test_a_format_we_cannot_write_is_refused_by_name(self, given: str) -> None:
        """
        Named in the refusal, with the alternatives, because the model has to choose again
        — and because writing a CSV for someone who asked for Parquet is worse than a no.
        """
        with pytest.raises(HTTPException) as excinfo:
            ConfirmDownloadArgs.parse({"file_format": given})

        assert excinfo.value.status_code == 400
        assert given in str(excinfo.value.detail)
        assert "csv" in str(excinfo.value.detail)

    def test_an_over_long_export_id_is_refused(self) -> None:
        """A model that pasted a paragraph into the field is not passing an id."""
        with pytest.raises(HTTPException):
            ConfirmDownloadArgs.parse({"export_id": "x" * 200})


class TestDownloadStatusArgs:
    def test_an_empty_object_asks_about_the_latest(self) -> None:
        assert DownloadStatusArgs.parse({}).wants_latest() is True

    def test_a_uuid_is_carried_through(self) -> None:
        export_id = str(uuid_pkg.uuid4())

        assert DownloadStatusArgs.parse({"export_id": export_id}).export_id == export_id


# ---- Requests ----

class TestPublicDownloadQuery:
    def test_absent_fields_are_none_not_empty(self) -> None:
        """
        The route refuses unless both are present, and that check reads clearly only if
        "missing" is distinguishable from "blank".
        """
        query = PublicDownloadQuery.parse({})

        assert query.key is None
        assert query.session_token is None

    def test_blank_fields_are_also_none(self) -> None:
        query = PublicDownloadQuery.parse({"key": "  ", "session_token": ""})

        assert query.key is None
        assert query.session_token is None


# ---- Responses ----

class TestDownloadExportView:
    def _export(self, **overrides):  # noqa: ANN001, ANN202
        base = {
            "uuid": uuid_pkg.uuid4(),
            "status": EXPORT_READY,
            "file_format": "csv",
            "file_name": "items_2026-08-06.csv",
            "total_rows": 4821,
            "count_is_lower_bound": False,
            "part_count": 97,
            "rows_written": 4821,
            "byte_size": 120_000,
            "error_message": None,
            "created_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc),
            "id": 42,
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_it_never_exposes_the_bigint_id(self) -> None:
        """
        The house rule, asserted rather than assumed: a response names a row by its uuid.
        """
        payload = DownloadExportView.of(self._export(), "/downloads/x").payload()

        assert "id" not in payload
        assert "uuid" in payload

    def test_the_url_is_carried_rather_than_derived(self) -> None:
        """
        The same export is reachable at two prefixes depending on who is asking, so the
        caller supplies the link and this schema only holds it.
        """
        payload = DownloadExportView.of(self._export(), "/public/downloads/x?key=1")

        assert payload.download_url == "/public/downloads/x?key=1"

    def test_no_url_for_an_export_that_is_not_ready(self) -> None:
        payload = DownloadExportView.of(self._export(status="building"), None)

        assert payload.download_url is None


class TestDownloadProgressEvent:
    def test_the_event_names_are_the_ones_the_stream_emits(self) -> None:
        assert (
            DownloadProgressEvent.PROGRESS,
            DownloadProgressEvent.RETRY,
            DownloadProgressEvent.READY,
            DownloadProgressEvent.FAILED,
        ) == ("progress", "retry", "ready", "failed")

    def test_a_progress_frame_serialises_flat(self) -> None:
        """
        One of these is emitted per completed batch, so the payload has to stay small and
        JSON-ready without further work at the route.
        """
        payload = DownloadProgressEvent.build(
            {
                "event": "progress",
                "export_id": uuid_pkg.uuid4(),
                "status": "building",
                "part": 12,
                "of": 97,
                "attempt": 1,
                "rows_written": 600,
                "total_rows": 4821,
            }
        ).payload()

        assert payload["event"] == "progress"
        assert payload["part"] == 12
        assert isinstance(payload["export_id"], str)
        assert payload["message"] is None
