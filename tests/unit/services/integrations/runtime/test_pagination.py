"""
Tests for ``runtime/pagination.py``.

Paging is the step most likely to be got subtly wrong, and a paging bug does not fail —
it returns the first page and reports success. So the assertions here are mostly about
stopping, not about continuing.

**Three stop conditions on every kind**, and the third is the one a number cannot
express: a repeated cursor. An API that hands out the same ``next`` token twice would
otherwise loop forever, unattended, spending an API quota all night on the same page.

**A ``next_url`` pointing at a different origin is refused.** That URL is chosen by the
server being read; following it unchecked hands the choice of destination to whoever
controls the response, through a feature we built on purpose.

**``link_header`` is used verbatim.** Shopify rejects ``page_info`` combined with a
filter, so rebuilding the URL from parsed parameters fails in exactly the case that
matters — and fails with a 400 that reads like a bad query rather than like a paging bug.
"""

from __future__ import annotations

import pytest

from app.services.integrations.connectors.spec import (
    PAGE_CURSOR,
    PAGE_INPUT_CURSOR,
    PAGE_LINK_HEADER,
    PAGE_NEXT_URL,
    PAGE_NONE,
    PAGE_NUMBER,
    PAGE_OFFSET,
    PageRule,
)
from app.services.integrations.errors import NodeFailure
from app.services.integrations.runtime import pagination

FIRST = "https://api.example.com/orders"


def walk(rule: PageRule) -> pagination.PageWalk:
    return pagination.begin(rule, FIRST)


def step(w, *, payload=None, headers=None, records=10):
    return pagination.advance(
        w, payload=payload or {}, headers=headers or {}, records_in_page=records
    )


class TestFirstPageParams:
    def test_no_paging_asks_for_nothing(self) -> None:
        assert pagination.first_page_params(PageRule()) == {}

    def test_a_page_number_starts_where_the_rule_says(self) -> None:
        rule = PageRule(kind=PAGE_NUMBER, param="page", start_at=1)

        assert pagination.first_page_params(rule) == {"page": 1}

    def test_an_offset_starts_at_zero_when_the_rule_says_so(self) -> None:
        rule = PageRule(kind=PAGE_OFFSET, param="offset", start_at=0)

        assert pagination.first_page_params(rule) == {"offset": 0}

    def test_a_cursor_sends_none_on_the_first_request(self) -> None:
        """
        Sending an empty cursor is how you get a 400 from an API that would have been
        perfectly happy with no parameter at all.
        """
        rule = PageRule(kind=PAGE_CURSOR, param="after", cursor_path="meta.next")

        assert pagination.first_page_params(rule) == {}

    def test_a_declared_page_size_is_asked_for(self) -> None:
        rule = PageRule(kind=PAGE_NUMBER, param="page", size_param="limit", size=250)

        assert pagination.first_page_params(rule) == {"limit": 250, "page": 1}


class TestThreePages:
    def test_page_number(self) -> None:
        w = walk(PageRule(kind=PAGE_NUMBER, param="page", start_at=1))

        seen = [dict(w.params)]
        for _ in range(2):
            w = step(w)
            seen.append(dict(w.params))

        assert seen == [{"page": 1}, {"page": 2}, {"page": 3}]

    def test_offset_steps_by_what_was_actually_returned(self) -> None:
        """
        Not by the declared page size. A vendor is free to return fewer than it was
        asked for, and an offset computed from the request skips the difference —
        silently, as a gap in the middle of a sync.
        """
        w = walk(PageRule(kind=PAGE_OFFSET, param="offset", size_param="limit",
                          size=100, start_at=0))

        w = step(w, records=100)
        assert w.params["offset"] == 100

        w = step(w, records=40)  # the vendor gave us fewer
        assert w.params["offset"] == 140

    def test_cursor(self) -> None:
        rule = PageRule(kind=PAGE_CURSOR, param="after", cursor_path="meta.next")
        w = walk(rule)

        w = step(w, payload={"meta": {"next": "c1"}})
        assert w.params == {"after": "c1"}

        w = step(w, payload={"meta": {"next": "c2"}})
        assert w.params == {"after": "c2"}

    def test_next_url(self) -> None:
        # The quoted form: SAP sends `@odata.nextLink` as one key, dot included. The
        # unquoted path would mean "the nextLink inside @odata" and read nothing.
        rule = PageRule(kind=PAGE_NEXT_URL, param="", cursor_path='["@odata.nextLink"]')
        w = walk(rule)

        w = step(w, payload={"@odata.nextLink": f"{FIRST}?skip=100"})

        assert w.next_url == f"{FIRST}?skip=100"
        assert w.finished is False

    def test_link_header(self) -> None:
        w = walk(PageRule(kind=PAGE_LINK_HEADER))

        w = step(w, headers={"link": f'<{FIRST}?page_info=abc>; rel="next"'})

        assert w.next_url == f"{FIRST}?page_info=abc"


