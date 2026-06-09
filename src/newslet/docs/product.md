# daily scoop — Product Guide

daily scoop is a personal daily news editor. Every morning it reads your feeds,
picks the handful of stories worth your time, writes a short note on why they
matter, and emails them to you. You react with a tap — a thumbs up or thumbs
down on each story — and the next morning's pick gets a little more like you.

It is built for one reader: you. There is no audience to chase, no engagement
to optimize, no other inbox to please. The whole product is a quiet loop:
read, rank, send, learn, repeat.

This guide explains what daily scoop does, feature by feature, and how each piece
works. You choose how much of the machinery you want to see.

---

## How to read this guide

The guide has three depth settings. Pick the one that fits how curious you are
right now; you can switch any time.

- **Plain** — what the feature does and why, in everyday words. No technology.
- **Some detail** — adds a light layer: the kinds of tools involved and the
  shape of what happens, without the wiring.
- **More detail** — adds the concrete parts: the services, the data, the
  schedules, and the names you would search for in the code.

In the live web version, a control at the top switches between the three. In
the plain-text source, the deeper layers are marked with `:::tier little` and
`:::tier medium` fences so you can skim or skip them.

Everything below is drawn from the code that actually runs. Where a number
appears (how many stories, how long something is kept), it is the real value,
and the admin console tells you which ones you can change yourself.

---

## The daily email

Once a day, daily scoop sends you one email: a short, ranked list of stories it
thinks you'll want, each with a one-line note on why it made the cut, plus a
couple of sources you don't follow yet that might be worth adding.

The email is the point of the whole product. It is meant to be read in a few
minutes over coffee and then closed. Every story carries a thumbs-up and a
thumbs-down button you can tap right there in your inbox — no app, no login.
Those taps are the only steering wheel daily scoop has, and they are enough.

At the bottom is a single link to your daily scoop homepage, a richer place to
browse when you want more than the morning's short list.

:::tier little

The email goes out once every morning on a fixed schedule. A background job
wakes up, does all the fetching and ranking, hands the finished email to an
email-delivery service, and goes back to sleep. You are the only recipient.

How long the email is — how many stories it carries — is something you set
yourself in the admin console, so a heavy news day and a quiet one can be
shaped to taste.

:::

:::tier medium

The send is a scheduled AWS Lambda (the "digest" function) triggered by an
EventBridge cron at **10:00 UTC** daily. It runs a fixed pipeline —
fetch → rank → summarize → discover → web-search → render → send — and delivers
the finished HTML through **Resend**. The reader address is a single
configured value (`TO_EMAIL`); daily scoop is single-tenant by design.

The send is idempotent on a `sent_at` marker rather than mere existence of the
day's record, so if delivery half-fails (the issue is stored but the email
provider is down), the next scheduled retry recovers cleanly instead of
skipping the day or sending an empty issue. The body length follows your
`max_rss_articles` and `max_web_articles` settings. Every enrichment step
(summary, discoveries, the web block) is best-effort: any one of them can fail
and the email still goes out, just without that piece.

:::

---

## How stories get picked

daily scoop doesn't show you everything new. It shows you a ranked short list. The
ranking is the heart of the product: given everything that arrived in the last
day, it scores each story against who you are and what you've liked lately,
then keeps the best.

Two things shape the ranking:

- **Your profile** — a short description, in your own words, of what you care
  about. You write it once and edit it whenever your interests drift.
- **Your recent reactions** — the thumbs up and down you've been tapping. These
  are concrete examples of what you actually opened and what you waved off.

A quiet news day still gets you a reasonable list; the ranker is asked for a
minimum number of picks so the email is never empty when there's anything
decent to show. A busy day gets trimmed to your chosen length.

:::tier little

The ranking is done by Claude, Anthropic's language model. daily scoop hands it
your profile, your recent votes, and the day's candidate stories, and asks it
to score each one from 0 to 1 and write the one-line "why this matters" note
you see in the email. The highest scores become the email, in order.

:::

:::tier medium

`rank.rank()` builds a single Anthropic Messages API call: a system prompt that
defines the scoring task and the target pick window (a soft `min_picks` floor
and a hard `max_picks` ceiling), a cached user block holding the stable profile
and recent feedback, and a fresh user block holding the day's candidates as a
compact JSON array.

