"""
What the routing prompt tells a model about reading a whole result set.

This file exists because of an answer a real agent gave. Asked "what was the revenue
generated in august", with one source — a graph whose own description said *"the user asks
for specific month, then filter the data on the created_at"* — it replied:

    I'm unable to filter the data by month, so I can't tell you the revenue that was
    generated specifically in August.

Nothing was broken. The prompt described `aggregate_records` as being for *totals* and
said nothing about narrowing, while **grounding rules 11 and 13 explicitly tell the model
that a tool applies only its own fixed filters, never to offer to narrow, and never to
describe a result as narrower than it is.** Those rules are right for a direct call and
wrong for a source that can be read whole — so the model followed them and apologised for
something it could have done.

So the two properties here are about *reachability of a capability*, not about the
capability:

* the note must **name filtering**, in words a model asked "in August" would match;
* it must **override rules 11 and 13 by number**, because an instruction that merely
  contradicts a numbered rule loses to it.

Plus the scope of the staleness fingerprint, which is what makes any of this deploy at
all — see :class:`TestTheFingerprintCoversEveryStaticBlock`.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict

import pytest

from app.services.deep_agents import prompt_builder as pb
from app.services.deep_agents.prompt_builder import (
    AGGREGATE_TOOL,
    build_tool_routing_prompt,
    rules_marker,
)


def graph_entry(**overrides: Any) -> Dict[str, Any]:
    """The user's own source: a graph whose description prescribes how to read it."""
    entry = {
        "kind": "graph",
        "tool_name": "for_loop_on_departmants",
        "description": (
            "this tool provides the complete data of projects and the departments and "
            "the revenue. - Always group by crm_id then by department. - Always sum of "
            "the total revenue after grouping. - The user asks for specific month, "
            "then filter the data on the created_at"
        ),
        "node_count": 4,
        "asks_questions": False,
        "sql_params": [],
        "allow_recursive_aggregate": True,
    }
    entry.update(overrides)

    return entry


def tool_entry(**overrides: Any) -> Dict[str, Any]:
    entry = {
        "tool_name": "sales_records",
        "description": "Every sale.",
        "table_name": "sales",
        "table_names": ["sales"],
        "query_mode": "sql",
        "sql_query": "SELECT * FROM sales",
        "config": {},
        "datasource_name": "warehouse",
        "db_type": "postgres",
        "allow_recursive_aggregate": True,
    }
    entry.update(overrides)

    return entry


class TestTheNoteNamesFiltering:
    """
    The half that was missing. Each assertion is a phrase a model matching "revenue in
    august" against its instructions would have to find.
    """

    def test_it_says_a_condition_can_be_applied(self) -> None:
        prompt = build_tool_routing_prompt("Agent", [graph_entry()])

        assert "A CONDITION the source itself does not take" in prompt

    def test_it_names_a_month_as_a_condition_that_works(self) -> None:
        """
        The specific question that failed. "one month" and an "in August" example, so the
        match is on the words the user's question actually uses.
        """
        prompt = build_tool_routing_prompt("Agent", [graph_entry()])

        assert "one month" in prompt
        assert "in August" in prompt

    def test_it_says_the_source_needs_no_parameter_for_it(self) -> None:
        """
        The exact belief behind the apology: "the tool does not accept a date parameter".
        The prompt now contradicts it in those terms.
        """
        prompt = build_tool_routing_prompt("Agent", [graph_entry()])

        assert "does NOT need a parameter for it" in prompt

    def test_it_forbids_answering_that_filtering_is_impossible(self) -> None:
        prompt = build_tool_routing_prompt("Agent", [graph_entry()])

        assert "never say you cannot filter" in prompt

    def test_it_says_totals_too_so_the_old_purpose_is_not_lost(self) -> None:
        prompt = build_tool_routing_prompt("Agent", [graph_entry()])

        assert "counted, totalled or averaged" in prompt

    def test_it_names_what_the_tool_will_refuse(self) -> None:
        """
        A model that does not know a median is unavailable asks for one and gets a
        failure, instead of choosing differently. Same argument as the tool's own
        description.
        """
        prompt = build_tool_routing_prompt("Agent", [graph_entry()])

        for absent in ("medians", "percentiles", "rankings"):
            assert absent in prompt


class TestItOverridesTheRulesThatCausedTheApology:
    """
    Rules 11 and 13 are not wrong — they are what stops a model claiming a result was
    narrowed when it was not, which is the worst failure this application has. They just
    cannot both stand for a source that *can* be narrowed, so the precedence is stated.
    """

    def test_the_override_names_both_rules_by_number(self) -> None:
        prompt = build_tool_routing_prompt("Agent", [graph_entry()])

        assert "overrides rules 11 and 13" in prompt

    def test_the_override_is_scoped_to_the_named_sources(self) -> None:
        """
        Scoped, or it would licence exactly the false answer rules 11 and 13 exist to
        prevent — for every other tool the agent has.
        """
        prompt = build_tool_routing_prompt("Agent", [graph_entry()])

        assert "and only for these" in prompt
        assert "For every other tool, rules 11 and 13 stand exactly as written" in prompt

    def test_the_rules_themselves_are_unchanged_when_nothing_is_opted_in(self) -> None:
        """
        An agent with no readable source must never see a reference to a tool it does not
        have — the module docstring's own rule. So the override goes in the conditional
        note, not into the numbered rules.
        """
        prompt = build_tool_routing_prompt(
            "Agent", [graph_entry(allow_recursive_aggregate=False)],
        )

        assert "overrides rules 11 and 13" not in prompt
        assert AGGREGATE_TOOL not in prompt


