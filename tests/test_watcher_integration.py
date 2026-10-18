"""End to end: a real scheduler waits for a real file, then a real job reads it."""

import time
from pathlib import Path

from pyscheduler.client import ApiClient
from service_helpers import POLL, today, with_service, write_config

WRITE_AFTER = 1.0
"""How long (real seconds) the test waits before writing the file."""

FINISH_WITHIN = 8.0
"""How long (real seconds) the watcher and ``load`` may take in all."""

CONTENT = "id,amount\n1,42\n"


def test_watcher_integration(tmp_path: Path) -> None:
    """Write the file a second in; the watcher finds it and ``load`` cats it to its log."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    jobs = f"""
    jobs:
      in:
        type: file_watcher
        schedule: manual
        watch: '{inbox}/in_{{run_date:%Y%m%d}}.csv'
        min_age: 1s
        poll: 1s
      load:
        command: 'cat "$PYSCHED_FILES"'
        depends_on: [in]
    """
    path = write_config(tmp_path, jobs)
    day = today()

    def body(url: str) -> dict[str, str]:
        """Trigger the watcher, write the file, and wait for ``load`` to succeed."""
        client = ApiClient(url, None)
        began = time.monotonic()
        client.trigger("in", run_date=day)
        time.sleep(WRITE_AFTER)
        (inbox / f"in_{day.replace('-', '')}.csv").write_text(CONTENT, encoding="utf-8")
        states: dict[str, str] = {}
        while time.monotonic() - began < FINISH_WITHIN:
            runs = client.runs(run_date=day)["runs"]
            states = {str(r["job"]): str(r["state"]) for r in runs}
            if states.get("load") in {"succeeded", "failed"}:
                break
            time.sleep(POLL)
        return states

    states = with_service(path, body)
    assert states == {"in": "succeeded", "load": "succeeded"}
    logs = list((tmp_path / "logs" / "load").rglob("*.out"))
    assert [log.read_text(encoding="utf-8") for log in logs] == [CONTENT]
