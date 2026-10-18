"""Tests for placeholder templates and ${ENV} expansion."""

from datetime import UTC, date, datetime

import pytest

from pyscheduler.templates import PLACEHOLDERS, expand_env, placeholders_in, render


class TestRender:
    """Templates are filled from a mapping."""

    def test_render_run_date_format(self) -> None:
        """Dates use strftime with a spec and isoformat without."""
        values = {"run_date": date(2026, 10, 17)}
        assert render("x_{run_date:%Y%m%d}", values) == "x_20261017"
        assert render("{run_date}", values) == "2026-10-17"
        assert render("{{lit}}", values) == "{lit}"

    def test_render_datetime(self) -> None:
        """Aware UTC datetimes render with a Z suffix."""
        moment = datetime(2026, 10, 17, 8, 30, tzinfo=UTC)
        assert render("{scheduled_at}", {"scheduled_at": moment}) == "2026-10-17T08:30:00Z"
        assert render("{scheduled_at:%H%M}", {"scheduled_at": moment}) == "0830"
        naive = datetime(2026, 10, 17, 8, 30)  # noqa: DTZ001
        assert render("{t}", {"t": naive}) == "2026-10-17T08:30:00"

    def test_render_plain_values(self) -> None:
        """Other values use normal formatting."""
        assert render("{job}-{attempt:03d}", {"job": "j", "attempt": 2}) == "j-002"
        assert render("{attempt!r}", {"attempt": 2}) == "2"

    def test_render_missing_and_bad(self) -> None:
        """Missing names raise KeyError; malformed templates raise ValueError."""
        with pytest.raises(KeyError):
            render("{nope}", {})
        with pytest.raises(ValueError, match="template"):
            render("{job", {"job": "j"})
        with pytest.raises(ValueError, match="template"):
            render("{job:{attempt}}", {"job": "j", "attempt": 1})

    def test_render_bad_format_spec(self) -> None:
        """A spec the value cannot take raises ValueError."""
        with pytest.raises(ValueError, match="template"):
            render("{x:d}", {"x": None})


class TestPlaceholders:
    """Placeholder names can be listed without rendering."""

    def test_placeholders_in(self) -> None:
        """Names in fields count and escaped braces do not."""
        assert placeholders_in("{job}/{run_date:%Y}/{{x}}") == {"job", "run_date"}
        assert placeholders_in("plain") == set()

    def test_placeholders_known(self) -> None:
        """The known set is the documented one."""
        known = {"job", "run_date", "run_id", "attempt", "scheduled_at", "log_dir", "files"}
        assert frozenset(known) == PLACEHOLDERS

    def test_placeholders_in_bad(self) -> None:
        """Malformed templates raise ValueError."""
        with pytest.raises(ValueError, match="template"):
            placeholders_in("{job")
        with pytest.raises(ValueError, match="nested"):
            placeholders_in("{a:{b}}")


class TestExpandEnv:
    """${NAME} comes from the given environment."""

    def test_expand_env(self) -> None:
        """Variables expand, $$ is a dollar, missing raises."""
        assert expand_env("${A}-$$", {"A": "1"}) == "1-$"
        with pytest.raises(KeyError) as info:
            expand_env("${B}", {"A": "1"})
        assert info.value.args == ("B",)

    def test_expand_env_leaves_other_dollars(self) -> None:
        """A lone dollar or unterminated brace is left alone."""
        assert expand_env("cost $5 ${", {}) == "cost $5 ${"
