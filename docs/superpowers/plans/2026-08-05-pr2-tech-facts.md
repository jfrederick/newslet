# PR 2: Tech Facts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two ~500-word tech-fact essays per issue (mid + end), generated from an 8-genre methodology with a no-repeat topic log, votable, feeding a facts-only profile (`id="facts"`) that never touches the general profile.

**Architecture:** New `facts.py` module (one Claude call returns both facts as JSON; best-effort). Facts ride `Issue.facts`; `email_render` signs synthetic `/facts/{date}/{slot}` rate links; `digest` splits feedback by URL path so fact votes go to a facts-only tune step writing the `id="facts"` profile-table row (which also carries the rolling topic log).

**Tech Stack:** Python 3.12, Anthropic SDK (no web_search tool — model knowledge only), pydantic, DynamoDB. Spec: `docs/superpowers/specs/2026-08-04-dashboard-pivot-design.md` §2. Branch from main after PR 1 merges.

## Global Constraints

- Gates: `.venv/bin/ruff check src tests scripts` AND `.venv/bin/python -m pytest -q` (exit codes checked directly, never through a pipe).
- House invariants: injectable `client`, try/except → absent, lenient reads (`default_factory`), signed links via `tokens.sign(url, issue_date)`.
- Fact vote URLs must be real HTTPS URLs (FeedbackRow.article_url is HttpUrl): `{base}/facts/{date}/{mid|end}`.
- Fact/quote feedback is identified by URL path prefix (`/facts/`, `/quote/`) — quote handling ships in PR 3 but the splitter recognizes both now.
- Update AGENTS.md / DESIGN.md / README.md / product.md in the same PR.

---

### Task 1: Contracts + db state row

**Files:**
- Modify: `src/newslet/contracts.py` (add `Fact`, `FactsState`; `Issue.facts`)
- Modify: `src/newslet/db.py` (facts state on the profile table)
- Test: `tests/test_db.py`, `tests/test_contracts.py` if present (else fold into test_db)

**Interfaces (produces):**

```python
class Fact(BaseModel):
    title: str
    body_md: str                      # ~500 words, plain markdown paragraphs
    genre: str = ""                   # one of facts.GENRES (lenient)
    slot: Literal["mid", "end"] = "mid"

class Issue(BaseModel):
    ...
    facts: list[Fact] = Field(default_factory=list)   # lenient on old rows

class FactsState(BaseModel):
    markdown: str = ""                # the facts taste profile (auto-managed)
    recent_topics: list[str] = Field(default_factory=list)  # newest last, cap 60

# db.py
def get_facts_state() -> FactsState: ...      # id="facts" row; lenient
def put_facts_state(state: FactsState) -> None: ...
```

- [ ] Steps: failing tests first — `FactsState` roundtrip via `db.put_facts_state`/`get_facts_state` (moto), missing row → empty state, bad `recent_topics_json` → empty list; `db.get_issue` on a row without `facts_json` loads with `facts == []`, and `put_issue` persists `facts_json`. Then implement: row `{"id": "facts", "markdown": ..., "recent_topics_json": json.dumps([...]), "updated_at": iso}`; extend `put_issue`/`get_issue` with `facts_json` mirroring `random_articles_json` (lenient per-item validation). Run, commit `feat: Fact/FactsState contracts + facts storage`.

### Task 2: facts.py

**Files:**
- Create: `src/newslet/facts.py`
- Test: `tests/test_facts.py`

**Interfaces (produces):**

```python
GENRES = (
    "computing history & lore", "how-it-works internals",
    "people & personalities", "hardware & physics of computing",
    "networks & protocols", "algorithms & math",
    "security & cryptography", "software culture & economics",
)

def fetch_facts(
    facts_profile_md: str,
    recent_topics: list[str],
    *,
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
) -> list[Fact]: ...   # exactly 2 on success (slots mid/end), [] on any failure
```

Mirror `serendipity.py`'s shape (no tools; `settings().claude_model`; `max_tokens=4096`; `search_common.extract_json_object` + `last_text_block` still work on a plain reply). System prompt requirements (write it out fully in the module):
- Choose TWO DIFFERENT genres from the list, guided by the reader's facts-taste profile ("(none yet — pick any two)" when empty), varying genres day to day rather than repeating favorites exclusively.
- Each fact: a one-line title; a ~500-word essay (450–550), concrete, technically accurate, timeless (no news, no "recently"), plain paragraphs separated by blank lines, no markdown headings/lists.
- Avoid every topic in the exclusion list (the `recent_topics` passed in the user block).
- Reply ONLY with JSON: `{"facts": [{"title", "body_md", "genre", "slot": "mid"|"end"}]}` — exactly two, one per slot.

Parsing: validate items leniently (drop malformed), require exactly 2 valid facts with distinct slots else return [] (a half-result would leave an empty bottom slot). Unknown genre string is kept as-is (lenient).

- [ ] Steps: failing tests (happy path via fake client returning canned JSON; API error → []; malformed JSON → []; one-fact reply → []; prose-wrapped JSON still parses; recent topics + profile appear in the request the fake captured), implement, run, commit `feat: facts.py — two timeless tech-fact essays per issue`.

### Task 3: Render the two fact blocks

**Files:**
- Modify: `src/newslet/email_render.py`, `src/newslet/templates/email.html.j2`
- Test: `tests/test_email_render.py`

**Interfaces:** context gains `fact_mid` / `fact_end` (dict or None): `{title, genre, paragraphs: list[str], up_link, down_link}` where `paragraphs = [p.strip() for p in body_md.split("\n\n") if p.strip()]` and links sign the synthetic URL `f"{base}/facts/{issue.date}/{fact.slot}"` via the existing `_rate_links` helper.

