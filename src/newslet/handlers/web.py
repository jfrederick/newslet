"""FastAPI web app: admin UI + /rate endpoint.

Two auth schemes:
- Admin routes: `admin_token` cookie matching `settings().admin_token`.
- `/rate`: HMAC-signed token in the query string (no cookie needed,
  so links work from any email client).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import boto3
from fastapi import Cookie, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from mangum import Mangum
from pydantic import ValidationError

from newslet import db, email_render, hn, tokens, websearch
from newslet.config import settings
from newslet.contracts import FeedbackRow

_TEMPLATES = Environment(
    loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "templates")),
    autoescape=select_autoescape(["html", "j2"]),
)

app = FastAPI(title="newslet")

# The interactive subject search runs synchronously behind the HTTP API's
# hard ~30s integration timeout, so it uses a fast model and few search
# rounds. The digest's web block (300s Lambda budget) keeps the thorough
# defaults in websearch.search_web.
_FAST_SEARCH_MODEL = "claude-haiku-4-5-20251001"
_FAST_SEARCH_ROUNDS = 2
_FAST_SEARCH_RESULTS = 12


def _interactive_search(query: str) -> list:
    """Run the snappy, timeout-safe variant of the subject search."""
    return websearch.search_web(
        query.strip(),
        max_results=_FAST_SEARCH_RESULTS,
        max_searches=_FAST_SEARCH_ROUNDS,
        model=_FAST_SEARCH_MODEL,
    )


def _is_https(request: Request) -> bool:
    """Detect whether the original client connection was HTTPS.

    Mangum populates ``request.url.scheme`` from the ASGI scope, which
    in turn derives from the API Gateway v2 event. To be robust against
    other proxies, also check the ``X-Forwarded-Proto`` header.
    """
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _require_admin(admin_token: str | None) -> None:
    if not admin_token or admin_token != settings().admin_token:
        raise HTTPException(status_code=303, headers={"Location": "/login"})


def _login_page(error: str = "") -> HTMLResponse:
    html = _TEMPLATES.get_template("login.html.j2").render(error=error)
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
def login_form() -> HTMLResponse:
    return _login_page()


@app.post("/login")
def login(request: Request, token: str = Form(...)) -> Response:
    if token != settings().admin_token:
        return _login_page("Invalid token")
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        "admin_token",
        token,
        httponly=True,
        secure=_is_https(request),
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return resp


@app.get("/logout")
def logout() -> Response:
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("admin_token")
    return resp


# ---------------------------------------------------------------------------
# Admin UI
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def admin_index(
    sent: str | None = Query(default=None),
    admin_token: str | None = Cookie(default=None),
) -> HTMLResponse:
    _require_admin(admin_token)
    feeds_rows = [
        {
            "url": str(f.url),
            "title": f.title,
            "added_at": f.added_at.strftime("%Y-%m-%d"),
        }
        for f in db.list_feeds()
    ]
    profile = db.get_profile()
    recent_issues = db.list_issues(limit=5)
    last_sent = next(
        (i["date"] for i in recent_issues if i.get("sent_at")),
        None,
    )
    html = _TEMPLATES.get_template("admin.html.j2").render(
        feeds=feeds_rows,
        profile_md=profile.markdown,
        recent_issues=recent_issues,
        last_sent=last_sent,
        sent=sent,
    )
    return HTMLResponse(html)


@app.get("/issues", response_class=HTMLResponse)
def issues_index(admin_token: str | None = Cookie(default=None)) -> HTMLResponse:
    _require_admin(admin_token)
    html = _TEMPLATES.get_template("issues.html.j2").render(
        issues=db.list_issues(limit=60),
    )
    return HTMLResponse(html)


@app.post("/api/feeds")
def add_feed(
    url: str = Form(...),
    title: str = Form(default=""),
    admin_token: str | None = Cookie(default=None),
) -> Response:
    _require_admin(admin_token)
    try:
        db.add_feed(url, title=title)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid feed URL: {exc.errors()[0]['msg']}",
        ) from exc
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/feeds/delete")
def delete_feed(
    url: str = Form(...),
    admin_token: str | None = Cookie(default=None),
) -> Response:
    _require_admin(admin_token)
    db.delete_feed(url)  # no-op on invalid input
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/profile")
def save_profile(
    markdown: str = Form(...),
    admin_token: str | None = Cookie(default=None),
) -> Response:
    _require_admin(admin_token)
    db.put_profile(markdown)
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/send-now")
def send_now(admin_token: str | None = Cookie(default=None)) -> Response:
    """Trigger an on-demand digest send.

    Invokes the digest Lambda asynchronously (``Event``) with a
    ``{"manual": true}`` payload — a real run with a live feedback loop
    that stays out of the daily cadence (see digest._run_manual). Async
    because a digest far exceeds this Lambda's timeout; the email lands a
    bit later.
    """
    _require_admin(admin_token)
    fn = settings().digest_function_name
    if not fn:
        raise HTTPException(
            status_code=503,
            detail="DIGEST_FUNCTION_NAME is not configured for the web app",
        )
    boto3.client("lambda").invoke(
        FunctionName=fn,
        InvocationType="Event",
        Payload=json.dumps({"manual": True}),
    )
    return RedirectResponse(url="/?sent=1", status_code=303)


# ---------------------------------------------------------------------------
# Rate (public, signed)
# ---------------------------------------------------------------------------


_THANKS_HTML_TEMPLATE = (
    '<!doctype html><html><head><meta charset="utf-8"><title>thanks</title>'
    "<style>body{font:14px system-ui;text-align:center;margin-top:5rem}"
    "textarea{font:inherit;width:90%;max-width:32rem;height:4rem}"
    "form{margin-top:1.5rem}</style></head>"
    '<body><h1>thanks</h1><p>recorded your __RATING__ for<br>'
    '<a href="__URL__">__URL__</a></p>'
    '<form method="post" action="/rate/note">'
    '<input type="hidden" name="a" value="__URL__">'
    '<input type="hidden" name="d" value="__DATE__">'
    '<input type="hidden" name="t" value="__TOKEN__">'
    '<p><label>why? (optional)<br>'
    '<textarea name="note"></textarea></label></p>'
    '<button type="submit">save note</button>'
    "</form></body></html>"
)


def _thanks_html(rating: str, url: str, issue_date: str, token: str) -> str:
    from html import escape

    return (
        _THANKS_HTML_TEMPLATE.replace("__RATING__", escape(rating))
        .replace("__URL__", escape(url, quote=True))
        .replace("__DATE__", escape(issue_date, quote=True))
        .replace("__TOKEN__", escape(token, quote=True))
    )


_NOTE_SAVED_HTML_TEMPLATE = (
    '<!doctype html><html><head><meta charset="utf-8"><title>thanks</title>'
    "<style>body{font:14px system-ui;text-align:center;margin-top:5rem}</style></head>"
    "<body><h1>thanks</h1><p>saved your note.</p></body></html>"
)


@app.get("/rate", response_class=HTMLResponse)
def rate(
    a: str = Query(..., description="article url"),
    d: str = Query(..., description="issue date YYYY-MM-DD"),
    v: str = Query(..., description="up or down"),
    t: str = Query(..., description="HMAC token"),
) -> HTMLResponse:
    if v not in ("up", "down"):
        raise HTTPException(status_code=400, detail="bad rating")
    # ``a`` has already been percent-decoded by Starlette's query
    # parser; calling unquote() a second time would corrupt URLs that
    # legitimately contain "%XX" sequences in their path (e.g.,
    # Wikipedia article titles encoded with %20).
    if not tokens.verify(a, d, t):
        raise HTTPException(status_code=403, detail="bad token")
    # Canonicalize the key the same way /rate/note does, so a note posted from
    # the thanks page lands on this exact row regardless of HttpUrl rewrites.
    article_url = db.normalize_url(a)

    # Best-effort title lookup from the stored issue
    title = ""
    issue = db.get_issue(d)
    if issue:
        for pick in issue.picks:
            if str(pick.url) == article_url:
                title = pick.title
                break

    db.put_feedback(
        FeedbackRow(
            article_url=article_url,
            title=title,
            rating=v,  # type: ignore[arg-type]
            ts=datetime.now(UTC),
            issue_date=d,
        )
    )
    # The note form carries the original ``a`` + token (what the HMAC signed),
    # not the normalized key, so /rate/note's token check still passes.
    return HTMLResponse(_thanks_html(v, a, d, t))


@app.post("/rate/note", response_class=HTMLResponse)
def rate_note(
    a: str = Form(..., description="article url"),
    d: str = Form(..., description="issue date YYYY-MM-DD"),
    t: str = Form(..., description="HMAC token"),
    note: str = Form(default=""),
) -> HTMLResponse:
    """Attach a free-text "why" note to an already-recorded rating.

    Re-verifies the same signed token as ``/rate`` so the form works from
    an email link with no admin cookie.
    """
    if not tokens.verify(a, d, t):
        raise HTTPException(status_code=403, detail="bad token")
    # Same canonical key as /rate so the note attaches to the existing row.
    db.update_feedback_note(db.normalize_url(a), d, note)
    return HTMLResponse(_NOTE_SAVED_HTML_TEMPLATE)


# ---------------------------------------------------------------------------
# Subscribe (public, signed) — one-click "add this discovered source"
# ---------------------------------------------------------------------------


_SUBSCRIBED_HTML_TEMPLATE = (
    '<!doctype html><html><head><meta charset="utf-8"><title>subscribed</title>'
    "<style>body{font:14px system-ui;text-align:center;margin-top:5rem}"
    "a{color:#0b3d91}</style></head>"
    "<body><h1>subscribed</h1><p>added <strong>__TITLE__</strong> to your feeds:<br>"
    '<a href="__FEED__">__FEED__</a></p>'
    '<p><a href="/">manage feeds</a></p></body></html>'
)


def _subscribed_html(title: str, feed_url: str) -> str:
    from html import escape

    return _SUBSCRIBED_HTML_TEMPLATE.replace(
        "__TITLE__", escape(title or feed_url)
    ).replace("__FEED__", escape(feed_url, quote=True))


@app.get("/subscribe", response_class=HTMLResponse)
def subscribe(
    f: str = Query(..., description="RSS/Atom feed url"),
    d: str = Query(..., description="issue date YYYY-MM-DD"),
    t: str = Query(..., description="HMAC token"),
    s: str = Query(default="", description="source title for display"),
) -> HTMLResponse:
    """Add a discovered source's feed to the user's subscriptions.

    Signed exactly like ``/rate`` (HMAC over ``(feed_url, issue_date)``) so
    a single click from any email client works with no admin cookie.
    Idempotent: ``db.add_feed`` upserts on the normalized URL, so clicking
    twice is harmless.
    """
    if not tokens.verify(f, d, t):
        raise HTTPException(status_code=403, detail="bad token")
    try:
        feed = db.add_feed(f, title=s)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid feed URL: {exc.errors()[0]['msg']}",
        ) from exc
    return HTMLResponse(_subscribed_html(s, str(feed.url)))


# ---------------------------------------------------------------------------
# View past issue
# ---------------------------------------------------------------------------


def _base_url(request: Request) -> str:
    """Compute the public base URL from the current request.

    The web Lambda doesn't get ``PUBLIC_BASE_URL`` in its environment
    (that would create a CloudFormation circular dependency with the
    HTTP API), so we derive it from the request the browser made.
    """
    return str(request.base_url).rstrip("/")


def _vote_lookup(issue) -> dict[str, str]:
    """Map every article url in the issue to its current rating (or absent).

    One batched read so the rich view can render sticky +/- state — making
    the *effect* of a vote visible after it is cast.
    """
    urls = [str(p.url) for p in issue.picks] + [str(w.url) for w in issue.web_articles]
    return db.feedback_ratings(urls, issue.date)


def _article_card(*, url, title, blurb, source, score, rating,
                  points=None, comments=None, comments_url="") -> dict:
    """Normalize a pick or web article into the template's card shape."""
    return {
        "url": str(url),
        "title": title,
        "blurb": blurb,
        "source": source or "",
        "score": score,
        "rating": rating,
        "points": points,
        "comments": comments,
        "comments_url": comments_url or "",
    }


