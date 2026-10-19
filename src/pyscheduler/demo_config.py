"""The demo's config: twelve jobs, and the ``scheduler:`` block naming the demo's folder.

Like the label on a jar, the config names where everything lives. The
folder's path is written into it in single quotes, so a folder called
``a: b`` or ``c #d`` is still read back exactly. A path with a quote,
a backslash, a ``$`` or a backtick is refused instead: it would break the
shell command of the flaky job.
"""

from pathlib import Path

__all__ = ["JOBS", "UNSAFE", "WATCH_POLL", "check_folder", "config_text", "quoted"]

UNSAFE = frozenset("'\"\\$`")
"""Characters a demo folder may not contain: they would break the generated shell command."""

WATCH_POLL = "1s"
"""How often the demo's file watcher looks."""

JOBS = """
jobs:
  extract:
    description: Copy the day's trades out of the trading system.
    command: sleep 0.1
    schedule: manual
    tags: [etl]
    owner: data-team

  transform:
    description: Turn raw trades into warehouse rows.
    command: sleep 0.1
    depends_on: [extract]
    tags: [etl]
    owner: data-team

  fetch-rates:
    description: Download exchange rates; the server fails once, then answers.
    command: 'if test -e "{marks}/{{run_date}}"; then exit 0; else touch "{marks}/{{run_date}}"; exit 1; fi'
    schedule: manual
    retries: {{count: 2, delay: 1s}}
    tags: [etl, flaky]
    owner: data-team

  load:
    description: Load the rows, priced with the day's rates.
    command: sleep 0.1
    depends_on: [transform, fetch-rates]
    criticality: critical
    tags: [etl]
    owner: data-team

  report:
    description: Mail the morning report once the load is in.
    command: "true"
    depends_on: [load]
    tags: [etl, reports]
    owner: finance

  inbox:
    description: Wait for the day's prices file to arrive and sit still.
    type: file_watcher
    schedule: manual
    watch: "{inbox}/prices.csv"
    min_age: 0s
    poll: {watch_poll}
    give_up_after: 1m
    tags: [files]
    owner: finance

  parse-prices:
    description: Read the prices the watcher found.
    command: sleep 0.1
    depends_on: [inbox]
    tags: [files]
    owner: finance

  payments-export:
    description: Export the day's payments. It is broken, on purpose.
    command: "false"
    schedule: manual
    criticality: critical
    tags: [payments]
    owner: payments

  payments-notify:
    description: Tell the bank the export is ready (never starts if the export fails).
    command: "true"
    depends_on: [payments-export]
    tags: [payments]
    owner: payments

  payments-cleanup:
    description: Tidy up after a failed export.
    command: sleep 0.1
    depends_on:
      - {{job: payments-export, on: failure}}
    tags: [payments]
    owner: payments

  housekeeping:
    description: Nightly tidy-up of old files.
    command: sleep 0.1
    schedule: "0 3 * * *"
    catch_up: none
    tags: [ops]
    owner: platform

  weekly-audit:
    description: Monday morning audit (nothing has run yet; see the forecast).
    command: "true"
    schedule: "0 6 * * 1"
    catch_up: none
    tags: [ops]
    owner: platform
"""
"""The twelve jobs; ``{marks}``, ``{inbox}`` and ``{watch_poll}`` are filled in, ``{{run_date}}`` is left for the scheduler."""


def quoted(path: Path) -> str:
    """Return ``path`` as a single-quoted YAML scalar (``: `` and `` #`` are then just text)."""
    return "'" + str(path) + "'"  # check_folder has refused any quote in it


def check_folder(folder: Path) -> None:
    """Refuse a folder the generated config's shell command could not hold.

    Raises:
        ValueError: If the path has a quote, backslash, ``$``, backtick or control character.

    """
    text = str(folder)
    if any(char in UNSAFE or not char.isprintable() for char in text):
        msg = f"the demo folder may not contain quotes, \\, $, ` or control characters: {text!r}"
        raise ValueError(msg)


def config_text(folder: Path, token_file: Path, users_file: Path) -> str:
    """Return the text of the demo's config for ``folder`` (paths quoted)."""
    header = (
        "scheduler:\n"
        "  name: pyscheduler-demo\n"
        "  timezone: UTC\n"
        f"  state_dir: {quoted(folder / 'state')}\n"
        f"  log_dir: {quoted(folder / 'logs')}\n"
        "  listen: 127.0.0.1:0\n"
        "  max_running: 8\n"
        f"  api_tokens_file: {quoted(token_file)}\n"
        f"  users_file: {quoted(users_file)}\n"
    )
    jobs = JOBS.format(
        marks=folder / "marks", inbox=folder / "inbox", watch_poll=WATCH_POLL
    ).lstrip("\n")
    return header + jobs
