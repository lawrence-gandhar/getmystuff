"""
Tests for the embeddable widget shell — ``chatbot_service.build_widget_script``.

This file is JavaScript inside a Python string, downloaded by operators and hosted
on their own sites, and nothing else in the suite touches it. It is also the one
part of the application whose failures are invisible from the server: it degrades
on purpose (a failed config fetch still renders the widget, a failed message still
answers the visitor politely), so a misconfigured embed looks exactly like a working
one and the server logs nothing at all.

Three real support cases motivated these assertions, all presenting as the identical
symptom — a healthy-looking widget titled "Chat with us":

1. the embedding page's origin was not on the chatbot's Allowed Domains list (403);
2. an HTTPS page was pointed at an ``http://localhost`` ``apiBase``, which the
   browser blocks before sending and reports as a *CORS error* with no response, so
   the server never saw a request to log;
3. ``apiBase`` could not be omitted for a same-origin embed, forcing an absolute URL
   that then had to track the embedding page's scheme — which is what caused (2).

What is asserted here is therefore mostly "the diagnostic exists and names the
thing", because that is the property that was missing. The script's *behaviour* is
exercised separately by driving it under a DOM stub; these tests guard the contract
that behaviour depends on, and would catch it being edited away.
"""

from __future__ import annotations

import re

import pytest

from app.services.chatbot.chatbot_service import build_widget_script


@pytest.fixture(scope="module")
def script() -> str:
    return build_widget_script()


@pytest.fixture(scope="module")
def prose(script: str) -> str:
    """
    The script with adjacent string literals joined, so a sentence can be asserted
    as the operator reads it rather than as the source happens to wrap it.

    Without this, an assertion on a message is really an assertion on where the
    concatenation happens to fall — it breaks on a reflow that changed nothing, and
    passes if a clause is dropped from the middle of a joined pair.
    """
    return re.sub(r'"\s*\+\s*\n?\s*"', "", script)


class TestItIsStillGeneric:
    def test_nothing_key_specific_is_templated_in(self, script: str) -> None:
        """The same download has to work for every chatbot — every key-specific
        value is fetched at runtime, which is why a settings change needs no
        re-download."""
        assert "cb_pub_" not in script
        assert "{{" not in script and "{%" not in script

    def test_it_is_byte_identical_across_calls(self, script: str) -> None:
        assert build_widget_script() == script


class TestApiBaseIsOptional:
    """
    A same-origin embed — the API behind the same domain or reverse proxy as the
    page — must be able to omit ``apiBase`` entirely. Requiring it forced an
    absolute URL whose scheme had to be kept in step with the embedding page's by
    hand, and getting that wrong is silently fatal (see the module docstring).
    """

    def test_only_the_api_key_is_required(self, script: str) -> None:
        assert "if (!API_KEY) {" in script
        # The old guard also refused a missing base, which is what made a relative
        # same-origin request impossible.
        assert "if (!API_BASE || !API_KEY)" not in script

    def test_a_missing_base_becomes_an_empty_string(self, script: str) -> None:
        """So `API_BASE + "/public/chatbot/..."` is a relative, same-origin URL —
        which cannot be blocked by a scheme mismatch and needs no CORS."""
        assert 'CFG.apiBase == null ? "" : CFG.apiBase' in script

    def test_a_trailing_slash_is_stripped(self, script: str) -> None:
        """Every request appends a path starting with "/", so a base ending in one
        would produce "//public/..." — a 404 that looks nothing like a config
        mistake."""
        assert r'.replace(/\/+$/, "")' in script

    def test_the_missing_key_error_says_the_base_is_optional(self, prose: str) -> None:
        assert "unless the API is on this same origin" in prose


