# PR 4: Brooklyn Weather Line Implementation Plan

**Goal:** One terse weather line under the email dateline (and therefore on `/`), from the free keyless NWS API, no LLM, stamped on the Issue.

## Tasks

### Task 1: weather.py + contracts
- `Issue.weather_line: str = ""` (lenient); `Config.weather_enabled: bool = True` (checkbox; no lat/lon knobs in the admin UI — env-style defaults in code, Brooklyn 40.678,-73.944, overridable via fetch_weather kwargs; YAGNI on config plumbing until a second city matters — note this in the PR body).
- `weather.py`: `fetch_weather(*, lat: float = BROOKLYN_LAT, lon: float = BROOKLYN_LON, fetch=None) -> str | None`.
  - `fetch(url) -> dict` injectable; default impl: urllib.request with `User-Agent: newslet (github.com/jfrederick/newslet)` and a 10s timeout, JSON-decoded.
  - Two calls: `https://api.weather.gov/points/{lat},{lon}` → `properties.forecast` URL → `properties.periods`.
  - Format from the first two periods (today/tonight or tonight/tomorrow): `f"{p0.temperature}° {p0.shortForecast.lower()}, {p1.name.lower()} {p1.temperature}° {p1.shortForecast.lower()}"` → e.g. `78° chance light rain, tonight 64° mostly clear`.
  - Best-effort → `None` on any error/missing key/empty periods.
- Tests: happy path with canned two-call fetch (assert both URLs hit, line format), missing periods → None, HTTP error → None, weird payload → None.

### Task 2: pipeline + render
- `run_digest(..., weather_fn=None, weather_enabled=True)` → `issue.weather_line` via try/except (`_build_issue(weather_line=...)`).
- `_fresh_issue` passes `weather_enabled=config.weather_enabled`.
- db: `weather_line` attr on the issue row (plain string, lenient default "").
- email.html.j2: one muted line under the date cell in the header row (both email + `/` for free); no voting.
- dry-run fixture line; `_fake_weather`.
- `/api/config` + admin checkbox (`weather_enabled`).
- Tests: render (line present/absent), digest wiring (attach, exception swallowed, disabled skips), db roundtrip + legacy, config roundtrip.

### Task 3: docs + ship
- DESIGN.md module section + table attr + config route; AGENTS map row + best-effort list + admin list; README module line; product.md short section.
- Branch `weather-line` from main after PR 3 merges → PR → reviews → squash-merge → deploy.
