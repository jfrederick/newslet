# PR 5: Deep-Dive Request Box Implementation Plan

**Goal:** Type a topic on `/` today; tomorrow's email opens with a ~500-word "You asked" explainer on it.

## Design decisions
- **Table**: new DynamoDB `newslet-requests` (PK `id` S = `{created_at iso}#{rand8}` so ids sort by age). Attrs: `topic`, `status` (pending|served), `created_at`, `served_date`. Infra: table + DynamoDBCrudPolicy on Digest and Web functions + `TABLE_REQUESTS` env var on both.
- **db.py**: `add_deepdive_request(topic) -> str`, `oldest_pending_deepdive() -> dict | None` (paginated scan, filter status=pending, min by id), `mark_deepdive_served(request_id, issue_date) -> None`, `count_pending_deepdives() -> int`. Lenient reads.
- **deepdive.py**: `fetch_deepdive(topic, *, client=None, model=None) -> DeepDive | None` — main model, ~500-word explainer, JSON `{"deepdive": {"title", "body_md"}}`, strict=False parse, stop_reason logging, best-effort None. NOT votable (on-demand content; user asked for it).
- **contracts**: `DeepDive(topic, title, body_md)`; `Issue.deepdive: DeepDive | None = None` (lenient); `Config.deepdive_enabled: bool = True`.
- **Pipeline**: `run_digest(..., deepdive_topic: str = "", deepdive_fn=None, ...)` — the *caller* pops the topic (db access stays out of the pure pipeline): `_fresh_issue` reads `oldest_pending_deepdive()` when enabled and passes its topic; run_digest attaches `issue.deepdive` best-effort. Post-send `_mark_deepdive_served(issue)` (matches the request by stored `pending["id"]` — carry the id via a module-level? No: `_fresh_issue` returns only issue; instead `_mark_deepdive_served` re-reads `oldest_pending_deepdive()` and marks it served iff its topic == `issue.deepdive.topic` — idempotent-ish and avoids plumbing). Failed generation or failed send → stays pending, retried next build. Marking is post-send like the logs/tuners.
- **Render**: "You asked" block after the quote epigraph, before picks: header "You asked", topic as muted subline, title + paragraphs like a fact block, no vote cell.
- **The form on `/`**: email.html.j2 gains a `{% if web_nav %}` footer section (below the main table): one text input posting to `POST /api/deepdive` (admin cookie, 303 → `/`), with a status line ("N queued — the next one arrives in tomorrow's edition") fed via new `render_email(deepdive_pending: int | None = None)` kwarg; `home()` passes `db.count_pending_deepdives()` when `config.deepdive_enabled`. Never rendered in sent emails (web_nav gate).
- **web.py**: `POST /api/deepdive` — `topic` Form, admin cookie, strip + 400 on empty/overlong (>200 chars), 303 `/`.
- Admin checkbox `deepdive_enabled`; dry-run fixture; integration stub for deepdive module.

## Tasks
1. contracts + db + infra table (tests: request lifecycle roundtrip, oldest-pending ordering, count; issue deepdive roundtrip + legacy).
2. deepdive.py + tests (happy/error/malformed/prose-wrapped).
3. digest wiring + post-send marking + fakes (tests: attach, boom, disabled, no-pending; marking only post-send; topic mismatch not marked).
4. render + form + /api/deepdive + admin + tests.
5. Docs (DESIGN/AGENTS/README/product incl. new table row + routes) + ship. NOTE: new table = SAM deploy creates it; no migration.