The stable block is marked with `cache_control` so prompt caching covers the
profile and feedback while the daily-changing candidate list does not bust the
cache. Claude must reply with JSON matching a fixed `RankResponse` schema
(`{"picks": [{url, title, blurb, source, score}]}`); on a parse failure daily scoop
retries once with a stricter instruction, then sorts by descending score and
keeps the top `max_picks`. Recent feedback for ranking is the last 50 votes — a
recency window, distinct from the wider window profile-tuning uses.

:::

---

## Telling daily scoop what you think

Every story in the email has two buttons: thumbs up and thumbs down. Tapping
one records your reaction and shows a small "thanks" page. That page also has
an optional box where you can say *why* in a sentence ("too much crypto",
"more like this"), which gives daily scoop a stronger signal than a bare vote.

You don't have to react to anything. But the more you do, the faster the
morning email becomes yours. An up-vote says "more like this"; a down-vote says
"less of this." Over days and weeks those reactions become the examples that
teach daily scoop your taste.

The same buttons work on the homepage, where a thumbs-down also tidies up: a
story you down-vote disappears from the page and stays gone.

:::tier little

The buttons in the email are plain links. Each one is signed so that only a
link daily scoop actually generated will be accepted — you can tap it from any
email app, on any device, without logging in, and nobody can forge a vote on
your behalf. Your reactions are saved and fed back into the next ranking.

:::

:::tier medium

Each vote link is `GET /rate?a=<url>&d=<issue-date>&v=up|down&t=<token>`, where
`t` is a URL-safe HMAC-SHA256 over `"{article_url}|{issue_date}"` signed with a
server-side `SIGNING_KEY`. Verification is constant-time and the issue date
bounds replay scope, so the link needs no session cookie and is safe to expose
in an email. The optional note posts back to `/rate/note` carrying the same
signed token.

Votes are written as `FeedbackRow`s keyed on `(article_url, issue_date)`, so
re-voting the same story overwrites rather than piling up contradictory rows.
The homepage uses the same row shape through `/api/vote` (authenticated by the
admin cookie instead of a signed token), and its "down-vote removes and stays
removed" behavior is enforced when the page is built: already-down-voted URLs
are dropped before render.

:::

---

## Your profile, and how it learns

The profile is a short piece of writing — a few lines to a few paragraphs — in
which you tell daily scoop what you're into. Plain language is fine: "machine
learning research, Postgres internals, the business of media, not much sports."
You edit it in the admin console whenever your interests change.

Underneath your own words, daily scoop keeps its own running notes: a "learned
preferences" section it maintains for you, summarizing what your votes have
revealed over time. You never have to touch it. It grows and revises itself as
you react to stories, so insight you taught it weeks ago still counts even
after the individual votes that taught it have aged away.

Your handwritten part is always left exactly as you wrote it. Only the notes
below it are auto-managed.

:::tier little

After each email is sent, daily scoop looks back over a wider stretch of your
recent reactions and asks Claude to update its notes: keep what still holds,
fold in the latest signal, drop anything your new votes contradict. The result
is a cumulative summary of your durable taste, kept in a clearly marked block
at the bottom of your profile.

:::

:::tier medium

`tune.tune_profile()` runs after a confirmed send (`_tune_profile_after_send`),
reading a wider window than ranking — the last 200 votes — so the summary
reflects durable taste, not just the last few days. It splits the profile at
sentinel comments (`<!-- learned-preferences:auto:start/end -->`), feeds the
existing auto-block back to Claude as the current understanding, and asks for a
merged, deduplicated bullet list (about a dozen bullets at most). The
hand-written portion above the sentinels is preserved verbatim.

Tuning is best-effort and never raises: an empty feedback list or any model
error returns the profile unchanged. The profile and the admin knobs share one
DynamoDB table, the profile under `id="me"` and the config under `id="config"`.

:::

---

## Where the stories come from

daily scoop pulls from several sources and ranks them all together, so the best
story of the day wins regardless of where it came from. You don't manage a
separate inbox per source; they compete on equal footing for a place in your
email.

