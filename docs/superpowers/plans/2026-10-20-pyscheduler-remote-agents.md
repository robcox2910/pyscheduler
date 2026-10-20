# PyScheduler Phase 4 (Remote Agents) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Jobs can run on remote `pysched-agent` worker processes. Agents and the scheduler talk over a pymq broker using signed messages. The scheduler watches agent heartbeats and makes remote attempts `lost` when an agent stays gone. Remote logs can be fetched, and an agents page shows each agent's status.

**Architecture:**
- **Broker.** The scheduler embeds a pymq `BrokerServer`, or uses an external broker. Both sides talk to it as `BrokerClient`s on the pynet loop.
- **Hub.** An `AgentHub` in the scheduler signs and dispatches work to `agent.<name>.work`. It consumes `scheduler.reports`, keeps an `AgentBook` of who is online and how busy each agent is, and resolves `RemoteHandle`s (the engine's attempt handle for remote runs) from reports.
- **Placement.** The engine picks an agent at admission time through a placement hook. Runs for offline or full agents wait in `queued` with a reason.
- **Agent.** The agent runs each job through the same minder and keeps undelivered `finished` reports in its own journalled PyDB until the scheduler acks them.

**Tech Stack:**
- Python 3.14
- pymq v0.4.1 (`BrokerServer`, `BrokerClient`, `Delivery`, `QueueConfig`)
- pynet v0.1.1, pydb v0.4.1 (agent journal), pyweb v0.3.1
- stdlib `hmac`, `hashlib`, `secrets`

**Spec:** `docs/superpowers/specs/2026-10-17-pyscheduler-design.md`, the "Phase 4 — Remote agents" section, plus the phase 4 lines in the Docs, PyStack and Testing sections.

## Global Constraints

- Python ≥ 3.14; uv; ruff (repo rules); pyright strict; coverage ≥ 80 %; `--timeout=10` per test.
- No `TYPE_CHECKING`, no `from __future__ import annotations`, no `# noqa` or `# type: ignore` without a commented per-file reason in pyproject (as the repo does).
- Files stay under about 400 lines. `engine/base.py` (399), `engine/recovery.py` (371) and `service.py` (379) are at the limit, so new code goes in new modules.
- Docstrings everywhere (imperative, Google style), tests included. Use named constants.
- Runtime dependencies are only pynet, pymq, pyweb and pydb plus the stdlib.
- Everything runs on the **pynet loop** (`pynet.loop` primitives; blocking work in `run_in_thread`).
- Exact names:
  - queues `agent.<name>.work` and `scheduler.reports`
  - report types `hello`, `started`, `heartbeat`, `finished`, `log_reply`
  - scheduler message types `work`, `kill`, `finished_ack`, `log_request`
- Defaults:
  - heartbeat every `10s` (`HEARTBEAT_EVERY`)
  - `agent_grace: 60s`
  - `lost_after: 10m`
  - message clock skew allowed `5m` (`MAX_SKEW`)
  - nonce memory `10m` (`NONCE_TTL`)
  - log request timeout `10s`
  - agent `--max-running` default 4
  - embedded broker listen default `127.0.0.1:8471`
- Signing: HMAC-SHA256 with the agent's shared secret over the canonical JSON of the body (`json.dumps(body, sort_keys=True, separators=(",", ":"))`). The hex digest goes in header `sig`. The body carries `type`, `agent`, `sent_at` (ISO UTC) and `nonce` (16 random bytes as hex).
  - The scheduler rejects reports that are unsigned, badly signed, stale (more than `MAX_SKEW` off), replayed (nonce seen) or from an unknown agent.
  - The agent rejects work that fails the same checks.
  - Rejections are logged without the secret or the payload, and never acted on.
- Names: agent names follow the job-name rule (`check_name`, 1–64 of `[A-Za-z0-9_-]`), and so do agent tags.
- `${ENV}` values never appear in logs, the API or HTML. Work payloads carry the rendered command and env (operators' secrets). The docs say plainly that the broker has no auth or encryption, so plain TCP needs a trusted network or a tunnel. The broker binds to loopback unless configured otherwise.
- Commits are conventional and end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Concept pages: `remote-agents`, `heartbeats` and `signed-messages`. Each is kid-friendly, with an analogy and a runnable snippet, and is added to the nav and the index.

## Rulings (where this plan fills gaps in the spec)

- **P4-R1 Broker topology.**
  - `agents: listen: host:port` embeds a `BrokerServer` in the scheduler (default `127.0.0.1:8471`).
  - `agents: broker: host:port` uses an external broker instead. Exactly one of the two is allowed, and with neither set agents are off.
  - The scheduler always talks to the broker through a `BrokerClient`, to its own embedded server or to the external one, so one code path covers both.
- **P4-R2 Agent secrets.**
  - `agents: secrets_file: agents.yaml` is a yamlish list of `{name, secret}`. The name is the agent name and the secret is at least 32 characters.
  - The file is reloaded on reload, like the tokens file, and is all-or-nothing.
  - A warning is printed if it is group- or world-readable.
  - A job's `agent:` must name an agent in this file (validation error otherwise).
  - `agent_tags` entries must pass `check_name`.
  - `agent` and `agent_tags` together is an error.
- **P4-R3 Queues.**
  - Work queues and `scheduler.reports` are declared with `QueueConfig(max_attempts=1000, ack_timeout=30.0)`, because client disconnects count as nacks.
  - The agent acks a work delivery as soon as it has recorded the dispatch in its journal, not when the job ends.
- **P4-R4 Idempotent dispatch.**
  - Work message ids are `f"{run_id}.{attempt}"`.
  - The agent's journal keeps every `(run_id, attempt)` it has accepted. A repeat dispatch only re-sends `started`, and the `finished` report if the attempt is already done.
  - The hub re-puts the same work every `agent_grace` until `started` arrives, which covers a broker restart losing the queue. If nothing arrives within `lost_after`, the attempt is lost.
- **P4-R5 Remote attempt rows.**
  - A remote attempt's row has `status_path = f"agent:{name}"` (recognised by recovery; no schema change) and `pid=None`.
  - `stdout_path` and `stderr_path` are the agent-side paths reported in `started`.
  - The `GET /api/runs/<id>/log` JSON reports `path` as `f"{name}:{agent_path}"` for remote attempts.
- **P4-R6 Work payload.**
  - The payload has `run_id`, `attempt`, `job`, `run_date`, `argv` (rendered), `cwd` (rendered or null) and `env`. The `env` holds only the job's rendered env plus the `PYSCHED_*` variables; the scheduler's `os.environ` is never sent, and the agent merges its own environment underneath.
  - Log paths are chosen by the agent: `{agent log_dir}/{job}/{run_date}/{run_id}.{attempt}.out` and `.err`. The job's `stdout`/`stderr` templates apply only to local jobs.
  - Timeouts stay on the scheduler, which sends signed `kill` messages (`{run_id, attempt, signal: "TERM"|"KILL"}`).
- **P4-R7 Placement.**
  - At admission, a run whose job has `agent` or `agent_tags` needs one online agent with free capacity.
  - For `agent_tags`, a candidate must have every tag. Candidates are ordered by fewest running attempts, then by name.
  - The chosen agent is held as a slot until the attempt ends. Global caps and pools still apply.
  - When no agent fits, the run stays `queued` with a reason:
    - `"waiting for agent build-01 (offline)"`
    - `"waiting for agent build-01 (busy)"`
    - `"waiting for an agent with tags gpu, linux"`
  - Queued runs are re-checked whenever an agent comes online or frees a slot.
- **P4-R8 Liveness.**
  - An agent is online after a valid `hello` or `heartbeat`, and offline once no valid report has arrived for `agent_grace`.
  - The `RemoteHandle`s of an offline agent resolve as lost after `lost_after` more, counted from when it went offline, unless a report for them arrives first.
  - After a scheduler restart, adopted remote attempts start their `lost_after` clock at restart if the agent has not reported.
  - All timing goes through the engine `Clock`.
- **P4-R9 Logs.**
  - `log_request {request_id, run_id, attempt, stream, mode: "tail"|"read", count|offset, limit}` returns `log_reply {request_id, lines|data_b64, offset, shrank, missing}`.
  - The hub keeps a future per `request_id` (16 hex), with a 10 s timeout. A missing file gives `missing: true`.
  - The API log route and the live log tail use a `LogSource` abstraction: local files as today, or the hub for remote attempts. An offline agent ends the tail with `end` and the message "the agent is offline".
- **P4-R10 Agent durability.** The agent's PyDB journal (`--state-dir`, `Database(journal=True)`) has two tables:
  - `accepted`, with `run_id`, `attempt`, `state` (running or finished) and `finished` (JSON or null);
  - `pending`, holding finished reports that have not been acked.
  - On every reconnect, and every `HEARTBEAT_EVERY`, the agent re-sends the pending reports until `finished_ack` arrives.

## Review Focus

1. **An agent loses its connection mid-run and reconnects after the job has finished.** The outcome arrives once and the run succeeds, not `lost`, if it is within `lost_after` (Task 5, `test_reconnect_reports_finish`).
2. **The scheduler restarts while a remote job runs.** After the restart the run is adopted, finishes from the agent's report, and never runs twice (Task 5, `test_restart_adopts_remote_run`).
3. **Forged, replayed, stale or unsigned messages.** A message signed with the wrong secret, a timestamp 10 minutes old, a nonce seen twice, or no `sig` is rejected and logged. Nothing is ever acted on, on either side (Task 1, `test_verify_rejects`; Task 3, `test_agent_rejects_forged_work`; Task 4, `test_hub_rejects_forged_reports`).
4. **A job whose tags no online agent has.** It stays `queued` with a clear reason and starts as soon as a matching agent connects (Task 5, `test_waits_for_tagged_agent`).
5. **Lost work after a broker restart.** The embedded broker is lost when the scheduler restarts, and work was dispatched but not started. It is re-dispatched and runs exactly once (Task 3, `test_duplicate_dispatch_runs_once`; Task 4, `test_redispatch_until_started`).

---

### Task 1: Signed messages and the agents config

**Files:**
- Create: `src/pyscheduler/agents/__init__.py`, `agents/signing.py`, `agents/messages.py` (type constants, payload builders and parsers).
- Create: `src/pyscheduler/config/agent_fields.py`, holding `read_agents_section(r, main) -> AgentsDef | None` and `AgentSecrets.load(path)` (yamlish, errors give file:line, `AgentSecretsError`).
- Modify:
  - `config/model.py`: `AgentsDef(listen: tuple[str, int] | None, broker: tuple[str, int] | None, secrets_file: Path, agent_grace: timedelta, lost_after: timedelta, source)` and `Config.agents: AgentsDef | None = None`.
  - `config/loader.py`: add `agents` to `MAIN_KEYS` only.
  - `config/validate.py`: replace the phase 1 refusal with a call to a new `config/validate_agents.py`, which checks agent names against the secrets file, tags with `check_name`, rejects `agent` plus `agent_tags`, and rejects agent jobs when no `agents:` section exists.
- Test: `tests/agents/test_signing.py`, `tests/config/test_agent_config.py`

**Interfaces (produces):**
- `sign(body: Mapping[str, JsonValue], secret: str) -> str`, returning the hex digest.
- `seal(kind: str, agent: str, payload: Mapping[str, JsonValue], secret: str, *, now: datetime) -> tuple[dict[str, JsonValue], dict[str, str]]`, returning the body and the headers (with `sig`).
- `class Verifier`:
  - `__init__(self, secrets: Callable[[str], str | None], *, clock: Clock)`
  - `check(self, body: JsonValue, headers: Mapping[str, str]) -> Message`, which raises `Rejected(reason)` on failure.
  - It keeps a nonce memory pruned after `NONCE_TTL`, and compares digests with `hmac.compare_digest`.
- `Message(kind: str, agent: str, sent_at: datetime, payload: Mapping[str, JsonValue])`.
- Constants `MAX_SKEW`, `NONCE_TTL`, `HEARTBEAT_EVERY`, and the queue and type names from Global Constraints.

- [ ] **Step 1: Failing tests.**
  - `test_round_trip`: a sealed message verifies, and its kind, agent and payload are unchanged.
  - `test_verify_rejects`, table-driven, each case raising `Rejected` with the reason shown: wrong secret, a tampered payload, a missing `sig`, `sent_at` 10 minutes ago, `sent_at` 10 minutes ahead, the same nonce twice, an unknown agent, and a body that is not an object.
  - `test_nonce_memory_pruned`: after `NONCE_TTL` the nonce set no longer grows (the fake clock advances past it).
  - Config tests:
    - `agents: {listen: "127.0.0.1:8471", secrets_file: agents.yaml}` loads with the default grace and lost_after;
    - `listen` and `broker` together is an error;
    - a job naming an unknown agent is an error;
    - a bad tag is an error;
    - `agent` plus `agent_tags` is an error;
    - an agent job with no `agents:` section is an error;
    - a secrets entry shorter than 32 characters is an error at file:line;
    - a group-readable secrets file gives a warning;
    - the secret value never appears in any `Problem` message.
- [ ] **Step 2: Implement; run tests/agents, tests/config and the full suite once.**
- [ ] **Step 3: Commit** `feat: sign agent messages and read the agents config`.

### Task 2: The agent book — who is online, how busy, and who fits

**Files:**
- Create: `src/pyscheduler/agents/book.py`
- Test: `tests/agents/test_book.py`

**Interfaces (produces):**
- `@dataclass AgentState(name, tags: frozenset[str], max_running: int, running: set[tuple[str, int]], online: bool, last_seen: datetime | None, went_offline: datetime | None)`.
- `class AgentBook`, constructed as `__init__(self, *, clock: Clock, grace: timedelta)`. Methods:
  - `heard(self, name: str, *, tags: Iterable[str] | None = None, max_running: int | None = None, running: Iterable[tuple[str, int]] | None = None) -> None`: marks the agent online and arms the offline timer through `clock.call_at`.
  - `choose(self, *, agent: str | None, tags: frozenset[str]) -> str | Refusal`: applies P4-R7, where `Refusal(reason: str)` carries the exact queued-reason text.
  - `reserve(self, name: str, key: tuple[str, int]) -> None`, and `release(self, name: str, key: tuple[str, int]) -> None`.
  - `states(self) -> list[AgentState]`.
  - `watch(self, callback: Callable[[AgentState], None]) -> Callable[[], None]`: notified when an agent's online state or load changes.
  - `close(self) -> None`: cancels the timers.

- [ ] **Step 1: Failing tests.**
  - `test_heartbeat_keeps_online_and_grace_makes_offline`: heartbeats at +0 s and +50 s keep it online; with nothing more it goes offline at +110 s and `went_offline` is set.
  - `test_choose_by_name`: an online agent with a free slot is chosen; at `max_running` the refusal is "busy"; offline gives "offline".
  - `test_choose_by_tags_least_busy_then_name`: three agents, two with the tags; the less busy one wins, and ties go by name. With no agent having all the tags, the refusal is `"waiting for an agent with tags gpu, linux"`, with the tags sorted.
  - `test_watch_notifies_on_change`.
- [ ] **Step 2: Implement; run the tests.**
- [ ] **Step 3: Commit** `feat: track agents, their load and who fits`.

### Task 3: `pysched-agent`

**Files:**
- Create: `src/pyscheduler/agents/worker.py` (the agent loop), `agents/journal.py` (PyDB journal per P4-R10) and `agents/cli.py` (`main(argv) -> int`).
- Modify: `pyproject.toml`, with `[project.scripts] pysched-agent = "pyscheduler.agents.cli:main"`.
- Test: `tests/agents/test_journal.py`, `tests/agents/test_worker.py` (a real `BrokerServer` on port 0, a real `BrokerClient` playing the scheduler, and real `sh -c` jobs through the minder).

**Interfaces:**
- CLI: `pysched-agent --broker host:port --name build-01 [--tags linux,gpu] --secret-file PATH [--max-running 4] [--state-dir DIR] [--log-dir DIR]`. The state and log dirs default to `./agent-state/<name>` and `./agent-logs/<name>`. A bad secret file or name exits 2 with a message, and the secret is never printed.
- `class Agent`:
  - `__init__(self, *, name, tags, secret, broker: tuple[str, int], state_dir: Path, log_dir: Path, max_running: int, clock: Clock)`
  - `async start()`: opens the journal, connects, declares `agent.<name>.work`, consumes it, sends `hello {tags, max_running, running}`, and starts the heartbeat loop (`heartbeat {running}` every `HEARTBEAT_EVERY`).
  - `async stop()`: running minders are left running, and they are adopted from the journal and the status files on the next start.
- Behaviour:
  - **Work:**
    1. Verify the message.
    2. Journal it as accepted, idempotently (P4-R4).
    3. Ack the delivery.
    4. Start the minder with the reused `MinderLauncher`, `cwd=` and `env={**os.environ, **payload env}`.
    5. Send `started {run_id, attempt, stdout_path, stderr_path}`.
    6. When the minder ends, journal `finished` with the result fields of `AttemptResult` (ISO times) and send it.
  - **`finished_ack`:** drop the report from `pending`.
  - **`kill`:** signal the attempt's minder group with TERM or KILL.
  - **`log_request`:** read the file on a thread with the existing `read_tail`/`read_from` helpers, then reply.
  - **Reconnect** (`BrokerClient.on_event` RECONNECTED): re-send `hello` and every pending report.
  - **Restart:** accepted-but-running attempts whose status file is already written become finished reports; those whose minder is still alive are adopted with `AdoptedHandle`; the rest are reported finished with `start_error "agent restarted; minder gone"`.

- [ ] **Step 1: Failing tests.**
  - `test_runs_work_and_reports`: signed work for `echo hi` gives a `started` report, then a `finished` report with exit_code 0; the stdout file holds `hi`.
  - `test_agent_rejects_forged_work`: wrong secret or a stale message runs nothing, sends nothing and logs a rejection.
  - `test_duplicate_dispatch_runs_once`: the same work id is sent twice, including after the dedupe window, by bypassing it with a fresh broker. The command runs exactly once (a counter file), and the second dispatch only re-sends `started`/`finished`.
  - `test_pending_until_acked`: `finished` is re-sent after a reconnect until `finished_ack`, then never again.
  - `test_kill`: a `kill TERM` ends a `sleep 30` job with signal 15.
  - `test_log_request_tail_and_read`.
  - `test_restart_reports_finished_from_status_file`.
  - `test_cli_bad_secret_file` exits 2.
- [ ] **Step 2: Implement; run tests/agents and the full suite once.**
- [ ] **Step 3: Commit** `feat: add pysched-agent`.

### Task 4: The scheduler's agent hub

**Files:**
- Create: `src/pyscheduler/agents/hub.py`, plus `agents/remote_handle.py` if `hub.py` would pass about 400 lines.
- Test: `tests/agents/test_hub.py` (a real broker, plus a scripted fake agent built on Task 1's `seal`).

**Interfaces:**
- `class AgentHub`:
  - `__init__(self, config: Config, *, clock: Clock, secrets: AgentSecrets)`
  - `async start()`: starts the embedded `BrokerServer` if `listen` is set, connects a `BrokerClient`, declares `scheduler.reports` and consumes it.
  - `async stop()`.
  - `book: AgentBook`.
  - `set_secrets(secrets)`, used on reload.
  - `dispatch(self, agent: str, payload: Mapping[str, JsonValue]) -> RemoteHandle`: signs and puts the work with id `f"{run_id}.{attempt}"`, and re-puts it every `agent_grace` until `started` (P4-R4).
  - `adopt(self, agent: str, run_id: str, attempt: int, started_at: datetime) -> RemoteHandle`, used by recovery.
  - `async fetch_log(self, agent, run_id, attempt, stream, *, mode, count=None, offset=None, limit=None) -> LogReply`, which raises `AgentOffline` or `TimeoutError`.
  - `watch_started(callback)`, so the engine can record `stdout_path`/`stderr_path` in the attempt row.
- `class RemoteHandle` implements `AttemptHandle`:
  - `pid` is always None.
  - `async wait() -> AttemptResult` resolves from a `finished` report. A lost attempt gives `AttemptResult(None, None, None, now, None)`, with `lost` set to True.
  - `terminate()` and `kill()` are sync and send a signed `kill` on a spawned task.
  - It exposes `lost: bool` and `stdout_path`/`stderr_path`, which are filled in from `started`.
- Report handling:
  - `hello` and `heartbeat` go to `book.heard`.
  - `started` goes to the handle.
  - `finished` resolves the handle, then sends `finished_ack`. Duplicates are acked again and ignored.
  - `log_reply` resolves the waiting future.
  - Any report also counts as heard for liveness.
  - Rejected reports are logged and acked (dropped).
- Liveness: when the book marks an agent offline, its handles arm `lost_after` timers. A report for a handle cancels its timer (P4-R8).

- [ ] **Step 1: Failing tests.**
  - `test_dispatch_and_finish`: the fake agent replies `started` then `finished`, and `wait()` gives exit_code 0. `finished_ack` is sent once.
  - `test_hub_rejects_forged_reports`: a forged `finished` does not resolve the handle.
  - `test_redispatch_until_started`: the fake agent ignores the first two puts. The work is re-put at each `agent_grace` (fake clock) with the same id, then `started` stops the re-puts.
  - `test_offline_then_lost`: no heartbeats, then offline after the grace, then the handle is lost after `lost_after`.
  - `test_reconnect_before_lost_cancels`: a `finished` report after going offline but before `lost_after` resolves normally.
  - `test_fetch_log_timeout_and_offline`.
  - `test_embedded_broker_binds_loopback_by_default`.
- [ ] **Step 2: Implement; run tests/agents and the full suite once.**
- [ ] **Step 3: Commit** `feat: add the scheduler's agent hub`.

### Task 5: Remote runs in the engine

**Files:**
- Create: `src/pyscheduler/engine/remote.py`, holding the `Placement` protocol, the remote branch of `_start_attempt`, recovery adoption for `agent:` rows, and the queued-reason updates.
- Modify:
  - `engine/admission.py`: `ReadyQueue._try` consults an optional `placement`. When it refuses, the run stays queued and its reason is set via a callback.
  - `engine/attempts.py`: a remote branch beside the watcher branch, calling into `engine/remote.py`.
  - `engine/recovery.py`: an `agent:` status_path goes to `hub.adopt`, with a slot adopted for that agent.
  - `engine/core.py`: `Engine(..., placement: Placement | None = None)`.
  - Keep `base.py` ≤ 400 lines.
- Test: `tests/engine/test_remote.py`, using a `FakeHub` in `src/pyscheduler/testing.py` that implements the hub's dispatch/adopt/book surface with fake-clock control.

**Interfaces:**
- `class Placement(Protocol)`:
  - `choose(self, job: JobDef) -> str | Refusal`
  - `reserve(self, agent, key)`
  - `release(self, agent, key)`
  - `watch(self, callback: Callable[[], None]) -> Callable[[], None]`, called when capacity may have freed.
- `AgentHub` satisfies `Placement` through `hub.book` plus `dispatch`/`adopt`.
- The engine records `status_path=f"agent:{name}"` at start, and the agent-side log paths once `started` arrives (attempt-row update).
- Kill, timeout and operator actions go through the `RemoteHandle`, unchanged.

- [ ] **Step 1: Failing tests.**
  - `test_remote_run_succeeds`: an `agent: build-01` job runs through the FakeHub; the attempt row has `status_path == "agent:build-01"`, and the run is `succeeded`.
  - `test_waits_for_tagged_agent`: an `agent_tags: [gpu]` job with no gpu agent is `queued` with reason `"waiting for an agent with tags gpu"`. A gpu agent connects and the run starts.
  - `test_busy_agent_queues_then_starts`.
  - `test_global_cap_still_applies`.
  - `test_timeout_sends_kill`.
  - `test_reconnect_reports_finish`: offline, then a report inside `lost_after`, gives `succeeded`.
  - `test_lost_after`: no report after offline plus `lost_after` gives `failed` with reason `lost`, retried if retries remain.
  - `test_restart_adopts_remote_run`: stop the engine while running, start a new engine with the same store; the hub adopts, a report finishes it, and the command was dispatched exactly once.
  - `test_slot_released_on_every_path`.
- [ ] **Step 2: Implement; run tests/engine and the full suite once.**
- [ ] **Step 3: Commit** `feat: run jobs on remote agents`.

### Task 6: Remote logs

**Files:**
- Create: `src/pyscheduler/agents/logsource.py`, with `class LogSource(Protocol)` and two implementations:
  - `LocalLogSource(path)`, wrapping today's `read_tail`/`read_from`/`_first_look` helpers;
  - `RemoteLogSource(hub, agent, run_id, attempt, stream)`.
  - Plus `log_source_for(engine, attempt_row, stream) -> LogSource`.
- Modify: `api/runs.py` (`show_log` uses the source; the remote `path` per P4-R5) and `ui/logtail.py` (the tail polls through the source; an offline agent ends it with "the agent is offline").
- Test: `tests/agents/test_logsource.py`, `tests/api/test_remote_log.py`, `tests/ui/test_remote_logtail.py`

- [ ] **Step 1: Failing tests:**
  - the API tail of a remote attempt returns its lines;
  - an offline agent gives a 503 `{"error": "the agent is offline"}`;
  - the live tail streams remote lines and ends on `finished`;
  - local logs are unchanged (the existing tests still pass).
- [ ] **Step 2: Implement; run tests/api, tests/ui and the full suite once.**
- [ ] **Step 3: Commit** `feat: fetch remote logs over the broker`.

### Task 7: Service wiring, API, UI agents page and CLI

**Files:**
- Modify:
  - `service.py`, or a new `service_agents.py` to stay under 400 lines:
    - build the hub before `Engine` (placement) when `config.agents` is set, start it before `_start(engine)`, and stop it after `engine.stop()`;
    - on reload, swap the secrets via `config_actions`; the `listen`/`broker` settings need a restart, and a change to them fails the reload with a problem.
- Create:
  - `api/agents.py`: `GET /api/agents` returns `{"agents": [{name, online, tags, max_running, running, last_seen}]}`, viewer and up.
  - `ui/agents_page.py`: `/agents` shows a table of status badge, tags, load `running/max_running`, last heartbeat (local time) and running attempts (links). An `agent` live event refreshes a row fragment `/ui/fragments/agent-row/<name>`.
  - A NAV entry `("agents", "/agents", "Agents", "viewer")`.
- Modify: `ui/live.py`, adding an `attach_agents(book)` that publishes `agent` events `{name, online, running}`.
- Modify: `client.py` and the CLI, adding a `pysched agents` table.
- Modify: the board and run pages, which show the agent name on remote runs.
- Test: `tests/agents/test_service_agents.py`, a real `serve` with an embedded broker and a real `Agent` in the same loop. A job runs remotely and `/api/agents` lists the agent online. Also `tests/ui/test_agents_page.py` (and add the page to `test_every_page_escapes`) and `tests/test_cli.py` (`pysched agents`).

- [ ] **Step 1: Failing tests** as listed.
- [ ] **Step 2: Implement; run the full suite once.**
- [ ] **Step 3: Commit** `feat: wire agents into the service, API, UI and CLI`.

### Task 8: Docs, a two-agent demo and the release gate

**Files:**
- Create:
  - `docs/concepts/remote-agents.md`, the delivery drivers;
  - `docs/concepts/heartbeats.md`, the walkie-talkie check-in;
  - `docs/concepts/signed-messages.md`, the wax seal and the date stamp.

  Each states what the broker does not protect (no auth, no encryption; use loopback, a trusted network or a tunnel). Add them to the nav and the index.
- Create: `src/pyscheduler/demo_agents.py`, with `run_agents_demo(folder: Path, *, seconds: float | None = None, ready=None) -> None`, and a thin wrapper `examples/agents_demo.py`.
  - It writes a config with an embedded broker, two agents (`build-01` tagged `linux`, `gpu-01` tagged `linux,gpu`) and their secrets.
  - It starts the scheduler and both agents in-process (one loop, main thread).
  - It runs a pinned job and a tagged job, and prints which agent ran each.
- Modify: the README (Phase 4 section, roadmap v0.4 done) and `docs/concepts/config.md` (the `agents:` section).
- Test: `tests/test_agents_demo.py`. Both jobs succeed on the expected agents within 8 s.
- [ ] **Step 1: Write the test, then the demo and docs; make it pass.**
- [ ] **Step 2: Gate.** `uv run pytest` three times (if only known load flakes fail, record `uptime`), `ruff check`, `ruff format --check`, `pyright src tests`, `mkdocs build --strict`, `uv build`, and check that the wheel has the `pysched-agent` entry point.
- [ ] **Step 3: Commit** `docs: add the phase 4 docs and the agents demo`.

### Task 9: PyStack `schedagents` (after the v0.4.0 tag)

Done by the controller in a PyStack worktree:
- Bump pyscheduler to v0.4.0.
- Add a `schedagents` command that calls `pyscheduler.demo_agents.run_agents_demo(tmp, seconds=…)` on the main thread and returns its output.
- Add `tests/test_schedagents.py`, which asserts both jobs ran on the expected agents and the run took under 10 s.
- Update the README and index tables.
- PR, CI green, merge.
