"""FastAPI web app: admin UI + /rate endpoint.

Two auth schemes:
- Admin routes: `admin_token` cookie matching `settings().admin_token`.
- `/rate`: HMAC-signed token in the query string (no cookie needed,
  so links work from any email client).
"""

from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import boto3
from fastapi import Cookie, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from mangum import Mangum
from markupsafe import Markup
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware

from newslet import (
    clock,
    db,
    email_render,
    facts,
    hn,
    newsletters,
    quotes,
    themes,
    tokens,
)
from newslet.config import settings
from newslet.contracts import Config, FeedbackRow

_TEMPLATES = Environment(
    loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "templates")),
    autoescape=select_autoescape(["html", "j2"]),
)

# docs_url/redoc_url/openapi_url are disabled so the product guide can own the
# `/docs` path (FastAPI's interactive API docs default there) — and so the web
# Lambda doesn't expose an API schema it has no use for.
app = FastAPI(title="newslet", docs_url=None, redoc_url=None, openapi_url=None)

# The product guide ships with the package under newslet/docs/. The HTML viewer
# (index.html) pulls the markdown (product.md) at runtime from /docs/content.md,
# so the rendered page can never drift from its source.
_DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"


def _read_doc(name: str) -> str:
    try:
        return (_DOCS_DIR / name).read_text(encoding="utf-8")
    except OSError:
        return ""


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject defensive HTTP headers on every response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if _is_https(request):
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains"
            )
        return response


class _CanonicalHostMiddleware(BaseHTTPMiddleware):
    """Redirect ``www.<domain>`` to the bare apex so the site has one
    canonical host. Both names terminate TLS at the same API (see the
    custom-domain resources in infra/template.yaml); this collapses them
    to a single origin so links, cookies, and emailed URLs don't split."""

    async def dispatch(self, request: Request, call_next):
        host = request.url.hostname or ""
        if host.startswith("www."):
            target = request.url.replace(
                scheme="https", hostname=host[4:], port=None
            )
            return RedirectResponse(url=str(target), status_code=301)
        return await call_next(request)


# Added before the security headers so that middleware stays outermost and
# still decorates the 301 response.
app.add_middleware(_CanonicalHostMiddleware)
app.add_middleware(_SecurityHeadersMiddleware)

def _theme_css(config: Config | None = None) -> Markup:
    """The theme's ``:root`` variable block (plus the text-size dial) for a
    template's stylesheet, from the stored admin config.

    ``Markup`` because the CSS contains theme tokens that HTML autoescaping
    would mangle; every value is a code-defined constant from
    ``newslet.themes`` (the text size is clamped there) — no user input
    flows in.
    """
    cfg = config or db.get_config()
    return Markup(themes.css(themes.get(cfg.theme), cfg.text_size))


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
    if not admin_token or not hmac.compare_digest(
        admin_token.encode(), settings().admin_token.encode()
    ):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


def _login_page(error: str = "") -> HTMLResponse:
    # Login renders pre-auth and must never be blocked by the config table,
    # so any failure reading the stored appearance falls back to defaults.
    try:
        theme_css = _theme_css()
    except Exception:  # noqa: BLE001 - auth must stay reachable
        theme_css = Markup(themes.css(themes.get(None)))
    html = _TEMPLATES.get_template("login.html.j2").render(
        error=error, theme_css=theme_css
    )
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
def login_form() -> HTMLResponse:
    return _login_page()


@app.post("/login")
def login(request: Request, token: str = Form(...)) -> Response:
    if not hmac.compare_digest(token.encode(), settings().admin_token.encode()):
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
# Product guide (public) — the attractive HTML docs, linked from /admin
# ---------------------------------------------------------------------------


@app.get("/docs", response_class=HTMLResponse)
def product_guide() -> HTMLResponse:
    """Serve the product guide's HTML viewer.

    Public (no admin cookie) so the guide is shareable. The viewer fetches the
    canonical markdown from ``/docs/content.md`` and renders it in the browser,
    with a selectable technical-detail level — so the HTML stays in lock-step
    with the markdown source rather than mirroring a stale copy.
    """
    html = _read_doc("index.html")
    if not html:
        raise HTTPException(status_code=404, detail="product guide not found")
    return HTMLResponse(html)


@app.get("/docs/content.md")
def product_guide_markdown() -> Response:
    """The canonical product-guide markdown — the single source of truth the
    HTML viewer pulls in real time."""
    md = _read_doc("product.md")
    if not md:
        raise HTTPException(status_code=404, detail="product guide not found")
    return Response(content=md, media_type="text/markdown; charset=utf-8")


# ---------------------------------------------------------------------------
# Homepage — the latest daily email, rendered fresh
# ---------------------------------------------------------------------------


