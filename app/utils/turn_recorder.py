"""
Per-turn performance measurement for a chatbot conversation.

One visitor turn can fan out into several language-model calls that live in
different layers — the action router picking a webhook, the grounded answer
itself, an AI Fallback node inside a flow — and the layer that *knows* a call's
token cost (the provider call in ai_analytics_service) is several frames below
the layer that persists the turn log (chatbot_turn_service). Threading a
"usage" out-parameter through every one of those signatures would touch code
that has no interest in metrics, so the totals are accumulated in a
context-local record instead: the turn boundary opens one with
``record_turn()``, and anything running inside it reports into that record with
``record_llm_call()`` / ``record_action()``.

Recording is always a no-op when no record is open, so any code path that runs
outside a chatbot turn (the authenticated "Ask AI" flow, a background job) is
unaffected and never has to opt out.
"""

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator, List, Optional

# Characters per token for the fallback estimate used when a provider does not
# report usage of its own. ~4 characters per token is the accepted rule of
# thumb for English prose — deliberately approximate, and always flagged as an
# estimate on the stored row so the dashboard can say so.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Rough token count for text, used only when a provider reports no usage."""
    return len(text or "") // _CHARS_PER_TOKEN


@dataclass
class LlmCall:
    """One language-model request/response pair made during a turn."""

    provider: str
    model: str
    request_tokens: int
    response_tokens: int
    # True when the counts above were derived from text length rather than
    # reported by the provider (see estimate_tokens).
    estimated: bool = False


@dataclass
class TurnRecord:
    """
    Everything measurable about one visitor turn, accumulated as it runs.

    ``started_at`` is a monotonic clock reading — wall-clock timestamps can jump
    backwards (NTP correction) and would then report a negative duration.
    """

    started_at: float = field(default_factory=time.perf_counter)
    llm_calls: List[LlmCall] = field(default_factory=list)
    action: Optional[dict] = None

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started_at) * 1000)

    @property
    def request_tokens(self) -> int:
        return sum(call.request_tokens for call in self.llm_calls)

    @property
    def response_tokens(self) -> int:
        return sum(call.response_tokens for call in self.llm_calls)

    @property
    def total_tokens(self) -> int:
        return self.request_tokens + self.response_tokens

    @property
    def tokens_estimated(self) -> bool:
        """True if any call in this turn contributed an estimated count."""
        return any(call.estimated for call in self.llm_calls)

    @property
    def provider(self) -> Optional[str]:
        """
        The provider that answered this turn. Comma-joined on the rare turn
        that used more than one (e.g. an action router on one provider and the
        answer on another), so the log never silently drops half the story.
        """
        return _join_distinct(call.provider for call in self.llm_calls)

    @property
    def model(self) -> Optional[str]:
        return _join_distinct(call.model for call in self.llm_calls)


def _join_distinct(values: Iterator[str]) -> Optional[str]:
    seen: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return ", ".join(seen) if seen else None


_current: ContextVar[Optional[TurnRecord]] = ContextVar("turn_record", default=None)


@contextmanager
def record_turn() -> Iterator[TurnRecord]:
    """
    Open a measurement scope for one visitor turn.

    Re-entrant on purpose: an inner scope yields the record the outer one
    already opened, so a nested call site can measure itself without splitting
    a single turn into two half-counted logs.
    """
    existing = _current.get()
    if existing is not None:
        yield existing
        return

    record = TurnRecord()
    token = _current.set(record)
    try:
        yield record
    finally:
        _current.reset(token)


def current_turn() -> Optional[TurnRecord]:
    """The open record, or None when nothing is measuring."""
    return _current.get()


def record_llm_call(
    provider: str,
    model: str,
    request_tokens: int,
    response_tokens: int,
    estimated: bool = False,
) -> None:
    """Add one language-model call's cost to the open turn (no-op if none)."""
    record = _current.get()
    if record is None:
        return
    record.llm_calls.append(
        LlmCall(
            provider=provider,
            model=model,
            request_tokens=max(int(request_tokens or 0), 0),
            response_tokens=max(int(response_tokens or 0), 0),
            estimated=estimated,
        )
    )


def record_action(action: Optional[dict]) -> None:
    """Attach the webhook-action audit record that ran during this turn."""
    if action is None:
        return
    record = _current.get()
    if record is not None:
        record.action = action
