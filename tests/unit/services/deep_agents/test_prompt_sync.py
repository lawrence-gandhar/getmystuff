"""
Tests for ``prompt_sync_service.is_prompt_stale``.

The routing prompt is generated once and stored on the agent, so this predicate is
the only thing that decides whether a running agent ever sees a newer one. Get it
wrong in the permissive direction and the agent keeps answering from a prompt that
no longer matches reality — silently, and for as long as nobody re-saves a tool.

Staleness has two independent sources, and the second is the one that was missing:

* the agent's **tools** changed, which a timestamp comparison catches;
* the **grounding rules** in ``prompt_builder`` changed, which no timestamp can see,
  because editing them touches no database row. A rule fixed in code stayed unused
  by every agent already in the database until one of its tools happened to change.

``deep_agent_service._resolved_prompt_and_tools`` rebuilds on the first answer after
this returns True, so what these assert is exactly "the fix reaches production".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.deep_agents.prompt_builder import (
    build_tool_routing_prompt,
    rules_marker,
)
from app.services.deep_agents.prompt_sync_service import is_prompt_stale

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def tool(updated_at: datetime = _NOW - timedelta(days=1)) -> dict:
    return {
        "tool_name": "projects_list",
        "description": "Every project.",
        "table_name": "projects",
        "query_mode": "builder",
        "config": {"columns": [{"column": "name", "alias": ""}], "filters": []},
        "sql_query": None,
        "datasource_name": "crm",
        "db_type": "postgres",
        "updated_at": updated_at,
    }


def agent(prompt: str, synced_at: datetime | None = _NOW) -> SimpleNamespace:
    return SimpleNamespace(
        name="Reporter",
        tool_routing_prompt=prompt,
        tool_prompt_synced_at=synced_at,
    )


class TestAPromptBuiltByThisBuildIsCurrent:
    def test_a_freshly_built_prompt_is_not_stale(self) -> None:
        tools = [tool()]
        prompt = build_tool_routing_prompt("Reporter", tools)

        assert is_prompt_stale(agent(prompt), tools) is False

    def test_a_never_synced_agent_is_stale(self) -> None:
        tools = [tool()]
        prompt = build_tool_routing_prompt("Reporter", tools)

        assert is_prompt_stale(agent(prompt, synced_at=None), tools) is True

    def test_a_tool_saved_after_the_sync_is_still_caught(self) -> None:
        """The original check, which the rules check must not have replaced."""
        tools = [tool(updated_at=_NOW + timedelta(hours=1))]
        prompt = build_tool_routing_prompt("Reporter", tools)

        assert is_prompt_stale(agent(prompt), tools) is True


class TestAChangeToTheRulesInvalidatesStoredPrompts:
    """
    The half a timestamp cannot see.

    Every prompt here is synced *after* its tools last changed, so the timestamp
    comparison says "current" and only the marker can disagree.
    """

    def test_a_prompt_carrying_an_older_rules_revision_is_stale(self) -> None:
        tools = [tool()]
        stored = build_tool_routing_prompt("Reporter", tools).replace(
            rules_marker(), "<!-- grounding-rules:000000000000 -->",
        )

        assert is_prompt_stale(agent(stored), tools) is True

    def test_a_prompt_from_before_the_marker_existed_is_stale(self) -> None:
        """
        Every prompt already in the database on the day this shipped. No marker
        means it predates the marker, which means it predates the rule change that
        introduced it — so rebuilding is right, and rebuilding once is all it costs.
        """
        tools = [tool()]
        stored = build_tool_routing_prompt("Reporter", tools).replace(
            f"\n\n{rules_marker()}", "",
        )

        assert rules_marker() not in stored
        assert is_prompt_stale(agent(stored), tools) is True

    def test_an_empty_stored_prompt_is_stale(self) -> None:
        assert is_prompt_stale(agent(""), [tool()]) is True
        assert is_prompt_stale(agent(None), [tool()]) is True


class TestTheNoToolsPrompt:
    """
    An agent with nothing to route. It is marked like any other prompt, so the
    rules check does not permanently condemn it to a rebuild on every answer.
    """

    def test_it_is_marked_and_therefore_settles(self) -> None:
        prompt = build_tool_routing_prompt("Reporter", [])

        assert rules_marker() in prompt
        assert is_prompt_stale(agent(prompt), []) is False

    def test_a_prompt_still_listing_deleted_tools_is_stale(self) -> None:
        """The tools-but-no-prompt case: every tool was deleted, so the stored
        prompt describes tools that no longer exist."""
        prompt = build_tool_routing_prompt("Reporter", [tool()])

        assert is_prompt_stale(agent(prompt), []) is True


class TestTheMarkerItself:
    def test_it_is_a_comment_so_a_model_has_nothing_to_act_on(self) -> None:
        marker = rules_marker()

        assert marker.startswith("<!--")
        assert marker.endswith("-->")

    def test_it_is_stable_across_calls(self) -> None:
        """A fingerprint that moved on its own would rebuild every prompt on
        every answer, which is a write per turn rather than a write per change."""
        assert rules_marker() == rules_marker()

    def test_every_generated_prompt_carries_exactly_one(self) -> None:
        for tools in ([], [tool()]):
            prompt = build_tool_routing_prompt("Reporter", tools)
            assert prompt.count("<!-- grounding-rules:") == 1