@app.get("/issues/{date}", response_class=HTMLResponse)
def view_issue(
    date: str,
    request: Request,
    q: str | None = Query(default=None, description="optional subject search"),
    admin_token: str | None = Cookie(default=None),
) -> HTMLResponse:
    """Rich web view of a stored issue: up to 40 ranked picks plus a
    "from around the web" block (≈60 articles), each with sticky +/- voting,
    a source filter, and a subject search box that runs a live web search.

    Voting here writes to the same Feedback table the email +/- links use,
    so it feeds the next day's ranking identically.
    """
    _require_admin(admin_token)
    issue = db.get_issue(date)
    if not issue:
        raise HTTPException(status_code=404, detail="no issue for that date")

    votes = _vote_lookup(issue)

    sorted_picks = sorted(issue.picks, key=lambda p: p.score, reverse=True)
    pick_cards = [
        _article_card(
            url=p.url, title=p.title, blurb=p.blurb, source=p.source,
            score=p.score, rating=votes.get(str(p.url), ""),
        )
        for p in sorted_picks
    ]
    web_cards = [
        _article_card(
            url=w.url, title=w.title, blurb=w.blurb, source=w.source,
            score=None, rating=votes.get(str(w.url), ""),
            points=w.points, comments=w.comments, comments_url=w.comments_url,
        )
        for w in issue.web_articles
    ]

    # Optional server-rendered subject search (progressive-enhancement
    # fallback for when JS is off). Best-effort: empty on any failure.
    query = (q or "").strip()
    search_cards: list[dict] = []
    if query:
        for r in _interactive_search(query):
            search_cards.append(
                _article_card(
                    url=r.url, title=r.title, blurb=r.blurb, source=r.source,
                    score=None, rating=votes.get(str(r.url), ""),
                    points=r.points, comments=r.comments,
                    comments_url=r.comments_url,
                )
            )

    sources = sorted(
        {c["source"] for c in pick_cards + web_cards if c["source"]},
        key=str.lower,
    )

    html = _TEMPLATES.get_template("read.html.j2").render(
        issue_key=issue.date,
        issue_date=email_render._display_date(issue.date),
        subject=issue.subject,
        intro=issue.intro,
        picks=pick_cards,
        web_articles=web_cards,
        sources=sources,
        total=len(pick_cards) + len(web_cards),
        query=query,
        search_results=search_cards,
        email_url=f"/issues/{date}/email",
    )
    return HTMLResponse(html)


