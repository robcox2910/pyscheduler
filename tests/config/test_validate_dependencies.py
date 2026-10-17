"""Validation tests: dependencies, cycles and calendars (including inheritance)."""

import textwrap
from pathlib import Path

from validate_helpers import TODAY, check, errors_of, jobs_text, line_of, only, write_config

from pyscheduler.config import build_config, load_config


class TestDependencyErrors:
    """Errors about ``depends_on``."""

    def test_unknown_job(self, tmp_path: Path) -> None:
        """A dependency on a job that does not exist is reported at its line."""
        text = jobs_text("""
            a:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - ghost
        """)
        problem = only(errors_of(tmp_path, text), "unknown job")
        check(problem, "a", line_of(text, "- ghost"))

    def test_cycle(self, tmp_path: Path) -> None:
        """An offset-0 cycle is reported once, listing the path."""
        text = jobs_text("""
            a:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - b
            b:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - a
        """)
        problem = only(errors_of(tmp_path, text), "cycle")
        assert "cycle: a -> b -> a" in problem.message
        check(problem, "a", line_of(text, "- b"))

    def test_three_job_cycle_reported_once(self, tmp_path: Path) -> None:
        """A cycle through three jobs gives one problem, starting at the smallest name."""
        text = jobs_text("""
            c:
              command: x
              schedule: "0 9 * * *"
              depends_on: [a]
            a:
              command: x
              schedule: "0 9 * * *"
              depends_on: [b]
            b:
              command: x
              schedule: "0 9 * * *"
              depends_on: [c]
        """)
        problem = only(errors_of(tmp_path, text), "cycle")
        assert "cycle: a -> b -> c -> a" in problem.message

    def test_self_dependency_is_a_cycle(self, tmp_path: Path) -> None:
        """A job depending on itself for the same run date is a cycle."""
        text = jobs_text("""
            a:
              command: x
              schedule: "0 9 * * *"
              depends_on: [a]
        """)
        assert "cycle: a -> a" in only(errors_of(tmp_path, text), "cycle").message

    def test_offset_cycle_allowed(self, tmp_path: Path) -> None:
        """A cycle broken by a -1 run-date offset is fine."""
        text = jobs_text("""
            a:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - {job: b, run_date: -1}
            b:
              command: x
              schedule: "0 9 * * *"
              depends_on: [a]
        """)
        config = load_config(write_config(tmp_path, text), env={}, today=TODAY)
        assert set(config.jobs) == {"a", "b"}

    def test_positive_offset(self, tmp_path: Path) -> None:
        """A positive run-date offset is an error at the dependency's line."""
        text = jobs_text("""
            a:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - {job: b, run_date: 1}
            b:
              command: x
              schedule: "0 9 * * *"
        """)
        problem = only(errors_of(tmp_path, text), "positive run-date offset")
        check(problem, "a", line_of(text, "run_date: 1"))

    def test_different_calendars(self, tmp_path: Path) -> None:
        """A uk-business job depending on a us-business job is an error."""
        text = jobs_text("""
            uk-job:
              command: x
              schedule: "0 9 * * *"
              calendar: uk-business
              depends_on:
                - us-job
            us-job:
              command: x
              schedule: "0 8 * * *"
              calendar: us-business
        """)
        problem = only(errors_of(tmp_path, text), "different calendars")
        check(problem, "uk-job", line_of(text, "- us-job"))

    def test_calendar_job_on_none_calendar_job(self, tmp_path: Path) -> None:
        """A calendar job depending on a job with no calendar is an error."""
        text = jobs_text("""
            uk-job:
              command: x
              schedule: "0 9 * * *"
              calendar: uk-business
              depends_on:
                - plain
            plain:
              command: x
              schedule: "0 8 * * *"
        """)
        problem = only(errors_of(tmp_path, text), "calendar job depends on a job with no calendar")
        check(problem, "uk-job", line_of(text, "- plain"))

    def test_scheduled_none_calendar_job_on_calendar_job(self, tmp_path: Path) -> None:
        """A scheduled job with no calendar depending on a calendar job is an error."""
        text = jobs_text("""
            plain:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - uk-job
            uk-job:
              command: x
              schedule: "0 8 * * *"
              calendar: uk-business
        """)
        problem = only(errors_of(tmp_path, text), "no calendar depends on a calendar job")
        check(problem, "plain", line_of(text, "- uk-job"))


