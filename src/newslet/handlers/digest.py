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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from newslet import db, email_render, feeds, rank
from newslet.config import settings
from newslet.contracts import Article, FeedbackRow, Issue, Pick, Profile, RankResponse

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _build_issue(picks: list[Pick], date: str) -> Issue:
    return Issue(date=date, picks=picks, created_at=datetime.now(UTC))


def run_digest(
    *,
    feed_urls: list[str],
    profile: Profile,
    feedback: list[FeedbackRow],
    is_seen: callable,
    rank_fn=rank.rank,
    now: datetime | None = None,
) -> tuple[Issue, list[Article]]:
    """Pure pipeline: fetch → rank → assemble Issue. No I/O of its own.

    Returns ``(issue, candidates)`` so callers can mark every fetched
    article seen (not just the picked ones) and avoid re-evaluating
    rejects on subsequent days.
    """
    now = now or datetime.now(UTC)
    since = now - timedelta(hours=24)
    candidates = feeds.fetch_recent(feed_urls, since=since, is_seen=is_seen)
    log.info("fetched %d candidate articles from %d feeds", len(candidates), len(feed_urls))
    date = now.strftime("%Y-%m-%d")
    if not candidates:
        return _build_issue([], date=date), []
    response: RankResponse = rank_fn(
        profile_md=profile.markdown,
        feedback=feedback,
        candidates=candidates,
    )
    log.info("claude returned %d picks", len(response.picks))
    return _build_issue(response.picks, date=date), candidates


def _send_email(subject: str, html: str) -> None:
    import resend

    s = settings()
    resend.api_key = s.resend_api_key
    resend.Emails.send(
        {
            "from": s.from_email,
            "to": [s.to_email],
            "subject": subject,
            "html": html,
        }
    )


def handler(event: dict, context: Any) -> dict:
    s = settings()
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    # Idempotency: don't re-send if today's issue already exists.
    # To force a re-run, delete the row in the Issues table.
    if db.issue_exists(today):
        log.info("issue %s already exists; skipping (delete row to force re-send)", today)
        return {"status": "already_sent", "date": today}

    feeds_list = db.list_feeds()
    profile = db.get_profile()
    feedback = db.recent_feedback(limit=50)

    issue, candidates = run_digest(
        feed_urls=[str(f.url) for f in feeds_list],
        profile=profile,
        feedback=feedback,
        is_seen=db.is_seen,
    )

    # Mark every candidate as seen (not just picks). An article Claude
    # rejected today shouldn't be re-evaluated tomorrow when it crosses
    # the 24h window boundary.
    if candidates:
        db.mark_seen([str(a.url) for a in candidates])

    db.put_issue(issue)
    subject, html = email_render.render_email(issue, s.public_base_url)
    _send_email(subject, html)
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


def _dry_run_env() -> None:
    """Populate harmless env vars so settings() succeeds without secrets."""
    os.environ.setdefault("ANTHROPIC_API_KEY", "dry-run")
    os.environ.setdefault("RESEND_API_KEY", "dry-run")
    os.environ.setdefault("FROM_EMAIL", "newslet@example.com")
    os.environ.setdefault("TO_EMAIL", "you@example.com")
    os.environ.setdefault("ADMIN_TOKEN", "dry-run")
    os.environ.setdefault("SIGNING_KEY", "dry-run-signing-key")
    os.environ.setdefault("PUBLIC_BASE_URL", "https://api.example.com")


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