The sources are: your RSS feeds, Hacker News, the open web, and any email
newsletters you've pointed at daily scoop. Each is covered below.

:::tier little

Whatever the source, a story has to be fresh (published in roughly the last
day) and new to you (not something daily scoop already showed you) to be
considered. Everything that clears those two bars goes into one pool, gets
ranked together, and the best rise to the top.

:::

:::tier medium

`digest.run_digest()` assembles the candidate pool: RSS via `feeds.fetch_recent`
(a 24-hour `since` window plus an injected `is_seen` check), Hacker News via
`hn.fetch_hn_articles`, and newsletter links via `db.recent_inbox_articles`.
The pool is de-duplicated by URL (HN and an RSS feed often carry the same link),
and every non-RSS source is best-effort and seen-filtered, so a source outage
can't block the send or resurface yesterday's stories. A shared seen-store with
a 21-day TTL is what keeps already-shown stories from coming back.

:::

### RSS feeds

The feeds you subscribe to are the backbone. You add them in the admin console
by pasting a feed URL (and an optional name); daily scoop checks each one every
morning for anything published in the last day.

A broken or malformed feed is quietly skipped rather than breaking your email —
one bad feed never takes down the rest.

:::tier medium

`feeds.fetch_recent` parses each URL with `feedparser`, keeps entries newer than
the 24-hour cutoff and not already seen, and skips any feed that fails to parse
or is flagged malformed (logged, never raised). It does no database work itself:
the caller injects the `is_seen` check so dedup state lives outside the fetcher.
Added feeds are stored normalized, so adding and deleting the same feed in
different letter-casing still hit the same record.

:::

### Hacker News

Hacker News is built in as a first-class source, not something you add. Each
morning daily scoop pulls the front pages and folds the stories into the same
ranking pool as your feeds, so a strong HN story can lead your email.

Crucially, HN stories arrive with their context — points, comment counts, and
for text posts the body — so the ranker can judge them on substance, and the
homepage can show you how much discussion a story is getting.

:::tier little

daily scoop reads Hacker News through its search API rather than the bare RSS feed,
because the plain feed carries little more than a title. The richer source gives
each story real signal — how many points and comments it has — which makes both
the ranking and the homepage better.

:::

:::tier medium

`hn.py` uses the **Algolia HN Search API**. `fetch_hn_articles` walks up to 20
pages (≈ the first 20 front pages, 30 hits each), keeps only stories with a
trustworthy timestamp inside a **7-day** freshness cap, sorts by points, and
passes the highest-signal subset (a cap of 120) to the ranker as `Article`s with
a content-rich summary line (points, comments, author, and any body snippet).
`fetch_hn_rich` returns the lighter `WebArticle` shape — points, comments, and a
link to the discussion thread — for the homepage's live HN panel. Ask/Show/text
posts with no external link fall back to their HN thread URL. Every fetch is
best-effort: a failed page is skipped, total failure returns nothing, HN never
blocks the digest. All network access goes through an injected fetch callable so
tests stay offline.

:::

### From around the web

Beyond the sources you follow, daily scoop runs a fresh search of the open web each
morning, distilled from your profile, and includes a block of what it finds.
This is how stories from publications you've never added still reach you when
they're squarely on your interests.

A **variety dial** in the admin console controls how adventurous this search
is. Turned low, it stays tightly on your stated interests. Turned high, it
wanders into adjacent, related territory — neighbouring fields and second-order
angles a curious reader would enjoy — without ever going random or off-topic.

:::tier little

The web block is a live search run by Claude using its web-search tool, with
the query built from your profile. The variety dial is passed into that search
as an instruction about how far to roam. You also control how many web results
the email carries, and setting that to zero turns the block off entirely.

:::

:::tier medium

`websearch.search_web()` calls Claude with the server-side `web_search` tool,
a query distilled from the profile, and a recency bias toward the last week.
The `variety` value (0–100) is translated into a graded exploration directive
(`_variety_directive`) ranging from "stay tightly on-topic" to "emphasize
exploratory, ancillary, second-order results, but never random." Results are
de-duplicated, validated against a `WebArticle` schema, and capped at your
`max_web_articles`. In the daily digest the web block does **not** exclude your
own feed domains (a profile-driven search naturally surfaces them, and excluding
them would empty the block). It runs on a fast model with a small number of
search rounds, a configuration proven reliable in production where a heavier
model spent its whole budget on tool calls and returned nothing.

