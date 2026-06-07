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

from newslet import db, discovery, email_render, feeds, hn, rank, summarize, tune, websearch
from newslet.config import settings
from newslet.contracts import (
    Article,
    Discovery,
    FeedbackRow,
    Issue,
    Pick,
    Profile,
    RankResponse,
    WebArticle,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# Ranking wants recency (what do I like *lately*?); profile tuning wants breadth
# (what is my *durable* taste?). They read the same table with different windows.
_RANK_FEEDBACK_LIMIT = 50
_TUNE_FEEDBACK_LIMIT = 200

# The standalone web homepage is the rich, browse-everything surface; it is
# generated on demand (the "home" mode) with generous counts, independent of
# the daily email's admin-configured counts.
_HOME_RANK_PICKS = 40
_HOME_MIN_PICKS = 25  # the homepage is a browse surface — keep it full
_HOME_WEB_ARTICLES = 20

# Fallback counts when no admin config is present (run_digest defaults).
_DEFAULT_MAX_PICKS = 10
_DEFAULT_MAX_WEB = 5

# How many HN front pages to pull into the ranking candidate pool.
_HN_PAGES = 20

# The web block uses a fast model with few search rounds: Opus with 3 rounds
# spends its token budget on tool calls and never emits the final JSON, so the
# block came back empty in production. Haiku + 2 rounds is what the live
# /api/search path proved reliable.
_WEB_SEARCH_MODEL = "claude-haiku-4-5-20251001"
_WEB_SEARCHES = 2

# Reserved issues-table key for the standalone web homepage aggregation.
HOME_KEY = "home"


def _web_search_query(profile_md: str) -> str:
    """Distill the profile into a single web-search request string."""
    profile_md = (profile_md or "").strip()
    base = (
        "Fresh, high-quality articles a reader with the following interests "
        "would want today, from across the open web:\n\n"
    )
    return base + (profile_md or "technology, science, and society")


def _build_issue(
    picks: list[Pick],
    date: str,
    *,
    subject: str = "",
    intro: str = "",
    discoveries: list[Discovery] | None = None,
    web_articles: list[WebArticle] | None = None,
) -> Issue:
    return Issue(
        date=date,
        picks=picks,
        created_at=datetime.now(UTC),
        subject=subject,
        intro=intro,
        discoveries=discoveries or [],
        web_articles=web_articles or [],
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
    max_picks: int = _DEFAULT_MAX_PICKS,
    min_picks: int = 5,
    max_web: int = _DEFAULT_MAX_WEB,
    web_variety: int = 30,
    web_model: str | None = None,
    now: datetime | None = None,
) -> tuple[Issue, list[Article]]:
    """Pure pipeline: fetch → rank → summarize → discover → web → assemble Issue.

    Returns ``(issue, candidates)`` so callers can mark every fetched
    article seen (not just the picked ones) and avoid re-evaluating
    rejects on subsequent days.  Summarize, discovery, the Hacker News
    source, and the web-search block are all best-effort: a failure in any
    of them degrades to empty and never blocks the send.
    """
    # Resolve at call time (not as defaults) so monkeypatching the module
    # attributes in tests is honoured.
    summarize_fn = summarize_fn or summarize.summarize_issue
    discovery_fn = discovery_fn or discovery.find_discoveries
    hn_fn = hn_fn or hn.fetch_hn_articles
    websearch_fn = websearch_fn or websearch.search_web
    newsletters_fn = newsletters_fn or db.recent_inbox_articles

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

    issue = _build_issue(
        response.picks,
        date=date,
        subject=subject,
        intro=intro,
        discoveries=discoveries,
        web_articles=web_articles,
    )
    return issue, candidates


def _send_email(subject: str, html: str) -> None:
    import resend

    s = settings()
    resend.api_key = s.resend_api_key
    resend.Emails.send(
        {
            "from": f"newslet <{s.from_email}>",
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
    # Recency window for ranking; tuning reads its own wider window.
    feedback = db.recent_feedback(limit=_RANK_FEEDBACK_LIMIT)
    return run_digest(
        feed_urls=[str(f.url) for f in feeds_list],
        profile=profile,
        feedback=feedback,
        is_seen=db.is_seen,
        max_picks=config.max_rss_articles,
        max_web=config.max_web_articles,
        web_variety=config.web_variety,
        now=now,
    )


def _tune_profile_after_send() -> None:
    """Re-tune the profile after a confirmed send, using a wider feedback
    window than ranking so the cumulative learned-preferences block
    reflects durable taste, not just the last few days. Best effort:
    tuning must never break the send (which already happened)."""
    try:
        profile = db.get_profile()
        tune_feedback = db.recent_feedback(limit=_TUNE_FEEDBACK_LIMIT)
        new_markdown = tune.tune_profile(profile.markdown, tune_feedback)
        if new_markdown != profile.markdown:
            db.put_profile(new_markdown)
    except Exception:  # noqa: BLE001 - tuning is best effort, never raise
        log.exception("profile tuning failed after send")


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

    subject, html = email_render.render_email(issue, s.public_base_url)
    _send_email(subject, html)
    # Intentionally no mark_issue_sent / mark_seen here — see docstring.
    _tune_profile_after_send()

    log.info("manual send %s with %d picks", issue.date, len(issue.picks))
    return {"status": "sent", "date": issue.date, "picks": len(issue.picks)}


def _run_home(s: Any) -> dict:
    """Generate the standalone rich homepage aggregation (no email).

    Builds a generous browse surface — RSS + Hacker News, ranked, plus a
    web-search block — and stores it under the reserved ``HOME_KEY`` (hidden
    from ``list_issues``). The homepage's refresh button re-runs this. It
    never emails, never marks seen, and stays out of the daily cadence; unlike
    the daily digest it ignores the seen-store (it's a browse surface, not a
    deduped feed) and skips discovery (a subscribe-link/email concern).
    """
    now = datetime.now(UTC)
    feeds_list = db.list_feeds()
    profile = db.get_profile()
    config = db.get_config()
    feedback = db.recent_feedback(limit=_RANK_FEEDBACK_LIMIT)
    issue, _candidates = run_digest(
        feed_urls=[str(f.url) for f in feeds_list],
        profile=profile,
        feedback=feedback,
        is_seen=lambda _u: False,
        discovery_fn=lambda *_a, **_k: [],
        max_picks=_HOME_RANK_PICKS,
        min_picks=_HOME_MIN_PICKS,
        max_web=_HOME_WEB_ARTICLES,
        web_variety=config.web_variety,
        now=now,
    )
    issue = issue.model_copy(update={"date": HOME_KEY, "created_at": now})
    db.put_issue(issue, manual=True)
    log.info(
        "home refreshed: %d picks, %d web articles",
        len(issue.picks),
        len(issue.web_articles),
    )
    return {
        "status": "home_refreshed",
        "picks": len(issue.picks),
        "web": len(issue.web_articles),
    }


def handler(event: dict, context: Any) -> dict:
    """Run the digest pipeline once.

    With ``event["manual"]`` truthy, runs an on-demand send isolated from
    the daily cadence (see :func:`_run_manual`). Otherwise runs the daily
    pipeline idempotently.

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

    if event and event.get("home"):
        return _run_home(s)

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

    subject, html = email_render.render_email(issue, s.public_base_url)
    _send_email(subject, html)
    db.mark_issue_sent(issue.date)

    # Mark every candidate as seen — but only *after* a confirmed send.
    # An article Claude rejected today shouldn't be re-evaluated when it
    # crosses tomorrow's 24h window boundary.  Discovery urls go too, so
    # they are not re-surfaced on later days.
    seen_urls = [str(a.url) for a in candidates]
    seen_urls += [str(d.url) for d in issue.discoveries]
    seen_urls += [str(w.url) for w in issue.web_articles]
    if seen_urls:
        db.mark_seen(seen_urls)

    _tune_profile_after_send()

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
    return (f"newslet · {picks[0].title}", intro)


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
    )

    if not issue.picks:
        print("no picks today (no recent feed entries within 24h)")
        return 0

    subject, html = email_render.render_email(issue, settings().public_base_url)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"subject: {subject}")
    print(f"wrote {out} ({len(html)} bytes, {len(issue.picks)} picks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
