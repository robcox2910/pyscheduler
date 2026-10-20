"""Run one attempt of a run: start it, watch it, and decide what happens next.

An attempt is one go on the trampoline. Before a kid climbs on, the
teacher writes "on the trampoline" in the register (durable), and only
then blows the whistle (launch). A stopwatch runs (the timeout): when it
rings the kid is asked to get off (SIGTERM), and if they ignore it, they
are lifted off (SIGKILL) a little later.

When the attempt ends, its outcome decides the run's fate: succeeded,
another try after a growing wait (``retry_wait``), or failed. Either way
the slot is given back and the next queued runs are started.

A file watcher's attempt has no process: a ``WatcherHandle`` stands in
for it (see ``watch_runs``), and its found files go in the final commit.
A remote attempt is sent to an agent instead of launched, and a
``RemoteHandle`` follows it (see ``remote``).
"""

import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from pyscheduler.agents.messages import WorkOrder
from pyscheduler.agents.remote_handle import RemoteHandle
from pyscheduler.config import JobDef
from pyscheduler.engine.base import EngineFailedError
from pyscheduler.engine.launcher import (
    CANCELLED_MESSAGE,
    AttemptHandle,
    AttemptResult,
    LaunchSpec,
)
from pyscheduler.engine.model import Attempt, Outcome, Run, RunState
from pyscheduler.engine.outcomes import LOST, Watch, classify
from pyscheduler.engine.remote import SHELL, RemoteRuns, is_remote
from pyscheduler.engine.watcher import WatcherHandle
from pyscheduler.templates import render

MERGED = "stdout"
"""The ``stderr`` value that sends standard error to the stdout file."""

_NO_RETRY = frozenset({Outcome.SUCCEEDED, Outcome.KILLED})

__all__ = ["LOST", "MERGED", "SHELL", "AttemptRunner", "Watch", "classify"]


type _Spec = LaunchSpec | WatcherHandle | WorkOrder | None
"""What starts an attempt: a launch spec, a watcher, a work order for an agent, or nothing."""


def _log_paths(spec: _Spec) -> tuple[str, str]:
    """Return the stdout and stderr paths to record for an attempt (``""`` for none yet)."""
    if isinstance(spec, WatcherHandle):
        return str(spec.log_path), ""
    if spec is None or isinstance(spec, WorkOrder):  # an agent reports its paths later
        return "", ""
    return str(spec.stdout_path), str(spec.stderr_path or spec.stdout_path)


