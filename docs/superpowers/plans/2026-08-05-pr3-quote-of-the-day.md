# PR 3: Quote of the Day Implementation Plan

Amendments over the draft, baking in PR 2's review learnings:
- Anchored full-shape vote regex lives in quotes.py (QUOTE_VOTE_RE / is_quote_vote), shared by digest + web — no path-prefix matching.
- _split_feedback becomes a 3-tuple (general, facts, quotes); _recent_feedback_split trims all three buckets.
- The recent-quotes log advances only post-send (_advance_quotes_log, deduped); the tuner re-reads state before writing.
- fetch_quote parses with json.loads(strict=False) and logs stop_reason on parse failures.
- /rate thanks page shows "Author — text…" as a label (no dead link).

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans.

**Goal:** One philosophical quote per issue (Stoics, Nietzsche, Einstein, Buddhist/Taoist texts, and kin), rendered as an epigraph under the intro, votable, with its own `id="quotes"` profile — mirroring the facts feature exactly.

**Architecture:** Mirrors PR 2's shapes 1:1 — `quotes.py` (Haiku call → JSON, best-effort None), `Issue.quote: Quote | None`, `QuotesState` on the profile table, `/quote/{date}` synthetic vote URL (the `_split_feedback` prefix is already reserved and excluded), a quotes-only tune step, `Config.quote_enabled` checkbox.

## Global Constraints
Same gates/invariants/GH workflow as PR 2. Branch from main after PR 2 merges.

### Task 1: Contracts + storage
- `Quote(text, author, source="", tradition="")`; `Issue.quote: Quote | None = None` (lenient old rows); `QuotesState(markdown="", recent_quotes=[])` (cap 120, "Author — text-prefix" entries).
- db: `quote_json` on issues (single object or absent); `get_quotes_state`/`put_quotes_state` on `id="quotes"` row (`recent_quotes_json`).
- Tests mirror the facts ones (roundtrip, legacy row, bad JSON).

### Task 2: quotes.py
- `fetch_quote(quotes_profile_md, recent_quotes, *, client=None, model=None) -> Quote | None`.
- Model: `claude-haiku-4-5-20251001` default (cheap; real attributable quotes are Haiku-safe) — constant `_QUOTE_MODEL`, overridable.
- Prompt: philosophy, not STEM trivia. Traditions to rotate: Stoic (Marcus Aurelius, Seneca, Epictetus), Nietzsche, Einstein's reflective remarks, Buddhist texts, Taoist texts (Laozi, Zhuangzi), plus kin (Montaigne, Kierkegaard, Camus, Thoreau, Rumi, Confucius). REAL quotes only with author + source; if unsure of authenticity, pick a different one; no invented attributions. Weight tradition choice by the quotes-taste profile; avoid every entry in the exclusion list. Reply JSON: `{"quote": {"text","author","source","tradition"}}`.
- `tune_quotes_profile(current_md, feedback, *, client=None) -> str` — same skeleton as facts tuner (~8 bullets on traditions/themes liked).
- Tests mirror test_facts.py (happy, prose-wrapped, API error → None, malformed → None, profile+exclusions reach prompt, tuner noop/error/rewrite).

### Task 3: Render
- email_render: `quote_ctx = {text, author, source, tradition, up_link, down_link}` signing `{base}/quote/{issue.date}`; template block directly after the intro row, before picks: italic quote text, "— Author, Source" attribution line, same +/− cell. Header-less (it's an epigraph, not a section).
- Tests: position (intro < quote text < first pick title), signed link, absent when `issue.quote is None`.

### Task 4: Pipeline + separation + tune + config
- digest: `_split_feedback` → returns 3-tuple `(general, fact_rows, quote_rows)` (update facts call sites + tests); `run_digest` gains `quote_fn/quotes_profile_md/recent_quotes/quote_enabled`; `_fresh_issue` reads `db.get_quotes_state()` and appends `f"{q.author} — {q.text[:60]}"` to the log (cap 120) when an issue carries a quote; `_tune_quotes_after_send` alongside the other two tuners in both send paths.
- `Config.quote_enabled: bool = True`; `/api/config` checkbox param; admin checkbox next to the facts one.
- `/rate` title lookup: `/quote/{date}` path → title `f"Quote: {issue.quote.author}"`.
- dry-run `_fake_quote` + fixture quote in scripts/dry_run.py.
- Tests mirror the facts digest/web tests.

### Task 5: Docs + ship
- DESIGN.md `newslet.quotes` section + email_render bullet + digest paragraph + table attrs + `/api/config` line; AGENTS.md map row + best-effort list + admin list; README module line; product.md "Quote of the day" section after Tech facts.
- Gates → branch `quote-of-the-day` → PR → pullfrog/codex → squash-merge → deploy watch.