class TestFailuresAreReported:
    """
    Every fallback path names the request it made and what came back. The fallback
    behaviour itself is correct and stays — refusing to render would be worse for a
    visitor — but silence is what made three different causes indistinguishable.
    """

    def test_there_is_a_single_reporting_helper(self, script: str) -> None:
        assert "function warnFailure(what, url, detail)" in script

    def test_every_diagnostic_names_the_request_url(self, script: str) -> None:
        assert '"\\n  request: " + url' in script

    @pytest.mark.parametrize(
        "call_site",
        [
            # config fetch: non-success response
            "could not load its settings",
            # config fetch: nothing came back at all
            "could not reach the API at all",
            # send: the server answered with a rejection
            "the API rejected a visitor's message.",
            # send: nothing came back at all
            "could not reach the API to send a visitor's message.",
        ],
    )
    def test_each_failure_path_reports(self, prose: str, call_site: str) -> None:
        assert call_site in prose

    def test_no_failure_path_is_silent(self, script: str) -> None:
        """Guards the actual regression: a `.catch` that swallows the reason. Every
        catch in the script must either report or be a JSON-parse recovery that
        hands the outcome on to a path that does."""
        catches = re.findall(r"\.catch\(function \([^)]*\) \{(.{0,400}?)\}\)", script, re.S)

        assert catches, "expected at least the config and send catch handlers"
        for body in catches:
            assert "warnFailure" in body, f"silent catch: {body.strip()[:120]}"

    def test_the_status_code_is_included(self, script: str) -> None:
        """A 403 (origin not allow-listed), a 404 (bad key) and a 502 (proxy) are
        three different fixes, and the server's message alone does not distinguish
        them."""
        assert '"HTTP " + res.status' in script


class TestTheSchemeMismatchHint:
    """
    The one cause worth naming explicitly, because it is invisible from both sides:
    the server logs nothing, and the browser calls it a CORS error.
    """

    def test_the_hint_exists(self, script: str) -> None:
        assert "function blockedRequestHint()" in script

    def test_it_only_fires_for_an_https_page_calling_http(self, script: str) -> None:
        assert 'window.location.protocol !== "https:"' in script
        assert 'API_BASE.indexOf("http://") !== 0' in script

    def test_it_does_not_fire_for_a_same_origin_embed(self, script: str) -> None:
        """No base means a relative request — there is no scheme to mismatch, so
        claiming one would be a false lead."""
        hint = script.split("function blockedRequestHint()")[1]
        assert 'if (!API_BASE) return "";' in hint.split("}")[0] + "}"

    def test_it_explains_the_cors_red_herring(self, prose: str) -> None:
        assert "report it as a CORS error even though the server never saw it" in prose

    def test_it_names_both_fixes(self, prose: str) -> None:
        assert (
            "Use an HTTPS apiBase, or omit apiBase entirely if the API answers on "
            "this origin." in prose
        )

    def test_it_is_on_its_own_labelled_line(self, script: str) -> None:
        """Appended to the reason it produced "Failed to fetch This page is served
        over HTTPS…" — one broken sentence, with the actionable half buried."""
        assert '"\\n  likely cause: " + hint' in script


class TestVisitorFacingMessagesStaySafe:
    def test_the_unreachable_message_names_no_infrastructure(self, prose: str) -> None:
        """The operator gets the URL and the reason in the console; the visitor gets
        a sentence that does not leak a hostname, a scheme or a status code."""
        assert '"Could not reach the chatbot service. Please try again."' in prose

    def test_a_non_json_error_body_does_not_throw(self, script: str) -> None:
        """A proxy's HTML error page used to reject inside `r.json()` and land in the
        generic catch, reporting "could not reach the API" for a request that was
        answered. Both fetches now recover and keep the status code."""
        assert script.count("function () { return { status: r.status, data: null }; }") == 1
        assert (
            script.count(
                "function () { return { ok: r.ok, status: r.status, data: null }; }"
            )
            == 1
        )


@pytest.fixture(scope="module")
def card(script: str) -> str:
    """
    Just the download card's JavaScript.

    Anchored on ``WORKING_WORDS`` rather than on the section's comment header, because
    that header's wording also opens the card's CSS — slicing on it returned the
    stylesheet, and every assertion about the card's behaviour passed against a block
    that contains none of it.
    """
    start = script.index("var WORKING_WORDS = [")
    end = script.index("function newSessionToken")

    assert start < end
    return script[start:end]