:::

### Newsletter subscriptions

Plenty of good writing only arrives by email newsletter, never as an RSS feed.
daily scoop can subscribe to those for you and pull their links into the same daily
ranking.

In the admin console you give a newsletter a name and daily scoop mints a unique
email address for it. You paste that address into the newsletter's signup form.
From then on, every issue that newsletter sends lands at daily scoop, which pulls
out the article links and lets them compete for your morning email alongside
everything else. If the newsletter sends a "please confirm your subscription"
email first, daily scoop clicks the confirmation link for you automatically.

This is the one feature that needs a mail domain set up; without one, the
console tells you so and leaves the controls switched off.

:::tier little

Each subscription gets its own throwaway-looking address (something like
`n-a8f3c2d1@your-mail-domain`). Mail sent to it is received by daily scoop,
scanned for the real article links (skipping the unsubscribe and social-media
chrome), and those links join the ranking pool. A subscription starts as
"pending" and flips to "confirmed" once a confirmation email has been handled —
either automatically or when the first real issue arrives.

:::

:::tier medium

The flow is: SES receives mail on your `MAIL_DOMAIN` (a catch-all receipt rule),
writes the raw message to an S3 bucket, and invokes the inbound Lambda. That
handler (`handlers/inbound.py`) matches the recipient address to a
`Subscription`, then either auto-follows a double-opt-in confirmation link
(`mark_subscription_confirmed`) or extracts article links and stores them
(`put_inbox_email` + `touch_subscription`). Extraction (`newsletters.py`) is
heuristic and lenient: it keeps headline-shaped anchors, drops boilerplate
(unsubscribe / preferences / social / "view in browser"), and stamps each link
with the message date so the digest's 24-hour window includes it. Confirmation
detection keys off subject/body phrasing and confirm-shaped links. The handler
never raises (a raise would make SES retry-storm), and its S3 read and
link-follow are injectable so tests stay offline. Raw mail in S3 expires after
30 days; extracted links in the inbox table carry a 30-day TTL. SES inbound is
only available in some AWS regions, and the active receipt-rule set has to be
switched on once by hand after deploy.

:::

### Discoveries — sources you don't follow yet

At the bottom of the email, daily scoop sometimes suggests a source you don't
already follow: a publication that fits your interests, with a one-line reason
and a one-tap button to start following it. Tap it and that source's feed is
added to your subscriptions, so its future articles join the ranking from then
on.

This is how your feed list grows on its own. daily scoop only suggests a source if
it can confirm the source publishes a working feed, so the "follow" button
never adds a dead link.

:::tier little

Discoveries come from a Claude web search aimed specifically at sources *outside*
your current feeds. Before a suggestion is shown, daily scoop fetches the proposed
feed and confirms it is a real, non-empty RSS/Atom feed. The "follow" button is
a signed one-tap link, like the vote buttons, so it works from any inbox.

:::

:::tier medium

`discovery.find_discoveries()` calls Claude with the `web_search` tool and a
prompt that excludes your existing feed domains and requires each result to
carry a real RSS/Atom `feed_url`. Each candidate is validated item-by-item
against the `Discovery` schema, filtered against already-followed hosts, and
checked for liveness (`feed_validator` actually parses the feed and requires at
least one entry) before it is offered. The follow link is `GET /subscribe`,
signed with the same HMAC scheme as `/rate` over `(feed_url, issue_date)`, and
`db.add_feed` upserts so a double-click is harmless. Discoveries are an email
concern: the homepage skips them entirely.

:::

---

## The homepage

The email is the short list. The homepage is the long one — a richer place to
browse when you want more than the morning's handful.

It shows a large, ranked spread of stories plus an open-web block, each as a
card you can vote on. A thumbs-up sticks; a thumbs-down makes the story vanish
from the page for good. There's a date header so you always know how fresh the
page is, and a research box (covered next) for chasing a topic on demand.

