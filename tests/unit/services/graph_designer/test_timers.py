"""
Tests for ``graph_designer.timers`` — the stopwatch maths and the wait's ceiling.

**Nothing here sleeps, and nothing here patches a clock.** Every transition takes its
``moment`` as an argument, so a test that wants to measure forty seconds passes two
datetimes forty seconds apart and gets an answer instantly. That is the whole reason the
transitions are shaped that way; the ``now()`` and ``sleep()`` seams exist for the
*runner* tests, which cannot pass a moment in.

No LangGraph, no database, no event loop. These are plain function calls over plain
dicts, which is what the module is.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.graph_designer import timers

# A fixed instant, so every expected number in this file can be written out by hand.
EPOCH = datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    """``seconds`` after the epoch these tests measure from."""
    return EPOCH + timedelta(seconds=seconds)


class TestStarting:
    """A timer that has only been started."""

    def test_it_begins_running_with_nothing_elapsed(self) -> None:
        record = timers.started("Job timer", "n7", at(0))

        assert record["phase"] == timers.PHASE_RUNNING
        assert record["elapsed_seconds"] == 0.0
        assert record["paused_seconds"] == 0.0
        assert record["ended_at"] is None
        assert record["carried_seconds"] == 0.0
        assert record["restarts"] == 0

    def test_it_opens_exactly_one_segment(self) -> None:
        """The span the clock is ticking over. Open, because it has not stopped."""
        segments = timers.started("Job timer", "n7", at(0))["segments"]

        assert len(segments) == 1
        assert segments[0]["ended_at"] is None

    def test_times_are_stored_as_strings(self) -> None:
        """
        State is checkpointed and previewed into JSONB. A ``datetime`` in there is a value
        whose round trip depends on which driver wrote it.
        """
        record = timers.started("Job timer", "n7", at(0))

        assert isinstance(record["started_at"], str)
        assert record["started_at"] == "2026-08-21T09:00:00+00:00"


class TestPausingAndResuming:
    """The reason a timer is not just two timestamps subtracted."""

    def test_a_paused_span_is_not_counted(self) -> None:
        """
        Ten seconds of work, forty paused, twenty more of work. The answer is thirty —
        this is the single most important number in the module.
        """
        record = timers.started("Job timer", "n7", at(0))
        record = timers.paused(record, at(10))
        record = timers.resumed(record, at(50))
        record = timers.stopped(record, at(70))

        assert record["elapsed_seconds"] == 30.0

    def test_the_paused_time_is_reported_separately(self) -> None:
        """Wall clock minus what was counted, so the two always add up to the elapsed wall."""
        record = timers.started("Job timer", "n7", at(0))
        record = timers.paused(record, at(10))
        record = timers.resumed(record, at(50))
        record = timers.stopped(record, at(70))

        assert record["paused_seconds"] == 40.0

    def test_a_timer_never_paused_reports_no_paused_time(self) -> None:
        """
        Wall and elapsed are two independent subtractions of the same clock, so this is
        the case that would show float noise as "paused for -0.0000001 seconds".
        """
        record = timers.started("Job timer", "n7", at(0))
        record = timers.stopped(record, at(12.5))

        assert record["elapsed_seconds"] == 12.5
        assert record["paused_seconds"] == 0.0

    def test_each_run_of_the_clock_is_its_own_segment(self) -> None:
        record = timers.started("Job timer", "n7", at(0))
        record = timers.paused(record, at(10))
        record = timers.resumed(record, at(50))
        record = timers.stopped(record, at(70))

        assert [segment["seconds"] for segment in record["segments"]] == [10.0, 20.0]

    def test_stopping_while_paused_is_allowed(self) -> None:
        """
        An ordinary thing to draw, and the elapsed time is already right because the
        paused span was never in a segment.
        """
        record = timers.started("Job timer", "n7", at(0))
        record = timers.paused(record, at(10))
        record = timers.stopped(record, at(600))

        assert record["phase"] == timers.PHASE_STOPPED
        assert record["elapsed_seconds"] == 10.0

    def test_pausing_twice_over_is_added_up(self) -> None:
        record = timers.started("Job timer", "n7", at(0))
        record = timers.paused(record, at(5))
        record = timers.resumed(record, at(15))
        record = timers.paused(record, at(20))
        record = timers.resumed(record, at(100))
        record = timers.stopped(record, at(103))

        assert record["elapsed_seconds"] == 13.0
        assert record["paused_seconds"] == 90.0


class TestARunningTimerCanBeRead:
    """A timer is readable before it stops — a pause reports where it has got to."""

    def test_an_open_segment_counts_up_to_the_moment_asked(self) -> None:
        record = timers.started("Job timer", "n7", at(0))
        record = timers.paused(record, at(7.25))

        assert record["elapsed_seconds"] == 7.25
        assert record["ended_at"] is None


class TestRestartingInsideALoop:
    """What a start/stop pair does on the second pass of a ``for_each``."""

    def test_the_new_pass_is_measured_on_its_own(self) -> None:
        first = timers.started("Job timer", "n7", at(0))
        first = timers.stopped(first, at(30))

        second = timers.restarted(first, at(100))
        second = timers.stopped(second, at(105))

        assert second["elapsed_seconds"] == 5.0

    def test_the_earlier_passes_are_carried_not_lost(self) -> None:
        """
        Without this, a loop timer reports the last pass and there is no way to ask what
        the loop cost altogether — which is usually the question.
        """
        first = timers.started("Job timer", "n7", at(0))
        first = timers.stopped(first, at(30))

        second = timers.restarted(first, at(100))
        second = timers.stopped(second, at(105))

        assert second["carried_seconds"] == 30.0
        assert second["total_elapsed_seconds"] == 35.0

    def test_restarting_counts_the_passes(self) -> None:
        record = timers.started("Job timer", "n7", at(0))
        record = timers.restarted(record, at(10))
        record = timers.restarted(record, at(20))

        assert record["restarts"] == 2

    def test_restarting_a_paused_timer_carries_only_what_it_counted(self) -> None:
        """The pause must not become carried time — it was never elapsed time."""
        record = timers.started("Job timer", "n7", at(0))
        record = timers.paused(record, at(4))
        record = timers.restarted(record, at(500))

        assert record["carried_seconds"] == 4.0


class TestIllegalTransitions:
    """
    Every one is an authoring mistake, and every one raises rather than quietly doing
    nothing. A timer that silently ignored a Stop would report a number that looks
    plausible and is wrong, which is the failure this whole module is shaped to avoid.
    """

    def test_pausing_a_paused_timer_is_refused(self) -> None:
        record = timers.paused(timers.started("T", "n", at(0)), at(1))

        with pytest.raises(timers.TimerError, match="already paused"):
            timers.paused(record, at(2))

    def test_resuming_a_running_timer_is_refused(self) -> None:
        record = timers.started("T", "n", at(0))

        with pytest.raises(timers.TimerError, match="already running"):
            timers.resumed(record, at(1))

    def test_stopping_a_stopped_timer_is_refused(self) -> None:
        record = timers.stopped(timers.started("T", "n", at(0)), at(1))

        with pytest.raises(timers.TimerError, match="already been stopped"):
            timers.stopped(record, at(2))

    def test_pausing_a_stopped_timer_is_refused(self) -> None:
        record = timers.stopped(timers.started("T", "n", at(0)), at(1))

        with pytest.raises(timers.TimerError, match="already been stopped"):
            timers.paused(record, at(2))

    def test_resuming_a_stopped_timer_is_refused(self) -> None:
        record = timers.stopped(timers.started("T", "n", at(0)), at(1))

        with pytest.raises(timers.TimerError, match="already been stopped"):
            timers.resumed(record, at(2))

    def test_the_refusal_names_the_timer(self) -> None:
        """The person reading this is looking at a canvas with several boxes on it."""
        record = timers.started("Nightly import", "n", at(0))

        with pytest.raises(timers.TimerError, match="Nightly import"):
            timers.resumed(record, at(1))


class TestTransitionsDoNotEditWhatTheyAreGiven:
    """
    The run's state belongs to LangGraph, which merges what a node *returns*. Editing the
    mapping in place would change a value the reducer was never told about.
    """

    def test_pausing_leaves_the_original_running(self) -> None:
        record = timers.started("T", "n", at(0))
        timers.paused(record, at(10))

        assert record["phase"] == timers.PHASE_RUNNING
        assert record["segments"][0]["ended_at"] is None

    def test_resuming_does_not_lengthen_the_original(self) -> None:
        record = timers.paused(timers.started("T", "n", at(0)), at(1))
        timers.resumed(record, at(5))

        assert len(record["segments"]) == 1


class TestTheSnapshot:
    """What a Timer node puts in ``outputs`` for a later node — usually an email — to read."""

    def test_it_is_flat_so_a_binding_reaches_it_in_one_hop(self) -> None:
        """
        A downstream binding reads with a dotted path. Nesting would make an email's
        variable row depend on this module's internals.
        """
        record = timers.stopped(timers.started("Job", "n7", at(0)), at(30))
        snapshot = timers.snapshot(record, "stop")

        for key in ("started_at", "ended_at", "elapsed_seconds", "elapsed_human"):
            assert key in snapshot
            assert not isinstance(snapshot[key], dict)

    def test_it_reports_the_start_and_the_end(self) -> None:
        """The question the feature exists to answer."""
        record = timers.stopped(timers.started("Job", "n7", at(0)), at(30))
        snapshot = timers.snapshot(record, "stop")

        assert snapshot["started_at"] == "2026-08-21T09:00:00+00:00"
        assert snapshot["ended_at"] == "2026-08-21T09:00:30+00:00"

    def test_a_running_timer_says_so(self) -> None:
        snapshot = timers.snapshot(timers.started("Job", "n7", at(0)), "start")

        assert snapshot["running"] is True
        assert snapshot["ended_at"] is None

    def test_a_stopped_timer_says_so(self) -> None:
        record = timers.stopped(timers.started("Job", "n7", at(0)), at(1))

        assert timers.snapshot(record, "stop")["running"] is False


class TestElapsedText:
    """
    The reason this exists: a template variable bound to ``elapsed_seconds`` renders
    ``3852.117`` into a sentence, and nobody wants to format that by hand.
    """

    @pytest.mark.parametrize(
        "seconds, expected",
        [
            (3852.117, "1h 4m 12s"),
            (62, "1m 2s"),
            (30, "30s"),
            (0.4, "0.40s"),
            (0, "0.00s"),
            (3600, "1h"),
        ],
    )
    def test_a_duration_reads_as_a_person_would_write_it(
        self, seconds: float, expected: str,
    ) -> None:
        assert timers.elapsed_text(seconds) == expected

    def test_something_that_is_not_a_number_is_empty_rather_than_a_crash(self) -> None:
        """A preview must never be the thing that fails a node."""
        assert timers.elapsed_text(None) == ""
        assert timers.elapsed_text("later") == ""


class TestTheWaitCeiling:
    """
    The only thing bounding how long a run can be parked. There is no ``asyncio.wait_for``
    around a runner in this package, and ``graph_data`` is JSONB that can be hand-edited.
    """

    def test_an_ordinary_wait_is_allowed(self) -> None:
        assert timers.validated_wait_seconds(30, "Wait") == 30

    def test_a_number_written_as_text_is_read(self) -> None:
        """What a form posts."""
        assert timers.validated_wait_seconds("30", "Wait") == 30

    def test_longer_than_the_ceiling_is_refused_and_the_ceiling_is_named(self) -> None:
        """
        Refused rather than clamped. Clamping would leave the drawing saying two hours
        while the run waited fifteen minutes.
        """
        with pytest.raises(timers.TimerError, match=str(timers.MAX_WAIT_SECONDS)):
            timers.validated_wait_seconds(timers.MAX_WAIT_SECONDS + 1, "Wait")

    @pytest.mark.parametrize("raw", [0, -1, "abc", None, "", True])
    def test_anything_that_is_not_a_positive_number_of_seconds_is_refused(
        self, raw: object,
    ) -> None:
        with pytest.raises(timers.TimerError):
            timers.validated_wait_seconds(raw, "Wait")

    def test_the_refusal_names_the_node(self) -> None:
        with pytest.raises(timers.TimerError, match="Hold on"):
            timers.validated_wait_seconds(0, "Hold on")


class TestTheClockIsAware:
    """
    Aware UTC, because these values are subtracted from each other and serialised, and a
    naive datetime makes both depend on where the server is.
    """

    def test_now_carries_a_timezone(self) -> None:
        assert timers.now().tzinfo is not None

    def test_a_stored_time_without_an_offset_is_read_as_utc(self) -> None:
        """
        The only assumption available, and the one every writer here makes. Without it a
        record written by a store that dropped the offset would raise mid-subtraction.
        """
        record = timers.started("T", "n", at(0))
        record["started_at"] = "2026-08-21T09:00:00"
        record["segments"][0]["started_at"] = "2026-08-21T09:00:00"

        assert timers.stopped(record, at(5))["elapsed_seconds"] == 5.0

    def test_a_stored_time_that_cannot_be_read_fails_with_a_sentence(self) -> None:
        record = timers.started("Job timer", "n", at(0))
        record["segments"][0]["started_at"] = "the other day"

        with pytest.raises(timers.TimerError, match="Job timer"):
            timers.stopped(record, at(5))


class TestAClockThatStepsBackwards:
    """NTP happens. A measurement should shorten to nothing, never go negative."""

    def test_a_backwards_span_is_zero_rather_than_negative(self) -> None:
        record = timers.started("T", "n", at(100))

        assert timers.stopped(record, at(90))["elapsed_seconds"] == 0.0
