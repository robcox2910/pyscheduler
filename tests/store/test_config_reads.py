"""Test reading one stored config version."""

from pathlib import Path

import pytest
from store_helpers import T0

from pyscheduler.store import Batch, Store
from pyscheduler.store.schema import ConfigRecord

FIRST = 1
SECOND = 2
MISSING = 7


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Return an open store."""
    return Store.open(tmp_path / "state", sync=False)


class TestGetConfig:
    """``get_config`` finds a version by number."""

    def test_get_config(self, store: Store) -> None:
        """Return the record of each saved version, and ``None`` for one never saved."""
        first = ConfigRecord(FIRST, T0, "abc", '{"a.yaml": "x"}')
        second = ConfigRecord(SECOND, T0, "def", "{}")
        store.apply(Batch(configs=[first, second]))
        assert store.get_config(FIRST) == first
        assert store.get_config(SECOND) == second
        assert store.get_config(MISSING) is None
        store.close()
