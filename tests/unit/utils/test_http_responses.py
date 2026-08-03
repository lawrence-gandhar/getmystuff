"""
Tests for app/utils/http_responses.py — the HTML alert fragments HTMX swaps
into the page on success and failure.

Two properties matter here. The status code must survive, because HTMX decides
whether to swap at all based on it and the caller distinguishes a conflict from
bad input by it. And the message must be escaped: these alerts routinely carry
values the user typed (datasource name, host, database name), so an unescaped
f-string would turn every failure path into an injection point.
"""

from __future__ import annotations

import pytest

from app.utils.http_responses import (
    GENERIC_ERROR_MESSAGE,
    html_error_response,
    html_success_response,
)


def _body(response) -> str:
    """Read the rendered fragment out of a Litestar Response."""
    content = response.content
    return content.decode() if isinstance(content, bytes) else content


class TestErrorResponse:
    def test_message_is_wrapped_in_a_danger_alert(self) -> None:
        response = html_error_response("Could not connect to the database.")

        assert "alert alert-danger" in _body(response)
        assert "Could not connect to the database." in _body(response)

    def test_defaults_to_400(self) -> None:
        assert html_error_response("nope").status_code == 400

    @pytest.mark.parametrize("status_code", [400, 403, 409, 422, 500])
    def test_status_code_is_preserved(self, status_code: int) -> None:
        """HTMX and the caller both branch on this — it must not be flattened."""
        assert html_error_response("nope", status_code).status_code == status_code

    def test_served_as_html(self) -> None:
        assert "text/html" in html_error_response("nope").media_type

    def test_non_string_detail_is_coerced(self) -> None:
        """`e.detail` and bare exception objects are both passed in by routes."""
        response = html_error_response(ValueError("bad host"), 403)

        assert "bad host" in _body(response)


class TestEscaping:
    @pytest.mark.parametrize(
        "payload",
        [
            "<script>alert(1)</script>",
            "<img src=x onerror=alert(1)>",
            "</div><script>alert(1)</script>",
        ],
    )
    def test_markup_in_the_message_is_never_rendered_as_markup(
        self, payload: str
    ) -> None:
        """
        The datasource name, host and database name are echoed back in failure
        messages, and all three are user-supplied.
        """
        body = _body(html_error_response(f"Could not connect to '{payload}'"))
        inner = body[len("<div class='alert alert-danger'>"):-len("</div>")]

        # Nothing inside the alert may still be a tag: with every `<` and `>`
        # escaped, an `onerror=` or `alert(1)` left in the text is inert.
        assert "<" not in inner
        assert ">" not in inner
        assert "&lt;" in inner

    def test_success_messages_are_escaped_too(self) -> None:
        body = _body(html_success_response("<b>done</b>"))

        assert "<b>" not in body
        assert "&lt;b&gt;done&lt;/b&gt;" in body

    def test_the_alert_wrapper_itself_survives_escaping(self) -> None:
        """Only the message is escaped — the surrounding div must stay markup."""
        body = _body(html_error_response("plain"))

        assert body.startswith("<div class='alert alert-danger'>")
        assert body.endswith("</div>")


class TestEmptyDetail:
    @pytest.mark.parametrize("detail", ["", "   ", "\n\t"])
    def test_blank_detail_falls_back_to_the_generic_message(self, detail: str) -> None:
        """An empty alert tells the user nothing; never render one."""
        assert GENERIC_ERROR_MESSAGE in _body(html_error_response(detail))

    def test_the_generic_message_leaks_nothing(self) -> None:
        assert "Traceback" not in GENERIC_ERROR_MESSAGE
        assert "Error" not in GENERIC_ERROR_MESSAGE


class TestSuccessResponse:
    def test_message_is_wrapped_in_a_success_alert(self) -> None:
        body = _body(html_success_response("Datasource renamed successfully"))

        assert "alert alert-success" in body
        assert "Datasource renamed successfully" in body

    def test_defaults_to_200(self) -> None:
        assert html_success_response("ok").status_code == 200

    def test_served_as_html(self) -> None:
        assert "text/html" in html_success_response("ok").media_type
