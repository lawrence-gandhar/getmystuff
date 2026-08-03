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
