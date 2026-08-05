# PR 1: Homepage Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/` renders the latest issue as the email HTML (plus a thin web nav), and the entire heavy "home" pipeline is deleted.

**Architecture:** `email_render.render_email` gains a `web_nav` flag that adds a nav strip when rendering for the web. The `/` route becomes: newest row from `db.list_issues(limit=1)` → `db.get_issue` → `render_email(web_nav=True)`. Everything that existed only to serve the old card homepage is removed: the `{"home"}` digest mode + cron, `/api/home/*`, `/api/vote`, `/api/search` + interactive search, and `read.html.j2`.

**Tech Stack:** Python 3.12, FastAPI + Mangum, Jinja2, DynamoDB (moto in tests), SAM. Spec: `docs/superpowers/specs/2026-08-04-dashboard-pivot-design.md`.

## Global Constraints

- Gates before push: `.venv/bin/ruff check src tests scripts` AND `.venv/bin/python -m pytest -q`.
- Update AGENTS.md / DESIGN.md / README.md / `src/newslet/docs/product.md` in the same PR (repo rule).
- Sent-email HTML must be byte-identical to before when `web_nav` is not passed (default False).
- `/emails/{date}` stays as-sent (no nav) for archive fidelity.
- `/api/hn` is NOT part of this cleanup — leave it.
- GH workflow: `gh auth switch -u jfrederick` before PR ops; squash merge; deploy-on-merge.

---

### Task 1: `web_nav` flag on the email renderer

**Files:**
- Modify: `src/newslet/email_render.py` (signature at :37, context dict near :155)
- Modify: `src/newslet/templates/email.html.j2` (immediately after `<body ...>` opens, ~:17)
- Test: `tests/test_email_render.py`

**Interfaces:**
- Produces: `render_email(issue, public_base_url, theme=None, text_size=100, web_nav=False) -> tuple[str, str]`. Task 2 calls it with `web_nav=True`.

- [ ] **Step 1: Write failing tests** (append to `tests/test_email_render.py`, matching its existing fixture style for building an Issue):

```python
def test_web_nav_off_by_default():
    _, html = email_render.render_email(_issue(), "https://ex.com")
    assert 'href="/admin"' not in html


def test_web_nav_renders_nav_strip():
    _, html = email_render.render_email(_issue(), "https://ex.com", web_nav=True)
    assert 'href="/discover"' in html
    assert 'href="/admin"' in html
    assert 'href="/emails"' in html
```