Template: mid block between the picks loop and `{% if web_articles %}`; end block after `{% endif %}` of discoveries, before the CTA row. Both use the section-header style of the existing blocks; header copy: mid = "Tech fact of the day", end = "One more fact"; genre as the muted subline; each paragraph its own `<div style="font-size:{{ fs(15) }}; line-height:1.55; color:{{ p.fg }}; margin:0 0 12px 0;">`; the +/− links in the same right-hand cell pattern as picks.

- [ ] Steps: failing tests — order assertions (`html.index(mid_title) < html.index(web_title)`; `html.index(discovery_title) < html.index(end_title) < html.index("Open daily scoop")`), signed link `f"{BASE_URL}/rate?a=" + quote(f"{BASE_URL}/facts/{DATE}/mid", safe="")` present, no fact blocks when `issue.facts` empty. Implement, run, commit `feat: render mid/end tech-fact blocks with signed votes`.

### Task 4: Pipeline wiring + feedback separation + facts tune

**Files:**
- Modify: `src/newslet/handlers/digest.py`, `src/newslet/contracts.py` (Config), `src/newslet/handlers/web.py` (/api/config + /rate title lookup), `src/newslet/templates/admin.html.j2`, `src/newslet/facts.py` (tune fn)
- Test: `tests/test_digest.py`, `tests/test_web.py`

**Interfaces (produces):**

```python
# digest.py
_EXTRA_VOTE_PATHS = ("/facts/", "/quote/")   # quote lands in PR 3
def _split_feedback(rows) -> tuple[list, list]:  # (general, facts) — path-prefix match on article_url
def _tune_facts_after_send() -> None          # best-effort; facts votes only

# run_digest gains:
facts_fn=None,                    # resolved to facts.fetch_facts at call time
facts_profile_md: str = "",
facts_recent_topics: list[str] | None = None,
facts_enabled: bool = True,
# → issue.facts populated inside try/except when enabled

# facts.py
def tune_facts_profile(current_md, feedback, *, client=None) -> str  # cumulative bullets, unchanged on empty/error

# contracts.Config
facts_enabled: bool = True
```

Details:
- `_split_feedback` parses `urlparse(str(row.article_url)).path`; a path starting with any `_EXTRA_VOTE_PATHS` prefix is extra-feedback (facts for `/facts/`; `/quote/` rows are *dropped from general* now and consumed in PR 3).
- `_fresh_issue`: reads `db.get_facts_state()` when `config.facts_enabled`; passes general-only feedback to `run_digest`; after `run_digest` returns an issue with facts, appends their titles to `recent_topics` (cap 60, newest last) via `put_facts_state` (markdown unchanged).
- `_tune_profile_after_send`: general rows only. New `_tune_facts_after_send`: facts rows from the same `_TUNE_FEEDBACK_LIMIT` window → `facts.tune_facts_profile(state.markdown, rows)` → `put_facts_state` if changed. Called wherever `_tune_profile_after_send` is (daily + manual).
- `tune_facts_profile`: same skeleton as `tune.tune_profile` but the whole markdown is auto-managed (no sentinels, no human part); system prompt maintains ~10 bullets on which genres/topics/styles the reader up/downvotes.
- `/rate` title lookup in web.py: after the picks loop, if still untitled and the URL path matches `/facts/{issue_date}/{slot}`, look up `issue.facts` by slot and use its title.
- `/api/config`: `facts_enabled: bool = Form(default=False)` (checkbox semantics, mirroring `x_enabled`); admin.html.j2 gets the checkbox next to the X toggle with the label "Tech facts (two ~500-word essays per email)".
- `dry_run` path: `_fake_facts` returning two canned facts, wired like `_fake_serendipity`.

- [ ] Steps: failing tests — `_split_feedback` (general/facts/quote-dropped), run_digest populates facts + survives a raising `facts_fn`, `facts_enabled=False` skips the call, `_fresh_issue` appends topics capped at 60, `_tune_facts_after_send` writes only the facts row (general profile untouched — assert `db.get_profile()` unchanged), `/rate` on a fact URL records feedback with the fact's title, config roundtrip. Implement, run both gates, commit `feat: facts ride the digest with their own feedback loop`.

### Task 5: Docs + previews + ship

- [ ] `scripts/dry_run.py` renders both fact blocks (via `_fake_facts`); eyeball `out/email.html` and `out/read.html`.
- [ ] Docs: DESIGN.md (`newslet.facts` module section with both signatures + methodology; `Issue.facts` in the table attrs; `email_render` bullet; digest paragraph: feedback split + facts tune; `/api/config` line), AGENTS.md (map row for `facts.py`; best-effort list; admin-config list; "the homepage is the email" bullet unchanged), README module list, product.md ("Tech facts" section: what it is, the 8 genres, votes teach a separate facts profile).
- [ ] Gates green → branch `tech-facts` → push → `gh auth switch -u jfrederick` → PR titled "Tech facts: two 500-word essays per issue with their own feedback loop" → address pullfrog → squash-merge → watch deploy.

## Plan self-review notes

- Spec §2 coverage: 2×500w ✓ (prompt), 8 genres ✓, weighting via facts profile ✓ (prompt uses it), 60-topic no-repeat ✓, mid/end placement ✓, separate profile row ✓, votes excluded from general rank/tune ✓, synthetic signed URLs ✓, timeless/no-news rule ✓.
- Types consistent: `Fact.slot` Literal matches template context keys `fact_mid`/`fact_end`; `_split_feedback` returns 2-tuple (quote rows dropped until PR 3 — documented).
