"""
Tests for how a Deep Agent turn reports a provider that is too busy to answer.

A 429 is not a broken agent. It is the provider saying "come back in a moment", and
for a while this application told whoever read the log to go and check an API key
that was never the problem — the same sentence it uses for a genuinely bad key. That
is the behaviour these tests pin down: a rate limit gets its own status, its own
words, and a log line that says what actually happened.

The retry budget is asserted here too, and it belongs with these tests rather than
with ``model_factory``'s own: the number only matters because of what happens when it
runs out, which is the branch below.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import openai
import pytest
from litestar.exceptions import HTTPException

from app.services.deep_agents import deep_agent_service, model_factory


def _rate_limit_error(sdk: str) -> Exception:
    """A real SDK rate-limit error, built the way the SDK builds one."""
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    response = httpx.Response(429, request=request)
    body = {
        "message": "We're experiencing high traffic right now! Please try again soon.",
        "type": "too_many_requests_error",
        "param": "queue",
        "code": "queue_exceeded",
    }

    if sdk == "openai":
        return openai.RateLimitError("429", response=response, body=body)

    return anthropic.RateLimitError("429", response=response, body=body)


@pytest.fixture
def agent() -> SimpleNamespace:
    return SimpleNamespace(id=1, uuid="11111111-1111-1111-1111-111111111111")


@pytest.fixture
def failing_turn(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Replace the prepared turn with one whose model raises what we hand it."""

    def _install(error: Exception) -> None:
        class _Agent:
            async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict:
                raise error

            def astream_events(self, *_args: Any, **_kwargs: Any):  # noqa: ANN202
                async def _events():  # noqa: ANN202
                    raise error
                    yield  # pragma: no cover — makes this an async generator

                return _events()

        async def _prepared(*_args: Any, **_kwargs: Any):  # noqa: ANN202
            return _Agent(), [], SimpleNamespace()

        monkeypatch.setattr(deep_agent_service, "_prepared_turn", _prepared)

    return _install


class TestARateLimitIsNotAConfigurationProblem:
    @pytest.mark.parametrize("sdk", ["openai", "anthropic"])
    async def test_it_is_a_503_naming_the_provider_not_the_key(
        self, sdk: str, agent: SimpleNamespace, failing_turn, db,  # noqa: ANN001
    ) -> None:
        """
        The regression. 502 with "check the agent's AI key in AI Settings" sent an
        operator hunting a configuration fault that did not exist, every time their
        provider had a busy minute.
        """
        failing_turn(_rate_limit_error(sdk))

        with pytest.raises(HTTPException) as caught:
            await deep_agent_service._answer_as_agent(db, 1, agent, "how many orders?")

        assert caught.value.status_code == 503
        assert "busy" in caught.value.detail
        assert "AI key" not in caught.value.detail
        assert "Nothing needs changing in AI Settings" in caught.value.detail

    async def test_every_other_failure_still_says_to_check_the_key(
        self, agent: SimpleNamespace, failing_turn, db,  # noqa: ANN001
    ) -> None:
        """
        The catch-all is unchanged, and has to be: a wrong key, a dead endpoint and a
        graph that threw are all things the operator does have to go and look at.
        """
        failing_turn(RuntimeError("the graph fell over"))

        with pytest.raises(HTTPException) as caught:
            await deep_agent_service._answer_as_agent(db, 1, agent, "how many orders?")

        assert caught.value.status_code == 502
        assert "check the agent's AI key" in caught.value.detail

    @pytest.mark.parametrize("sdk", ["openai", "anthropic"])
    async def test_the_streaming_path_says_the_same_thing(
        self, sdk: str, agent: SimpleNamespace, failing_turn, db,  # noqa: ANN001
    ) -> None:
        """
        Two code paths, one answer. The console streams and the widget's blocking POST
        does not, and a visitor must not get a different account of the same failure
        depending on which one served them.
        """
        failing_turn(_rate_limit_error(sdk))

        events = [
            event
            async for event in deep_agent_service._stream_as_agent(
                db, 1, agent, "how many orders?",
            )
        ]

        assert events[-1]["event"] == "error"
        assert "busy" in events[-1]["message"]
        assert "AI key" not in events[-1]["message"]

    async def test_the_visitor_is_never_shown_the_operators_sentence(self) -> None:
        """
        _BUSY_MESSAGE is for the console and the log. A widget visitor gets
        chatbot_reply_service's _NO_FALLBACK_REPLY, which names no system they can see
        and offers them something to do instead.
        """
        from app.services.chatbot.chatbot_reply_service import _NO_FALLBACK_REPLY

        assert "provider" not in _NO_FALLBACK_REPLY.lower()
        assert "AI Settings" not in _NO_FALLBACK_REPLY
        assert "try again" in _NO_FALLBACK_REPLY.lower()


class TestTheRetryBudget:
    def test_the_client_retries_more_than_the_sdk_default(self) -> None:
        """
        Both SDKs default to 2, which is sized for a per-key burst limit. A gateway
        that queues under load takes seconds to drain, not milliseconds — so two fast
        retries land inside the same busy window that caused the first refusal.
        """
        assert model_factory.MAX_RETRIES > 2

    def test_the_retry_is_on_the_client_and_not_around_the_graph(self) -> None:
        """
        The constraint that decides where this lives. A Deep Agent turn is a loop, so
        re-running the graph on a 429 would re-execute every tool call that had already
        succeeded — running the user's SQL a second time for a failure that happened
        after it. Asserted on the source because it is an architectural rule, and the
        tempting wrong version passes every behavioural test.
        """
        import inspect

        source = inspect.getsource(deep_agent_service._answer_as_agent)

        assert source.count("ainvoke(") == 1
        assert "for attempt" not in source
