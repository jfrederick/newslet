# Dashboard pivot: homepage = today's email, plus facts, quote, weather, deep-dives

**Date:** 2026-08-04
**Status:** Approved by Jim (design review in session); pending spec review.

## Goal

Shift newslet from "newsletter with a web mirror" toward a live personalized
web dashboard. The daily email stays the product's heart; the web `/` page
becomes a faithful, always-fresh view of it, and new personalized content
blocks ride the existing Issue pipeline into both surfaces.

Five features, one design:

1. Replace the homepage with today's email (delete the heavy home pipeline).
2. Two ~500-word tech facts per issue, with a methodology and a feedback
   loop that is **separate** from the general profile.
3. Philosophical quote of the day (Stoic, Nietzsche, Einstein, Buddhist,
   Taoist, and kin), votable, with its own feedback loop.
4. Concise Brooklyn weather line (no LLM).
5. Deep-dive request box: ask a topic today, get a ~500-word explainer in
   tomorrow's issue.

## 1. Homepage replacement

**Now:** `/` renders a card UI from a stored `date="home"` Issue rebuilt only
by a 9:45 UTC cron (`{"home": true}`). The rebuild is the most expensive
build in the system (25–40 picks ranked from RSS + 120 HN candidates +
newsletters + X in one large Opus call, plus web-search and serendipity
calls) and the page goes stale whenever it fails.

**New:** `/` fetches the newest issue (`db.list_issues(limit=1)`) and renders
it with the existing `email_render.render_email`, wrapped in a thin page
shell: nav bar (Discover / Admin / Emails), dateline, and — once PR 5 lands
— the deep-dive request box (§5). No LLM calls, no staleness logic — the page always shows
the latest issue; before today's 10:00 UTC send that is yesterday's, clearly
dated. Rate links inside the rendered email work as-is (same caveat as
`/emails/{date}`: links are re-signed with the current key).

**Deleted outright:**

- `HomeRefreshSchedule` cron and the `{"home"}` handler mode
  (`digest._run_home`, `HOME_KEY`).
- `/api/home/refresh`, `/api/home/status`.
- The subject-search box and `/api/search` (+ `web._interactive_search`).
- `templates/read.html.j2` card UI (replaced by the thin wrapper template;
  `scripts/preview_read.py` now previews the wrapper).
- `web._home_cards`, `_vote_lookup`, and `/api/vote`: with `/` rendering the
  email HTML, all voting (web and email) goes through the signed `/rate`
  links, so the cookie-authed JSON vote path has no remaining caller.

Net effect on spend: removes ~4–5 model calls/day, which more than covers
the new blocks below.

## 2. Tech facts (two ~500-word blocks)

**Module:** `facts.py` →
`fetch_facts(facts_profile_md, recent_topics, *, client=None, model=None) -> list[Fact]`.
One call to the main Claude model per build returns **both** facts as a JSON
object. Best-effort: any error → `[]` → blocks absent.

**Model:** `Fact(title, body_md, genre, slot)` in `contracts.py`;
`Issue.facts: list[Fact]` with `default_factory=list` (lenient on old rows).

**Methodology:**

- Fixed taxonomy of 8 genres: computing history & lore; how-it-works
  internals; people & personalities; hardware & physics of computing;
  networks & protocols; algorithms & math; security & cryptography;
  software culture & economics.
- Each issue picks two *different* genres. Genre weighting comes from the
  facts profile (uniform when empty, drifts with votes).
- No-repeat: a rolling log of the last 60 covered topics is stored on the
  `id="facts"` profile row (`recent_topics` JSON list) and passed to the
  prompt as exclusions; the two new topics are appended after a successful
  build.
- Content rules in the prompt: ~500 words each, timeless (no news pegs),
  concrete and verifiable, one-line title, distinct genres, JSON reply.

**Placement:** mid fact after the picks block, before "From around the web";
end fact at the very bottom, after discoveries and before the footer. Both
in email and (automatically) on `/`.

**Voting & the separate profile:**

- Each fact gets the standard +/− UI with synthetic signed URLs:
  `{base}/facts/{issue_date}/{mid|end}` via the existing
  `tokens.sign(url, issue_date)` scheme. Votes land in the normal feedback
  table.
- **Separation rule:** feedback rows whose URL matches the facts (or quote,
  §3) synthetic-path prefixes are *excluded* from the general ranking window
  (`_RANK_FEEDBACK_LIMIT`) and the general `tune.tune_profile` window, and
  are *routed* to their own post-send tune step.