_NO_ISSUES_HTML = (
    '<!doctype html><html><head><meta charset="utf-8"><title>daily scoop</title>'
    "<style>body{font:14px system-ui;text-align:center;margin-top:5rem}"
    "a{margin:0 8px}</style></head>"
    "<body><h1>daily scoop</h1><p>No editions yet — the first daily email builds one.</p>"
    '<p><a href="/discover">discover</a><a href="/admin">admin</a>'
    '<a href="/emails">emails</a></p></body></html>'
)


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    admin_token: str | None = Cookie(default=None),
) -> HTMLResponse:
    """The homepage: the latest daily email, rendered fresh with a web nav.

    Always the newest *delivered* issue (before today's send that is
    yesterday's, clearly dated in the header) — no rebuild, no LLM calls, no
    staleness logic. Same re-render as ``/emails/{date}`` (including its
    rate-link re-signing caveat), plus the ``web_nav`` strip.

    Preferring ``sent_at`` rows keeps the page honest when a daily run
    stored its issue but failed before the send: that undelivered edition
    stays off the homepage until a retry actually lands it. The fallback to
    the newest stored row applies only when *nothing in the last 60
    editions* was ever sent — effectively a fresh install (60 straight
    delivery failures would mean the system is down, not that the page
    should resurrect an undelivered edition).
    """
    _require_admin(admin_token)
    rows = db.list_issues(limit=60)
    row = next((r for r in rows if r.get("sent_at")), rows[0] if rows else None)
    issue = db.get_issue(row["date"]) if row else None
    if issue is None:
        return HTMLResponse(_NO_ISSUES_HTML)
    _, html = email_render.render_email(
        issue,
        _base_url(request),
        theme=themes.get(issue.theme),
        text_size=issue.text_size,
        web_nav=True,
    )
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Discover — stored recommendations of RSS feeds + X accounts
# ---------------------------------------------------------------------------


@app.get("/discover", response_class=HTMLResponse)
def discover_page(admin_token: str | None = Cookie(default=None)) -> HTMLResponse:
    """The Discover page: RSS feeds and X accounts the user might like.

    Renders the stored board (built by the weekly ``{"discover": true}``
    digest run — see ``digest._run_discover``); the page never generates
    on visit, so it always loads instantly. Feeds the user already follows
    are hidden; each remaining feed gets a one-click add (``POST
    /api/feeds``), and each X account links out to its profile.
    """
    _require_admin(admin_token)
    board = db.get_discover()

    followed = {db.normalize_url(str(f.url)) for f in db.list_feeds()}
    feed_cards = [
        {
            "title": f.title,
            "site_url": str(f.site_url),
            "feed_url": str(f.feed_url),
            "reason": f.reason,
        }
        for f in board.feeds
        if db.normalize_url(str(f.feed_url)) not in followed
    ]
    account_cards = [
        {"handle": a.handle, "name": a.name, "reason": a.reason, "url": str(a.url)}
        for a in board.accounts
    ]
    generated_iso = board.generated_at.isoformat() if board.generated_at else ""
    generated_label = (
        clock.local_now(board.generated_at).strftime("%A, %B %-d")
        if board.generated_at
        else ""
    )
    html = _TEMPLATES.get_template("discover.html.j2").render(
        theme_css=_theme_css(),
        feeds=feed_cards,
        accounts=account_cards,
        generated_at=generated_label,
        baseline_iso=generated_iso,
    )
    return HTMLResponse(html)


