"""Daily digest pipeline.

Lambda entry point (`handler`) plus a CLI dry-run (`main`) that renders
to `out/email.html` instead of sending, using fixture data when DynamoDB
is unavailable.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from newslet import (
    db,
    deepdive,
    discover,
    discovery,
    email_render,
    facts,
    feeds,
    hn,
    quotes,
    rank,
    serendipity,
    summarize,
    themes,
    tune,
    weather,
    websearch,
    x_grok,
)
from newslet.config import settings
from newslet.contracts import (
    Article,
    DeepDive,
    Discovery,
    Fact,
    FactsState,
    FeedbackRow,
    Issue,
    Pick,
    Profile,
    Quote,
    QuotesState,
    RankResponse,
    WebArticle,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# Ranking wants recency (what do I like *lately*?); profile tuning wants breadth
# (what is my *durable* taste?). They read the same table with different windows.
_RANK_FEEDBACK_LIMIT = 50
_TUNE_FEEDBACK_LIMIT = 200

# Synthetic vote URLs (minted by email_render for non-article blocks) match
# exact, anchored path shapes — the middle segment is an issue key (a date
# or a manual-send key). They belong to their feature's own feedback loop
# and must never steer article ranking or the general profile; full-shape
# matching keeps a real article at e.g. example.com/facts/tcp classified as
# general. Each feature's shape lives in its module (shared with the web
# handler so the two can never drift).
_FACTS_VOTE_RE = facts.VOTE_PATH_RE
_QUOTE_VOTE_RE = quotes.VOTE_PATH_RE

# The facts no-repeat log keeps this many recently-covered topics.
_FACTS_TOPIC_LOG_CAP = 60

# The quotes no-repeat log keeps this many recently-shown quotes.
_QUOTES_LOG_CAP = 120

# Synthetic rows are dropped from a bucket *after* fetching, so fetch a
# multiple of the wanted window — otherwise a streak of fact clicks could
# fill the fetch limit and starve article ranking/tuning of feedback that
# exists just past it (and vice versa).
_SPLIT_FETCH_MULTIPLIER = 4


def _split_feedback(
    rows: list[FeedbackRow],
) -> tuple[list[FeedbackRow], list[FeedbackRow], list[FeedbackRow]]:
    """Split feedback into (general, facts, quotes) by synthetic-URL shape."""
    general: list[FeedbackRow] = []
    fact_rows: list[FeedbackRow] = []
    quote_rows: list[FeedbackRow] = []
    for row in rows:
        path = urlparse(str(row.article_url)).path
        if _FACTS_VOTE_RE.match(path):
            fact_rows.append(row)
        elif _QUOTE_VOTE_RE.match(path):
            quote_rows.append(row)
        else:
            general.append(row)
    return general, fact_rows, quote_rows


def _recent_feedback_split(
    limit: int,
) -> tuple[list[FeedbackRow], list[FeedbackRow], list[FeedbackRow]]:
    """Fetch and split recent feedback, ``limit`` rows per bucket.

    Over-fetches by ``_SPLIT_FETCH_MULTIPLIER`` before splitting, then trims
    each bucket (rows arrive newest-first) — so one vote stream can't crowd
    another out of its window.
    """
    general, fact_rows, quote_rows = _split_feedback(
        db.recent_feedback(limit=limit * _SPLIT_FETCH_MULTIPLIER)
    )
    return general[:limit], fact_rows[:limit], quote_rows[:limit]

# Fallback counts when no admin config is present (run_digest defaults).
_DEFAULT_MAX_PICKS = 10
_DEFAULT_MAX_WEB = 5
_DEFAULT_MAX_RANDOM = 4

# How many HN front pages to pull into the ranking candidate pool.
_HN_PAGES = 20

# How many X (Twitter) posts to pull into the ranking candidate pool, when an
# XAI_API_KEY is configured (the source is disabled and empty otherwise).
_X_MAX_POSTS = 15

# The web block uses a fast model with few search rounds: Opus with 3 rounds
# spends its token budget on tool calls and never emits the final JSON, so the
# block came back empty in production. Haiku + 2 rounds is what the live
# /api/search path proved reliable.
_WEB_SEARCH_MODEL = "claude-haiku-4-5-20251001"
_WEB_SEARCHES = 2


def _web_search_query(profile_md: str) -> str:
    """Distill the profile into a single web-search request string."""
    profile_md = (profile_md or "").strip()
    base = (
        "Fresh, high-quality articles a reader with the following interests "
        "would want today, from across the open web:\n\n"
    )
    return base + (profile_md or "technology, science, and society")


def _x_search_query(profile_md: str) -> str:
    """The interests slot for the X source — just the profile, nothing more.

    Unlike the web block, ``x_grok`` already supplies all the search framing
    ("find recent high-signal posts from X matching the interests below"), so
    this passes only the distilled interests. Reusing the web query here would
    double the framing and waste tokens on "from across the open web" wording
    that doesn't fit an X search.
    """
    return (profile_md or "").strip() or "technology, science, and society"


def _build_issue(
    picks: list[Pick],
    date: str,
    *,
    subject: str = "",
    intro: str = "",
    discoveries: list[Discovery] | None = None,
    web_articles: list[WebArticle] | None = None,
    random_articles: list[WebArticle] | None = None,
    facts_list: list[Fact] | None = None,
    quote: Quote | None = None,
    weather_line: str = "",
    deepdive: DeepDive | None = None,
) -> Issue:
    return Issue(
        date=date,
        picks=picks,
        created_at=datetime.now(UTC),
        subject=subject,
        intro=intro,
        discoveries=discoveries or [],
        web_articles=web_articles or [],
        random_articles=random_articles or [],
        facts=facts_list or [],
        quote=quote,
        weather_line=weather_line,
        deepdive=deepdive,
    )


def _dedupe_candidates(candidates: list[Article]) -> list[Article]:
    """Drop duplicate candidate urls, keeping first-seen order.

    RSS and HN can surface the same link (HN often points at an article a
    feed also carries); ranking it twice wastes tokens and risks a doubled
    pick.
    """
    seen: set[str] = set()
    out: list[Article] = []
    for art in candidates:
        key = str(art.url)
        if key in seen:
            continue
        seen.add(key)
        out.append(art)
    return out


def _feed_domains(feed_urls: list[str]) -> list[str]:
    """Derive the netloc of each feed url, dropping any that lack one."""
    domains = []
    for url in feed_urls:
        netloc = urlparse(url).netloc
        if netloc:
            domains.append(netloc)
    return domains


def run_digest(
    *,
    feed_urls: list[str],
    profile: Profile,
    feedback: list[FeedbackRow],
    is_seen: callable,
    rank_fn=rank.rank,
    summarize_fn=None,
    discovery_fn=None,
    hn_fn=None,
    websearch_fn=None,
    newsletters_fn=None,
    x_fn=None,
    serendipity_fn=None,
    facts_fn=None,
    facts_profile_md: str = "",
    facts_recent_topics: list[str] | None = None,
    facts_enabled: bool = True,
    quote_fn=None,
    quotes_profile_md: str = "",
    recent_quotes: list[str] | None = None,
    quote_enabled: bool = True,
    weather_fn=None,
    weather_enabled: bool = True,
    deepdive_fn=None,
    deepdive_topic: str = "",
    x_enabled: bool = True,
    max_x_posts: int = _X_MAX_POSTS,
    max_picks: int = _DEFAULT_MAX_PICKS,
    min_picks: int = 5,
    max_web: int = _DEFAULT_MAX_WEB,
    max_random: int = _DEFAULT_MAX_RANDOM,
    web_variety: int = 30,
    web_model: str | None = None,
    now: datetime | None = None,
) -> tuple[Issue, list[Article]]:
    """Pure pipeline: fetch → rank → summarize → discover → web → assemble Issue.

    Returns ``(issue, candidates)`` so callers can mark every fetched
    article seen (not just the picked ones) and avoid re-evaluating
    rejects on subsequent days.  Summarize, discovery, the Hacker News
    source, the X (Twitter) source, the web-search block, and the
    "off your beat" serendipity block are all best-effort: a failure in
    any of them degrades to empty and never blocks the send.
    """
    # Resolve at call time (not as defaults) so monkeypatching the module
    # attributes in tests is honoured.
    summarize_fn = summarize_fn or summarize.summarize_issue
    discovery_fn = discovery_fn or discovery.find_discoveries
    hn_fn = hn_fn or hn.fetch_hn_articles
    websearch_fn = websearch_fn or websearch.search_web
    newsletters_fn = newsletters_fn or db.recent_inbox_articles
    x_fn = x_fn or x_grok.fetch_x_articles
    serendipity_fn = serendipity_fn or serendipity.fetch_serendipity
    facts_fn = facts_fn or facts.fetch_facts
    quote_fn = quote_fn or quotes.fetch_quote
    weather_fn = weather_fn or weather.fetch_weather
    deepdive_fn = deepdive_fn or deepdive.fetch_deepdive

    now = now or datetime.now(UTC)
    since = now - timedelta(hours=24)
    candidates = feeds.fetch_recent(feed_urls, since=since, is_seen=is_seen)
    log.info("fetched %d candidate articles from %d feeds", len(candidates), len(feed_urls))

    # Hacker News, via its rich API, joins the ranking pool so HN stories
    # compete with RSS for the day's picks. Best-effort and seen-filtered so
    # it neither breaks the digest nor resurfaces yesterday's stories.
    try:
        hn_candidates = [
            a for a in hn_fn(pages=_HN_PAGES) if not is_seen(str(a.url))
        ]
        log.info("fetched %d Hacker News candidates", len(hn_candidates))
        candidates = _dedupe_candidates(candidates + hn_candidates)
    except Exception:  # noqa: BLE001 - HN is best effort, never block the send
        log.exception("Hacker News fetch failed; ranking without it")

    # Subscribed newsletters: links extracted from emails received in the last
    # 24h, stored by the inbound Lambda. Joins the ranking pool like HN — same
    # best-effort, seen-filtered shape so a storage hiccup never blocks the send.
    try:
        nl_candidates = [
            a for a in newsletters_fn(since) if not is_seen(str(a.url))
        ]
        log.info("fetched %d newsletter candidates", len(nl_candidates))
        candidates = _dedupe_candidates(candidates + nl_candidates)
    except Exception:  # noqa: BLE001 - newsletters are best effort, never block
        log.exception("newsletter fetch failed; ranking without it")

    # X (Twitter), via Grok's x_search tool: recent on-profile posts join the
    # ranking pool like HN and newsletters. Same best-effort, seen-filtered
    # shape. Gated by the admin toggle (``x_enabled``) and, inside x_grok, by
    # the presence of an XAI_API_KEY — either off means an empty, non-blocking
    # source.
    if x_enabled:
        try:
            x_candidates = [
                a
                for a in x_fn(_x_search_query(profile.markdown), max_results=max_x_posts)
                if not is_seen(str(a.url))
            ]
            log.info("fetched %d X candidates", len(x_candidates))
            candidates = _dedupe_candidates(candidates + x_candidates)
        except Exception:  # noqa: BLE001 - X is best effort, never block the send
            log.exception("X fetch failed; ranking without it")

    date = now.strftime("%Y-%m-%d")
    if not candidates:
        return _build_issue([], date=date), []
    response: RankResponse = rank_fn(
        profile_md=profile.markdown,
        feedback=feedback,
        candidates=candidates,
        max_picks=max_picks,
        min_picks=min_picks,
    )
    log.info("claude returned %d picks", len(response.picks))

    subject, intro = "", ""
    try:
        subject, intro = summarize_fn(response.picks)
    except Exception:  # noqa: BLE001 - best effort, never block the send
        log.exception("summarize failed; sending without subject/intro")

    discoveries: list[Discovery] = []
    try:
        discoveries = discovery_fn(profile.markdown, _feed_domains(feed_urls))
    except Exception:  # noqa: BLE001 - best effort, never block the send
        log.exception("discovery failed; sending without discoveries")

    # discovery_fn doesn't consult the seen-store, so a later day's web search
    # can return an article we already surfaced. Filter it out here against the
    # same is_seen the fetcher uses, so marked-seen discoveries don't recur.
    discoveries = [d for d in discoveries if not is_seen(str(d.url))]

    # The "from around the web" block: a live web search distilled from the
    # profile, separate from the RSS/HN picks, with the admin variety dial
    # controlling how far it roams into ancillary areas. Best-effort and
    # seen-filtered like discoveries. ``max_web == 0`` disables it entirely.
    # Note: unlike discovery (whose job is to surface *new* sources), the web
    # block does NOT exclude the user's feed domains — a profile-driven search
    # naturally surfaces those very domains, and excluding them would empty the
    # block. Overlap with picks is acceptable on a "from around the web" list.
    web_articles: list[WebArticle] = []
    if max_web > 0:
        try:
            web_articles = websearch_fn(
                _web_search_query(profile.markdown),
                max_results=max_web,
                variety=web_variety,
                model=web_model or _WEB_SEARCH_MODEL,
                max_searches=_WEB_SEARCHES,
            )
        except Exception:  # noqa: BLE001 - best effort, never block the send
            log.exception("web search failed; sending without the web block")
    web_articles = [w for w in web_articles if not is_seen(str(w.url))]

    # The "off your beat" block: popular past-week pieces deliberately outside
    # the reader's usual (tech) beat — see newslet.serendipity. Its own block
    # on both surfaces (not folded into the ranked pool, where the
    # profile-driven ranker would bury it). Best-effort and seen-filtered like
    # the web block; ``max_random == 0`` disables it entirely.
    random_articles: list[WebArticle] = []
    if max_random > 0:
        try:
            random_articles = serendipity_fn(
                profile.markdown, max_results=max_random
            )
        except Exception:  # noqa: BLE001 - best effort, never block the send
            log.exception("serendipity failed; sending without the off-beat block")
    random_articles = [r for r in random_articles if not is_seen(str(r.url))]

    # The two tech-fact essays (mid + end) — model knowledge only, steered by
    # the facts-taste profile and the no-repeat topic log (see newslet.facts).
    # Best-effort like every enrichment block; the admin toggle disables it.
    issue_facts: list[Fact] = []
    if facts_enabled:
        try:
            issue_facts = facts_fn(facts_profile_md, facts_recent_topics or [])
        except Exception:  # noqa: BLE001 - best effort, never block the send
            log.exception("facts failed; sending without the fact blocks")

    # The quote of the day: best-effort like facts, admin-toggleable.
    issue_quote: Quote | None = None
    if quote_enabled:
        try:
            issue_quote = quote_fn(quotes_profile_md, recent_quotes or [])
        except Exception:  # noqa: BLE001 - best effort, never block the send
            log.exception("quote failed; sending without the epigraph")

    # The weather line: free NWS call, no LLM. Best-effort like the rest.
    weather_line = ""
    if weather_enabled:
        try:
            weather_line = weather_fn() or ""
        except Exception:  # noqa: BLE001 - best effort, never block the send
            log.exception("weather failed; sending without the forecast line")

    # The reader-requested deep dive ("You asked"): the caller pops the
    # oldest pending topic and passes it in (db stays out of this pure
    # pipeline); empty topic means nothing queued. Best-effort — a failed
    # generation leaves the request pending for the next build.
    issue_deepdive: DeepDive | None = None
    if deepdive_topic:
        try:
            issue_deepdive = deepdive_fn(deepdive_topic)
        except Exception:  # noqa: BLE001 - best effort, never block the send
            log.exception("deep dive failed; the request stays queued")

    issue = _build_issue(
        response.picks,
        date=date,
        subject=subject,
        intro=intro,
        discoveries=discoveries,
        web_articles=web_articles,
        random_articles=random_articles,
        facts_list=issue_facts,
        quote=issue_quote,
        weather_line=weather_line,
        deepdive=issue_deepdive,
    )
    return issue, candidates


def _send_email(subject: str, html: str) -> None:
    import resend

    s = settings()
    resend.api_key = s.resend_api_key
    resend.Emails.send(
        {
            "from": f"daily scoop <{s.from_email}>",
            "to": [s.to_email],
            "subject": subject,
            "html": html,
        }
    )


def _fresh_issue(now: datetime | None = None) -> tuple[Issue, list[Article]]:
    """Fetch + rank a brand-new issue from the current feeds/profile.

    Shared by the daily and manual paths. ``now`` lets the manual path
    pass the same instant it uses for the synthetic key.
    """
    feeds_list = db.list_feeds()
    profile = db.get_profile()
    config = db.get_config()
    # Recency window for ranking; tuning reads its own wider window. Synthetic
    # fact/quote votes are split out — they steer their own features, never
    # article ranking.
    feedback, _fact_votes, _quote_votes = _recent_feedback_split(_RANK_FEEDBACK_LIMIT)
    facts_state = db.get_facts_state() if config.facts_enabled else FactsState()
    quotes_state = db.get_quotes_state() if config.quote_enabled else QuotesState()
    pending_deepdive = (
        db.oldest_pending_deepdive() if config.deepdive_enabled else None
    )
    issue, candidates = run_digest(
        feed_urls=[str(f.url) for f in feeds_list],
        profile=profile,
        feedback=feedback,
        is_seen=db.is_seen,
        max_picks=config.max_rss_articles,
        max_web=config.max_web_articles,
        max_random=config.max_random_articles,
        web_variety=config.web_variety,
        x_enabled=config.x_enabled,
        max_x_posts=config.max_x_articles,
        facts_profile_md=facts_state.markdown,
        facts_recent_topics=facts_state.recent_topics,
        facts_enabled=config.facts_enabled,
        quotes_profile_md=quotes_state.markdown,
        recent_quotes=quotes_state.recent_quotes,
        quote_enabled=config.quote_enabled,
        weather_enabled=config.weather_enabled,
        deepdive_topic=pending_deepdive["topic"] if pending_deepdive else "",
        now=now,
    )

    # Stamp the appearance (theme + text size) the issue will be sent with,
    # so the stored row keeps the /emails/{date} archive faithful to the
    # as-sent look even after the admin changes appearance settings.
    issue = issue.model_copy(
        update={"theme": config.theme, "text_size": config.text_size}
    )
    return issue, candidates


def _tune_profile_after_send() -> None:
    """Re-tune the profile after a confirmed send, using a wider feedback
    window than ranking so the cumulative learned-preferences block
    reflects durable taste, not just the last few days. Best effort:
    tuning must never break the send (which already happened)."""
    try:
        profile = db.get_profile()
        tune_feedback, _fact_votes, _quote_votes = _recent_feedback_split(
            _TUNE_FEEDBACK_LIMIT
        )
        new_markdown = tune.tune_profile(profile.markdown, tune_feedback)
        if new_markdown != profile.markdown:
            db.put_profile(new_markdown)
    except Exception:  # noqa: BLE001 - tuning is best effort, never raise
        log.exception("profile tuning failed after send")


def _advance_facts_topic_log(issue: Issue) -> None:
    """Append the sent issue's fact titles to the no-repeat log.

    Called only after a confirmed send (like ``mark_seen`` and the tuners) so
    a failed send never burns topics no reader saw. Skips titles already
    present so a duplicate-send retry stays idempotent. Re-reading the state
    row at write time keeps the window between read and write to
    milliseconds (there is no model call in between); it narrows — but does
    not eliminate — the lost-update race with a concurrent run's tune write.
    Best effort.
    """
    if not issue.facts:
        return
    try:
        state = db.get_facts_state()
        new_titles = [
            f.title for f in issue.facts if f.title not in state.recent_topics
        ]
        if not new_titles:
            return
        topics = (state.recent_topics + new_titles)[-_FACTS_TOPIC_LOG_CAP:]
        db.put_facts_state(
            FactsState(markdown=state.markdown, recent_topics=topics)
        )
    except Exception:  # noqa: BLE001 - the log is best effort
        log.exception("failed to update the facts topic log")


def _quote_log_entry(quote: Quote) -> str:
    """The no-repeat-log line for a quote: author + a text prefix."""
    return f"{quote.author} — {quote.text[:60]}"


def _advance_quotes_log(issue: Issue) -> None:
    """Append the sent issue's quote to the no-repeat log.

    Same discipline as ``_advance_facts_topic_log``: post-send only, fresh
    read at write time, deduped so retries are idempotent, best effort.
    """
    if issue.quote is None:
        return
    try:
        state = db.get_quotes_state()
        entry = _quote_log_entry(issue.quote)
        if entry in state.recent_quotes:
            return
        recent = (state.recent_quotes + [entry])[-_QUOTES_LOG_CAP:]
        db.put_quotes_state(
            QuotesState(markdown=state.markdown, recent_quotes=recent)
        )
    except Exception:  # noqa: BLE001 - the log is best effort
        log.exception("failed to update the quotes no-repeat log")


def _mark_deepdive_served(issue: Issue) -> None:
    """Flip the answered request to served, only after a confirmed send.

    Re-reads the oldest pending request and marks it iff its topic matches
    the one the issue actually answered — a request queued mid-build is
    left alone, and a duplicate-send retry is a no-op (the matching row is
    already served). Best effort.
    """
    if issue.deepdive is None:
        return
    try:
        pending = db.oldest_pending_deepdive()
        if pending and pending["topic"] == issue.deepdive.topic:
            db.mark_deepdive_served(pending["id"], issue.date)
    except Exception:  # noqa: BLE001 - serving is best effort
        log.exception("failed to mark the deep-dive request served")


def _tune_quotes_after_send() -> None:
    """Re-tune the quotes-taste profile from quote votes only. Mirrors
    ``_tune_facts_after_send`` including the fresh re-read before write."""
    try:
        _general, _fact_votes, quote_votes = _recent_feedback_split(
            _TUNE_FEEDBACK_LIMIT
        )
        if not quote_votes:
            return
        state = db.get_quotes_state()
        new_markdown = quotes.tune_quotes_profile(state.markdown, quote_votes)
        if new_markdown != state.markdown:
            fresh = db.get_quotes_state()
            db.put_quotes_state(
                QuotesState(markdown=new_markdown, recent_quotes=fresh.recent_quotes)
            )
    except Exception:  # noqa: BLE001 - tuning is best effort, never raise
        log.exception("quotes tuning failed after send")


def _tune_facts_after_send() -> None:
    """Re-tune the facts-taste profile from fact votes only, after a
    confirmed send. The general profile tuner never sees these rows and
    this tuner never sees general rows — the two loops stay disjoint.
    Best effort: never raises, and the no-repeat topic log rides along
    unchanged."""
    try:
        _general, fact_votes, _quote_votes = _recent_feedback_split(
            _TUNE_FEEDBACK_LIMIT
        )
        if not fact_votes:
            return
        state = db.get_facts_state()
        new_markdown = facts.tune_facts_profile(state.markdown, fact_votes)
        if new_markdown != state.markdown:
            # Re-read before writing: the tune model call above takes
            # seconds, and this run's own _advance_facts_topic_log (or a
            # concurrent run's) may have appended topics in the meantime —
            # carrying the pre-call copy would revert them.
            fresh = db.get_facts_state()
            db.put_facts_state(
                FactsState(markdown=new_markdown, recent_topics=fresh.recent_topics)
            )
    except Exception:  # noqa: BLE001 - tuning is best effort, never raise
        log.exception("facts tuning failed after send")


def _run_manual(s: Any) -> dict:
    """On-demand "send now" run.

    A faithful real run — fetch → rank → summarize → discover → send →
    tune, with a live feedback loop — but deliberately isolated from the
    daily cadence: it ignores the ``issue_sent`` gate, stores under a
    synthetic key hidden from ``list_issues``, and never marks
    ``issue_sent`` or ``mark_seen`` (so it neither counts toward timing
    nor consumes the scheduled digest's candidate pool).
    """
    now = datetime.now(UTC)
    issue, _candidates = _fresh_issue(now=now)
    # Re-key to a synthetic, URL-safe id: hidden from "recent issues",
    # can't collide with the daily date, yet rate links + the HMAC token
    # (both signed over this key) still resolve back to it. The random
    # suffix keeps two sends fired in the same instant — a double-click or
    # an async-invoke retry on separate Lambda instances — from sharing a
    # key and clobbering each other's stored picks/feedback. A timestamp
    # alone (even to the microsecond) can't guarantee this across hosts.
    key = "manual-" + now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    issue = issue.model_copy(update={"date": key})
    db.put_issue(issue, manual=True)

    subject, html = email_render.render_email(
        issue,
        s.public_base_url,
        theme=themes.get(issue.theme),
        text_size=issue.text_size,
    )
    _send_email(subject, html)
    # Intentionally no mark_issue_sent / mark_seen here — see docstring.
    _advance_facts_topic_log(issue)
    _advance_quotes_log(issue)
    _mark_deepdive_served(issue)
    _tune_profile_after_send()
    _tune_facts_after_send()
    _tune_quotes_after_send()

    log.info("manual send %s with %d picks", issue.date, len(issue.picks))
    return {"status": "sent", "date": issue.date, "picks": len(issue.picks)}


def _run_discover(s: Any) -> dict:
    """Regenerate the Discover page's stored recommendations (no email).

    Builds a board of RSS feeds + X accounts matched to the profile (see
    :mod:`newslet.discover`) and stores it under ``id="discover"`` in the
    profile table. Fired by the weekly EventBridge schedule and by the
    page's "Refresh recommendations" button — the page itself only ever
    reads the stored board. On a failed build (empty board, no
    ``generated_at``) the previous board is left in place rather than
    overwritten, so a bad run never blanks the page.
    """
    profile = db.get_profile()
    feeds_list = db.list_feeds()
    board = discover.build_discover_board(
        profile.markdown,
        _feed_domains([str(f.url) for f in feeds_list]),
    )
    if board.generated_at is None:
        log.warning("discover build failed; keeping the previous board")
        return {"status": "discover_failed"}
    db.put_discover(board)
    log.info(
        "discover refreshed: %d feeds, %d accounts",
        len(board.feeds),
        len(board.accounts),
    )
    return {
        "status": "discover_refreshed",
        "feeds": len(board.feeds),
        "accounts": len(board.accounts),
    }


def handler(event: dict, context: Any) -> dict:
    """Run the digest pipeline once.

    With ``event["manual"]`` truthy, runs an on-demand send isolated from
    the daily cadence (see :func:`_run_manual`). ``event["discover"]``
    rebuilds the Discover page's stored recommendations. Otherwise runs the
    daily pipeline idempotently — including for stray ``{"home"}`` events
    from the retired homepage-rebuild mode, which fall through here and are
    stopped by the sent-today gate.

    Daily idempotency is keyed on ``sent_at`` — *not* mere existence of
    the Issue row — so a partial failure (e.g. ``put_issue`` succeeded but
    Resend was down) on the first attempt does not cause subsequent
    EventBridge retries to silently skip the day.

    Operation order is chosen so that any single-step failure leaves
    the system in a state a retry can recover cleanly from:

      1. issue_sent check  -> if True, exit
      2. Reuse a previously-stored issue if present (skip rank cost on retry)
      3. Otherwise: fetch + rank, then put_issue immediately
      4. _send_email
      5. mark_issue_sent (flips the idempotency marker)
      6. mark_seen (only after a confirmed send)

    If 5 fails after a successful 4, a retry will re-send (duplicate
    email — annoying but not silent). Better than the previous order
    which could either lose the day's content entirely or emit an
    empty email when ``mark_seen`` ran but ``put_issue`` failed.
    """
    s = settings()
    if not s.public_base_url:
        # Optional in config because the web Lambda doesn't need it,
        # but the digest *must* have it to render rate links.
        raise RuntimeError("PUBLIC_BASE_URL env var is required for the digest Lambda")

    if event and event.get("manual"):
        return _run_manual(s)

    if event and event.get("discover"):
        return _run_discover(s)

    today = datetime.now(UTC).strftime("%Y-%m-%d")

    if db.issue_sent(today):
        log.info("issue %s already sent; skipping", today)
        return {"status": "already_sent", "date": today}

    existing = db.get_issue(today)
    if existing is not None and existing.picks:
        log.info("reusing partial issue %s from previous attempt", today)
        issue = existing
        candidates: list[Article] = []  # already-marked on the failed run
    else:
        issue, candidates = _fresh_issue()
        db.put_issue(issue)

    # Render with the issue's stamped appearance (not a live config read) so
    # a retry that reuses a stored partial issue sends the same look it was
    # built with — and matches what the archive will show.
    subject, html = email_render.render_email(
        issue,
        s.public_base_url,
        theme=themes.get(issue.theme),
        text_size=issue.text_size,
    )
    _send_email(subject, html)
    db.mark_issue_sent(issue.date)

    # Mark every candidate as seen — but only *after* a confirmed send.
    # An article Claude rejected today shouldn't be re-evaluated when it
    # crosses tomorrow's 24h window boundary.  Discovery urls go too, so
    # they are not re-surfaced on later days.
    seen_urls = [str(a.url) for a in candidates]
    seen_urls += [str(d.url) for d in issue.discoveries]
    seen_urls += [str(w.url) for w in issue.web_articles]
    seen_urls += [str(r.url) for r in issue.random_articles]
    if seen_urls:
        db.mark_seen(seen_urls)

    _advance_facts_topic_log(issue)
    _advance_quotes_log(issue)
    _mark_deepdive_served(issue)
    _tune_profile_after_send()
    _tune_facts_after_send()
    _tune_quotes_after_send()

    log.info("sent issue %s with %d picks", issue.date, len(issue.picks))
    return {"status": "sent", "date": issue.date, "picks": len(issue.picks)}


# ---------------------------------------------------------------------------
# CLI dry-run
# ---------------------------------------------------------------------------


def _fake_rank(
    profile_md: str,
    feedback: list[FeedbackRow],
    candidates: list[Article],
    **_,
) -> RankResponse:
    """Deterministic stand-in for the Anthropic call when --dry-run is set."""
    picks = [
        Pick(
            url=a.url,
            title=a.title,
            blurb=(a.summary or a.title)[:160],
            source=a.source,
            score=1.0 - (i * 0.05),
        )
        for i, a in enumerate(candidates[:10])
    ]
    return RankResponse(picks=picks)


def _fake_summarize(picks: list[Pick], **_) -> tuple[str, str]:
    """Deterministic, offline (subject, intro) for --dry-run."""
    if not picks:
        return ("", "")
    intro = f"{len(picks)} stories today, led by {picks[0].title}."
    return (f"daily scoop · {picks[0].title}", intro)


def _fake_discoveries(profile_md: str, feed_domains: list[str], **_) -> list[Discovery]:
    """Deterministic, offline discoveries for --dry-run."""
    return [
        Discovery(
            url="https://example.com/discovery-sample",
            title="A source you don't follow yet",
            source="Example Wire",
            reason="Sample discovery rendered in the dry-run output.",
            feed_url="https://example.com/feed.xml",
        )
    ]


def _fake_hn(pages: int = 0, **_) -> list[Article]:
    """Deterministic, offline Hacker News candidates for --dry-run."""
    return [
        Article(
            url="https://news.ycombinator.com/item?id=40000000",
            title="Show HN: A tiny offline-first note app",
            summary="312 points, 145 comments on Hacker News (by pg).",
            source="Hacker News",
            published=datetime.now(UTC),
        )
    ]


def _fake_x(query: str = "", **_) -> list[Article]:
    """Deterministic, offline X (Twitter) candidates for --dry-run."""
    return [
        Article(
            url="https://x.com/example/status/1700000000000000000",
            title="A widely-shared post matching your interests",
            summary="980 likes, 240 reposts on X (by @example).",
            source="X",
            published=datetime.now(UTC),
        )
    ]


def _fake_newsletters(since: datetime, **_) -> list[Article]:
    """Deterministic, offline subscribed-newsletter candidates for --dry-run."""
    return [
        Article(
            url="https://example.com/newsletter-story",
            title="A story pulled from a newsletter you subscribed to",
            summary="",
            source="Example Newsletter",
            published=datetime.now(UTC),
        )
    ]


def _fake_websearch(query: str, **_) -> list[WebArticle]:
    """Deterministic, offline web-search results for --dry-run."""
    return [
        WebArticle(
            url="https://example.com/web-1",
            title="An article pulled from the open web",
            blurb="Sample web-search result rendered in the dry-run output.",
            source="Example Web",
        )
    ]


def _fake_serendipity(profile_md: str, **_) -> list[WebArticle]:
    """Deterministic, offline "off your beat" articles for --dry-run."""
    return [
        WebArticle(
            url="https://example.com/off-beat-1",
            title="A widely-shared story from well outside your beat",
            blurb="Sample off-your-beat result rendered in the dry-run output.",
            source="Example Magazine",
        )
    ]


def _fake_facts(profile_md: str, topics: list[str], **_) -> list[Fact]:
    """Deterministic, offline tech facts for --dry-run."""
    body = (
        "In 1971, engineers discovered something surprising about the way "
        "early networks handled congestion.\n\n"
        "This sample essay stands in for a ~500-word fact in the dry-run "
        "output, so the mid and end blocks render with realistic structure."
    )
    return [
        Fact(title="A sample mid-issue tech fact", body_md=body,
             genre="networks & protocols", slot="mid"),
        Fact(title="A sample closing tech fact", body_md=body,
             genre="computing history & lore", slot="end"),
    ]


def _fake_quote(profile_md: str, recent: list[str], **_) -> Quote:
    """Deterministic, offline quote for --dry-run."""
    return Quote(
        text="The impediment to action advances action. What stands in the way becomes the way.",
        author="Marcus Aurelius",
        source="Meditations",
        tradition="Stoic",
    )


def _fake_weather(**_) -> str:
    """Deterministic, offline weather line for --dry-run."""
    return "today 78° chance light rain, tonight 64° mostly clear"


def _fake_deepdive(topic: str, **_) -> DeepDive:
    """Deterministic, offline deep dive for --dry-run."""
    return DeepDive(
        topic=topic,
        title="How DNS resolution actually works",
        body_md=(
            "You asked, so here is the mechanism end to end.\n\n"
            "This fixture paragraph stands in for a ~500-word explainer in "
            "the dry-run output."
        ),
    )


def _dry_run_env() -> None:
    """Force dry-run env values.

    Uses ``os.environ[key] = ...`` (not ``setdefault``) so a developer
    machine that has real ``ANTHROPIC_API_KEY`` / ``RESEND_API_KEY``
    exported can't accidentally leak them into a dry run — even if the
    dry-run code path is later modified to hit a real API.
    """
    os.environ["ANTHROPIC_API_KEY"] = "dry-run"
    os.environ["RESEND_API_KEY"] = "dry-run"
    os.environ["FROM_EMAIL"] = "newslet@example.com"
    os.environ["TO_EMAIL"] = "you@example.com"
    os.environ["ADMIN_TOKEN"] = "dry-run"
    os.environ["SIGNING_KEY"] = "dry-run-signing-key"
    os.environ["PUBLIC_BASE_URL"] = "https://api.example.com"
    # Bust the lru_cache since settings() may have been called already
    # with whatever real env was present at import.
    settings.cache_clear()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="newslet digest")
    parser.add_argument(
        "--dry-run", action="store_true", help="render to out/email.html, don't send"
    )
    parser.add_argument("--feeds", default="feeds.txt", help="path to newline-delimited feed urls")
    parser.add_argument("--profile", default="profile.md", help="path to profile markdown")
    parser.add_argument("--out", default="out/email.html", help="output HTML path (dry-run)")
    parser.add_argument(
        "--theme",
        default=themes.DEFAULT_THEME,
        choices=sorted(themes.THEMES),
        help="email theme to render (dry-run)",
    )
    args = parser.parse_args(argv)

    if not args.dry_run:
        # Non-dry runs require real env vars and DynamoDB; defer to handler().
        return handler({}, None).get("status") == "sent"  # type: ignore[return-value]

    _dry_run_env()
    feed_urls = [
        line.strip()
        for line in Path(args.feeds).read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    profile_md = Path(args.profile).read_text() if Path(args.profile).exists() else ""
    profile = Profile(markdown=profile_md, updated_at=datetime.now(UTC))

    issue, _candidates = run_digest(
        feed_urls=feed_urls,
        profile=profile,
        feedback=[],
        is_seen=lambda _u: False,
        rank_fn=_fake_rank,
        summarize_fn=_fake_summarize,
        discovery_fn=_fake_discoveries,
        hn_fn=_fake_hn,
        websearch_fn=_fake_websearch,
        newsletters_fn=_fake_newsletters,
        x_fn=_fake_x,
        serendipity_fn=_fake_serendipity,
        facts_fn=_fake_facts,
        quote_fn=_fake_quote,
        weather_fn=_fake_weather,
        deepdive_fn=_fake_deepdive,
        deepdive_topic="how does DNS resolution work?",
    )

    if not issue.picks:
        print("no picks today (no recent feed entries within 24h)")
        return 0

    subject, html = email_render.render_email(
        issue, settings().public_base_url, theme=themes.get(args.theme)
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"subject: {subject}")
    print(f"wrote {out} ({len(html)} bytes, {len(issue.picks)} picks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
