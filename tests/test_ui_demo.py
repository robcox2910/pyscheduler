"""Test the seeded UI demo: runs on several dates, the login, and serving for a while."""

import http.cookiejar
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from pyweb.security import verify_password

from pyscheduler.config import load_config
from pyscheduler.config.model import Criticality
from pyscheduler.demo import LOGIN_NAME, LOGIN_PASSWORD, main, serve_demo
from pyscheduler.engine.model import Run, RunState
from pyscheduler.store import Store

ROOT = Path(__file__).resolve().parent.parent
"""The repository root."""

SEED_SECONDS = 8.0
"""How long seeding may take."""

MIN_DATES = 2
"""The least number of run dates the seed must cover."""

MIN_JOBS = 12
"""The demo has about this many jobs."""

SERVE_SECONDS = 3.0
"""How long ``serve_demo`` is let run in the serve test."""

REQUEST_SECONDS = 10.0
"""How long one browser request may take."""


def _runs(folder: Path) -> list[Run]:
    """Return every stored run of the seeded demo in ``folder``."""
    config = load_config(folder / "pysched.yaml", env={})
    store = Store.open(config.settings.state_dir, sync=False)
    try:
        return list(store.query_runs(limit=1000))
    finally:
        store.close()


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, float]:
    """Seed the demo once through ``--seed-only``; return its folder and how long it took."""
    folder = tmp_path_factory.mktemp("ui") / "demo"
    started = time.monotonic()
    assert main(["--seed-only", "--folder", str(folder)]) == 0
    return folder, time.monotonic() - started


class TestSeed:
    """``seed_demo`` writes a config, a login and a few days of real runs."""

    def test_seed_only_makes_runs_on_several_dates(self, seeded: tuple[Path, float]) -> None:
        """``--seed-only`` seeds quickly: runs on at least two dates, every state kind shown."""
        folder, took = seeded
        assert took < SEED_SECONDS
        runs = _runs(folder)
        assert len({run.run_date for run in runs}) >= MIN_DATES
        by_job = {(run.job, run.run_date): run for run in runs}
        states = {job: run.state for (job, _), run in by_job.items()}
        assert states["payments-export"] is RunState.FAILED
        assert states["report"] is RunState.SUCCEEDED
        assert states["fetch-rates"] is RunState.SUCCEEDED  # failed once, then retried
        assert states["parse-prices"] is RunState.SUCCEEDED  # the watcher found its file

    def test_config_and_login(self, seeded: tuple[Path, float]) -> None:
        """The config is valid with about 12 jobs; the user file holds a real hash for admin/demo."""
        folder = seeded[0]
        config = load_config(folder / "pysched.yaml", env={})
        assert len(config.jobs) >= MIN_JOBS
        assert config.jobs["payments-export"].criticality is Criticality.CRITICAL
        text = (folder / "users.yaml").read_text(encoding="utf-8")
        assert LOGIN_PASSWORD not in text.replace("pbkdf2", "")
        stored = text.split("password_hash:")[1].split(",")[0].strip()
        assert verify_password(LOGIN_PASSWORD, stored)
        assert f"name: {LOGIN_NAME}" in text
        assert config.settings.listen[0] == "127.0.0.1"

    def test_example_script(self, tmp_path: Path) -> None:
        """``examples/ui_demo.py --seed-only`` is a thin wrapper that works."""
        done = subprocess.run(  # noqa: S603 -- our own example
            [
                sys.executable,
                str(ROOT / "examples" / "ui_demo.py"),
                "--seed-only",
                "--folder",
                str(tmp_path / "demo"),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=SEED_SECONDS * 2,
        )
        assert done.returncode == 0, done.stderr
        assert (tmp_path / "demo" / "pysched.yaml").exists()


class TestServe:
    """``serve_demo`` serves the seeded folder, stops after ``seconds`` and lets admin in."""

    def test_serve_for_a_while_and_log_in(
        self, seeded: tuple[Path, float], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The URL is announced, admin/demo logs in and sees the board, then it stops by itself."""
        urls: list[str] = []
        seen: dict[str, str] = {}

        def browse(url: str) -> None:
            """Log in like a browser (cookies kept) and fetch the board."""
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
            )
            form = urllib.parse.urlencode(
                {"name": LOGIN_NAME, "password": LOGIN_PASSWORD, "next": "/"}
            ).encode()
            with opener.open(url + "/login", form, timeout=REQUEST_SECONDS) as page:
                seen["board"] = page.read().decode()

        def ready(url: str) -> None:
            """Record the URL and browse from another thread (the loop must stay free)."""
            urls.append(url)
            worker = threading.Thread(target=browse, args=(url,))
            worker.start()
            workers.append(worker)

        workers: list[threading.Thread] = []
        serve_demo(seeded[0], seconds=SERVE_SECONDS, ready=ready)
        for worker in workers:
            worker.join()
        assert len(urls) == 1
        assert urls[0].startswith("http://127.0.0.1:")
        out = capsys.readouterr().out
        assert urls[0] in out
        assert LOGIN_NAME in out
        assert LOGIN_PASSWORD in out
        assert "payments-export" in seen["board"]