@app.post("/api/discover/refresh")
def discover_refresh(admin_token: str | None = Cookie(default=None)) -> JSONResponse:
    """Kick off a Discover-board regeneration (async; takes a minute or two).

    Async-invokes the digest Lambda with ``{"discover": true}`` — the same
    fire-and-forget pattern as the home refresh — because the build (web
    search + feed liveness checks) far exceeds this Lambda's timeout. The
    page polls ``/api/discover/status`` and reloads when the board is newer.
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
        Payload=json.dumps({"discover": True}),
    )
    return JSONResponse({"ok": True, "status": "refreshing"})


@app.get("/api/discover/status")
def discover_status(admin_token: str | None = Cookie(default=None)) -> JSONResponse:
    """Return the stored Discover board's generation timestamp.

    The page captures this before a refresh and polls until it changes,
    then reloads — same await-the-async-build shape as the home status.
    """
    _require_admin(admin_token)
    board = db.get_discover()
    generated_iso = board.generated_at.isoformat() if board.generated_at else ""
    return JSONResponse({"generated_at": generated_iso, "ready": bool(generated_iso)})


# ---------------------------------------------------------------------------
# Admin UI
# ---------------------------------------------------------------------------


@app.get("/admin", response_class=HTMLResponse)
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
    config = db.get_config()
    recent_issues = db.list_issues(limit=5)
    last_sent = next(
        (i["date"] for i in recent_issues if i.get("sent_at")),
        None,
    )
    subscriptions = [
        {
            "address": s.address,
            "source": s.source,
            "status": s.status,
            "created_at": s.created_at.strftime("%Y-%m-%d"),
            "last_received_at": (
                s.last_received_at.strftime("%Y-%m-%d")
                if s.last_received_at
                else ""
            ),
        }
        for s in db.list_subscriptions()
    ]
    html = _TEMPLATES.get_template("admin.html.j2").render(
        theme_css=_theme_css(config),
        themes=themes.list_themes(),
        feeds=feeds_rows,
        profile_md=profile.markdown,
        config=config,
        recent_issues=recent_issues,
        last_sent=last_sent,
        sent=sent,
        subscriptions=subscriptions,
        mail_domain=settings().mail_domain,
    )
    return HTMLResponse(html)


@app.get("/emails", response_class=HTMLResponse)
def emails_index(admin_token: str | None = Cookie(default=None)) -> HTMLResponse:
    _require_admin(admin_token)
    html = _TEMPLATES.get_template("emails.html.j2").render(
        theme_css=_theme_css(),
        issues=db.list_issues(limit=60),
    )
    return HTMLResponse(html)


@app.post("/api/feeds")
def add_feed(
    request: Request,
    url: str = Form(...),
    title: str = Form(default=""),
    admin_token: str | None = Cookie(default=None),
) -> Response:
    """Add a feed. JSON for fetch-based callers (the Discover page adds
    in place without navigating away — same negotiation as ``/api/vote``);
    303 to ``/admin`` for the no-JS form post."""
    _require_admin(admin_token)
    try:
        feed = db.add_feed(url, title=title)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid feed URL: {exc.errors()[0]['msg']}",
        ) from exc
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": True, "url": str(feed.url)})
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/api/feeds/delete")
def delete_feed(
    url: str = Form(...),
    admin_token: str | None = Cookie(default=None),
) -> Response:
    _require_admin(admin_token)
    db.delete_feed(url)  # no-op on invalid input
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/api/profile")
def save_profile(
    markdown: str = Form(...),
    admin_token: str | None = Cookie(default=None),
) -> Response:
    _require_admin(admin_token)
    db.put_profile(markdown)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/api/config")
def save_config(
    max_rss_articles: int = Form(...),
    max_web_articles: int = Form(...),
    web_variety: int = Form(...),
    # An unchecked HTML checkbox submits nothing, so absence means "off".
    # New fields default-optional so older clients/tests posting the original
    # three still validate.
    x_enabled: bool = Form(default=False),
    max_x_articles: int = Form(default=15),
    max_random_articles: int = Form(default=4),
    theme: str = Form(default=themes.DEFAULT_THEME),
    text_size: int = Form(default=themes.TEXT_SIZE_DEFAULT),
    facts_enabled: bool = Form(default=False),
    quote_enabled: bool = Form(default=False),
    weather_enabled: bool = Form(default=False),
    admin_token: str | None = Cookie(default=None),
) -> Response:
    """Persist the daily-email article counts, web-search variety, X source,
    the off-your-beat count, the tech-facts and quote toggles, and the app
    appearance (theme + text size). Checkbox semantics: an unchecked box
    submits nothing, so the boolean toggles absent means off."""
    _require_admin(admin_token)
    # Strict on write (the read path is the lenient one): reject names the
    # picker could never have sent.
    if theme not in themes.THEMES:
        raise HTTPException(status_code=400, detail="unknown theme")
    try:
        cfg = Config(
            max_rss_articles=max_rss_articles,
            max_web_articles=max_web_articles,
            web_variety=web_variety,
            x_enabled=x_enabled,
            max_x_articles=max_x_articles,
            max_random_articles=max_random_articles,
            theme=theme,
            text_size=text_size,
            facts_enabled=facts_enabled,
            quote_enabled=quote_enabled,
            weather_enabled=weather_enabled,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid config: {exc.errors()[0]['msg']}",
        ) from exc
    db.put_config(cfg)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/api/subscriptions")
def add_subscription(
    source: str = Form(default=""),
    admin_token: str | None = Cookie(default=None),
) -> Response:
    """Mint a fresh inbound address for a newsletter and store it as pending.

    The generated address is shown on the admin page; the user pastes it into
    the newsletter's signup form. Requires ``MAIL_DOMAIN`` to be configured —
    without it there is nowhere for the mail to land.
    """
    _require_admin(admin_token)
    try:
        address = newsletters.generate_address(settings().mail_domain)
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail="MAIL_DOMAIN is not configured; cannot create subscriptions",
        ) from exc
    db.add_subscription(source.strip(), address=address)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/api/subscriptions/delete")
def delete_subscription(
    address: str = Form(...),
    admin_token: str | None = Cookie(default=None),
) -> Response:
    _require_admin(admin_token)
    db.delete_subscription(address)
    return RedirectResponse(url="/admin", status_code=303)


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
    return RedirectResponse(url="/admin?sent=1", status_code=303)


# ---------------------------------------------------------------------------
# Rate (public, signed)
# ---------------------------------------------------------------------------


_THANKS_HTML_TEMPLATE = (
    '<!doctype html><html><head><meta charset="utf-8"><title>thanks</title>'
    "<style>body{font:14px system-ui;text-align:center;margin-top:5rem}"
    "textarea{font:inherit;width:90%;max-width:32rem;height:4rem}"
    "form{margin-top:1.5rem}</style></head>"
    "<body><h1>thanks</h1><p>recorded your __RATING__ for<br>__TARGET__</p>"
    '<form method="post" action="/rate/note">'
    '<input type="hidden" name="a" value="__URL__">'
    '<input type="hidden" name="d" value="__DATE__">'
    '<input type="hidden" name="t" value="__TOKEN__">'
    '<p><label>why? (optional)<br>'
    '<textarea name="note"></textarea></label></p>'
    '<button type="submit">save note</button>'
    "</form></body></html>"
)


def _thanks_html(
    rating: str, url: str, issue_date: str, token: str, label: str = ""
) -> str:
    """The post-vote thanks page.

    ``label`` (used for synthetic vote targets like facts) shows a plain
    title instead of a link — the synthetic /facts/... path has no route,
    so linking it would 404. The hidden note-form fields always carry the
    original ``url`` + token: that is what the HMAC signed.
    """
    from html import escape

    if label:
        target = f"<strong>{escape(label)}</strong>"
    else:
        escaped = escape(url, quote=True)
        target = f'<a href="{escaped}">{escaped}</a>'
    return (
        _THANKS_HTML_TEMPLATE.replace("__RATING__", escape(rating))
        .replace("__TARGET__", target)
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

    # Fact votes carry synthetic /facts/{issue-key}/{slot} URLs (see
    # email_render); they get a title lookup by slot and a link-free thanks
    # page (the synthetic path has no route to link to). The match uses the
    # same anchored full-shape regex the digest's feedback routing uses
    # (newslet.facts.vote_slot), so a real article whose path merely ends
    # ".../facts/<x>/end" still takes the normal picks branch.
    vote_path = urlparse(article_url).path
    fact_slot = facts.vote_slot(vote_path)
    is_fact_vote = fact_slot is not None
    is_quote_vote = quotes.is_quote_vote(vote_path)

    # Best-effort title lookup from the stored issue
    title = ""
    issue = db.get_issue(d)
    if issue:
        if is_fact_vote:
            title = next(
                (f.title for f in issue.facts if f.slot == fact_slot), ""
            )
        elif is_quote_vote:
            if issue.quote is not None:
                # Author alone can't teach the tuner which line landed —
                # carry a text prefix, same shape as the no-repeat log.
                title = (
                    f"Quote: {issue.quote.author} — {issue.quote.text[:60]}"
                )
        else:
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
    label = ""
    if is_fact_vote:
        label = title or "this fact"
    elif is_quote_vote:
        label = title or "the quote of the day"
    return HTMLResponse(_thanks_html(v, a, d, t, label=label))


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
    '<p><a href="/admin">manage feeds</a></p></body></html>'
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


@app.get("/emails/{date}", response_class=HTMLResponse)
def view_email(
    date: str,
    request: Request,
    admin_token: str | None = Cookie(default=None),
) -> HTMLResponse:
    """Re-render a past daily email's HTML (the as-sent archive view).

    Unlike the homepage (which adds a web nav strip), the archive shows the
    email exactly as it was sent.

    Note: rate links are regenerated with the *current* ``SIGNING_KEY``.
    If you rotate that key, every old issue's +/- links will start
    returning 403 — there is no migration path.
    """
    _require_admin(admin_token)
    issue = db.get_issue(date)
    if not issue:
        raise HTTPException(status_code=404, detail="no issue for that date")
    # The issue's stamped send-time appearance, not the current config —
    # changing the theme or text size must not restyle the as-sent archive
    # (pre-themes rows default to classic at 100%, what they shipped with).
    # themes.get falls back to the app default for *unknown* names; the
    # explicit "classic" stamp on legacy rows is what keeps them accurate.
    _, html = email_render.render_email(
        issue,
        _base_url(request),
        theme=themes.get(issue.theme),
        text_size=issue.text_size,
    )
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Live JSON endpoints (admin-cookie authed)
# ---------------------------------------------------------------------------


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
