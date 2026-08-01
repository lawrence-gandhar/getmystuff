"""
Tests for app/utils/turn_recorder.py — the context-local per-turn metrics record.

Two properties carry the design and are asserted hardest:

* recording outside an open scope is a silent no-op, which is what lets the
  authenticated "Ask AI" path share code with the chatbot path without opting
  out of metrics;
* ``record_turn`` is re-entrant, so a nested scope contributes to the outer
  turn rather than splitting one turn into two half-counted logs.
"""

from __future__ import annotations

import asyncio

import pytest

from app.utils.turn_recorder import (
    LlmCall,
    TurnRecord,
    current_turn,
    estimate_tokens,
    record_action,
    record_llm_call,
    record_turn,
)


class TestEstimateTokens:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("", 0),
            ("abc", 0),
            ("abcd", 1),
            ("a" * 400, 100),
            ("a" * 7, 1),
        ],
    )
    def test_four_characters_per_token(self, text: str, expected: int) -> None:
        assert estimate_tokens(text) == expected

    def test_none_is_treated_as_empty(self) -> None:
        """Providers that return no content at all hand this None; it must not
        raise inside a metrics path."""
        assert estimate_tokens(None) == 0


class TestNoOpOutsideAScope:
    def test_current_turn_is_none_by_default(self) -> None:
        assert current_turn() is None

    def test_record_llm_call_outside_a_scope_does_nothing(self) -> None:
        record_llm_call("anthropic", "claude", 10, 20)
        assert current_turn() is None

    def test_record_action_outside_a_scope_does_nothing(self) -> None:
        record_action({"name": "webhook"})
        assert current_turn() is None


class TestRecordTurn:
    def test_opens_and_closes_a_scope(self) -> None:
        with record_turn() as record:
            assert current_turn() is record
        assert current_turn() is None

    def test_resets_the_scope_even_when_the_body_raises(self) -> None:
        with pytest.raises(ValueError):
            with record_turn():
                raise ValueError("boom")

        assert current_turn() is None

    def test_is_re_entrant_and_shares_one_record(self) -> None:
        """The inner scope must yield the record the outer one opened —
        otherwise a nested call site would start a second turn and each log
        would show half the tokens."""
        with record_turn() as outer:
            with record_turn() as inner:
                assert inner is outer
                record_llm_call("anthropic", "claude", 5, 5)
            # Still open after the inner scope exits.
            assert current_turn() is outer
            record_llm_call("anthropic", "claude", 5, 5)

        assert outer.total_tokens == 20
        assert current_turn() is None

    def test_sequential_scopes_do_not_share_state(self) -> None:
        with record_turn() as first:
            record_llm_call("anthropic", "claude", 10, 10)
        with record_turn() as second:
            pass

        assert first is not second
        assert second.llm_calls == []


class TestRecordLlmCall:
    def test_accumulates_calls(self) -> None:
        with record_turn() as record:
            record_llm_call("anthropic", "claude-opus", 100, 50)
            record_llm_call("anthropic", "claude-opus", 30, 20)

        assert len(record.llm_calls) == 2
        assert record.request_tokens == 130
        assert record.response_tokens == 70
        assert record.total_tokens == 200

    @pytest.mark.parametrize(
        ("request_tokens", "response_tokens"),
        [(None, None), (-5, -5), ("0", "0")],
    )
    def test_coerces_and_floors_counts_at_zero(
        self, request_tokens, response_tokens  # noqa: ANN001
    ) -> None:
        """A provider reporting None or a negative count must not make the
        stored total go backwards."""
        with record_turn() as record:
            record_llm_call("openai", "gpt", request_tokens, response_tokens)

        assert record.request_tokens == 0
        assert record.response_tokens == 0

    def test_estimated_flag_defaults_false_and_is_sticky(self) -> None:
        """One estimated call taints the whole turn, so the dashboard can label
        the number as approximate rather than implying it was reported."""
        with record_turn() as record:
            record_llm_call("anthropic", "claude", 10, 10)
            assert record.tokens_estimated is False
            record_llm_call("ollama", "llama3", 10, 10, estimated=True)

        assert record.tokens_estimated is True

    def test_a_turn_with_no_calls_totals_zero(self) -> None:
        with record_turn() as record:
            pass

        assert record.total_tokens == 0
        assert record.tokens_estimated is False
        assert record.provider is None
        assert record.model is None


