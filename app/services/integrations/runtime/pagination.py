"""
Getting the next page, seven ways, and three reasons to stop.

Paging is the step most likely to be got subtly wrong, and a paging bug does not fail —
it returns the first page and reports success. Everything here is arranged around that.

**Three stop conditions apply to every kind**, not just to the ones that look risky:

``max_pages`` and ``max_records``
    Numbers, on the :class:`PageRule`.

**a repeated cursor**
    Not expressible as a number, and the one that catches a malformed vendor response.
    An API that returns the same ``next`` token twice would otherwise loop forever,
    unattended, at whatever the rate limit allows — spending an API quota all night on
    the same page.

**``link_header`` uses the URL verbatim.** Shopify's ``Link: <…>; rel="next"`` carries a
``page_info`` token, and Shopify rejects ``page_info`` combined with any filter — so
rebuilding the URL from parsed parameters fails in exactly the case that matters, and
fails with a 400 that reads like a bad query rather than like a paging bug.

**``next_url`` is re-validated and asserted same-origin as page one.** That URL is chosen
by the server being read. Following it unchecked hands the choice of destination to
whoever controls the response — which is a server-side request forgery delivered through
a feature we built on purpose.

**``input_cursor`` hands the cursor back as an operation input** rather than writing it
into the query string. That is the only way to page a GraphQL API, where ``after:`` lives
in the POST body's variables — and it keeps this module ignorant of request shape, because
the operation's own templates decide where an input lands. The two carriers are kept
apart on the walk: :attr:`PageWalk.params` for query parameters, :attr:`PageWalk.arguments`
for inputs. A kind writes to exactly one of them.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Set

from app.services.integrations.connectors.spec import (
    PAGE_CURSOR,
    PAGE_INPUT_CURSOR,
    PAGE_INPUT_KINDS,
    PAGE_LINK_HEADER,
    PAGE_NEXT_URL,
    PAGE_NONE,
    PAGE_NUMBER,
    PAGE_OFFSET,
    PageRule,
)
from app.services.integrations.errors import NodeFailure
from app.services.integrations.mapping import paths
from app.utils import outbound_http

logger = logging.getLogger(__name__)


#: ``<https://…>; rel="next"``, in any order and with any other links present.
_LINK_ENTRY = re.compile(r'<(?P<url>[^>]+)>\s*;\s*(?P<params>[^,]*)')
_REL_NEXT = re.compile(r'rel\s*=\s*"?next"?', re.IGNORECASE)


@dataclass
class PageWalk:
    """
    Where a paged read has got to.

    Mutable, unlike almost everything else in this module, because it *is* the state of
    a walk — and threading it through as a new frozen object per page would make the
    repeat detection read like an accident rather than the point.
    """

    rule: PageRule
    first_url: str

    page_index: int = 0
    records_seen: int = 0

    #: The query parameters for the page about to be requested. Seeded from the rule in
    #: ``__post_init__`` rather than by a default factory, because page one's parameters
    #: are a fact about the rule and computing them here is what stops a caller
    #: constructing a walk that starts on nothing.
    params: Optional[Dict[str, Any]] = None

    #: The operation *inputs* for the page about to be requested, for the kinds that page
    #: by input rather than by query parameter. Separate from ``params`` because they go
    #: to different places: these are merged into the arguments ``build_request`` renders
    #: templates from, so the operation decides whether the cursor ends up in a body, a
    #: header or a query string.
    arguments: Optional[Dict[str, Any]] = None

    #: Set instead of ``params`` when the next page is a whole URL.
    next_url: Optional[str] = None

    #: Every cursor this walk has followed. The repeat guard. See the module docstring.
    seen_cursors: Optional[Set[str]] = None

    finished: bool = False
    stopped_because: str = ""

    def __post_init__(self) -> None:
        if self.params is None:
            self.params = first_page_params(self.rule)
        if self.arguments is None:
            self.arguments = first_page_arguments(self.rule)
        if self.seen_cursors is None:
            self.seen_cursors = set()


def begin(rule: PageRule, first_url: str) -> PageWalk:
    return PageWalk(rule=rule, first_url=first_url)


def first_page_params(rule: PageRule) -> Dict[str, Any]:
    """
    What to send for page one.

    A cursor kind sends none: the first request is the unqualified one, and sending an
    empty cursor is how you get a 400 from an API that would have been perfectly happy
    with no parameter at all.
    """
    params: Dict[str, Any] = {}

    if rule.kind in PAGE_INPUT_KINDS:
        # This kind carries everything as inputs. Putting the page size in the query
        # string as well would send `?page_size=250` alongside a GraphQL body that
        # already asked for 250 — harmless against Shopify, and exactly the sort of
        # stray parameter that a stricter API rejects for no discoverable reason.
        return params

    if rule.size and rule.size_param:
        params[rule.size_param] = rule.size

    if rule.kind in (PAGE_NUMBER, PAGE_OFFSET) and rule.param:
        params[rule.param] = rule.start_at

    return params


def first_page_arguments(rule: PageRule) -> Dict[str, Any]:
    """
    What to supply as *inputs* for page one.

    The page size, and nothing else. No cursor: the first request is the unqualified one,
    and the operation's template drops an absent input rather than sending it empty —
    which is what makes ``after`` simply not appear in the first GraphQL query instead of
    appearing as null.
    """
    arguments: Dict[str, Any] = {}

    if rule.kind in PAGE_INPUT_KINDS and rule.size and rule.size_param:
        arguments[rule.size_param] = rule.size

    return arguments


def advance(
    walk: PageWalk,
    *,
    payload: Any,
    headers: Mapping[str, str],
    records_in_page: int,
) -> PageWalk:
    """
    Consume one page's response and work out whether there is another.

    Sets ``walk.finished`` and ``walk.stopped_because`` when there is not. The reason is
    kept rather than discarded because "stopped at 1,000 pages" and "the API said that
    was the last one" are very different facts about a sync, and only one of them means
    everything was read.
    """
    walk.page_index += 1
    walk.records_seen += records_in_page

    if walk.rule.kind == PAGE_NONE:
        return _stop(walk, "this API returns everything in one response")

    if records_in_page == 0:
        # Every kind agrees about this one: a page with nothing in it is the end,
        # whatever the cursor says. A vendor that keeps handing out cursors for empty
        # pages is one we stop believing here rather than after max_pages.
        return _stop(walk, "the last page was empty")

    if walk.page_index >= walk.rule.max_pages:
        return _stop(
            walk,
            f"the limit of {walk.rule.max_pages} pages was reached — there may be more",
        )

    if walk.records_seen >= walk.rule.max_records:
        return _stop(
            walk,
            f"the limit of {walk.rule.max_records} records was reached — there may be "
            "more",
        )

    return _next(walk, payload=payload, headers=headers)


def _next(walk: PageWalk, *, payload: Any, headers: Mapping[str, str]) -> PageWalk:
    kind = walk.rule.kind

    if kind == PAGE_NUMBER:
        return _step_param(walk, walk.rule.start_at + walk.page_index)

    if kind == PAGE_OFFSET:
        # Stepped by the records actually returned rather than by the declared page
        # size. A vendor is free to return fewer than it was asked for, and an offset
        # computed from the request rather than the response skips whatever the
        # difference was — silently, as a gap in the middle of a sync.
        return _step_param(walk, walk.rule.start_at + walk.records_seen)

    if kind == PAGE_CURSOR:
        cursor = _read_cursor(walk, payload)
        if cursor is None:
            return _stop(walk, "the API sent no cursor for a next page")
        return _step_param(walk, cursor, cursor_key=str(cursor))

    if kind == PAGE_INPUT_CURSOR:
        cursor = _read_cursor(walk, payload)
        if cursor is None:
            return _stop(walk, "the API sent no cursor for a next page")
        return _step_argument(walk, cursor)

    if kind == PAGE_NEXT_URL:
        url = _read_cursor(walk, payload)
        if not url:
            return _stop(walk, "the API sent no link to a next page")
        return _step_url(walk, str(url))

    if kind == PAGE_LINK_HEADER:
        url = next_link_from_header(headers.get("link") or headers.get("Link") or "")
        if not url:
            return _stop(walk, "the API sent no link to a next page")
        return _step_url(walk, url)

    return _stop(walk, "there is no way to ask for another page")


# ``offset`` needs a page size to step by; when the operation did not declare one the
# records actually returned are the step. Named rather than inlined so the fallback is
# visible: an offset rule with no size is a rule that trusts the vendor's own page size,
# which is the only honest thing to do when nobody said what it was.
_no_records_hint: tuple = ()


def _step_param(walk: PageWalk, value: Any, *, cursor_key: str = "") -> PageWalk:
    key = cursor_key or f"{walk.rule.param}={value}"

    if key in walk.seen_cursors:
        return _stop(
            walk,
            "this API sent the same page marker twice, so reading stopped rather than "
            "asking for it again",
        )

    walk.seen_cursors.add(key)
    walk.params = dict(walk.params or {})
    walk.params[walk.rule.param] = value
    walk.next_url = None
    return walk


def _step_argument(walk: PageWalk, cursor: Any) -> PageWalk:
    """
    Carry the cursor into the next request as an operation input.

    The repeat guard is the same set ``_step_param`` uses, and sharing it is deliberate:
    the failure it catches — a vendor handing out the same token forever — does not care
    which carrier the token travelled in, and two guards would be two places to forget.
    """
    key = str(cursor)

    if key in walk.seen_cursors:
        return _stop(
            walk,
            "this API sent the same page marker twice, so reading stopped rather than "
            "asking for it again",
        )

    walk.seen_cursors.add(key)
    walk.arguments = dict(walk.arguments or {})
    walk.arguments[walk.rule.param] = cursor
    walk.next_url = None
    return walk


def _step_url(walk: PageWalk, url: str) -> PageWalk:
    """
    Follow a URL the response gave us — after checking it goes where page one went.

    See the module docstring. The check is here rather than in the sender because this is
    the only place a URL arrives from outside, and a guard at the point of entry is one
    a later caller cannot forget.
    """
    if not outbound_http.same_origin(walk.first_url, url):
        raise NodeFailure(
            "This API's link to the next page points somewhere other than the API "
            "itself, so it was not followed. Only the first page was read.",
            permanent=True,
        )

    if url in walk.seen_cursors:
        return _stop(
            walk,
            "this API sent the same next-page link twice, so reading stopped rather "
            "than asking for it again",
        )

    walk.seen_cursors.add(url)
    walk.next_url = url
    walk.params = {}
    return walk


def _read_cursor(walk: PageWalk, payload: Any) -> Optional[Any]:
    """
    The next cursor, honouring ``has_more`` where the operation declared one.

    ``has_more`` is checked *before* the cursor because an API that says there is no more
    and sends a cursor anyway is describing a page that does not exist — and following it
    is how a walk goes round again on the last page.
    """
    rule = walk.rule

    if rule.has_more_path:
        has_more = paths.read(payload, rule.has_more_path)
        if has_more is not None and not has_more:
            return None

    return paths.read(payload, rule.cursor_path)


def _stop(walk: PageWalk, because: str) -> PageWalk:
    walk.finished = True
    walk.stopped_because = because
    return walk


def next_link_from_header(link_header: str) -> str:
    """
    The ``rel="next"`` URL out of an RFC 5988 ``Link`` header, **verbatim**.

    Returned exactly as sent. See the module docstring on why rebuilding it from parsed
    parameters is the version that breaks.
    """
    if not link_header:
        return ""

    for match in _LINK_ENTRY.finditer(link_header):
        if _REL_NEXT.search(match.group("params") or ""):
            return match.group("url").strip()

    return ""


def describe_walk(walk: PageWalk) -> Dict[str, Any]:
    """
    What the step row records about a paged read.

    ``complete`` is the field worth having: it is the difference between "we read
    everything" and "we read as much as the limits allowed", and the counters alone
    cannot tell those apart.
    """
    return {
        "pages": walk.page_index,
        "records": walk.records_seen,
        "stopped_because": walk.stopped_because,
        "complete": walk.finished and "limit of" not in walk.stopped_because,
    }
