"""Tests for small config model behaviours."""

from datetime import timedelta

import pytest

from pyscheduler.config import ConfigError, Criticality, Problem, RetryPolicy

LINE_TWO = 2
LINE_THREE = 3
SECOND_RETRY = 2
FIFTH_RETRY = 5
TWO_PROBLEMS = 2
BACKOFF = 2.0


class TestModel:
    """Small model behaviours."""

    def test_retry_delay(self) -> None:
        """Retry delays grow by the backoff and are capped."""
        policy = RetryPolicy(count=3, delay=timedelta(minutes=1), backoff=BACKOFF)
        assert policy.delay_for(1) == timedelta(minutes=1)
        assert policy.delay_for(SECOND_RETRY) == timedelta(minutes=2)
        assert policy.delay_for(FIFTH_RETRY) == timedelta(minutes=10)

    def test_criticality_from_text(self) -> None:
        """Criticality is read from its lower-case name."""
        assert Criticality.from_text("high") is Criticality.HIGH
        with pytest.raises(ValueError, match="criticality"):
            Criticality.from_text("urgent")

    def test_problem_str(self) -> None:
        """A problem prints its place and, if any, its job."""
        assert str(Problem("a.yaml", LINE_THREE, "bad")) == "a.yaml:3: bad"
        assert str(Problem("a.yaml", LINE_THREE, "bad", job="j")) == "a.yaml:3: job j: bad"

    def test_config_error_lists_all(self) -> None:
        """A config error's message lists every problem."""
        error = ConfigError((Problem("a", 1, "one"), Problem("b", LINE_TWO, "two")))
        assert "a:1: one" in str(error)
        assert "b:2: two" in str(error)
        assert len(error.problems) == TWO_PROBLEMS
