"""Smoke test for the package."""

import pyscheduler


class TestPackage:
    """The package imports."""

    def test_has_docstring(self) -> None:
        """The package explains itself."""
        assert pyscheduler.__doc__