class TestProviderAndModelJoining:
    def test_single_provider_is_reported_plainly(self) -> None:
        with record_turn() as record:
            record_llm_call("anthropic", "claude-opus", 1, 1)
            record_llm_call("anthropic", "claude-opus", 1, 1)

        assert record.provider == "anthropic"
        assert record.model == "claude-opus"

    def test_multiple_providers_are_comma_joined_in_first_seen_order(self) -> None:
        with record_turn() as record:
            record_llm_call("ollama", "llama3", 1, 1)
            record_llm_call("anthropic", "claude-opus", 1, 1)
            record_llm_call("ollama", "llama3", 1, 1)

        assert record.provider == "ollama, anthropic"
        assert record.model == "llama3, claude-opus"

    def test_blank_values_are_skipped(self) -> None:
        with record_turn() as record:
            record_llm_call("", "", 1, 1)
            record_llm_call("anthropic", "claude", 1, 1)

        assert record.provider == "anthropic"
        assert record.model == "claude"

    def test_all_blank_reports_none(self) -> None:
        with record_turn() as record:
            record_llm_call("", "", 1, 1)

        assert record.provider is None
        assert record.model is None


class TestRecordAction:
    def test_attaches_the_action(self) -> None:
        with record_turn() as record:
            record_action({"name": "create_ticket", "status": 200})

        assert record.action == {"name": "create_ticket", "status": 200}

    def test_none_leaves_a_previously_recorded_action_intact(self) -> None:
        """The early return on None means a later no-action call cannot wipe an
        action already recorded during the same turn."""
        with record_turn() as record:
            record_action({"name": "create_ticket"})
            record_action(None)

        assert record.action == {"name": "create_ticket"}

    def test_last_action_wins(self) -> None:
        with record_turn() as record:
            record_action({"name": "first"})
            record_action({"name": "second"})

        assert record.action == {"name": "second"}

    def test_action_defaults_to_none(self) -> None:
        with record_turn() as record:
            pass

        assert record.action is None


class TestElapsed:
    def test_elapsed_is_non_negative_and_monotonic(self) -> None:
        record = TurnRecord()
        first = record.elapsed_ms()
        second = record.elapsed_ms()

        assert first >= 0
        assert second >= first

    def test_elapsed_reflects_a_delay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        perf_counter is stubbed rather than slept on, so the assertion is exact
        instead of timing-dependent.

        ``started_at`` is passed explicitly: its default_factory captured the
        real ``time.perf_counter`` when the dataclass was defined, so patching
        the module attribute afterwards does not affect construction — only the
        ``elapsed_ms`` call site reads the patched function.
        """
        from app.utils import turn_recorder

        monkeypatch.setattr(turn_recorder.time, "perf_counter", lambda: 100.25)

        record = TurnRecord(started_at=100.0)
        assert record.elapsed_ms() == 250

    def test_elapsed_truncates_rather_than_rounds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.utils import turn_recorder

        monkeypatch.setattr(turn_recorder.time, "perf_counter", lambda: 100.0009)

        assert TurnRecord(started_at=100.0).elapsed_ms() == 0


class TestContextIsolation:
    async def test_concurrent_tasks_do_not_share_a_record(self) -> None:
        """
        A ContextVar is copied into each task, so two turns running
        concurrently must accumulate separately. A module-level global here
        would cross-contaminate two visitors' token counts.
        """
        results: dict = {}

        async def one_turn(name: str, tokens: int) -> None:
            with record_turn() as record:
                record_llm_call("anthropic", "claude", tokens, 0)
                await asyncio.sleep(0)
                record_llm_call("anthropic", "claude", tokens, 0)
                results[name] = record.total_tokens

        await asyncio.gather(one_turn("a", 10), one_turn("b", 100))

        assert results == {"a": 20, "b": 200}


class TestLlmCallDataclass:
    def test_fields(self) -> None:
        call = LlmCall(provider="openai", model="gpt", request_tokens=5, response_tokens=7)

        assert (call.provider, call.model) == ("openai", "gpt")
        assert (call.request_tokens, call.response_tokens) == (5, 7)
        assert call.estimated is False