class TestCalendarInheritance:
    """A dependency-only job with no calendar inherits its upstreams' calendar."""

    UPSTREAMS = """
        uk-job:
          command: x
          schedule: "0 8 * * 1-5"
          calendar: uk-business
        us-job:
          command: x
          schedule: "0 8 * * 1-5"
          calendar: us-business
    """

    def jobs_text(self, jobs: str) -> str:
        """Return a config with the two upstreams followed by ``jobs``."""
        return jobs_text(textwrap.dedent(self.UPSTREAMS) + textwrap.dedent(jobs))

    def test_follower_inherits(self, tmp_path: Path) -> None:
        """A follower of a uk-business job is effectively on uk-business, even with an offset."""
        text = self.jobs_text("""
            follower:
              command: x
              depends_on:
                - uk-job
                - {job: uk-job, run_date: -1}
        """)
        config = load_config(write_config(tmp_path, text), env={}, today=TODAY)
        assert config.warnings == ()
        assert config.effective_calendars["follower"] == "uk-business"
        assert config.calendar_for(config.jobs["follower"]) is config.calendars["uk-business"]

    def test_inheritance_is_transitive(self, tmp_path: Path) -> None:
        """A chain of followers all inherit; a scheduled uk job may follow the last one."""
        text = self.jobs_text("""
            first:
              command: x
              depends_on: [uk-job]
            second:
              command: x
              depends_on: [first]
            report:
              command: x
              schedule: "0 9 * * 1-5"
              calendar: uk-business
              depends_on: [second]
        """)
        config = load_config(write_config(tmp_path, text), env={}, today=TODAY)
        assert config.effective_calendars["second"] == "uk-business"

    def test_inherited_calendar_must_match(self, tmp_path: Path) -> None:
        """A us job following an inherited uk follower is on a different calendar."""
        text = self.jobs_text("""
            follower:
              command: x
              depends_on: [uk-job]
            us-report:
              command: x
              schedule: "0 9 * * 1-5"
              calendar: us-business
              depends_on:
                - follower
        """)
        problem = only(errors_of(tmp_path, text), "different calendars")
        check(problem, "us-report", line_of(text, "- follower"))

    def test_scheduled_none_job_on_follower(self, tmp_path: Path) -> None:
        """A scheduled job with no calendar does not inherit, so it may not follow a uk follower."""
        text = self.jobs_text("""
            follower:
              command: x
              depends_on: [uk-job]
            plain:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - follower
        """)
        problem = only(errors_of(tmp_path, text), "no calendar depends on a calendar job")
        check(problem, "plain", line_of(text, "- follower"))

    def test_upstreams_disagree(self, tmp_path: Path) -> None:
        """A follower of a uk job and a us job has no one calendar to inherit."""
        text = self.jobs_text("""
            follower:
              command: x
              depends_on: [uk-job, us-job]
        """)
        problems = errors_of(tmp_path, text)
        problem = only(problems, "upstreams are on different calendars")
        assert "(uk-job: uk-business, us-job: us-business)" in problem.message
        check(problem, "follower", line_of(text, "  follower:"))
        assert len(problems) == 1

    def test_none_upstream_disagrees_too(self, tmp_path: Path) -> None:
        """An upstream with no calendar counts as a different calendar."""
        text = self.jobs_text("""
            plain:
              command: x
              schedule: "0 8 * * *"
            follower:
              command: x
              depends_on: [uk-job, plain]
        """)
        problem = only(errors_of(tmp_path, text), "upstreams are on different calendars")
        assert "(plain: none, uk-job: uk-business)" in problem.message

    def test_follower_loop_does_not_hang(self, tmp_path: Path) -> None:
        """Followers that follow each other (with an offset) still resolve."""
        text = self.jobs_text("""
            a:
              command: x
              depends_on: [uk-job, {job: b, run_date: -1}]
            b:
              command: x
              depends_on: [a]
        """)
        config = load_config(write_config(tmp_path, text), env={}, today=TODAY)
        assert config.effective_calendars["a"] == "uk-business"
        assert config.effective_calendars["b"] == "uk-business"

    def test_closed_follower_loop_gets_no_calendar(self, tmp_path: Path) -> None:
        """Followers that follow only each other have nothing to inherit."""
        text = jobs_text("""
            a:
              command: x
              depends_on: [{job: b, run_date: -1}]
            b:
              command: x
              depends_on: [a]
        """)
        config, problems = build_config(write_config(tmp_path, text), env={})
        assert problems == []
        assert config.effective_calendars == {"a": None, "b": None}