There is no refresh button, on purpose. The homepage rebuilds itself every
morning, a little before the email goes out, and if you visit and the page
isn't today's, it quietly regenerates while you wait and reloads when it's
ready.

:::tier little

The homepage is a separate, bigger edition than the email — it carries far more
stories and skips the email-only bits (no "follow this source" suggestions).
It's built by the same ranking machinery, just with generous limits. When it's
stale, the page kicks off a rebuild in the background, polls until the new
edition is ready, and reloads itself.

:::

:::tier medium

The homepage (`GET /`, `read.html.j2`) renders a standalone aggregation stored
under the reserved issue key `"home"`, built by the digest's home mode
(`_run_home`) with generous fixed counts (around 40 ranked picks and 20 web
articles). Unlike the daily email it ignores the seen-store (it's a browse
surface, not a deduped feed) and skips discovery. A scheduled EventBridge rule
rebuilds it daily at **09:45 UTC**, 15 minutes before the email. On demand, when
the stored edition is missing or not from today, the client posts to
`/api/home/refresh` (an async digest `{"home": true}` invoke), polls
`/api/home/status` for a newer timestamp, and reloads. Voting goes through
`/api/vote`; down-voted URLs are dropped when the page is assembled, so a
removed story stays removed. The homepage requires the admin cookie.

:::

---

## Researching a subject on demand

Sometimes you want to go deeper on one thing right now. The homepage has a box
where you type a subject and daily scoop runs a fresh web search on it, returning a
set of relevant, recent articles as cards you can read and vote on.

It honours the same variety setting as your daily web block, so the results can
stay narrow or fan out into related territory depending on how you've set the
dial. It's built to answer quickly, so you're not left waiting.

:::tier little

The research box runs the same kind of Claude web search the daily email uses,
but tuned to come back fast — a quicker model and fewer search rounds — so it
returns while you're still looking at the page.

:::

:::tier medium

The subject box hits `/api/search` (and a no-JS fallback path renders inline via
`?q=` on the homepage). Because it runs synchronously behind the HTTP API's
~30-second integration timeout, it calls `websearch.search_web` with a fast
model (`claude-haiku-4-5`), a low search-round cap, and a modest result count,
while still passing your admin variety dial. The daily digest, with a 300-second
Lambda budget, keeps the more thorough defaults. A separate `/api/hn` endpoint
serves the live Hacker News front page (points, comments, thread link) the same
way.

:::

---

## The admin console

The admin console is your control panel. From one page you manage everything:

- **Feeds** — add and remove the RSS feeds you follow.
- **Profile** — write and edit the description of your interests.
- **Daily email settings** — how many ranked stories the email carries, how
  many web results it adds, and the variety dial for how far the web search
  roams.
- **Newsletter subscriptions** — mint addresses for email newsletters and see
  whether each is pending or confirmed.
- **Send now** — trigger a real email immediately, for when you don't want to
  wait for the morning.
- A link to **this guide**, and to your **sent-email archive**.

The console also shows you when the last email went out, and flags a day's
issue that hasn't been delivered.

:::tier little

The settings have sensible ranges: the email carries between 1 and 40 ranked
stories (default 10) and up to 30 web results (default 5, or 0 to switch the web
block off), and the variety dial runs 0 to 100 (default 30). "Send now" doesn't
disturb your daily rhythm — it sends a genuine email with working vote buttons,
but it stays out of the regular schedule and your recent-issues list.

:::

:::tier medium

The console is `GET /admin` in the web Lambda. Settings persist through
`/api/config` as a `Config` model (`max_rss_articles` 1–40, `max_web_articles`
0–30, `web_variety` 0–100), read leniently with defaults on a missing or bad
row. "Send now" (`/api/send-now`) async-invokes the digest Lambda with
`{"manual": true}`; that manual run is a faithful fetch → rank → send → tune
with a live feedback loop, but it stores under a synthetic
`manual-<timestamp>-<rand>` key, ignores the daily idempotency gate, and never
marks the day sent or consumes the scheduled run's candidate pool. All admin
routes require the `admin_token` cookie and 303-redirect to `/login` without it.

:::

---

## The email archive

