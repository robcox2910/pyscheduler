# PyScheduler Phase 3 (Web UI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A browser UI served by the scheduler: login, a live job board, job and run detail with a live log tail, a dependency graph, a timeline/Gantt, a forecast table and admin pages. It runs on the same pyweb app and port as the JSON API, with one auth layer for both.

**Architecture:**
- **Rendering:** pages are server-rendered by small escaping element helpers. pyweb's `html_template` only fills `{{ name }}` / `{{ name|safe }}`, so it is used just for the page shell.
- **Page script:** one vanilla `app.js` listens to one SSE stream (`/events`). When a run changes it fetches that row's server-rendered HTML fragment and swaps it in, so rendering lives only in Python. Operator actions call the existing JSON API with the CSRF header.
- **Auth:** pyweb's `SessionAuth` (cookies, CSRF), extended in pyweb v0.3.0 with a pluggable bearer verifier, so API tokens keep working through one middleware. pyscheduler maps either kind of user to its existing `Principal`.

**Tech Stack:**
- Python 3.14
- pyweb **v0.3.0** (new: `SessionAuth(bearer_verifier=...)`); plus `App`, `mount_static`, `EventStream`, `EventHub`, `ServerEvent`, `html_template`, `escape`, `SessionSigner`, `hash_password`, `LoginThrottle`, `authenticate`, `TestClient`
- pynet v0.1.0, pydb v0.4.0, pymq v0.3.1
- stdlib `difflib`, `html.parser` (tests), `getpass`
- No frontend framework, no build step, no third-party JS.