def _button(script: str) -> str:
    """Just the download-button renderer, so a match cannot come from elsewhere."""
    start = script.index("function renderFileButton")
    end = script.index("function fileMeta")

    assert start < end
    return script[start:end]


class TestTheDownloadButton:
    """
    The button a flow's Download File block offers.

    Source assertions, like the rest of this file. What is guarded is the handful of
    properties that are cheap to edit away and expensive to notice: it has to be a real
    link, its URL has to go through ``apiUrl`` (the regression the card below documents at
    length), its label has to be set as text rather than as HTML, and it must be drawn on
    the POST path only.
    """

    def test_it_is_drawn_from_the_turn_payload_on_the_post_path_only(
        self, script: str,
    ) -> None:
        """
        A flow turn never streams — ``chatbot_turn_service.stream_turn`` refuses one — so a
        button payload cannot arrive on the SSE path. Wiring it there would imply it could.
        """
        assert "function renderFileButton(container, payload, cfg)" in script
        assert script.count("renderFileButton(messagesEl, ") == 1

    def test_it_is_an_anchor_and_not_a_button(self, script: str) -> None:
        """
        A real link, so middle-click, "save as" and keyboard Enter all work — and so the
        browser's own download machinery does the transfer rather than a fetch holding the
        whole file in memory to hand it back to the same browser.
        """
        button = _button(script)

        assert 'document.createElement("a")' in button
        assert 'link.setAttribute("download"' in button
        assert "link.href = apiUrl(payload.url)" in button

    def test_its_url_is_not_left_relative(self, script: str) -> None:
        """
        The same regression the card's own test documents: a URL used as it arrives
        resolves against the *embedding page*, so the visitor's own site is asked for the
        file and the failure is silent.
        """
        for expression in re.findall(r"\.href = ([^;\n]*)", _button(script)):
            assert "apiUrl(" in expression, f"unresolved URL: {expression}"

    def test_the_label_is_set_as_text_and_never_as_html(self, script: str) -> None:
        """
        The label is operator-authored and may have had a visitor's own words interpolated
        into it, which makes it exactly as untrusted as message text.
        """
        button = _button(script)

        assert "link.textContent =" in button
        # An *assignment*, not the word: the renderer's own comment says "textContent,
        # never innerHTML", and a test that banned the word would delete the explanation.
        assert "innerHTML =" not in button

    def test_a_payload_with_no_url_draws_nothing(self, script: str) -> None:
        assert "if (!payload || !payload.url) return;" in _button(script)

    def test_the_colour_falls_back_rather_than_leaving_it_unstyled(
        self, script: str,
    ) -> None:
        """A payload from an older server still gets a button, in the brand colour."""
        assert "payload.colour || cfg.brand_color" in _button(script)