@app.get("/issues/{date}/email", response_class=HTMLResponse)
def view_issue_email(
    date: str,
    request: Request,
    admin_token: str | None = Cookie(default=None),
) -> HTMLResponse:
    """Re-render a past issue's raw email HTML (the as-sent view).

    Note: rate links are regenerated with the *current* ``SIGNING_KEY``.
    If you rotate that key, every old issue's +/- links will start
    returning 403 — there is no migration path. Either store the
    rendered HTML at send time, or treat the signing key as permanent.
    """
    _require_admin(admin_token)
    issue = db.get_issue(date)
    if not issue:
        raise HTTPException(status_code=404, detail="no issue for that date")
    _, html = email_render.render_email(issue, _base_url(request))
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Web-view actions (admin-cookie authed; no HMAC needed)
# ---------------------------------------------------------------------------


@app.post("/api/vote")
def vote(
    request: Request,
    url: str = Form(...),
    rating: str = Form(...),
    date: str = Form(...),
    title: str = Form(default=""),
    admin_token: str | None = Cookie(default=None),
) -> Response:
    """Record a +/- vote from the rich web view.

    Writes the same :class:`FeedbackRow` shape the signed email ``/rate``
    link writes — keyed on ``(article_url, issue_date)`` so re-voting
    overwrites — so a web vote feeds the next ranking exactly like an email
    vote. Returns JSON for the fetch-based UI; falls back to a redirect for
    the no-JS form post.
    """
    _require_admin(admin_token)
    if rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="bad rating")
    try:
        article_url = db.normalize_url(url)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="invalid url") from exc

    db.put_feedback(
        FeedbackRow(
            article_url=article_url,
            title=title,
            rating=rating,  # type: ignore[arg-type]
            ts=datetime.now(UTC),
            issue_date=date,
        )
    )

    wants_json = "application/json" in request.headers.get("accept", "")
    if wants_json:
        return JSONResponse({"ok": True, "rating": rating, "url": article_url})
    return RedirectResponse(url=f"/issues/{date}", status_code=303)


