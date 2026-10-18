"""Offsets past a year of business days, and ``manual`` beside real schedules."""

from pathlib import Path

from validate_helpers import check, errors_of, jobs_text, line_of, only, warnings_of


class TestOffsetLimit:
    """A ``run_date`` offset must be between -260 and 0 (ruling R31)."""

    def test_too_far_back(self, tmp_path: Path) -> None:
        """Refuse -261, naming the allowed range at the dependency's line."""
        text = jobs_text(
            """
            a: {command: run, schedule: "0 1 * * *"}
            b:
              command: run-b
              schedule: "0 2 * * *"
              depends_on:
                - {job: a, run_date: -261}
            """
        )
        problem = only(errors_of(tmp_path, text), "run_date offset must be between -260 and 0")
        check(problem, "b", line_of(text, "run_date: -261"))

    def test_limit_itself_is_fine(self, tmp_path: Path) -> None:
        """Accept -260."""
        text = jobs_text(
            """
            a: {command: run, schedule: "0 1 * * *"}
            b:
              command: run-b
              schedule: "0 2 * * *"
              depends_on:
                - {job: a, run_date: -260}
            """
        )
        assert not [w for w in warnings_of(tmp_path, text) if "between -260" in w.message]


class TestManualAlongside:
    """``manual`` next to a real schedule changes nothing, so it is worth a warning."""

    def test_manual_with_cron_warns(self, tmp_path: Path) -> None:
        """Warn at the ``manual`` item."""
        text = jobs_text(
            """
            a:
              command: run
              schedule:
                - manual
                - "0 1 * * *"
            """
        )
        warning = only(warnings_of(tmp_path, text), "manual has no effect alongside other")
        check(warning, "a", line_of(text, "- manual"))
        assert warning.severity == "warning"

    def test_manual_alone_is_quiet(self, tmp_path: Path) -> None:
        """No warning for ``schedule: manual`` on its own."""
        text = jobs_text("a: {command: run, schedule: manual}")
        assert not [w for w in warnings_of(tmp_path, text) if "manual" in w.message]