class TestTheDownloadCard:
    """
    The file a visitor asked for, from queued to clickable.

    Source assertions rather than behaviour, matching the rest of this file — the card's
    behaviour is exercised against a real browser instead. What is guarded here is the
    handful of properties that are easy to edit away by accident and expensive to notice:
    the card must be a real link, it must stop watching when the build ends, and it must
    never touch the widget's input.
    """

    def test_the_card_is_rendered_from_the_turn_payload(self, script: str) -> None:
        """Both reply paths — the blocking POST and the stream's `done` — draw it."""
        assert script.count("renderDownloadCard(messagesEl, ") == 2
        assert "function renderDownloadCard(container, download, cfg)" in script

    def test_a_ready_export_becomes_an_anchor_and_not_a_button(
        self, script: str,
    ) -> None:
        """
        A real link, so middle-click, "save as" and keyboard Enter all work. A ``<button>``
        with a click handler looks identical and does none of them.
        """
        assert 'document.createElement("a")' in script
        assert 'link.setAttribute("download"' in script
        assert "link.href = apiUrl(view.download_url)" in script

    def test_no_url_the_card_uses_is_left_relative(self, card: str) -> None:
        """
        The regression this test exists for, and it failed silently in a browser.

        A URL used as it arrives resolves against the *embedding page*, not against this
        application. The anchor shipped that way once: the visitor was told "file wasn't
        available on site" for a file that existed and was being served perfectly a
        hostname away, because their own site had been asked for it. Nothing threw,
        nothing was logged, and the card looked correct.

        Every network URL in the card now goes through ``apiUrl()``, which passes an
        already-absolute URL (what the server sends when SITE_URL is configured) through
        untouched and prefixes ``API_BASE`` onto a bare path. Written as a sweep rather
        than three fixed strings so a fourth network call cannot be added without one.
        """
        uses = re.findall(r"(?:new EventSource\(|fetch\(|\.href = )([^;\n]*)", card)

        assert len(uses) >= 3, "expected the stream, the status poll and the download"
        for expression in uses:
            assert "apiUrl(" in expression, f"unresolved URL in the card: {expression}"

    def test_an_absolute_url_is_not_prefixed_a_second_time(self, script: str) -> None:
        """
        The download URL arrives absolute whenever SITE_URL is set on the server.

        Prefixing that with apiBase would produce
        "https://api.example.com/https://api.example.com/file_downloaders/..." — a link
        that fails in a way pointing at nothing in particular.
        """
        assert "function apiUrl(url)" in script
        assert "if (/^https?:\\/\\//i.test(value)) return value;" in script

    def test_the_link_is_only_drawn_once_the_artifact_exists(
        self, script: str,
    ) -> None:
        """A download button for an export that is not ready is a button that 404s."""
        assert 'if (status === "ready" && view.download_url)' in script

    def test_the_progress_bar_is_a_real_fraction_and_never_reaches_full(
        self, script: str,
    ) -> None:
        """A bar sitting at 100% beside "still working" is the one thing it must not
        say, so it is capped until the artifact exists."""
        assert "Math.round((written / total) * 100)" in script
        assert "Math.min(99," in script

    def test_the_working_words_rotate(self, script: str) -> None:
        assert "var WORKING_WORDS = [" in script
        assert "WORKING_WORDS[state.wordIndex % WORKING_WORDS.length]" in script

    def test_every_timer_and_socket_is_released_on_a_terminal_state(
        self, script: str,
    ) -> None:
        """
        An EventSource left open is reopened by the browser forever, and the widget
        would re-run the progress stream for a file that finished long ago.
        """
        assert "function settle(state)" in script
        for teardown in (
            "window.clearInterval(state.wordTimer)",
            "window.clearInterval(state.pollTimer)",
            "state.source.close()",
        ):
            assert teardown in script

        # Reached from both endings, which is the whole point of it being one function.
        assert script.count("settle(state);") >= 2

    def test_a_dropped_progress_stream_falls_back_to_polling(
        self, script: str,
    ) -> None:
        """A build can outlast one SSE connection. A card frozen by a dead socket must
        not read as a dead build."""
        assert "function pollStatus(state)" in script
        assert "if (!state.settled) pollStatus(state);" in script

    def test_the_card_never_touches_the_input(self, card: str) -> None:
        """
        "You can keep asking while it builds" is exactly this: the turn ended when the
        reply arrived, and the build is not a turn. If the card ever disabled the input
        or drove the typing indicator, a long export would lock the widget.
        """
        for forbidden in ("inputEl", "sendBtn", "typingEl", "armIdleTimer"):
            assert forbidden not in card, f"the download card touches {forbidden}"


