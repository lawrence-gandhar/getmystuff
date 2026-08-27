"""
Tests for ``runtime/response_reader.py``.

Three refusals, and each prevents a failure that would otherwise read as success.

**Oversize raises rather than truncating.** ``chatbot_action_service`` truncates, and
that is right there — the result is shown to a person who can see it was cut off. Here
it is *parsed*, and a truncated document is invalid JSON at best and a silently short
record list at worst.

**A 2xx that is not JSON is refused.** A WAF challenge, an expired-session redirect
rendered as HTML and a maintenance notice all arrive as ``200 text/html``. Parsing one as
an empty list reports "0 records synced" as success, and nobody investigates a green run.

**An error body is kept, redacted.** The destination's own message is more specific than
anything we could compose, and an error body frequently echoes the request that caused it
— headers included.
"""

from __future__ import annotations

from typing import Dict, Optional

import pytest

from app.services.integrations.errors import NodeFailure
from app.services.integrations.runtime.response_reader import (
    MAX_RESPONSE_BYTES,
    ReadResponse,
    failure_message,
    read_json,
    retry_after_seconds,
    vendor_message,
)


class FakeResponse:
    """
    A streaming response's duck type: a status, headers and ``aiter_bytes``.

    Chunked deliberately, so the cap is exercised the way a real body arrives — a single
    chunk would let a cap that only checks the total still pass.
    """

    def __init__(
        self,
        status_code: int = 200,
        headers: Optional[Dict[str, str]] = None,
        body: bytes = b"",
        chunk_size: int = 64 * 1024,
    ) -> None:
        self.status_code = status_code
        self.headers = headers if headers is not None else {"content-type": "application/json"}
        self._body = body
        self._chunk_size = chunk_size

    async def aiter_bytes(self):  # noqa: ANN201
        for start in range(0, len(self._body), self._chunk_size) or [0]:
            yield self._body[start : start + self._chunk_size]


class TestReadingJson:
    async def test_a_json_body_is_parsed(self) -> None:
        read = await read_json(FakeResponse(body=b'{"orders": [{"id": 1}]}'))

        assert read.ok is True
        assert read.payload == {"orders": [{"id": 1}]}

    async def test_an_empty_body_is_not_an_error(self) -> None:
        """204 No Content, and the several APIs that answer a successful delete with
        nothing. Not an error and not a record."""
        read = await read_json(FakeResponse(status_code=204, body=b""))

        assert read.ok is True
        assert read.payload is None

    async def test_a_content_type_with_a_charset_still_counts(self) -> None:
        read = await read_json(
            FakeResponse(
                headers={"content-type": "application/json; charset=utf-8"},
                body=b"{}",
            )
        )

        assert read.payload == {}

    async def test_a_vendor_json_suffix_counts(self) -> None:
        """``application/vnd.api+json`` is JSON, and refusing it would refuse a whole
        class of perfectly ordinary API."""
        read = await read_json(
            FakeResponse(headers={"content-type": "application/vnd.api+json"}, body=b"[]")
        )

        assert read.payload == []

    async def test_a_body_that_claims_json_and_is_not(self) -> None:
        read = FakeResponse(body=b"<html>oops</html>")

        with pytest.raises(NodeFailure, match="said it was sending JSON"):
            await read_json(read)


class TestTheByteCap:
    async def test_an_oversize_body_raises_rather_than_truncating(self) -> None:
        big = b"x" * (MAX_RESPONSE_BYTES + 1)

        with pytest.raises(NodeFailure, match="more than"):
            await read_json(FakeResponse(body=big))

    async def test_the_refusal_says_what_to_change(self) -> None:
        """A cap the operator cannot act on is a dead end; the batch size is the lever
        they actually have."""
        with pytest.raises(NodeFailure, match="Reduce the batch size"):
            await read_json(FakeResponse(body=b"x" * 200), max_bytes=100)

    async def test_it_stops_before_the_whole_body_is_in_memory(self) -> None:
        """
        The point of streaming. A cap applied after reading is a report of how much was
        allocated, not a limit — so the refusal must arrive while chunks are still
        coming.
        """
        consumed = []

        class Counting(FakeResponse):
            async def aiter_bytes(self):  # noqa: ANN201
                for index in range(100):
                    consumed.append(index)
                    yield b"x" * 100

        with pytest.raises(NodeFailure):
            await read_json(Counting(), max_bytes=250)

        assert len(consumed) < 10

    async def test_a_body_exactly_at_the_cap_is_fine(self) -> None:
        body = b'"' + b"x" * 98 + b'"'

        read = await read_json(FakeResponse(body=body), max_bytes=len(body))

        assert read.payload == "x" * 98


