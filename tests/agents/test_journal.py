"""Tests for the agent's journal: accepted attempts and unacked finished reports."""

from pathlib import Path

from pynet.messaging import JsonValue

from pyscheduler.agents.journal import FINISHED, RUNNING, Entry, Journal

RUN_ID = "nightly.2026-10-20.1"
OTHER_RUN = "nightly.2026-10-20.2"
REPORT: dict[str, JsonValue] = {"run_id": RUN_ID, "attempt": 1, "exit_code": 0}


def _entry(tmp_path: Path, run_id: str = RUN_ID, attempt: int = 1) -> Entry:
    """Return a running entry whose files live under ``tmp_path``."""
    stem = f"{run_id}.{attempt}"
    return Entry(
        run_id=run_id,
        attempt=attempt,
        state=RUNNING,
        finished=None,
        stdout_path=tmp_path / f"{stem}.out",
        stderr_path=tmp_path / f"{stem}.err",
        status_path=tmp_path / f"{stem}.json",
    )


class TestAccept:
    """An attempt is accepted once ever."""

    def test_accept_once(self, tmp_path: Path) -> None:
        """Say the first accept is new and every later one is not."""
        journal = Journal.open(tmp_path / "state", sync=False)
        entry = _entry(tmp_path)
        first, new = journal.accept(entry)
        again, new_again = journal.accept(entry)
        assert (first, new) == (entry, True)
        assert (again, new_again) == (entry, False)
        assert journal.running() == [entry]
        journal.close()

    def test_survives_reopen(self, tmp_path: Path) -> None:
        """Keep accepted attempts and pending reports across a close and reopen."""
        journal = Journal.open(tmp_path / "state", sync=False)
        journal.accept(_entry(tmp_path))
        journal.accept(_entry(tmp_path, OTHER_RUN))
        journal.finish((RUN_ID, 1), REPORT)
        journal.close()
        reopened = Journal.open(tmp_path / "state", sync=False)
        done = reopened.get((RUN_ID, 1))
        assert done is not None
        assert (done.state, done.finished) == (FINISHED, REPORT)
        assert [e.run_id for e in reopened.running()] == [OTHER_RUN]
        assert reopened.pending() == {(RUN_ID, 1): REPORT}
        _, new = reopened.accept(_entry(tmp_path))
        assert not new
        reopened.close()


class TestPending:
    """Finished reports stay pending until acked."""

    def test_acked_drops_pending(self, tmp_path: Path) -> None:
        """Drop the report on ack; a second ack (or an unknown one) is a no-op."""
        journal = Journal.open(tmp_path / "state", sync=False)
        journal.accept(_entry(tmp_path))
        journal.finish((RUN_ID, 1), REPORT)
        assert journal.acked((RUN_ID, 1))
        assert not journal.acked((RUN_ID, 1))
        assert not journal.acked((OTHER_RUN, 1))
        assert journal.pending() == {}
        entry = journal.get((RUN_ID, 1))
        assert entry is not None
        assert entry.finished == REPORT
        journal.close()

    def test_finish_twice_keeps_one_pending(self, tmp_path: Path) -> None:
        """Keep a single pending report when an attempt is finished twice."""
        journal = Journal.open(tmp_path / "state", sync=False)
        journal.accept(_entry(tmp_path))
        journal.finish((RUN_ID, 1), REPORT)
        journal.finish((RUN_ID, 1), REPORT)
        assert journal.pending() == {(RUN_ID, 1): REPORT}
        journal.close()

    def test_unknown_get(self, tmp_path: Path) -> None:
        """Return None for an attempt never accepted."""
        journal = Journal.open(tmp_path / "state", sync=False)
        assert journal.get((RUN_ID, 1)) is None
        journal.close()
