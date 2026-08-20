"""
The widget's Markdown renderer, exercised by running it.

``renderMarkdown`` output goes into ``innerHTML`` on a page this application does not
control, carrying text a language model wrote. That is the one place in the widget
where reading the source is not enough: an escaping bug is invisible to a source
assertion and is a cross-site scripting hole in somebody else's website. So these
tests extract the function from the built script and execute it in Node.

**Skipped when Node is absent** (the app container has no Node), which is a real gap
and is stated rather than papered over: `test_widget_script.py` holds the structural
half — that the escape happens before any parsing, and that no anchor is ever built —
so a change that breaks the ordering fails somewhere even where these cannot run.

The safety property asserted is stronger than "no <script> survives": every tag in the
output must be one this code wrote, from a fixed set, carrying no attribute but a known
class. That holds for any input rather than for the attacks somebody thought of.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap

import pytest

from app.services.chatbot.chatbot_service import build_widget_script

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is needed to execute the widget's renderer",
)

# Every function renderMarkdown reaches, in the order it needs them declared.
_FUNCTIONS = (
    "renderMarkdown", "isListItem", "isTableRow", "isTableDivider", "startsTable",
    "startsBlock", "tableCells", "renderTableBlock", "renderListBlock",
    "inlineMarkdown",
)

# A stand-in for the browser's textContent -> innerHTML escape, matching what the
# widget's own escapeHtml produces including the quote pass.
_ESCAPE_SHIM = """
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
"""


def _extracted() -> str:
    """The renderer's functions, lifted out of the widget IIFE."""
    source = build_widget_script()
    out = []

    for name in _FUNCTIONS:
        start = source.index(f"  function {name}(")
        end = source.index("\n  }", start)
        out.append(source[start:end + 4])

    return "\n".join(out)


def _render(text: str) -> str:
    """Run renderMarkdown over one input and return the HTML it produced."""
    script = (
        _ESCAPE_SHIM
        + _extracted()
        + "\nprocess.stdout.write(renderMarkdown("
        + json.dumps(text)
        + "));"
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )

    if result.returncode != 0:
        raise AssertionError(f"renderer threw: {result.stderr.strip()}")

    return result.stdout


# Everything this renderer is allowed to emit. A tag outside this set, or any
# attribute that is not one of these classes, is a defect by definition.
_ALLOWED_TAGS = {
    "p", "br", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td",
    "strong", "em", "code", "div",
}
_ALLOWED_CLASSES = {
    "gms-chatbot-table", "gms-chatbot-table-wrap", "gms-chatbot-md-h",
}


def _unsafe_tag(html: str) -> str | None:
    """The first tag that is not one this renderer wrote, or None."""
    import re

    for tag in re.findall(r"<[^>]*>", html):
        body = tag[1:-1].lstrip("/")
        name = re.split(r"[\s/]", body, maxsplit=1)[0]

        if name not in _ALLOWED_TAGS:
            return tag

        rest = body[len(name):].strip()

        if not rest:
            continue

        attribute = re.fullmatch(r'class="([^"]*)"', rest)

        if not attribute or attribute.group(1) not in _ALLOWED_CLASSES:
            return tag

    return None


class TestModelOutputCannotBecomeMarkup:
    @pytest.mark.parametrize("attack", [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
        "<svg/onload=alert(1)>",
        '<div onclick="steal()">hi</div>',
        '| a |\n|---|\n| " onmouseover="alert(1) |',
        "**<img src=x onerror=alert(1)>**",
        "`<script>alert(1)</script>`",
        "- <script>alert(1)</script>",
        "### <script>alert(1)</script>",
    ])
    def test_no_tag_survives_that_this_renderer_did_not_write(
        self, attack: str,
    ) -> None:
        html = _render(attack)

        # The allowlist is the assertion. Looking for "<script" or "onerror" would
        # be weaker *and* wrong: those words appearing as escaped text — inside
        # &lt;img … onerror=… &gt; — is the renderer working, not failing. What
        # matters is that no such text is a tag or an attribute.
        assert _unsafe_tag(html) is None, html
        assert "<script" not in html

    def test_a_link_is_never_built(self) -> None:
        """
        `[text](javascript:…)` is how markdown becomes script execution, so links are
        not supported at all — and grounding rule 10 forbids the model writing a URL
        anyway. The syntax is left as the literal text it wrote.
        """
        html = _render("[click me](javascript:alert(1))")

        assert "<a" not in html
        assert "href" not in html
        assert "click me" in html

    def test_angle_brackets_in_ordinary_prose_are_shown_not_swallowed(self) -> None:
        """Escaping has to be visible to the reader, not silently drop their text."""
        html = _render("rows where a < b and c > d")

        assert "&lt;" in html and "&gt;" in html


class TestTheFormattingThatWasAskedFor:
    def test_a_markdown_table_becomes_a_real_table(self) -> None:
        """
        The reason this exists. The escaped-text renderer showed a query result as a
        wall of pipe characters — which is what a visitor was actually looking at.
        """
        html = _render(textwrap.dedent("""\
            Here are the projects:

            | id | crm_id | technology |
            |----|--------|------------|
            | 1 | 200 | Python |
            | 2 | 201 | Django |"""))

        assert '<table class="gms-chatbot-table">' in html
        assert "<th>id</th>" in html
        assert "<td>Python</td>" in html
        assert html.count("<tr>") == 3
        assert "<p>Here are the projects:</p>" in html

    def test_the_table_is_wrapped_so_it_can_scroll(self) -> None:
        """A widget is around 340px wide and a six-column result is not."""
        html = _render("| a | b |\n|---|---|\n| 1 | 2 |")

        assert 'class="gms-chatbot-table-wrap"' in html

    def test_pipes_without_a_divider_row_stay_prose(self) -> None:
        """Otherwise any sentence mentioning a pipe becomes a one-cell table."""
        html = _render("Use the | character to split the file.")

        assert "<table" not in html

    @pytest.mark.parametrize("source,expected", [
        ("- one\n- two", "<ul><li>one</li><li>two</li></ul>"),
        ("1. one\n2. two", "<ol><li>one</li><li>two</li></ol>"),
        ("**bold**", "<strong>bold</strong>"),
        ("*slanted*", "<em>slanted</em>"),
        ("`code`", "<code>code</code>"),
        ("### Totals", '<div class="gms-chatbot-md-h">Totals</div>'),
        ("Just a sentence.", "<p>Just a sentence.</p>"),
        ("line one\nline two", "<p>line one<br>line two</p>"),
    ])
    def test_the_supported_syntax(self, source: str, expected: str) -> None:
        assert expected in _render(source)

    def test_an_empty_answer_renders_nothing(self) -> None:
        """
        renderBotMessage skips the bubble on empty HTML — an empty one is a stray
        blank rectangle that reads as a broken reply.
        """
        assert _render("") == ""
        assert _render("   \n  ") == ""
