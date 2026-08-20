"""
Tests for ``engine/record_buffer.py``.

Two properties, and both are about a failure that would otherwise be silent.

**A missing batch raises.** Returning an empty list would let a run report success
having moved 49,500 of 50,000 records, with nothing in the log to say so. That is the
exact outcome the three-level failure model exists to prevent, and it would be undone
here by a single ``dict.get(key, [])``.

**A run leaves nothing behind.** This is process memory, so a leak is an
out-of-memory kill with no explanation attached. The suite asserts it per test rather
than discovering it in aggregate.
"""

from __future__ import annotations

import pytest

from app.services.integrations.engine import record_buffer
from app.services.integrations.errors import IntegrationFailure


@pytest.fixture(autouse=True)
def empty_buffer():
    """
    The registry is module-level, so a test that left something behind would change the
    next one's answer. Cleared on both sides: before, so a leak from elsewhere does not
    fail these; after, so these do not leak into anything else.
    """
    record_buffer.clear_all()
    yield
    record_buffer.clear_all()


class TestPutAndTake:
    def test_a_batch_comes_back_as_it_went_in(self) -> None:
        records = [{"id": 1}, {"id": 2}]
        handle = record_buffer.put("run-1:read-1:b0", records)

        assert record_buffer.take(handle["key"]) == records

    def test_the_handle_carries_the_count(self) -> None:
        """
        So a downstream node can branch on how many records this is without touching
        the buffer — and so the run's final state still says how much moved after the
        buffer has been released.
        """
        handle = record_buffer.put("run-1:read-1:b0", [{"id": i} for i in range(500)])

        assert handle == {"kind": "recordset", "key": "run-1:read-1:b0", "count": 500}

    def test_taking_removes_it(self) -> None:
        """
        A hundred-pass loop that left every batch behind would hold a hundred batches.
        """
        record_buffer.put("run-1:read-1:b0", [{"id": 1}])
        record_buffer.take("run-1:read-1:b0")

        assert record_buffer.open_keys() == []

    def test_peeking_does_not(self) -> None:
        record_buffer.put("run-1:read-1:b0", [{"id": 1}])
        record_buffer.peek("run-1:read-1:b0")

        assert record_buffer.open_keys() == ["run-1:read-1:b0"]

    def test_an_empty_batch_is_a_real_batch(self) -> None:
        """
        "This page returned nothing" is a fact about the source, not a missing key —
        and conflating the two is how a loop fails to notice it has finished.
        """
        handle = record_buffer.put("run-1:read-1:b7", [])

        assert handle["count"] == 0
        assert record_buffer.take("run-1:read-1:b7") == []


class TestAMissingBatchRaises:
    """The negative assertion this module exists for. See the module docstring."""

    def test_take_refuses_rather_than_returning_nothing(self) -> None:
        with pytest.raises(IntegrationFailure, match="no longer available"):
            record_buffer.take("run-1:read-1:b0")

    def test_peek_refuses_too(self) -> None:
        with pytest.raises(IntegrationFailure, match="no longer available"):
            record_buffer.peek("run-1:read-1:b0")

    def test_taking_twice_refuses_the_second_time(self) -> None:
        record_buffer.put("run-1:read-1:b0", [{"id": 1}])
        record_buffer.take("run-1:read-1:b0")

        with pytest.raises(IntegrationFailure):
            record_buffer.take("run-1:read-1:b0")

    def test_the_message_tells_the_operator_what_to_do(self) -> None:
        """
        A user reading this has a run that cannot continue. "KeyError" is not an
        instruction; "start it again" is.
        """
        with pytest.raises(IntegrationFailure) as caught:
            record_buffer.take("run-1:missing")

        assert "start it again" in str(caught.value)


class TestReleaseRun:
    def test_it_drops_everything_that_run_stashed(self) -> None:
        for index in range(3):
            record_buffer.put(f"run-1:read-1:b{index}", [{"i": index}])

        assert record_buffer.release_run("run-1") == 3
        assert record_buffer.open_keys() == []

    def test_it_leaves_another_run_alone(self) -> None:
        """
        Two runs execute concurrently by design — the worker's concurrency is 2 — so a
        release that reached across runs would delete live data.
        """
        record_buffer.put("run-1:read-1:b0", [{"i": 1}])
        record_buffer.put("run-2:read-1:b0", [{"i": 2}])

        record_buffer.release_run("run-1")

        assert record_buffer.open_keys() == ["run-2:read-1:b0"]

    def test_releasing_a_run_that_stashed_nothing_is_fine(self) -> None:
        """
        Called from a ``finally``, so it must never raise: a failure to clean up must
        not replace the failure actually being reported.
        """
        assert record_buffer.release_run("never-existed") == 0

    def test_a_run_id_that_prefixes_another_does_not_take_it(self) -> None:
        """
        ``run-1`` must not release ``run-10``. The colon in the key is what makes that
        true, and it is worth a test because run ids are uuids in practice and this
        would only ever break under a test-shaped id.
        """
        record_buffer.put("run-1:n:b0", [{"i": 1}])
        record_buffer.put("run-10:n:b0", [{"i": 2}])

        record_buffer.release_run("run-1")

        assert record_buffer.open_keys() == ["run-10:n:b0"]


class TestKeys:
    def test_a_key_says_where_in_the_run_it_came_from(self) -> None:
        assert record_buffer.batch_key("run-1", "read-1", 4) == "run-1:read-1:b4"

    def test_two_nodes_on_the_same_pass_do_not_collide(self) -> None:
        first = record_buffer.batch_key("run-1", "read-1", 4)
        second = record_buffer.batch_key("run-1", "read-2", 4)

        assert first != second

    def test_open_keys_can_be_scoped_to_one_run(self) -> None:
        record_buffer.put("run-1:a:b0", [])
        record_buffer.put("run-2:a:b0", [])

        assert record_buffer.open_keys("run-1") == ["run-1:a:b0"]


class TestHandles:
    def test_a_handle_is_recognisable(self) -> None:
        assert record_buffer.is_handle(record_buffer.handle("k", 3))

    @pytest.mark.parametrize(
        "value", [None, 5, "k", [], {}, {"kind": "something_else"}, {"key": "k"}]
    )
    def test_anything_else_is_not(self, value: object) -> None:
        assert not record_buffer.is_handle(value)
