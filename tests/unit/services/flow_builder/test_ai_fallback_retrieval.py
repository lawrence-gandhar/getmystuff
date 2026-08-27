"""
Tests for what an AI Fallback node searches its knowledge base *for*.

The bug behind them, measured against a real proposal document rather than imagined: a
Menu option labelled "Email me the data" wired into an AI Fallback block retrieved that
document's **security and authentication** sections — the nearest chunks to the word
"data" — and the model, told to answer strictly from what was retrieved, explained that it
could not share user data. Grounded, faithful, and about the wrong subject. The same
knowledge base searched with the block's own instructions ("get the detailed summary…")
returned the scope, the deliverables and the estimates, which is what the operator wired
the block to say.

So the rule under test: **a label is written to be clicked, not to be searched for.** On a
selection turn the node's instructions join the query; on a typed turn they stay out,
because the visitor's own question is a better query than any standing instruction and
folding "answer in a friendly tone" into every search would only make it worse.
"""

from __future__ import annotations

from app.services.flow_builder.ai_fallback_service import _retrieval_query

PROMPT = "get the detailed summary from the knowledge base"
TYPED = "what is the warranty period?"
LABEL = "Email me the data"


class TestATypedQuestionIsSearchedForAsItIs:
    def test_the_instructions_are_left_out(self) -> None:
        assert _retrieval_query(PROMPT, TYPED, from_selection=False) == TYPED

    def test_with_no_instructions_either(self) -> None:
        assert _retrieval_query("", TYPED, from_selection=False) == TYPED

    def test_surrounding_whitespace_goes(self) -> None:
        assert _retrieval_query(PROMPT, f"  {TYPED}  ", from_selection=False) == TYPED


class TestASelectionBorrowsTheNodesInstructions:
    def test_they_are_prepended_to_the_label(self) -> None:
        assert _retrieval_query(PROMPT, LABEL, from_selection=True) == f"{PROMPT}\n{LABEL}"

    def test_the_label_stays_in(self) -> None:
        """
        Two options wired to two blocks have to retrieve differently, so the click is not
        discarded — it is the instructions that are missing, not the choice.
        """
        assert LABEL in _retrieval_query(PROMPT, LABEL, from_selection=True)

    def test_a_node_with_no_instructions_falls_back_to_the_label(self) -> None:
        """Nothing to borrow. The label is still better than searching for nothing."""
        assert _retrieval_query("", LABEL, from_selection=True) == LABEL

    def test_an_empty_message_leaves_the_instructions_alone(self) -> None:
        assert _retrieval_query(PROMPT, "", from_selection=True) == PROMPT