class TestStopping:
    def test_no_paging_stops_after_one(self) -> None:
        w = step(walk(PageRule(kind=PAGE_NONE)))

        assert w.finished is True
        assert "one response" in w.stopped_because

    def test_an_empty_page_is_the_end_whatever_the_cursor_says(self) -> None:
        """
        A vendor that keeps handing out cursors for empty pages is one we stop believing
        here rather than after a thousand pages.
        """
        rule = PageRule(kind=PAGE_CURSOR, param="after", cursor_path="meta.next")

        w = step(walk(rule), payload={"meta": {"next": "c1"}}, records=0)

        assert w.finished is True
        assert "empty" in w.stopped_because

    def test_no_cursor_is_the_end(self) -> None:
        rule = PageRule(kind=PAGE_CURSOR, param="after", cursor_path="meta.next")

        w = step(walk(rule), payload={"meta": {}})

        assert w.finished is True
        assert "no cursor" in w.stopped_because

    def test_has_more_false_is_believed_over_a_cursor(self) -> None:
        """
        An API that says there is no more and sends a cursor anyway is describing a page
        that does not exist — and following it is how a walk goes round again on the
        last page.
        """
        rule = PageRule(
            kind=PAGE_CURSOR,
            param="after",
            cursor_path="meta.next",
            has_more_path="meta.has_more",
        )

        w = step(walk(rule), payload={"meta": {"next": "c1", "has_more": False}})

        assert w.finished is True

    def test_the_page_limit_stops_and_says_there_may_be_more(self) -> None:
        """
        The reason matters: "stopped at the limit" and "the API said that was the last
        one" are very different facts about a sync, and only one means everything was
        read.
        """
        rule = PageRule(kind=PAGE_NUMBER, param="page", max_pages=3)
        w = walk(rule)

        for _ in range(3):
            w = step(w)

        assert w.finished is True
        assert "there may be more" in w.stopped_because
        assert pagination.describe_walk(w)["complete"] is False

    def test_the_record_limit_stops(self) -> None:
        rule = PageRule(kind=PAGE_NUMBER, param="page", max_records=25)
        w = walk(rule)

        w = step(w, records=10)
        w = step(w, records=10)
        w = step(w, records=10)

        assert w.finished is True
        assert "records was reached" in w.stopped_because

    def test_a_clean_finish_is_reported_as_complete(self) -> None:
        rule = PageRule(kind=PAGE_CURSOR, param="after", cursor_path="meta.next")
        w = step(walk(rule), payload={"meta": {}})

        assert pagination.describe_walk(w)["complete"] is True


class TestTheRepeatGuard:
    """The stop condition a number cannot express. See the module docstring."""

    def test_the_same_cursor_twice_stops_the_walk(self) -> None:
        rule = PageRule(kind=PAGE_CURSOR, param="after", cursor_path="meta.next")
        w = walk(rule)

        w = step(w, payload={"meta": {"next": "stuck"}})
        assert w.finished is False

        w = step(w, payload={"meta": {"next": "stuck"}})

        assert w.finished is True
        assert "same page marker twice" in w.stopped_because

    def test_the_same_next_url_twice_stops_the_walk(self) -> None:
        rule = PageRule(kind=PAGE_NEXT_URL, cursor_path="next")
        w = walk(rule)

        w = step(w, payload={"next": f"{FIRST}?skip=100"})
        w = step(w, payload={"next": f"{FIRST}?skip=100"})

        assert w.finished is True
        assert "same next-page link twice" in w.stopped_because

    def test_a_repeated_page_number_stops_it_too(self) -> None:
        """
        A page-number walk cannot repeat by itself, but the guard is on every kind
        rather than only the ones that look risky — the cost is a set, and the failure
        it prevents is an unattended overnight loop.
        """
        rule = PageRule(kind=PAGE_NUMBER, param="page", start_at=1)
        w = walk(rule)
        w = step(w)

        # Force the state a bug would produce.
        w.page_index = 0
        w = step(w)

        assert w.finished is True


class TestNextUrlIsChecked:
    """
    The URL is chosen by the server being read. See the module docstring.
    """

    def test_a_different_host_is_refused(self) -> None:
        rule = PageRule(kind=PAGE_NEXT_URL, cursor_path="next")
        w = walk(rule)

        with pytest.raises(NodeFailure, match="points somewhere other than the API"):
            step(w, payload={"next": "https://evil.example.com/orders?skip=100"})

    def test_a_different_scheme_is_refused(self) -> None:
        rule = PageRule(kind=PAGE_NEXT_URL, cursor_path="next")
        w = walk(rule)

        with pytest.raises(NodeFailure):
            step(w, payload={"next": "http://api.example.com/orders?skip=100"})

    def test_a_lookalike_host_is_refused(self) -> None:
        rule = PageRule(kind=PAGE_NEXT_URL, cursor_path="next")
        w = walk(rule)

        with pytest.raises(NodeFailure):
            step(w, payload={"next": "https://api.example.com.evil.net/orders"})

    def test_the_refusal_says_only_the_first_page_was_read(self) -> None:
        """The operator needs to know the sync is incomplete, not merely that something
        was blocked."""
        rule = PageRule(kind=PAGE_NEXT_URL, cursor_path="next")

        with pytest.raises(NodeFailure, match="Only the first page was read"):
            step(walk(rule), payload={"next": "https://evil.example.com/x"})