class TestTheOperatorsDescriptionIsActionable:
    """
    The user's actual complaint: "the description should work as a prompt — I already
    explained the calculations in the description".

    Their description does reach the model, as ``Purpose:``. What was missing is the
    join: nothing told the model that "always group by department" is an instruction it
    can *carry out*, or through what. Neither description can state that on its own — the
    operator writes it on the graph, and only the prompt knows which tool does grouping.
    """

    def test_the_description_reaches_the_prompt_verbatim(self) -> None:
        prompt = build_tool_routing_prompt("Agent", [graph_entry()])

        assert "Always group by crm_id then by department" in prompt

    def test_the_note_says_the_sources_own_instructions_are_carried_out_here(
        self,
    ) -> None:
        prompt = build_tool_routing_prompt("Agent", [graph_entry()])

        assert "A source's own description may tell you how it should be read" in prompt
        assert "include them in `instruction`" in prompt

    def test_it_gives_a_worked_example_of_combining_the_two(self) -> None:
        """
        The description says "group by department and sum the revenue"; the user says
        "August". A model has to produce one instruction from both, and being shown the
        combination is worth more than being told to combine them.
        """
        prompt = build_tool_routing_prompt("Agent", [graph_entry()])

        assert "total revenue per department in August" in prompt


class TestTheFingerprintCoversEveryStaticBlock:
    """
    The bug that would have kept all of the above from ever reaching a live agent.

    A routing prompt is generated once and **stored** on the agent. Staleness is decided
    by the newest tool change, plus a fingerprint of the prompt text this build owns — and
    that fingerprint hashed ``_GROUNDING_RULES`` **alone**. So rewriting the whole-result
    note would have changed what new prompts said and left every stored prompt describing
    the old capability, with nothing anywhere to notice: precisely the failure the
    fingerprint exists to prevent, arriving through a different door.
    """

    @pytest.mark.parametrize("index", range(len(pb._STATIC_PROMPT_TEXT)))
    def test_perturbing_any_block_changes_the_fingerprint(self, index: int) -> None:
        """
        The property, asserted per block rather than by naming them: a block that is in
        the tuple but does not affect the hash would pass a spot check and fail here.
        """
        blocks = list(pb._STATIC_PROMPT_TEXT)
        current = hashlib.sha256("\n".join(blocks).encode("utf-8")).hexdigest()[:12]

        blocks[index] = blocks[index] + " edited"
        perturbed = hashlib.sha256("\n".join(blocks).encode("utf-8")).hexdigest()[:12]

        assert current == pb._RULES_FINGERPRINT
        assert perturbed != pb._RULES_FINGERPRINT

    def test_the_whole_result_note_is_one_of_the_hashed_blocks(self) -> None:
        """
        Named explicitly as well as covered by the property above, because this is the
        block whose omission was the bug.
        """
        assert pb._AGGREGATE_NOTE_TEMPLATE in pb._STATIC_PROMPT_TEXT

    def test_every_hashed_block_is_non_empty(self) -> None:
        """A block that became empty would silently stop contributing to the hash."""
        for block in pb._STATIC_PROMPT_TEXT:
            assert block.strip()

    def test_the_marker_is_stamped_into_a_prompt_that_carries_the_note(self) -> None:
        prompt = build_tool_routing_prompt("Agent", [graph_entry()])

        assert rules_marker() in prompt

    def test_a_prompt_built_before_this_note_reads_as_stale(self) -> None:
        """
        End to end through the predicate the runtime actually uses: an agent holding
        yesterday's prompt gets it rebuilt on its next answer rather than keeping a
        description of a capability it now has.
        """
        from types import SimpleNamespace
        from datetime import datetime, timedelta, timezone

        from app.services.deep_agents.prompt_sync_service import is_prompt_stale

        now = datetime(2026, 8, 13, 6, 20, tzinfo=timezone.utc)
        tools = [graph_entry(updated_at=now - timedelta(days=1))]
        current = build_tool_routing_prompt("Agent", tools)
        yesterdays = current.replace(
            rules_marker(), "<!-- grounding-rules:000000000000 -->",
        )
        agent = SimpleNamespace(
            name="Agent", tool_routing_prompt=yesterdays, tool_prompt_synced_at=now,
        )

        assert is_prompt_stale(agent, tools) is True

        agent.tool_routing_prompt = current

        assert is_prompt_stale(agent, tools) is False


class TestBothKindsOfSourceGetTheNote:
    def test_a_tool_config_is_named(self) -> None:
        prompt = build_tool_routing_prompt("Agent", [tool_entry()])

        assert "`sales_records`" in prompt

    def test_a_graph_is_named(self) -> None:
        """
        The graph half is what the user has. One expression filters both kinds, so this
        is a check that the shared key really is shared.
        """
        prompt = build_tool_routing_prompt("Agent", [graph_entry()])

        assert "`for_loop_on_departmants`" in prompt

    def test_only_the_opted_in_one_of_two_is_named(self) -> None:
        prompt = build_tool_routing_prompt(
            "Agent",
            [graph_entry(), tool_entry(allow_recursive_aggregate=False)],
        )

        assert "`for_loop_on_departmants`" in prompt
        assert "`sales_records`" not in prompt

    def test_nothing_opted_in_leaves_the_prompt_byte_identical(self) -> None:
        """
        The guard that keeps this additive, restated for the graph path: compared against
        an entry with no such key at all, which is what every stored graph looked like
        before the column existed.
        """
        off = graph_entry(allow_recursive_aggregate=False)
        before = {key: value for key, value in off.items() if key != "allow_recursive_aggregate"}

        assert build_tool_routing_prompt("Agent", [off]) == (
            build_tool_routing_prompt("Agent", [before])
        )