class TestNonJsonOnSuccess:
    """The asymmetry: refused on 2xx, kept on 4xx/5xx. See the module docstring."""

    async def test_html_on_a_200_is_refused(self) -> None:
        response = FakeResponse(
            headers={"content-type": "text/html"},
            body=b"<html><body>Please sign in</body></html>",
        )

        with pytest.raises(NodeFailure) as caught:
            await read_json(response)

        assert "text/html" in str(caught.value)
        assert "sign-in page" in str(caught.value) or "security check" in str(caught.value)

    async def test_the_refusal_is_permanent(self) -> None:
        """Retrying a WAF challenge produces another WAF challenge."""
        response = FakeResponse(headers={"content-type": "text/html"}, body=b"<html>")

        with pytest.raises(NodeFailure) as caught:
            await read_json(response)

        assert caught.value.permanent is True

    async def test_html_on_a_500_is_kept_as_the_explanation(self) -> None:
        """
        Throwing it away would leave the operator with a status code and nothing else.
        """
        response = FakeResponse(
            status_code=500,
            headers={"content-type": "text/html"},
            body=b"<html>Gateway is restarting</html>",
        )

        read = await read_json(response)

        assert read.ok is False
        assert "Gateway is restarting" in read.text

    async def test_a_body_with_no_content_type_on_a_200_is_refused(self) -> None:
        with pytest.raises(NodeFailure, match="no content type"):
            await read_json(FakeResponse(headers={}, body=b"something"))


class TestVendorMessage:
    @pytest.mark.parametrize(
        "payload",
        [
            {"message": "Email has already been taken"},
            {"error": "Email has already been taken"},
            {"detail": "Email has already been taken"},
            {"error_description": "Email has already been taken"},
        ],
    )
    def test_it_finds_the_message_wherever_the_vendor_put_it(self, payload: dict) -> None:
        read = ReadResponse(status_code=422, payload=payload)

        assert vendor_message(read) == "Email has already been taken"

    def test_a_structured_error_is_serialised_rather_than_dropped(self) -> None:
        read = ReadResponse(status_code=422, payload={"errors": {"email": ["taken"]}})

        assert "taken" in vendor_message(read)

    def test_an_echoed_credential_is_redacted(self) -> None:
        """
        An error body frequently echoes the request that caused it. This is the one
        function every error path goes through, so the redaction belongs here rather
        than at each caller.
        """
        read = ReadResponse(
            status_code=400,
            payload={"message": "bad request", "request": {"Authorization": "Bearer sk-live-1"}},
        )

        assert "sk-live-1" not in str(vendor_message(ReadResponse(
            status_code=400, payload=read.payload
        )))

    def test_a_text_body_is_used_when_there_is_no_payload(self) -> None:
        read = ReadResponse(status_code=502, text="upstream connect error")

        assert vendor_message(read) == "upstream connect error"

    def test_nothing_at_all_is_empty(self) -> None:
        assert vendor_message(ReadResponse(status_code=500)) == ""


class TestFailureMessage:
    def test_it_prefers_the_destinations_own_words(self) -> None:
        read = ReadResponse(status_code=422, payload={"message": "Email has already been taken"})

        message = failure_message(read, label="'Shopify EU'")

        assert "Email has already been taken" in message
        assert "422" in message

    def test_it_translates_the_status_for_somebody_who_is_not_an_engineer(self) -> None:
        read = ReadResponse(status_code=401)

        assert "not signed in" in failure_message(read, label="'Shopify EU'")

    def test_an_unknown_status_still_produces_a_sentence(self) -> None:
        read = ReadResponse(status_code=418)

        assert "418" in failure_message(read, label="'X'")


class TestRetryAfter:
    def test_a_numeric_value_is_read(self) -> None:
        assert retry_after_seconds({"retry-after": "30"}) == 30.0

    def test_the_header_name_is_case_insensitive(self) -> None:
        assert retry_after_seconds({"Retry-After": "30"}) == 30.0

    def test_a_date_form_is_deliberately_not_parsed(self) -> None:
        """
        It needs a clock comparison against a server whose clock we cannot see, and every
        API in scope sends the numeric form. Guessing produces a wait that is wrong in
        whichever direction the clocks differ.
        """
        assert retry_after_seconds({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None

    def test_no_header_is_none(self) -> None:
        assert retry_after_seconds({}) is None

    def test_a_negative_value_is_clamped_to_zero(self) -> None:
        assert retry_after_seconds({"retry-after": "-5"}) == 0.0