class TestLinkHeaderParsing:
    def test_the_next_url_is_returned_verbatim(self) -> None:
        """
        Rebuilding it from parsed parameters drops ``page_info``, and Shopify rejects
        ``page_info`` combined with filters — so the reconstruction fails in exactly the
        case that matters.
        """
        header = (
            '<https://x.myshopify.com/admin/api/2026-01/orders.json?'
            'page_info=eyJsYXN0X2lkIjo1&limit=250>; rel="next"'
        )

        assert pagination.next_link_from_header(header) == (
            "https://x.myshopify.com/admin/api/2026-01/orders.json"
            "?page_info=eyJsYXN0X2lkIjo1&limit=250"
        )

    def test_it_finds_next_among_several_links(self) -> None:
        header = '<https://x/a>; rel="previous", <https://x/b>; rel="next"'

        assert pagination.next_link_from_header(header) == "https://x/b"

    def test_it_accepts_an_unquoted_rel(self) -> None:
        assert pagination.next_link_from_header("<https://x/b>; rel=next") == "https://x/b"

    def test_no_next_link_is_empty(self) -> None:
        assert pagination.next_link_from_header('<https://x/a>; rel="previous"') == ""
        assert pagination.next_link_from_header("") == ""

    def test_a_response_with_no_link_header_ends_the_walk(self) -> None:
        w = step(walk(PageRule(kind=PAGE_LINK_HEADER)), headers={})

        assert w.finished is True
        assert "no link to a next page" in w.stopped_because


class TestDescribeWalk:
    def test_it_records_what_the_step_row_needs(self) -> None:
        rule = PageRule(kind=PAGE_CURSOR, param="after", cursor_path="meta.next")
        w = walk(rule)
        w = step(w, payload={"meta": {"next": "c1"}}, records=10)
        w = step(w, payload={"meta": {}}, records=7)

        described = pagination.describe_walk(w)

        assert described["pages"] == 2
        assert described["records"] == 17
        assert described["complete"] is True


class TestInputCursor:
    """
    ``input_cursor`` — the kind that exists because a GraphQL cursor cannot live in a
    query string.

    The distinction being tested is *where the cursor lands*, so every assertion here is
    about ``walk.arguments`` rather than ``walk.params``. A test that only checked "the
    walk continued" would pass just as well against the query-string kind, which is the
    thing this was written to stop being.
    """

    RULE = PageRule(
        kind=PAGE_INPUT_CURSOR,
        param="cursor",
        size_param="page_size",
        size=250,
        cursor_path="data.orders.pageInfo.endCursor",
        has_more_path="data.orders.pageInfo.hasNextPage",
    )

    @staticmethod
    def page(cursor: str, *, more: bool = True) -> dict:
        return {
            "data": {
                "orders": {"pageInfo": {"hasNextPage": more, "endCursor": cursor}}
            }
        }

    def test_page_one_asks_for_a_size_and_no_cursor(self) -> None:
        """An empty cursor is not the same as no cursor, and several APIs 400 on one."""
        w = walk(self.RULE)

        assert w.arguments == {"page_size": 250}
        assert "cursor" not in w.arguments

    def test_page_one_puts_nothing_in_the_query_string(self) -> None:
        """
        The whole point. A stray ``?page_size=250`` beside a GraphQL body that already
        asked for 250 is harmless against Shopify and rejected by stricter APIs — and it
        would mean this kind was only half distinct from ``cursor``.
        """
        assert walk(self.RULE).params == {}

    def test_the_cursor_arrives_as_an_argument(self) -> None:
        w = step(walk(self.RULE), payload=self.page("CUR2"))

        assert w.finished is False
        assert w.arguments == {"page_size": 250, "cursor": "CUR2"}
        assert w.params == {}

    def test_it_walks_three_pages(self) -> None:
        w = walk(self.RULE)
        w = step(w, payload=self.page("CUR2"))
        w = step(w, payload=self.page("CUR3"))
        w = step(w, payload=self.page("", more=False))

        assert w.page_index == 3
        assert w.finished is True

    def test_has_next_page_false_stops_even_with_a_cursor(self) -> None:
        """
        Shopify keeps sending an ``endCursor`` on the last page. Believing the cursor
        alone would mean one wasted request per read, for ever.
        """
        w = step(walk(self.RULE), payload=self.page("CUR2", more=False))

        assert w.finished is True

    def test_a_repeated_cursor_stops_the_walk(self) -> None:
        """
        The same guard the query-string kind uses, and shared on purpose: a vendor handing
        out one token forever does not care which carrier it travelled in.
        """
        w = walk(self.RULE)
        w = step(w, payload=self.page("SAME"))
        w = step(w, payload=self.page("SAME"))

        assert w.finished is True
        assert "same page marker twice" in w.stopped_because

    def test_no_cursor_stops_the_walk(self) -> None:
        w = step(
            walk(self.RULE),
            payload={"data": {"orders": {"pageInfo": {"hasNextPage": True}}}},
        )

        assert w.finished is True
        assert "no cursor" in w.stopped_because