class AttemptRunner(RemoteRuns):
    """Start attempts, arm their timeouts, and turn their endings into run states."""

    def _start_attempt(self, run: Run) -> bool:
        """Mark ``run`` running (in memory now), then commit and launch it in a task.

        The slots were already acquired by the ready queue. Return False
        (with the slots given back) if the run's job is gone.
        """
        job = self.config.jobs.get(run.job)
        if job is None:  # removed by a reload while queued: nothing to start
            self._release(run.run_id)
            return False
        now = self.clock.now()
        number = run.attempt + 1
        running = replace(
            run,
            state=RunState.RUNNING,
            reason="",
            attempt=number,
            config_version=self.config_version,
            updated_at=now,
        )
        spec: _Spec = None
        error: str | None = None
        try:
            if job.watch is not None:
                log = self._watch_log(running, job)
                spec = self._watcher(running, job, job.watch, now, log)
            elif is_remote(job):
                spec = self._work_order(running, job)
            else:
                spec = self._launch_spec(running, job)
        except (KeyError, ValueError) as exc:
            error = f"bad template: {exc}"
        stdout, stderr = _log_paths(spec)
        row = Attempt(
            run_id=run.run_id,
            attempt=number,
            started_at=now,
            ended_at=None,
            exit_code=None,
            signal=None,
            outcome=None,
            pid=None,
            stdout_path=stdout,
            stderr_path=stderr,
            status_path=self._status_text(run.run_id, number, job),
            reason="",
        )
        self.book.put(running)
        task = self._spawn(self._attempt(running, row, spec, error), f"attempt-{run.run_id}")
        self._attempt_tasks[run.run_id] = task
        return True

    def _status_path(self, run_id: str, attempt: int) -> Path:
        """Return where the minder writes the status note of this attempt."""
        return self.config.settings.state_dir / "status" / f"{run_id}.{attempt}.json"

    def _status_text(self, run_id: str, attempt: int, job: JobDef) -> str:
        """Return the attempt row's status path: none for a watcher, ``agent:<name>`` if remote."""
        if job.watch is not None:
            return ""
        return self._remote_status(run_id) or str(self._status_path(run_id, attempt))

    def _launch_spec(self, run: Run, job: JobDef) -> LaunchSpec:
        """Render the command and log paths of ``run``'s current attempt.

        Raises:
            KeyError: If a template names an unknown value.
            ValueError: If a template is malformed.

        """
        values = self._values(run, job)
        argv, cwd, env = self._command(run, job)
        stderr = None if job.stderr == MERGED else Path(render(job.stderr, values))
        return LaunchSpec(
            run_id=run.run_id,
            attempt=run.attempt,
            argv=argv,
            cwd=cwd,
            env={**os.environ, **env},
            stdout_path=Path(render(job.stdout, values)),
            stderr_path=stderr,
            status_path=self._status_path(run.run_id, run.attempt),
            closing=self.closing,
        )

    async def _attempt(
        self,
        running: Run,
        row: Attempt,
        spec: _Spec,
        error: str | None,
    ) -> None:
        """Commit ``running``, launch, commit the pid, wait, then record the ending.

        If the pid commit fails, the engine has failed stop, but the
        process is already running: the attempt stays tracked (task and
        handle) until the process ends, so ``stop(kill_running=True)`` can
        still kill it. Its ending is not recorded; recovery sorts it out.
        """
        task = self._attempt_tasks.get(running.run_id)
        if self.book.get(running.run_id) is not running:  # changed before our turn came
            self._attempt_tasks.pop(running.run_id, None)
            self._release(running.run_id)
            self._pump()
            return
        started = self._event(running.run_id, "attempt_started", attempt=row.attempt)
        await self._transition([running], attempts=[row], events=[started])
        handle: AttemptHandle | None = spec if isinstance(spec, WatcherHandle) else None
        if isinstance(spec, LaunchSpec):
            try:
                handle = await self.launcher.launch(spec)
            except (OSError, ValueError, TypeError) as exc:  # Popen refuses a NUL with ValueError
                error = str(exc)
        elif isinstance(spec, WorkOrder):
            try:
                handle = self._dispatch(row, spec)
            except (RuntimeError, ValueError) as exc:  # no hub, or the hub refuses it
                error = str(exc)
        if handle is None:
            result = AttemptResult(None, None, None, self.clock.now(), error)
            await self._finish(row, result, Watch())
            return
        if isinstance(handle, RemoteHandle):
            await self._follow_remote(running, row, handle)
            return
        self._handles[running.run_id] = handle
        if running.run_id in self._kill_requested:  # an operator kill came mid-launch
            self._signal_kill(running.run_id, handle)
        row = replace(row, pid=handle.pid)
        try:
            await self._transition([], attempts=[row])
        except EngineFailedError:
            if task is not None:
                self._attempt_tasks[running.run_id] = task
            try:
                await self._idle_while(handle)
            finally:
                self._handles.pop(running.run_id, None)
                self._attempt_tasks.pop(running.run_id, None)
                self._cancel_kill_timer(running.run_id)
            raise
        watch = self._arm(running, handle)
        try:
            result = await self._idle_while(handle)
        finally:
            watch.cancel()
            self._handles.pop(running.run_id, None)
        watch.note(handle)
        await self._finish(row, result, watch)

    async def _idle_while(self, handle: AttemptHandle) -> AttemptResult:
        """Wait for ``handle`` without counting as busy (the process may take hours)."""
        self._busy -= 1
        try:
            return await handle.wait()
        finally:
            self._busy += 1

    def _arm(self, run: Run, handle: AttemptHandle, started: datetime | None = None) -> Watch:
        """Arm the job's timeout: SIGTERM when it rings, SIGKILL after the grace.

        The timeout counts from ``started`` (default now); an adopted
        attempt passes its real start, so a timeout already passed rings
        at once.
        """
        watch = Watch()
        job = self.config.jobs.get(run.job)
        if job is None or job.timeout is None:
            return watch
        grace = self.config.settings.kill_grace

        def ring() -> None:
            """Ask the attempt to stop, and plan the kill."""
            watch.timed_out = True
            handle.terminate()
            watch.kill = self.clock.call_at(self.clock.now() + grace, self._guard(handle.kill))

        begin = started or self.clock.now()
        watch.term = self.clock.call_at(begin + job.timeout, self._guard(ring))
        return watch

    def _signal_kill(self, run_id: str, handle: AttemptHandle) -> None:
        """Ask the attempt to stop now (SIGTERM), and plan SIGKILL after ``kill_grace``."""
        handle.terminate()
        when = self.clock.now() + self.config.settings.kill_grace
        self._kill_timers[run_id] = self.clock.call_at(when, self._guard(handle.kill))

    async def _finish(self, row: Attempt, result: AttemptResult, watch: Watch) -> None:
        """Free the slots, record how the attempt ended, then retry or end the run.

        The process is over, so its slots are given back before the commit.
        The run's lock is held across the commit, so no operator action
        sees the run halfway. A killed attempt never retries, and neither
        does one whose job was removed or disabled by a reload while it ran.
        A watcher a reload stopped (see ``reload``) ends its run cancelled.
        """
        run_id = row.run_id
        async with self._run_lock(run_id):
            final = await self._record_ending(row, result, watch)
        if final is None:
            self._pump()
            return
        self._reevaluate([final.job])
        await self._flush()

    async def _record_ending(self, row: Attempt, result: AttemptResult, watch: Watch) -> Run | None:
        """Commit the attempt's ending; return the final run, or ``None`` if it retries."""
        run_id = row.run_id
        killed = run_id in self._kill_requested
        self._kill_requested.discard(run_id)
        actor = self._kill_actors.pop(run_id, None)
        self._cancel_kill_timer(run_id)
        self._attempt_tasks.pop(run_id, None)
        # The process has ended, so its slots are free now; until the commit
        # lands, published copies may briefly show a pool over its limit.
        self._release(run_id)
        outcome, reason = classify(
            result, timed_out=watch.timed_out, killed=killed, lost=watch.lost
        )
        if outcome is Outcome.KILLED and actor is not None:
            reason = f"killed by {actor}"
        if outcome is Outcome.TIMEOUT and watch.why:
            reason = watch.why
        dropped = self._dropped.pop(run_id, None)  # a reload stopped this watcher
        if dropped is not None and outcome is not Outcome.SUCCEEDED:
            reason = dropped
        else:
            dropped = None
        now = self.clock.now()
        ended = replace(
            row,
            ended_at=result.ended_at,
            exit_code=result.exit_code,
            signal=result.signal,
            outcome=outcome,
            reason=reason,
        )
        events = [
            self._event(
                run_id, "attempt_ended", attempt=row.attempt, outcome=outcome.value, reason=reason
            )
        ]
        run = self.book.get(run_id)
        if run is None:  # pragma: no cover -- runs are never dropped while running
            return None
        job = self.config.jobs.get(run.job)
        tries = run.attempt - run.rerun
        count = job.retries.count if job is not None and job.enabled else 0
        cancelled = result.start_error == CANCELLED_MESSAGE  # the scheduler is stopping
        retry = outcome not in _NO_RETRY and not (killed or cancelled or dropped)
        if retry and tries <= count and job is not None:
            when = now + job.retries.delay_for(tries)
            waiting = self._changed(
                run, RunState.RETRY_WAIT, f"retry {tries}/{count} after {reason}"
            )
            events.append(self._event(run_id, "retry_scheduled", attempt=row.attempt, at=when))
            await self._transition([waiting], attempts=[ended], events=events)
            current = self.book.get(run_id)
            if not self._stopping and current and current.state is RunState.RETRY_WAIT:
                retry = self._guard(lambda: self._retry(run_id))
                self._retry_timers[run_id] = self.clock.call_at(when, retry)
            return None
        state = RunState.SUCCEEDED if outcome is Outcome.SUCCEEDED else RunState.FAILED
        state = RunState.CANCELLED if dropped else state
        final = self._changed(run, state, reason)
        events.append(self._event(run_id, "finished", state=state.value, reason=reason))
        files = {run_id: watch.files} if watch.files and state is RunState.SUCCEEDED else {}
        # Before the transition: it puts the run in the book at once, so a
        # dependant woken by another run's commit may launch before ours lands.
        self._files.update(files)
        await self._transition([final], attempts=[ended], events=events, run_files=files)
        return final

    def _cancel_kill_timer(self, run_id: str) -> None:
        """Call off the planned SIGKILL of ``run_id``'s attempt, if any."""
        timer = self._kill_timers.pop(run_id, None)
        if timer is not None:
            timer.cancel()

    def _retry(self, run_id: str) -> None:
        """Queue a run whose retry wait is over (if it is still waiting)."""
        self._retry_timers.pop(run_id, None)
        run = self.book.get(run_id)
        job = self.config.jobs.get(run.job) if run else None
        if run is None or job is None or run.state is not RunState.RETRY_WAIT:
            return
        self._reevaluate(self._ready(run, job))
