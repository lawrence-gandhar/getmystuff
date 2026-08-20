"""
Tests for ``engine/flow_state.py``.

The headline assertion is the first class: **``_accumulate`` sums.** The bug it prevents
is a fifty-thousand-record sync reporting five hundred, which would look entirely
plausible in the dock and be contradicted by nothing. It is simulated here over a
hundred passes because that is what the bug needs to appear — a two-pass test passes
under a last-wins merge too, since with two writes of the same size the wrong answer and
the right one differ by a factor no one notices.

The second is redaction. This module previews webhook bodies and third-party API
responses, so a bearer token in an ``Authorization`` header is the ordinary case rather
than a contrived one, and redaction happening at write time is what makes it a property
of the stored row instead of whichever template renders it.
"""

from __future__ import annotations

import json

import pytest

from app.services.integrations.engine.flow_state import (
    MAX_PREVIEW_ITEMS,
    REDACTED,
    _accumulate,
    _merge,
    delta,
    initial_state,
    preview_of,
    redact,
    total,
    totals,
)


class TestAccumulateSums:
    """The 500-versus-50,000 bug, stated four ways."""

    def test_a_hundred_passes_add_up(self) -> None:
        state = {}
        for _ in range(100):
            state = _accumulate(state, delta("write-1", written=500))

        assert state == {"write-1": {"written": 50_000}}

    def test_a_last_wins_merge_would_fail_this(self) -> None:
        """
        The control. ``_merge`` is the correct reducer for ``outputs`` and the wrong one
        for ``counts``; pinning the difference stops somebody "simplifying" the two into
        one.
        """
        merged = _merge({"write-1": {"written": 49_500}}, delta("write-1", written=500))

        assert merged == {"write-1": {"written": 500}}

    def test_metrics_accumulate_independently(self) -> None:
        state = _accumulate({}, delta("write-1", written=10, failed=1))
        state = _accumulate(state, delta("write-1", written=10, skipped=2))

        assert state["write-1"] == {"written": 20, "failed": 1, "skipped": 2}

    def test_nodes_accumulate_independently(self) -> None:
        state = _accumulate({}, delta("read-1", read=500))
        state = _accumulate(state, delta("write-1", written=500))
        state = _accumulate(state, delta("read-1", read=500))

        assert state == {"read-1": {"read": 1000}, "write-1": {"written": 500}}


class TestAccumulateIsUnbreakable:
    """
    It runs inside LangGraph's own reduction, where an exception surfaces as a graph
    execution error with no attribution. Losing a counter is bad; losing the run to
    bookkeeping is worse.
    """

    @pytest.mark.parametrize("junk", ["ten", None, [1], {"a": 1}])
    def test_a_non_numeric_delta_counts_as_nothing(self, junk: object) -> None:
        state = _accumulate({"n": {"written": 5}}, {"n": {"written": junk}})

        assert state == {"n": {"written": 5}}

    def test_a_boolean_is_not_one(self) -> None:
        """``bool`` is a subclass of ``int``, so ``True`` would otherwise add 1."""
        assert _accumulate({}, {"n": {"written": True}}) == {"n": {"written": 0}}

    def test_a_float_that_is_whole_counts(self) -> None:
        assert _accumulate({}, {"n": {"written": 3.0}}) == {"n": {"written": 3}}

    @pytest.mark.parametrize("empty", [None, {}])
    def test_an_empty_side_is_fine(self, empty: object) -> None:
        assert _accumulate(empty, {"n": {"x": 1}}) == {"n": {"x": 1}}
        assert _accumulate({"n": {"x": 1}}, empty) == {"n": {"x": 1}}

    def test_the_left_side_is_not_mutated(self) -> None:
        """LangGraph may hold a reference to the previous state; editing it in place
        would corrupt a checkpoint that has already been written."""
        left = {"n": {"written": 5}}
        _accumulate(left, {"n": {"written": 5}})

        assert left == {"n": {"written": 5}}


class TestTotals:
    def test_one_metric_across_every_node(self) -> None:
        state = {"counts": {"w1": {"written": 100}, "w2": {"written": 250}}}

        assert total(state, "written") == 350

    def test_every_metric_at_once(self) -> None:
        state = {
            "counts": {
                "r1": {"read": 500},
                "w1": {"written": 480, "failed": 20},
                "w2": {"written": 500},
            }
        }

        assert totals(state) == {"read": 500, "written": 980, "failed": 20}

    def test_an_empty_state_totals_zero_rather_than_raising(self) -> None:
        assert total({}, "written") == 0
        assert totals(None) == {}