(Use the file's existing issue-builder helper; if it's named differently, adapt the two tests to it rather than adding a new builder.)

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_email_render.py -q` — expect the two new tests FAIL (unexpected keyword / missing links).

- [ ] **Step 3: Implement.** In `email_render.py` add `web_nav: bool = False` to `render_email`'s signature, document it in the docstring ("adds the web page's nav strip; never set for sent emails"), and pass `web_nav=web_nav` in the template context. In `email.html.j2`, directly after the `<body ...>` tag insert:

```jinja
{% if web_nav %}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{{ p.bg }};">
  <tr>
    <td align="center" style="padding:14px 12px 0;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:640px;">
        <tr>
          <td style="font-size:{{ fs(13) }}; color:{{ p.muted }};">
            <a href="/discover" style="color:{{ p.muted }}; text-decoration:none;">discover</a>
            &nbsp;&middot;&nbsp;
            <a href="/admin" style="color:{{ p.muted }}; text-decoration:none;">admin</a>
            &nbsp;&middot;&nbsp;
            <a href="/emails" style="color:{{ p.muted }}; text-decoration:none;">emails</a>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
{% endif %}
```

- [ ] **Step 4: Run** the full email-render file: `.venv/bin/python -m pytest tests/test_email_render.py -q` — all PASS (existing tests prove sent-email output unchanged).

- [ ] **Step 5: Commit** `feat: web_nav flag renders a nav strip on web-rendered emails`.

### Task 2: `/` = latest issue; delete web-side home plumbing

**Files:**
- Modify: `src/newslet/handlers/web.py`
- Delete: `src/newslet/templates/read.html.j2`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `render_email(..., web_nav=True)` from Task 1; existing `db.list_issues`, `db.get_issue`, `_base_url`, `_require_admin`.
- Produces: `GET /` → email HTML of newest issue; 200 empty-state page when no issues. Gone (404/405 afterward): `POST /api/vote`, `GET /api/search`, `POST /api/home/refresh`, `GET /api/home/status`.

- [ ] **Step 1: Rewrite tests.** In `tests/test_web.py`, replace the "Rich homepage" test block (`test_homepage_*`, ~:570-800) and delete the `/api/vote` (~:826-880), `/api/search` (~:892-905), and `/api/home/*` (~:1040-1090) tests. `_seed_issue` stays but seed under a real date. New tests:

```python
def test_homepage_renders_latest_issue_email(client):
    _seed_issue("2026-08-01")
    _seed_issue("2026-08-03")
    r = client.get("/")
    assert r.status_code == 200
    assert "2026-08-03" in r.text          # newest issue wins
    assert 'href="/admin"' in r.text       # web nav strip present
    assert "/rate?" in r.text              # voting via signed rate links


def test_homepage_empty_state(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "No editions yet" in r.text


def test_homepage_requires_admin(client):
    r = client.get("/", cookies={})  # match existing unauth pattern in this file
    assert r.status_code in (303, 307)


def test_home_endpoints_removed(client):
    assert client.post("/api/vote", data={"url": "https://x.com/a", "rating": "up", "date": "d"}).status_code in (404, 405)
    assert client.get("/api/search", params={"q": "x"}).status_code == 404
    assert client.post("/api/home/refresh").status_code in (404, 405)
    assert client.get("/api/home/status").status_code == 404
```

(Adapt auth-test style to the file's existing `test_homepage_requires_admin`; keep `/emails/{date}` tests untouched — they must still pass, proving the archive stays nav-free. If other deleted tests referenced `_seed_issue("home")`, remove those references.)

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_web.py -q` — new tests FAIL, old deleted ones gone.

- [ ] **Step 3: Implement in `web.py`:**
  - Delete: `_HOME_KEY` (:91), `_FAST_SEARCH_MODEL/_ROUNDS/_RESULTS` (:97-99), `_interactive_search` (:102-114), old `home()` (:273-338), `_home_cards` (:235-270), `_vote_lookup` (:801-812), `_article_card` (:815-828), `vote` (:870-909), `home_refresh` (:912-934), `home_status` (:937-947), `api_search` (:950-976). Drop the now-unused `websearch` import and `Query` import if unused (`Query` is still used by `/rate` etc. — check before removing).
  - Add the empty-state constant near the other `_*_HTML_TEMPLATE` constants:

```python
_NO_ISSUES_HTML = (
    '<!doctype html><html><head><meta charset="utf-8"><title>daily scoop</title>'
    "<style>body{font:14px system-ui;text-align:center;margin-top:5rem}"
    "a{margin:0 8px}</style></head>"
    "<body><h1>daily scoop</h1><p>No editions yet — the first daily email builds one.</p>"
    '<p><a href="/discover">discover</a><a href="/admin">admin</a>'
    '<a href="/emails">emails</a></p></body></html>'
)
```

  - New route (same spot in the file as the old one):

```python
@app.get("/", response_class=HTMLResponse)
def home(request: Request, admin_token: str | None = Cookie(default=None)) -> HTMLResponse:
    """The homepage: the latest daily email, rendered fresh with a web nav.

    Always the newest stored issue (before today's send that is yesterday's,
    clearly dated in the header) — no rebuild, no LLM calls, no staleness.
    """
    _require_admin(admin_token)
    rows = db.list_issues(limit=1)
    issue = db.get_issue(rows[0]["date"]) if rows else None
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
```

  - `git rm src/newslet/templates/read.html.j2`. Update the module docstring's description of the homepage.

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_web.py -q` — PASS. Then `.venv/bin/ruff check src tests scripts`.

- [ ] **Step 5: Commit** `feat: homepage renders the latest email; drop card UI, vote/search/home APIs`.

### Task 3: Delete the digest "home" mode + cron

**Files:**
- Modify: `src/newslet/handlers/digest.py`, `infra/template.yaml`
- Test: `tests/test_digest.py`

**Interfaces:**
- Produces: `digest.handler` supports only daily / `{"manual"}` / `{"discover"}` events. No `HOME_KEY` export (web.py no longer mirrors it after Task 2).

- [ ] **Step 1:** Delete `test_handler_routes_home` (tests/test_digest.py:465-485). Add:

```python
def test_handler_ignores_stale_home_event(aws, monkeypatch):
    # A leftover {"home": true} invoke (e.g. an in-flight async event during
    # deploy) must fall through to the idempotent daily path, not crash.
    monkeypatch.setattr(digest.db, "issue_sent", lambda _d: True)
    result = digest.handler({"home": True}, None)
    assert result["status"] == "already_sent"
```

(Match the `aws` fixture/env setup used by the deleted test.)

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_digest.py -q` — new test FAILS (routes to `_run_home`).

- [ ] **Step 3: Implement:** in `digest.py` delete `_HOME_RANK_PICKS`, `_HOME_MIN_PICKS`, `_HOME_WEB_ARTICLES` (:58-60), `HOME_KEY` (:82), `_run_home` (:424-465), and the `event.get("home")` branch in `handler` (:539-540); update the module/handler docstrings (drop "home" mode mentions). In `infra/template.yaml` delete the `HomeRefreshSchedule` event block (the `cron(45 9 * * ? *)` entry) and its comment.

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_digest.py -q` — PASS. `sam validate --lint` if sam is installed; otherwise rely on CI.

- [ ] **Step 5: Commit** `feat: remove the home rebuild mode and its cron`.

### Task 4: Rewrite the web preview script

**Files:**
- Modify: `scripts/preview_read.py`

**Interfaces:**
- Consumes: the new `/` route. Keeps CLI contract: `preview_read.py [theme]` → writes `out/read.html`.

- [ ] **Step 1:** In `_make_issue`, build a realistic *daily* issue (keep the generators, but sized like an email: 10 picks, 5 web, 4 off-beat) and in `main()` replace the two `"home"`-keyed seeds with a dated issue stored via `db.put_issue(_make_issue("2026-08-05"))` (not manual — `list_issues` must see it). Drop the two `put_feedback` seed calls (sticky vote state no longer exists). Update the module docstring: it now previews "the homepage (the latest email rendered with the web nav)".

- [ ] **Step 2: Run** `.venv/bin/python scripts/preview_read.py && open out/read.html` — page shows the email layout with the nav strip. Also `.venv/bin/python scripts/dry_run.py` — email unchanged, no nav strip in `out/email.html`.

- [ ] **Step 3: Run** both gates (ruff + full pytest). **Commit** `chore: preview_read renders the new email-based homepage`.

### Task 5: Documentation sweep

**Files:**
- Modify: `AGENTS.md`, `DESIGN.md`, `README.md`, `src/newslet/docs/product.md`

- [ ] **Step 1:** `grep -n "home\|read.html\|api/search\|api/vote" AGENTS.md DESIGN.md README.md src/newslet/docs/product.md` and update every hit that describes deleted behavior: AGENTS.md architecture-map rows (`read.html.j2`, `handlers/digest.py` modes, `handlers/web.py` route list) and the preview-script description; DESIGN.md's module contracts (`email_render` signature gains `web_nav`, `handlers.digest` modes, routes list — remove `/api/vote`, `/api/search`, `/api/home/*`; rewrite the `/` description); README.md operational mentions of the home refresh; product.md's homepage section (now: "the web homepage is today's email, always fresh, voting via the same +/− links"). Also note in DESIGN.md that stray `{"home"}` events fall through to the daily path.

- [ ] **Step 2:** Gates again (docs don't break them, but cheap). **Commit** `docs: homepage is the latest email; home pipeline removed`.

### Task 6: Ship PR 1

- [ ] **Step 1:** Fresh branch off main (`git checkout -b homepage-email`), all commits present, `.venv/bin/ruff check src tests scripts && .venv/bin/python -m pytest -q` green.
- [ ] **Step 2:** `gh auth switch -u jfrederick`, push, `gh pr create` titled "Homepage: render the latest email; remove the home pipeline" with a body summarizing spec §1 and noting the LLM-cost reduction.
- [ ] **Step 3:** Wait for pullfrog's review (arrives within a few minutes); address inline threads with fixes + replies. Don't wait on OpenCodeReview (its secrets aren't configured).
- [ ] **Step 4:** `gh pr merge --squash --delete-branch`; watch the CI deploy on main (`gh run watch`) until the SAM deploy succeeds.