class TestTheMarkdownRendererIsSafeByConstruction:
    """
    The structural half of the renderer's guarantee.

    `test_widget_markdown.py` proves the behaviour by executing it, but needs Node and
    skips in the app container. These assertions run everywhere, and they guard the
    one property the whole design rests on: the escape happens *first*, before any
    markdown pattern is examined. Parse-then-escape produces identical output for
    every benign input and is a cross-site scripting hole, so it cannot be left to a
    test that might be skipped.
    """

    def test_the_escape_happens_before_any_parsing(self, script: str) -> None:
        body = script[script.index("function renderMarkdown("):]
        body = body[:body.index("\n  }")]
        flat = " ".join(body.split())

        # The very first thing done to the input.
        assert "var lines = escapeHtml(String(text == null ? \"\" : text))" in flat

    def test_the_summary_goes_through_the_renderer(self, script: str) -> None:
        assert "var html = renderMarkdown(result.summary" in script

    def test_insights_are_escaped_before_emphasis_is_applied(self, script: str) -> None:
        """
        The one caller of inlineMarkdown outside renderMarkdown. Handing it raw text
        would apply emphasis to unescaped markup — the exact ordering mistake the
        module docstring warns about, made in the one place that looks harmless.
        """
        assert "inlineMarkdown(escapeHtml(i))" in script

    def test_no_anchor_or_image_is_ever_constructed(self, script: str) -> None:
        """
        `[text](javascript:…)` is how markdown becomes script execution. Links are
        not supported at all rather than sanitised, so there is no URL scheme check
        to get wrong.
        """
        # Bounded to the renderer's own block. Slicing to the end of the file would
        # sweep in the download card, which sets an anchor's href quite legitimately.
        renderer = script[
            script.index("function renderMarkdown("):script.index("function buildDom(")
        ]

        assert '"<a' not in renderer
        assert "href" not in renderer
        assert '"<img' not in renderer

    def test_quotes_are_escaped_as_well_as_angle_brackets(self, script: str) -> None:
        """
        Not needed by today's callers — nothing builds an attribute from message
        text — and kept so that the day one does, it gets a working escape rather
        than an attribute break.
        """
        assert 'replace(/"/g, "&quot;")' in script


class TestBothReplyPathsRenderMarkdown:
    """
    A reply must look the same whether it streamed or was posted.

    The widget answers a turn two ways — an SSE stream for a data-agent chatbot,
    a POST for everything else — and only the POST path rendered Markdown. So the
    identical answer displayed as a table one way and as raw `**bold**` and a wall
    of `|` characters the other, decided by which transport happened to be taken.

    The model is *instructed* to write Markdown tables (prompt_builder grounding
    rule 15), so this was not a rare shape. It is the shape of every multi-row
    answer a data agent gives.
    """

    @pytest.fixture()
    def paint(self, script: str) -> str:
        """The streaming painter, which owns every frame of a streamed reply."""
        body = script[script.index("      function paint() {"):]
        return body[:body.index("\n      }") + 8]

    def test_the_streamed_reply_goes_through_the_renderer(self, paint: str) -> None:
        assert "renderMarkdown(answer)" in paint

    def test_the_streamed_reply_is_never_painted_as_raw_text(self, paint: str) -> None:
        """
        The regression itself. `textContent = answer` renders correctly-formed
        Markdown as its own source, which is what the visitor was shown.
        """
        assert "textContent" not in paint

    def test_the_raw_answer_never_reaches_innerHTML(self, paint: str) -> None:
        """
        The other way to fix the above, and the one that is a cross-site scripting
        hole: `innerHTML = answer` puts model-written text on somebody else's page
        unescaped. Only renderMarkdown's output — escaped before it was parsed —
        may be assigned here.
        """
        assert "innerHTML = answer" not in paint
        assert "innerHTML = renderMarkdown(answer)" in paint

    def test_the_final_answer_is_repainted_and_not_appended(self, script: str) -> None:
        """
        `done` carries the whole answer, which replaces whatever the tokens built.
        It has to go through paint() rather than be written directly, or the last
        frame of a streamed turn would be the one that skipped the renderer.
        """
        assert "if (payload.answer) { answer = payload.answer; paint(); }" in script