@app.get("/api/search")
def api_search(
    q: str = Query(..., description="subject to research"),
    admin_token: str | None = Cookie(default=None),
) -> JSONResponse:
    """Live subject search ("textbook"): web-search a topic and return
    JSON cards the page renders inline. Best-effort — an empty list on any
    failure, never a 500."""
    _require_admin(admin_token)
    results = _interactive_search(q)
    return JSONResponse(
        {
            "query": q,
            "results": [
                {
                    "url": str(r.url),
                    "title": r.title,
                    "blurb": r.blurb,
                    "source": r.source,
                    "points": r.points,
                    "comments": r.comments,
                    "comments_url": r.comments_url,
                }
                for r in results
            ],
        }
    )


@app.get("/api/hn")
def api_hn(admin_token: str | None = Cookie(default=None)) -> JSONResponse:
    """Live Hacker News front page (rich): points, comments, and a thread
    link. Best-effort — empty on any failure."""
    _require_admin(admin_token)
    stories = hn.fetch_hn_rich(pages=2, limit=20)
    return JSONResponse(
        {
            "results": [
                {
                    "url": str(s.url),
                    "title": s.title,
                    "blurb": s.blurb,
                    "source": s.source,
                    "points": s.points,
                    "comments": s.comments,
                    "comments_url": s.comments_url,
                }
                for s in stories
            ],
        }
    )


# Lambda entry point
handler = Mangum(app)
