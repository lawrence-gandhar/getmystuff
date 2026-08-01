"""Tests for app/services/ai_inbuilt/chunking.py — paragraph-aware text splitting."""

from __future__ import annotations

import pytest

from app.services.ai_inbuilt.chunking import split_text


class TestEmptyInput:
    @pytest.mark.parametrize("text", ["", "   ", "\n\n\t", None])
    def test_nothing_to_split_yields_no_chunks(self, text) -> None:
        assert split_text(text) == []


class TestParagraphPacking:
    def test_short_text_stays_one_chunk(self) -> None:
        assert split_text("Hello world.") == ["Hello world."]

    def test_consecutive_paragraphs_are_packed_together(self) -> None:
        text = "First paragraph.\n\nSecond paragraph."
        assert split_text(text, max_chars=1000) == [
            "First paragraph.\n\nSecond paragraph."
        ]

    def test_paragraphs_split_when_the_pack_would_overflow(self) -> None:
        first, second = "a" * 60, "b" * 60
        chunks = split_text(f"{first}\n\n{second}", max_chars=100)
        assert chunks == [first, second]

    def test_blank_lines_with_whitespace_are_treated_as_a_break(self) -> None:
        chunks = split_text("a" * 60 + "\n   \n" + "b" * 60, max_chars=100)
        assert chunks == ["a" * 60, "b" * 60]

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert split_text("   padded   ") == ["padded"]

    def test_every_chunk_respects_the_limit(self) -> None:
        text = "\n\n".join(f"para {i} " + "x" * 50 for i in range(20))
        assert all(len(chunk) <= 120 for chunk in split_text(text, max_chars=120))


class TestHardSplitting:
    def test_an_oversized_paragraph_is_hard_split(self) -> None:
        chunks = split_text("x" * 250, max_chars=100, overlap_chars=0)
        assert [len(chunk) for chunk in chunks] == [100, 100, 50]

    def test_overlap_carries_context_into_the_next_piece(self) -> None:
        text = "".join(str(i % 10) for i in range(250))
        chunks = split_text(text, max_chars=100, overlap_chars=20)
        # Each chunk after the first restarts 20 characters before the previous end.
        assert chunks[1].startswith(chunks[0][-20:])

    def test_overlap_does_not_lose_any_content(self) -> None:
        text = "".join(str(i % 10) for i in range(250))
        chunks = split_text(text, max_chars=100, overlap_chars=20)
        assert "".join(chunks[0][:80] + chunks[1][:80] + chunks[2]) == text

    def test_hard_split_terminates_on_an_exact_multiple(self) -> None:
        """A final piece landing exactly on the boundary must not loop forever."""
        chunks = split_text("x" * 200, max_chars=100, overlap_chars=0)
        assert chunks == ["x" * 100, "x" * 100]

    def test_mixed_short_and_oversized_paragraphs(self) -> None:
        text = "short one\n\n" + "y" * 250
        chunks = split_text(text, max_chars=100, overlap_chars=0)
        assert chunks[0] == "short one"
        assert "".join(chunks[1:]) == "y" * 250

    def test_default_limit_matches_the_embedding_context_window(self) -> None:
        """1200 chars (~300 tokens) keeps chunks inside nomic-embed-text's window."""
        chunks = split_text("z" * 3000)
        assert all(len(chunk) <= 1200 for chunk in chunks)
        assert len(chunks) > 1
