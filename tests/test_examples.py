"""The example config in ``examples/`` is valid and its scripts behave as the README says."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pyscheduler.cli import main
from pyscheduler.config import load_config

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
"""The examples folder at the repository root."""

ETL = EXAMPLES / "etl.yaml"
"""The example ETL config."""

SCRIPTS = ("extract.sh", "transform.sh", "load.sh", "flaky.sh")
"""The example's job scripts."""

RUN_SECONDS = 10.0
"""How long one example script may take."""


class TestEtlExample:
    """``examples/etl.yaml`` and its scripts."""

    def test_validates_cleanly(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``pysched validate`` says ok, with no warnings."""
        assert main(["validate", str(ETL)]) == 0
        out = capsys.readouterr()
        assert out.out.startswith("ok:")
        assert "warning" not in out.out + out.err

    def test_has_what_it_shows_off(self) -> None:
        """A calendar, a pool, a retrying job, an on-failure cleanup and previous_business_day."""
        config = load_config(ETL, env={})
        jobs = config.jobs
        assert set(config.calendars) >= {"uk-business"}
        assert config.pools == {"warehouse": 1}
        assert jobs["fetch-rates"].retries.count > 0
        assert [(d.job, d.on.value) for d in jobs["cleanup"].depends_on] == [("load", "failure")]
        assert jobs["extract"].run_date_rule.value == "previous_business_day"
        assert not config.warnings

    @pytest.mark.parametrize("name", SCRIPTS)
    def test_scripts_are_executable(self, name: str) -> None:
        """Every script can be run directly."""
        assert os.access(EXAMPLES / "scripts" / name, os.X_OK)

    def test_flaky_fails_once_then_succeeds(self, tmp_path: Path) -> None:
        """The flaky job fails its first attempt and succeeds on the retry."""
        scripts = tmp_path / "scripts"
        shutil.copytree(EXAMPLES / "scripts", scripts)
        env = {**os.environ, "PYSCHED_RUN_ID": "fetch-rates.2026-10-16.1", "PYSCHED_ATTEMPT": "1"}
        flaky = [str(scripts / "flaky.sh"), "2026-10-16"]

        def attempt() -> int:
            """Run the script once and return its exit code."""
            return subprocess.run(  # noqa: S603 -- our own example script
                flaky, env=env, check=False, capture_output=True, timeout=RUN_SECONDS
            ).returncode

        assert attempt() == 1
        assert list((tmp_path / "state").iterdir())  # the marker is in the state folder
        assert attempt() == 0
        assert list((tmp_path / "state").iterdir()) == []