Every daily email is kept. The archive lists your recent issues, newest first,
and shows which ones were delivered. Open any one to see it exactly as it was
sent — the same stories, the same notes, the same buttons.

It's a record of what daily scoop has been sending you, useful for finding a story
you remember getting but didn't save.

:::tier medium

`GET /emails` lists recent issues (with sent/unsent status); `GET /emails/{date}`
re-renders that day's email as-sent. The archive deliberately shows the email
the way it was delivered, which keeps it distinct from the homepage's richer
browse view. Both are admin-only. (One caveat: vote links in archived issues are
re-signed with the *current* signing key, so rotating that key invalidates old
issues' buttons.)

:::

---

## Access and privacy

daily scoop is yours alone. Reaching the console, the homepage, or the archive
takes a single secret token that you set up once; everything behind it is
private to you.

The one thing that's deliberately *not* behind a login is the set of buttons in
your email — the vote and follow links — because they have to work from inside
your inbox on any device. Those are protected a different way: each link is
cryptographically signed, so only links daily scoop itself created will be accepted,
and a tampered or guessed link is rejected.

Your data stays small and ages out on its own. The record of which stories
you've already seen is kept only long enough to avoid repeats. Raw newsletter
emails and the links pulled from them are discarded after a month. There is no
tracking, no third-party analytics, and no audience but you.

:::tier little

Sign-in is a single admin token, stored as a browser cookie for 30 days. The
email's buttons use signed links instead, so they need no cookie. Secrets (the
admin token, the signing key, and the keys for the AI and email services) are
kept in encrypted parameter storage, not in the code.

:::

:::tier medium

Authentication is a single shared `admin_token` (an `httponly`, `lax` cookie,
`secure` over HTTPS, 30-day max-age) checked on every admin route. Email-action
links use HMAC-SHA256 signatures (`tokens.sign`/`verify`, constant-time) over
`(target, issue_date)` with a server-side `SIGNING_KEY`; the issue date bounds
replay scope and no token expires on its own. All four secrets
(`ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `ADMIN_TOKEN`, `SIGNING_KEY`) load from
SSM Parameter Store SecureString values at cold start, with an env-var override
for local development. Retention: the seen-store carries a 21-day TTL, raw
inbound mail in S3 expires after 30 days, and the inbox table carries a 30-day
TTL. The only personal data stored is your feeds, your profile, your votes, and
the issues daily scoop built for you.

:::

---

## How it all runs

You never have to think about this part, but here's the shape of it.

daily scoop runs entirely as small, on-demand cloud functions rather than a server
that's always on. One function wakes up each morning to build and send your
email; another rebuilds your homepage a little earlier; a third receives any
newsletter mail as it arrives; and a small web service answers the console and
homepage when you open them. Between runs, nothing is running and nothing costs
anything to sit idle.

Two outside services do the specialized work: Anthropic's Claude does the
reading, ranking, and writing, and Resend delivers the mail.

:::tier little

It's a serverless app on AWS: a few functions that run only when needed, a set
of small databases for your feeds, profile, votes, and issues, and scheduled
timers that fire the morning jobs. Updates ship automatically — when new code is
approved, it deploys itself, and this very guide is refreshed by an AI step
whenever the code it describes changes.

:::

:::tier medium

The stack is AWS SAM: three Lambdas (digest, web, inbound), an HTTP API in front
of the web Lambda, seven DynamoDB tables (feeds, profile, seen-articles, issues,
feedback, subscriptions, inbox), an S3 bucket for raw inbound mail, and SES
inbound for the newsletter source. Two EventBridge crons drive the daily
cadence (home rebuild at 09:45 UTC, email at 10:00 UTC). External dependencies
are Anthropic (ranking, summaries, discovery, web search) and Resend (delivery).
Continuous integration runs tests and linting on every change and deploys to AWS
on merge to the main branch. This product guide is regenerated from the code by
a scheduled AI step so it doesn't drift from what actually ships.

:::

---

*This guide is generated from daily scoop's own source code. The plain-language
sections describe behavior; the deeper layers name the parts. If a number or a
claim here ever disagrees with what the app does, the code is the source of
truth — and an automated step keeps this guide caught up with it.*