class TestInitialState:
    def test_every_merged_channel_starts_present_and_empty(self) -> None:
        """
        A reducer that has to cope with ``None`` on its left is a reducer with a branch
        nobody exercises until production.
        """
        state = initial_state(run_id="r1", version_hash="abc")

        for channel in ("outputs", "batches", "counts", "errors"):
            assert state[channel] == {}

    def test_the_run_starts_uncancelled_and_unfailed(self) -> None:
        state = initial_state(run_id="r1", version_hash="abc")

        assert state["cancelled"] is False
        assert state["failed_at"] == ""
        assert state["dry_run"] is False


class TestRedact:
    @pytest.mark.parametrize(
        "key",
        [
            "authorization",
            "Authorization",
            "X-API-Key",
            "api_key",
            "refresh_token",
            "access_token",
            "client_secret",
            "password",
            "Cookie",
            "card_number",
            "cvv",
        ],
    )
    def test_a_sensitive_key_loses_its_value(self, key: str) -> None:
        assert redact({key: "hunter2"})[key] == REDACTED

    def test_it_reaches_into_nesting(self) -> None:
        body = {
            "request": {"headers": {"Authorization": "Bearer sk-live-123"}},
            "records": [{"email": "a@b.com", "api_key": "sk-1"}],
        }

        cleaned = redact(body)

        assert cleaned["request"]["headers"]["Authorization"] == REDACTED
        assert cleaned["records"][0]["api_key"] == REDACTED
        assert cleaned["records"][0]["email"] == "a@b.com"

    def test_the_structure_survives(self) -> None:
        """
        The key stays. Somebody debugging a mapping needs to see that the field was
        sent at all, which is exactly the question a preview answers.
        """
        cleaned = redact({"token": "x", "id": 7})

        assert set(cleaned) == {"token", "id"}

    def test_a_flows_own_field_names_are_honoured(self) -> None:
        """
        No general pattern could guess that this customer's ``national_id`` is
        sensitive, which is what ``redacted_fields`` is for.
        """
        cleaned = redact({"national_id": "AB123456C", "city": "Leeds"}, ["national_id"])

        assert cleaned == {"national_id": REDACTED, "city": "Leeds"}

    def test_an_ordinary_field_is_untouched(self) -> None:
        assert redact({"email": "a@b.com", "total": 12.5}) == {
            "email": "a@b.com",
            "total": 12.5,
        }

    def test_very_deep_nesting_stops_rather_than_recursing_forever(self) -> None:
        value: dict = {"a": 1}
        for _ in range(50):
            value = {"nested": value}

        redact(value)  # does not raise


class TestPreviewOf:
    def test_the_count_is_the_real_count(self) -> None:
        """
        The assertion that matters most here. A preview reporting five when fifty
        thousand moved would be a log actively lying about the volume.
        """
        preview = preview_of([{"i": i} for i in range(50_000)])

        assert preview["count"] == 50_000
        assert len(preview["sample"]) == MAX_PREVIEW_ITEMS
        assert preview["truncated"] is True

    def test_a_short_list_is_not_truncated(self) -> None:
        preview = preview_of([{"i": 1}, {"i": 2}])

        assert preview == {"sample": [{"i": 1}, {"i": 2}], "count": 2, "truncated": False}

    def test_redaction_happens_before_truncation(self) -> None:
        """
        Order matters: redacting after the cut would leave whatever fell outside the
        sample un-redacted in the intermediate value, and a later change that widened
        the sample would silently start leaking.
        """
        records = [{"api_key": f"sk-{i}"} for i in range(100)]

        preview = preview_of(records)

        assert all(item["api_key"] == REDACTED for item in preview["sample"])
        assert "sk-" not in json.dumps(preview)

    def test_a_few_enormous_records_are_shrunk_by_size_not_by_count(self) -> None:
        """
        One record with a large text field can outweigh fifty small ones, so an item
        count is not a size.
        """
        records = [{"body": "x" * 5000} for _ in range(3)]

        preview = preview_of(records)

        assert preview["truncated"] is True
        assert len(json.dumps(preview["sample"])) <= 4096 or isinstance(
            preview["sample"], str
        )

    def test_a_scalar_previews_as_itself(self) -> None:
        preview = preview_of({"status": "ok"})

        assert preview == {"sample": {"status": "ok"}, "count": 1, "truncated": False}

    def test_something_unserialisable_does_not_fail_the_node(self) -> None:
        """
        Failing to *measure* a value is not a reason to fail the node that produced it —
        a preview is a convenience and must never be load-bearing.
        """
        class Opaque:
            pass

        preview_of([{"thing": Opaque()}])  # does not raise


class TestDelta:
    def test_it_builds_a_counts_fragment(self) -> None:
        assert delta("w1", written=5, failed=1) == {"w1": {"written": 5, "failed": 1}}

    def test_it_says_delta_out_loud(self) -> None:
        """
        The point of the helper. ``{"counts": {node: {"written": n}}}`` reads equally
        like a total at a call site, and the contract this whole module rests on is
        that it is not one.
        """
        assert delta.__doc__ and "delta" in delta.__doc__.lower()