**Spec:** `docs/superpowers/specs/2026-10-17-pyscheduler-design.md` (Phase 3 section; the HTTP API and CLI section's note on extending `SessionAuth`; Docs, PyStack and Testing lines for phase 3).

Facts on pyweb v0.2.0 and the pyscheduler API this plan relies on (signatures, file:line) are in each task's Interfaces block. Read the named files before changing them.

## Global Constraints

- Python ≥ 3.14; uv; ruff (repo rules); pyright strict on `src` and `tests`; pytest coverage ≥ 80 % and `--timeout=10` per test.
- No `TYPE_CHECKING`, no `from __future__ import annotations`.
- Files stay under about 400 lines; `engine/base.py` (399) and `store/store.py` (~382) are at the limit, so new code goes in new modules.
- Docstrings on every public module, class and function (imperative, Google style), and on test classes and methods; named constants instead of magic numbers.
- Runtime dependencies are only pynet, pymq, pyweb and pydb, pinned by git tag, plus the stdlib. Third-party CSS, JS or fonts are not allowed; the UI uses system fonts.
- **Every value rendered into HTML is escaped**: build HTML only through `ui/html.py` helpers, and never use string concatenation of untrusted text. `|safe` is used only for HTML built by those helpers.
- No inline `<script>` or `on*=` attributes. Every HTML response sends:
  - `Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'`
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: same-origin`
- Blocking work (store reads, file reads, forecast and calendar walks, PBKDF2) runs in `pynet.loop.run_in_thread`, or in a pyweb route with `blocking=True`.
- `${ENV}` values never appear in HTML. Pass data through the existing `*_json` helpers and `redact(..., engine.config.expansions)`.
- Roles:
  - `viewer` reads only.
  - `operator` can also take run and job actions.
  - `admin` can also open the admin pages, reload and replay.
  - `admin` ≥ `operator` ≥ `viewer`.
- The visual design is the user's call, delegated: a clean default, adjustable later. All colours and sizes are CSS custom properties in `ui/static/app.css`, so a restyle touches one file.
- Every concept page goes in `docs/concepts/<name>.md`, is kid-friendly with an everyday analogy and a runnable snippet (or `<!-- no-run -->`), and is added to the `mkdocs.yml` nav and `docs/index.md`. Phase 3 pages: `web-ui`, `live-updates`, `graph-layout`, `timelines`.
- Commits are conventional and end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Rulings (where this plan fills gaps in the spec)

- **P3-R1 Rendering:** server-side Python builds HTML with `ui/html.py` (`h()`, `Safe`), because `html_template` has no loops or conditionals. Templates hold only the page shell. Live row updates fetch `/ui/fragments/...` HTML from the server, so rendering exists only in Python.
- **P3-R2 One auth layer:**
  - pyweb v0.3.0 adds `SessionAuth(..., bearer_verifier: Callable[[str], User | None] | None = None)`. When it is set, a bearer token is checked by the verifier, with the old signed-api-token check as the fallback. The rules stay: a bearer present means the cookie is never used, and bearer requests skip the CSRF check.
  - pyscheduler's verifier maps a token-file entry to `User(name, "", frozenset({role}))`.
  - A pyscheduler `access` middleware runs inside `SessionAuth`. It turns the pyweb `User` into `Principal(name, role)` and applies the existing API rules:
    - `/api/*` needs a user (401 JSON);
    - a viewer is limited to GET/HEAD (403);
    - a `POST /api/*` must be JSON (415).
  - Cookie users' JSON POSTs therefore carry `X-CSRF-Token`, which `SessionAuth` checks.
- **P3-R3 Modes:**
  - **Dev mode** (no `api_tokens_file` and no `users_file`): loopback Host only, as now. Every request, UI or API, is `Principal("dev", "admin")`, with no login and no CSRF. The JSON-only POST rule stays as the CSRF defence, and UI forms in dev mode post through fetch as JSON.
  - **Tokens file but no `users_file`:** the UI pages answer 403 "the web UI needs a users_file", and the API works as before.
  - **Normal mode:** a `users_file` is set (tokens optional).
- **P3-R4 Users file:** `scheduler: users_file: users.yaml` is a yamlish list of `{name, password_hash, role}`. Role is one of viewer, operator or admin. `password_hash` comes from `pysched hash-password`, which prompts twice with `getpass` and prints `password_hash: pbkdf2_sha256$...`. A reload re-reads the file, as the tokens file is re-read. A bad file fails the reload as a whole.
- **P3-R5 Session secret:** 48 random bytes, made on first start, kept in `<state_dir>/session.key` with mode 0600, and read on later starts. Sessions last 8 h, the `SessionSigner` default. `secure_cookies` is True unless the listen host is loopback.
- **P3-R6 Live feed:** pyweb's `EventHub` has no ids or replay, so `ui/live.py` adds them.
  - Every event gets a rising integer id and goes into a ring buffer of the last 1000 (`LIVE_BUFFER`).
  - A client reconnecting with `Last-Event-ID` gets the events after that id if they are still buffered. Otherwise it gets one `reset` event, and the page reloads itself.
  - Event types: `run` (`{run_id, job, run_date, state}`), `attempt` (`{run_id, attempt, ended}`), `reload` (`{version}`).
  - They are fed by `engine.subscribe`, `engine.watch_attempts` and `engine.watch_reloads`.
- **P3-R7 Log tail:** `GET /events/log/<run_id>?stream=stdout|stderr&attempt=` is an SSE stream.
  - It first sends the last 200 lines through the existing `read_tail`.
  - Every second (`LOG_POLL`) a worker thread reads the new bytes from the remembered offset, at most 64 KiB per tick (`LOG_CHUNK`).
  - It sends whole lines, and ends with an `end` event a poll after the attempt has ended.
  - A file that shrinks (rotation) restarts from 0 with a `reset` line.
- **P3-R8 "Definition from the config version the run used":**
  - Run detail shows the current definition when `run.config_version` equals the current version.
  - Otherwise it shows the stored source text of that version for the run's job file, from `ConfigRecord.sources_json`, plus a link to the admin diff. Re-parsing old configs, which may need `${ENV}` values that are gone, is not attempted.
- **P3-R9 Typical duration:** the median of `recent_durations(job, 10)`, read for every job on the page in one thread call (`Store.recent_durations_for(jobs)`). "vs typical" shows `duration / typical` as a percentage, flagged when it is over 150 % or under 50 %.
- **P3-R10 Board order:** runs that are FAILED and `critical` are pinned first. The rest follow in `scheduled_at` order, and runs with the same `scheduled_at` sort by job name.
- **P3-R11 Graph:**
  - Layers come from longest-path layering over offset-0 dependencies only. Edges with an offset other than 0 (cross-date) never take part in layering, so self- and loop-offsets cannot loop. They are drawn dashed.
  - Ordering uses 4 barycentre sweeps (down, up, down, up).
  - Edge styles by `on`: success is solid, failure is red dashed, done is grey.
  - Node fill is the state of that run date's latest run, or "no run".
  - Focusing on a job (`?focus=`) highlights its transitive upstream and downstream, done on the server with no JS.
  - `?tag=` keeps the jobs with that tag and the edges between them.
- **P3-R12 Timeline:**
  - Lanes are one per job that has an attempt or forecast entry in the window, sorted by name.
  - Bars:
    - each attempt is one segment coloured by outcome, and the retries of a run sit next to each other;
    - a running attempt extends to now;
    - forecast entries in the future part of the window are ghost bars (dashed outline);
    - there is a "now" line.
  - Zoom presets: hour (the last 1 h plus the next 1 h), day (00:00–24:00 local on the given date), week (7 local days).
  - The longest window is 7 days, and the default is day/today.
  - Attempts are read through the runs of the run dates the window touches (each local day minus 1), with one batched `Store.attempts_for_runs(run_ids)`.
- **P3-R13 Admin:**
  - The versions list comes from `Store.config_versions() -> list[ConfigVersion(version, loaded_at, digest)]` (no sources). One version comes from `Store.get_config(version) -> ConfigRecord | None`.
  - The diff is a `difflib.unified_diff` between version N and N-1 for each file, rendered with escaping.
  - The reload button POSTs `/api/reload`. The dead-letter list has a replay button that POSTs `/api/alerts/<id>/replay`.
  - The admin pages need the admin role; the API keeps its phase-1/2 roles.
- **P3-R14 Look (default):**
  - System font stack, light and dark themes via `prefers-color-scheme`, and a top bar with nav: Board · Graph · Timeline · Forecast · Admin.
  - State badges follow traffic lights:
    - succeeded is green, failed red, and running blue;
    - queued, waiting and scheduled are grey;
    - retry_wait and held are amber;
    - skipped and cancelled are muted.
  - Critical criticality shows a ▲ marker. Max content width is 1400 px, and the tables scroll sideways on narrow screens.

## Review Focus

1. **Hostile text in job names, descriptions, reasons, log lines and stored config sources** (`<script>`, `"`, `&`). It must be shown escaped on every page and fragment. Owned by Task 3's `test_every_page_escapes`, which each later page task extends with its own route.
2. **A cookie-authenticated POST without `X-CSRF-Token`** (a cross-site form or fetch) to `/api/runs/<id>/kill`, `/api/reload` or `/logout` gets 403 and does nothing. A bearer request still works (Task 2 `test_cookie_post_needs_csrf`).
3. **A browser that reconnects after missing events.**
   - After a short gap it receives exactly the missed events.
   - After a gap longer than the buffer it receives one `reset` event.
   - After a scheduler restart (ids start again) it receives `reset`, not stale replays.
   - Task 4 `test_replay_and_reset`; the feed carries a boot id in each event id, `f"{boot}-{n}"`.
4. **A board for a busy day** (500 runs over 120 jobs) renders in well under a second, using one store thread call for durations (Task 5 `test_busy_board_is_fast`).
5. **Graphs that are awkward to lay out:**
   - a job depending on its own `-1`;
   - `a → b(-1)` with `b → a`;
   - a disconnected job;
   - 200 jobs in a chain.

   Each finishes, and every node gets a position (Task 7 `test_awkward_graphs`).

---

### Task 1: pyweb v0.3.0, a pluggable bearer verifier (pyweb repo)

Done in a pyweb worktree (`../pyweb-bearer-wt`), released, then pinned.

**Files (pyweb repo):**
- Modify: `src/pyweb/auth.py`. It gains the `SessionAuth.__init__` kwarg `bearer_verifier: Callable[[str], User | None] | None = None` (around lines 225–234). In `_identify` (around lines 307–312), when a bearer is present: if a verifier is set and returns a `User`, use it; otherwise fall back to the signed API token. If both fail, the request is anonymous. The cookie is never consulted when a bearer is present, and bearer requests are never CSRF-checked.
- Modify: `docs/concepts/` (the roles/sessions page), adding a short "your own tokens" section.
- Test: `tests/aio/test_auth.py`

- [ ] **Step 1: Failing tests.**
  - `test_bearer_verifier_user`: a verifier returning `User("ci", "", frozenset({"viewer"}))` for `"tok"` makes `require("viewer")` pass, and `require("admin")` gives 403.
  - `test_bearer_verifier_none_falls_back`: a signed `api_token(user)` still works when the verifier returns None.
  - `test_bearer_skips_csrf_and_cookie`: a POST with a bearer and no CSRF header works; with both a bad bearer and a valid cookie, the request is anonymous.
  - `test_verifier_exception_is_anonymous`: a raising verifier is logged, and the request is anonymous, not a 500.
- [ ] **Step 2: Implement; full pyweb suite, ruff, pyright, `mkdocs build --strict`.**
- [ ] **Step 3: Commit, then PR → CI green → merge → `git tag -a v0.3.0 -m v0.3.0` and push.**
- [ ] **Step 4: In PyStack**, bump pyweb to v0.3.0 in a worktree, run the full suite, then PR → merge.

### Task 2: Users, roles, the session key and one access layer

**Files:**
- Modify: `pyproject.toml` and `uv.lock` (pyweb `v0.3.0`).
- Modify: `config/settings_fields.py` and `config/model.py`: `SCHEDULER_KEYS` gains `users_file`; `Settings.users_file: Path | None`, resolved like `api_tokens_file`.
- Create: `src/pyscheduler/api/users.py`:
  - `UsersFileError(ValueError)`
  - `UserTable.load(path) -> UserTable` (yamlish; errors name file:line; duplicate names, unknown roles and bad hashes are errors)
  - `UserTable.get(name) -> User | None` (it is a pyweb `UserStore`)
  - `UserHolder` (a mutable cell, like `TokenHolder`)
- Create: `src/pyscheduler/api/access.py`, which holds `build_access(app, *, tokens: TokenHolder, users: UserHolder, state_dir: Path, secure: bool) -> SessionAuth | None` and the access middleware. It installs either the dev-mode middleware or `SessionAuth` plus the access middleware, per P3-R2 and P3-R3. It replaces `app.middleware(token_middleware(tokens))` in `build_app`, but keeps `token_middleware`'s dev-mode Host check and its 401/403/415 JSON bodies exactly.
- Create: `src/pyscheduler/api/session_key.py`, with `load_or_make_key(state_dir: Path) -> str` (P3-R5).
- Modify: `api/auth.py`: `Role` becomes `Literal["viewer", "operator", "admin"]`; add `ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}` and `def allows(role: Role, needed: Role) -> bool`; `DEV_PRINCIPAL = Principal("dev", "admin")`.
- Modify: `api/support.py`: `role_of` and `actor_of` **fail closed**. A `request.user` that is not a `Principal` raises `ApiError(401, "unauthorised")`, instead of falling back to the dev principal.
- Modify: `api/admin.py` (`config_actions` re-reads the users file with the tokens; an error fails the reload as a whole), `api/app.py` (`build_app(..., users: UserHolder | None = None)`), and `service.py` (load users, build access, choose `secure`).
- Modify: `cli.py` and `cli_commands.py`: the offline command `hash-password` (prompts twice, mismatch → exit 2 "passwords do not match", prints `password_hash: ...`).
- Test: `tests/api/test_users.py`, `tests/api/test_access.py`, `tests/test_cli.py` (hash-password).

**Interfaces:**
- Consumes:
  - pyweb `SessionAuth(signer, users, *, cookie_name, login_path, secure_cookies, allowed_origins, bearer_verifier)`
  - `SessionSigner(secret)`
  - `User(name, password_hash, roles)`
  - `hash_password`
  - `TokenTable.lookup(token) -> (name, role) | None`
  - `is_loopback_host`
- Produces:
  - `Principal(name, role)` on `request.user` for every authenticated request, with `role_of`/`actor_of` unchanged in name
  - `allows(role, needed)`
  - `request.state["pyscheduler.csrf"]`: the CSRF token for cookie users (from `SessionAuth.csrf_token`), so pages can embed it
  - `session_auth` returned for the login routes (Task 3)

- [ ] **Step 1: Failing tests.**
  - `test_users_file_loads_and_errors`: a good file loads; a duplicate name, an unknown role or a bad hash each fails with file:line.
  - `test_bearer_tokens_still_work`: phase-1 token tests keep passing, and a viewer token POST gives 403.
  - `test_cookie_session_maps_to_principal`: after `session_auth.login`, `/api/runs` answers 200 with `actor_of == name`.
  - `test_cookie_post_needs_csrf`: a cookie POST to `/api/runs/<id>/hold` gives 403 without `X-CSRF-Token` and 200 with it (Review Focus 2).
  - `test_non_principal_fails_closed`: a handler seeing a bare pyweb `User` gives 401.
  - `test_dev_mode_unchanged`: loopback is admin, and a foreign Host gives 403 `forbidden host`.
  - `test_tokens_without_users_file`: `/` gives 403 "the web UI needs a users_file", and `/api/jobs` with a token gives 200.
  - `test_session_key_persists_and_is_private`: the key file has mode 0600 and is the same across two calls.
  - `test_reload_rereads_users`: a removed user's cookie gives 401 after the reload.
  - `test_hash_password_cli`: monkeypatched getpass; matching entries print a hash that `verify_password` accepts; a mismatch exits 2.
- [ ] **Step 2: Implement; run tests/api, tests/test_cli.py and the full suite once.**
- [ ] **Step 3: Commit** `feat: one access layer for API tokens and web sessions`.

### Task 3: UI shell — HTML helpers, layout, static files, login and logout

**Files:**
- Create: `src/pyscheduler/ui/__init__.py`, with `add_ui(app, engine, *, session_auth: SessionAuth | None, dispatcher, live) -> None` (the routes are added by later tasks).
- Create: `ui/html.py`:
  - `class Safe(str)`
  - `def h(tag: str, attrs: Mapping[str, str | bool | None] | None = None, *children: str | Safe | None) -> Safe`: escapes text children and attribute values; `True` gives a bare attribute and `None`/`False` omits it; tags and attribute names come from a fixed allow-list, and others raise `ValueError`
  - `def join(*parts: Safe | None) -> Safe`
- Create: `ui/layout.py`:
  - `def page(request, *, title: str, body: Safe, active: str) -> HttpResponse`, which renders `ui/templates/page.html` with `html_template`. The page holds the nav, the user's name and role, and a logout button (a form posting `csrf_token`); `<meta name="csrf-token">`; and `app.css` and `app.js`.
  - It sets the security headers listed in Global Constraints.
- Create: `ui/templates/page.html` and `ui/templates/login.html`; `ui/static/app.css` (CSS custom properties, light and dark, P3-R14) and `ui/static/app.js` (empty module skeleton: CSRF fetch helper `post(url, body)`, plus a `confirmThen(button)` helper).
- Create: `ui/login.py`:
  - `GET /login` (with `?next=`)
  - `POST /login` (`blocking=True`, form fields `name`, `password`; `authenticate` with a `LoginThrottle`; success gives 303 to `safe_next(next)`; failure re-renders with "wrong name or password" and status 401)
  - `POST /logout` (form `csrf_token`; 303 to `/login`)
- Mount: `app.mount_static("/static", <package>/ui/static, max_age=300)`. Make sure the templates and static files ship in the wheel: check `pyproject.toml`'s build config, and add an include rule if needed.
- Test: `tests/ui/test_shell.py`, `tests/ui/ui_helpers.py` (a TestClient with a logged-in user per role; `parse(html) -> list[tuple[tag, attrs, text]]` with `html.parser`; `HOSTILE = '<script>alert("x")</script>&'`).

- [ ] **Step 1: Failing tests.**
  - `test_h_escapes_text_and_attrs`; `test_h_rejects_unknown_tag`.
  - `test_login_flow`: a GET of `/` while logged out gives 303 to `/login?next=/`; a wrong password gives 401 and the page says so; the right one gives 303 to `/` with the session cookie set; logout clears it.
  - `test_login_throttle`: the 6th wrong password in a row gives 429.
  - `test_security_headers`: the CSP and nosniff headers are present on HTML pages.
  - `test_static_served`: `/static/app.css` gives 200 `text/css`, and `/static/../config` gives 404.
  - `test_every_page_escapes`: a user named `HOSTILE` logs in, and the shell shows the escaped name with no raw `<script>`. **Later tasks add their routes to this test's parameter list** (Review Focus 1).
  - `test_viewer_sees_no_admin_nav`: the Admin nav link is only shown to admins.
- [ ] **Step 2: Implement; run tests/ui and the full suite once.**
- [ ] **Step 3: Commit** `feat: add the web UI shell with login`.

### Task 4: Live feed — `/events` with ids, replay and reset; live log tail

**Files:**
- Create: `ui/live.py`:
  - `class LiveFeed`:
    - `__init__(self, *, buffer: int = LIVE_BUFFER, boot: str | None = None)`, where `boot` defaults to 8 random hex characters
    - `publish(self, kind: str, data: Mapping[str, JsonValue]) -> str` returns the event id `f"{boot}-{n}"`
    - `stream(self, last_event_id: str | None) -> EventStream`
    - `attach(self, engine) -> Callable[[], None]` subscribes to `subscribe`, `watch_attempts` and `watch_reloads`, and returns a detach function
  - Constants: `LIVE_BUFFER = 1000`, `KEEPALIVE = 15.0`.
- Create: `ui/logtail.py`:
  - `def read_from(path: Path, offset: int, limit: int) -> tuple[bytes, int, bool]`, returning the data, the new offset, and whether the file shrank
  - the `GET /events/log/<run_id>` route (P3-R7)
  - `LOG_POLL = 1.0`, `LOG_CHUNK = 65536`
- Modify: `service.py`: create a `LiveFeed`, attach it before `engine.start()`, detach it on shutdown, and pass it to `add_ui`.
- Test: `tests/ui/test_live.py`, `tests/ui/test_logtail.py`

- [ ] **Step 1: Failing tests.**
  - `test_events_stream_run_changes`: triggering a job (FakeLauncher harness) gives `run` events in order, with ids `boot-1`, `boot-2`, and so on.
  - `test_replay_and_reset` (Review Focus 3):
    - `Last-Event-ID: boot-3` gives events 4 and onwards;
    - an id older than the buffer gives a single `reset`;
    - a different boot gives `reset`;
    - a garbage id gives `reset`.
  - `test_events_need_login`: logged out gives 401 JSON, or a 303 when HTML is accepted.
  - `test_read_from`: appended bytes come back from the offset, the limit is respected, and a shrunk file is reported.
  - `test_log_tail_streams_new_lines` (real file, short `LOG_POLL` via monkeypatch): the first event holds the tail; lines appended later arrive; `end` comes after the attempt ends; partial lines are held back until their newline arrives.
  - `test_log_tail_escapes_nothing_but_is_text`: events are JSON-encoded data, which app.js inserts with `textContent`.
- [ ] **Step 2: Implement; run tests/ui and the full suite once.**
- [ ] **Step 3: Commit** `feat: stream live run changes and log tails`.

### Task 5: Job board (`/`)

**Files:**
- Create: `ui/board.py`:
  - `GET /` with `?date=&state=&tag=&criticality=&q=`
  - `GET /ui/fragments/board-row/<run_id>`, which returns one `<tr>`, or 404 if the run is not on that date or no longer matches the filters
- Create: `store/duration_reads.py` (or an `AlertReads`-style mixin) with `Store.recent_durations_for(jobs: Collection[str], limit: int = 10) -> dict[str, list[timedelta]]`, a single locked pass. Keep `store.py` ≤ 400 lines.
- Modify: `ui/static/app.js`:
  - on a `run` event for the shown date, fetch the row fragment and replace or insert `tr[data-run-id]`, and update the counts by refetching `/ui/fragments/board-counts?date=…`
  - on `reset`, call `location.reload()`
  - action buttons (`data-action`, `data-run`) confirm, then POST `/api/runs/<id>/<action>` (`rerun` asks about downstream)
- The row columns are: job (link), state badge, criticality, scheduled, start, end, duration, vs typical (P3-R9), attempt, and tags. The actions column is shown only when the role allows operator.
- Above the table: counts by state as filter links, a date picker (`<input type=date>` in a GET form), and previous/next day links. The default date is today in the scheduler's time zone.
- Test: `tests/ui/test_board.py`

- [ ] **Step 1: Failing tests.**
  - `test_board_lists_runs_for_date`: the rows match `/api/runs?run_date=` and are ordered per P3-R10 (a critical failure first).
  - `test_filters_and_counts`: state, tag, criticality and q each narrow the rows; the counts reflect the whole day, not the filtered view.
  - `test_prev_next_and_default_date`.
  - `test_viewer_has_no_action_buttons`; `test_operator_actions_use_api` (buttons carry `data-action`/`data-run`; the API call itself is covered by Task 2).
  - `test_row_fragment`: the run's `<tr>` alone; 404 for an unknown run.
  - `test_vs_typical`: a 2× duration is flagged, and a job with no history shows "–".
  - `test_busy_board_is_fast` (Review Focus 4): 500 runs over 120 jobs seeded in the store render in under 1.0 s, with exactly one `recent_durations_for` call (spy).
  - Add `/` and the row fragment to `test_every_page_escapes`.
- [ ] **Step 2: Implement; run tests/ui and the full suite once.**
- [ ] **Step 3: Commit** `feat: add the live job board`.

### Task 6: Job detail (`/jobs/<name>`) and run detail (`/runs/<run_id>`)

**Files:**
- Create: `ui/detail.py`:
  - **Job page:** definition table from `definition_json` (alerts hidden unless operator); next 10 firings (`next_firings`, in a thread); history of the last 30 runs; a duration sparkline (inline SVG, 120×24, built with `h`, last 30 succeeded durations); trigger and backfill buttons for operators.
  - **Run page:** run summary; the definition per P3-R8; attempts table; events table; alerts with their deliveries; a log viewer with stdout/stderr tabs and an attempt picker, where app.js opens `/events/log/...` and appends lines with `textContent`; operator action buttons.
- Create: `ui/sparkline.py`, with `def sparkline(values: Sequence[float], *, width: int = 120, height: int = 24) -> Safe`.
- Modify: `app.js`, adding the log viewer and tabs.
- Test: `tests/ui/test_detail.py`

- [ ] **Step 1: Failing tests.**
  - `test_job_page`: definition, firings, history, sparkline path count; unknown job gives 404.
  - `test_run_page`: attempts, events, alerts and log viewer data attributes; unknown run gives 404.
  - `test_old_config_version_shows_source`: when the run's version differs from the current one, the page shows that version's source text (escaped) and a link to `/admin/configs/<v>`.
  - `test_sparkline`: no values gives an empty SVG; one value gives a flat line; values scale into the box.
  - `test_secrets_redacted`: an `${ENV}` value used in `env` does not appear on the job or run page.
  - Add both pages to `test_every_page_escapes` (a hostile description, reason and log line).
- [ ] **Step 2: Implement; run tests/ui and the full suite once.**
- [ ] **Step 3: Commit** `feat: add job and run detail pages with a live log`.

### Task 7: Dependency graph (`/graph`)

**Files:**
- Create: `ui/graph_layout.py` (pure, no I/O):
  - `@dataclass(frozen=True) class Edge: up: str; down: str; on: str; offset: int`
  - `@dataclass(frozen=True) class Layout: positions: Mapping[str, tuple[int, int]]; paths: Mapping[Edge, str]; width: int; height: int`, where positions are `(layer, order)` and paths are SVG `d` strings
  - `def layout(nodes: Sequence[str], edges: Sequence[Edge], *, sweeps: int = 4) -> Layout` (P3-R11)
  - `NODE_W = 160`, `NODE_H = 36`, `LAYER_GAP = 80`, `ROW_GAP = 16`
- Create: `ui/graph.py`, with `GET /graph?run_date=&tag=&focus=`. Each node is a `<a href>` group holding a rect with a state class and the job name. Edge classes are `on-success`, `on-failure`, `on-done` and `cross-date`. Focus highlights the transitive up- and downstream and dims the rest.
- Test: `tests/ui/test_graph_layout.py`, `tests/ui/test_graph.py`

- [ ] **Step 1: Failing tests.**
  - `test_longest_path_layers`: `a→b→c`, `a→c` puts c on layer 2.
  - `test_barycentre_reduces_crossings`: a known 2-layer crossing case ends with 0 crossings.
  - `test_cross_date_edges_ignored_for_layers`.
  - `test_awkward_graphs` (Review Focus 5): self `-1`; `a→b(-1)` with `b→a`; a disconnected job; a 200-job chain in under 0.5 s; every node is positioned.
  - Page tests: node state classes for the date, a click link to the run (or the job when there is no run), the tag filter, the focus highlight, and dashed cross-date edges.
  - Add `/graph` to `test_every_page_escapes`.
- [ ] **Step 2: Implement; run tests/ui and the full suite once.**
- [ ] **Step 3: Commit** `feat: add the dependency graph`.

### Task 8: Timeline (`/timeline`) and forecast (`/forecast`)

**Files:**
- Create: `ui/timeline.py`, with `GET /timeline?zoom=hour|day|week&date=` (P3-R12). The SVG has a time axis with ticks (every 10 min, 1 h or 1 day by zoom), one lane per job, attempt segments with outcome classes and a `<title>` tooltip (`attempt n: outcome, start–end`), ghost bars, a now line, and zoom/previous/next links.
- Create: `ui/forecast_page.py`, with `GET /forecast?from=&to=` (defaults now to +24 h, maximum 31 days, as in the API). The table lists expected start (local), job, run date, expected duration and via. It uses `forecast()` with `recent_durations_for`, in a thread.
- Store: `Store.attempts_for_runs(run_ids: Collection[str]) -> dict[str, list[Attempt]]`, one locked call, kept in a new read module.
- Test: `tests/ui/test_timeline.py`, `tests/ui/test_forecast_page.py`

- [ ] **Step 1: Failing tests.**
  - `test_segments_per_attempt`: a run with 2 attempts gives 2 segments in its lane, coloured failed then succeeded.
  - `test_running_extends_to_now`.
  - `test_ghost_bars_for_future`.
  - `test_zoom_windows`: hour, day and week bounds in the scheduler's time zone, including the DST day 2026-10-25 in Europe/London (a 25-hour day).
  - `test_window_cap`: week is the largest.
  - `test_forecast_page`: rows match the API forecast; a bad `from` gives 400 HTML.
  - Add both pages to `test_every_page_escapes`.
- [ ] **Step 2: Implement; run tests/ui and the full suite once.**
- [ ] **Step 3: Commit** `feat: add the timeline and forecast pages`.

### Task 9: Admin pages (`/admin`)

**Files:**
- Create: `ui/admin.py`, with these routes (all admin only):
  - `GET /admin`: config versions (newest first, with loaded time and digest prefix), a reload button, and a dead-letters table (alert, notifier, attempts, last error, updated, and a replay button)
  - `GET /admin/configs/<int:version>`: the files' source text, plus a diff against `version-1` per P3-R13
- Store: `Store.config_versions()` and `Store.get_config(version)` (P3-R13), in a read module.
- Modify: `app.js`. The reload button POSTs `/api/reload` and shows the returned problems (as text) or "reloaded". Replay POSTs `/api/alerts/<id>/replay`, then refetches the dead-letters fragment `/ui/fragments/dead-letters`.
- Test: `tests/ui/test_admin.py`

- [ ] **Step 1: Failing tests.**
  - `test_admin_needs_admin`: viewer and operator get 403, admin gets 200.
  - `test_versions_and_diff`: two versions give a diff with `-`/`+` lines; version 1 shows "first version"; an unknown version gives 404.
  - `test_dead_letters_listed`: seeded DEAD deliveries appear with replay buttons.
  - `test_secret_in_source_stays_placeholder`: stored sources hold `${SLACK_URL}`, never the value.
  - Add `/admin` and the config page to `test_every_page_escapes` (hostile source text and `last_error`).
- [ ] **Step 2: Implement; run tests/ui and the full suite once.**
- [ ] **Step 3: Commit** `feat: add the admin pages`.

### Task 10: Docs, seeded demo, README and the release gate

**Files:**
- Create: `docs/concepts/web-ui.md` (the school noticeboard), `live-updates.md` (the radio with numbered messages, and why ids and replay), `graph-layout.md` (seating plan layers and barycentres), and `timelines.md` (the train timetable with ghost trains). Add them to the nav and index.
- Create: `src/pyscheduler/demo.py` with `def seed_demo(folder: Path) -> Path` (returns the config path) and `def serve_demo(folder: Path, *, seconds: float | None = None, ready: Callable[[str], None] | None = None) -> None`; `examples/ui_demo.py` is a thin wrapper around them. The seeded UI demo:
  - In a temporary or given folder it writes a config with about 12 jobs (chains, a watcher, a flaky job, a critical failure), a `users.yaml` with `admin`/`demo` (hash made at runtime), and seeds a few days of runs by triggering with real `true`/`false`/`sleep` commands.
  - It then serves until Ctrl-C and prints the URL and login.
  - `--seed-only` exits after seeding, which the test uses.
  - `seconds` stops `serve_demo` after that long (used by PyStack's test in Task 11).
- Modify: the README (Phase 3 section, roadmap v0.3 done, a screenshot-free description) and `docs/concepts/http-api.md` (auth modes, `users_file`, CSRF header).
- Test: `tests/test_ui_demo.py` (`--seed-only` in tmp gives a state with runs on ≥ 2 dates, in under 8 s).
- [ ] **Step 1: Write the failing test, then the demo and docs; make it pass.**
- [ ] **Step 2: Gate.**
  - `uv run pytest` three times; if `test_two_hundred_jobs` is the only failure, re-run it alone and report the load.
  - `ruff check`, `ruff format --check`, `pyright src tests`, `mkdocs build --strict`.
  - A wheel check: `uv build`, then confirm the wheel contains `pyscheduler/ui/static/app.css` and `pyscheduler/ui/templates/page.html`.
- [ ] **Step 3: Commit** `docs: add the phase 3 docs and the UI demo`.

### Task 11: PyStack `schedweb` (after the v0.3.0 tag)

Done by the controller after merge and tag, in a PyStack worktree:
- Bump pyscheduler to v0.3.0 (pyweb v0.3.0 is already bumped by Task 1).
- Add a `schedweb` command to `PySchedulerPlugin`. It calls `pyscheduler.demo.seed_demo` and `serve_demo` (Task 10), serving until Ctrl-C. An optional argument `--seconds N` stops it after N seconds, which tests use.
- Add the test `tests/test_schedweb.py`. It runs `schedweb --seconds 2` in a thread, fetches `/login` and gets 200, and checks the output names the URL and the login.
- Update the README and index tables.
- PR, then CI green, then merge.
