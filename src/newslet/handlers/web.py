"""FastAPI web app: admin UI + /rate endpoint.

Two auth schemes:
- Admin routes: `admin_token` cookie matching `settings().admin_token`.
- `/rate`: HMAC-signed token in the query string (no cookie needed,
  so links work from any email client).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import Cookie, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from mangum import Mangum
from pydantic import ValidationError

from newslet import db, email_render, tokens
from newslet.config import settings
from newslet.contracts import FeedbackRow

_TEMPLATES = Environment(
    loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "templates")),
    autoescape=select_autoescape(["html", "j2"]),
)

app = FastAPI(title="newslet")


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
def admin_index(admin_token: str | None = Cookie(default=None)) -> HTMLResponse:
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


# ---------------------------------------------------------------------------
# Rate (public, signed)
# ---------------------------------------------------------------------------


_THANKS_HTML_TEMPLATE = (
    '<!doctype html><html><head><meta charset="utf-8"><title>thanks</title>'
    "<style>body{font:14px system-ui;text-align:center;margin-top:5rem}</style></head>"
    '<body><h1>thanks</h1><p>recorded your __RATING__ for<br>'
    '<a href="__URL__">__URL__</a></p></body></html>'
)


def _thanks_html(rating: str, url: str) -> str:
    from html import escape

    return _THANKS_HTML_TEMPLATE.replace("__RATING__", escape(rating)).replace(
        "__URL__", escape(url, quote=True)
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
    article_url = a
    if not tokens.verify(article_url, d, t):
        raise HTTPException(status_code=403, detail="bad token")

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
    return HTMLResponse(_thanks_html(v, article_url))


# ---------------------------------------------------------------------------
# View past issue
# ---------------------------------------------------------------------------


@app.get("/issues/{date}", response_class=HTMLResponse)
def view_issue(
    date: str,
    request: Request,
    admin_token: str | None = Cookie(default=None),
) -> HTMLResponse:
    """Re-render a past issue's email HTML.

    Note: rate links are regenerated with the *current* ``SIGNING_KEY``.
    If you rotate that key, every old issue's +/- links will start
    returning 403 — there is no migration path. Either store the
    rendered HTML at send time, or treat the signing key as permanent.
    """
    _require_admin(admin_token)
    issue = db.get_issue(date)
    if not issue:
        raise HTTPException(status_code=404, detail="no issue for that date")
    _, html = email_render.render_email(issue, settings().public_base_url)
    return HTMLResponse(html)


# Lambda entry point
handler = Mangum(app)