- New profile row `id="facts"` in the existing profile table: markdown blob
  ("what kind of facts Jim likes") + `recent_topics`. Tuned after a
  confirmed send by a facts-only tune call (same shape as `tune.py`, fed
  only fact votes + fact titles). Never touches `id="me"`.

## 3. Quote of the day (philosophy)

**Module:** `quotes.py` →
`fetch_quote(quotes_profile_md, recent_quotes, *, client=None, model=None) -> Quote | None`.
One Haiku call. Best-effort → `None` → block absent.

**Model:** `Quote(text, author, source, tradition)`;
`Issue.quote: Quote | None = None`.

**Methodology:** philosophy, not STEM trivia — Stoics (Marcus Aurelius,
Seneca, Epictetus), Nietzsche, Einstein's reflective/philosophical remarks,
Buddhist and Taoist texts, and similar traditions. Rotate traditions;
require real, attributable quotes (author + source); no-repeat log
(`recent_quotes`, cap 120) on the `id="quotes"` profile row. Vote weighting
drifts the tradition mix like §2.

**Placement:** an epigraph directly under the intro. Votable via
`{base}/quote/{issue_date}`; votes feed a quotes-only tune step updating
`id="quotes"`. Same separation rule as §2.

## 4. Brooklyn weather line

**Module:** `weather.py` →
`fetch_weather(*, lat, lon, fetch=None) -> str | None` using the free,
keyless National Weather Service API (api.weather.gov), default coordinates
Brooklyn, NY (config-overridable). Formatting is pure template code — no
LLM: e.g. `78° drizzle before noon, clearing tonight` built from
`shortForecast` + temps. One line under the dateline in email and web.
Stamped on the Issue at build time (`Issue.weather_line: str | None`) so
archive views show that day's weather. Best-effort → `None` → line absent.
Not votable.

## 5. Deep-dive request box

**Flow:** one-field form on `/` → `POST /api/deepdive` (admin-cookie authed)
→ row in a new small DynamoDB `requests` table (PK `id` = timestamp-sortable
string; attrs `topic`, `status` pending|served, `created_at`,
`served_date`). The form shows queue state ("1 queued — arrives in
tomorrow's edition").

**Build:** the digest build pops the oldest pending request, generates a
~500-word explainer with the main model (`deepdive.py` →
`fetch_deepdive(topic, *, client=None, model=None) -> DeepDive | None`),
attaches it as `Issue.deepdive`, and marks the request served **only after
the issue row is stored**. Placement: a "You asked" block directly after the
intro (above picks). Best-effort: generation failure leaves the request
pending for the next build. One request consumed per issue.

## Cross-cutting

**Config:** new admin knobs — `facts_enabled` (default on),
`quote_enabled` (on), `weather_enabled` (on) + lat/lon, `deepdive_enabled`
(on). Lenient read / strict write like existing config fields.

**Pipeline:** all four generators are injectable `*_fn` parameters on
`run_digest`, called inside `try/except → absent`, results stored on the
Issue. `dry_run.py` gains fakes for each so `out/email.html` shows every
block offline.

**Error handling invariants (house rules):** injectable network edges;
best-effort everywhere (nothing new may block the send); lenient reads on
old Issue rows; signed links for anything email-clickable.

**Testing:** unit tests per new module with fake clients (JSON contract,
error paths, no-repeat exclusions); digest tests for wiring + the
feedback-separation rule; render tests for block order and placement; web
tests for the new `/`, `/api/deepdive`, and deleted-route 404s; db tests for
the requests table and new profile rows.

**Docs to update in the same PRs:** DESIGN.md module contracts + routes +
table attrs; AGENTS.md architecture map + invariants list; README deploy
notes (new table); `docs/product.md`.

## Implementation order (one PR each)

1. Homepage replacement + deletions (unblocks everything, pure removal + a
   small wrapper template).
2. Tech facts (module, Issue field, render, votes, separate tune).
3. Quote of the day (mirrors PR 2, smaller).
4. Weather line.
5. Deep-dive request box (new table + form + build step).

## Backlog (not in scope, kept from brainstorm)

On this day in tech history; word-of-the-day etymology; puzzle corner with
next-day answers; xkcd embed; release radar via GitHub release RSS;
profile-matched GitHub trending; podcast/talk pick; NYC tech events weekly
block; reading queue (save-for-later); taste transparency panel (learned-
preferences diff + revert); spaced-repetition review of upvoted facts;
weekly retrospective edition.
