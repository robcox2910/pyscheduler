"""Test reading one stored config version."""

from datetime import timedelta
from pathlib import Path

import pytest
from store_helpers import T0

from pyscheduler.store import Batch, Store
from pyscheduler.store.config_reads import ConfigVersion
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


class TestConfigVersions:
    """``config_versions`` lists the saved versions without their file texts."""

    def test_newest_first(self, store: Store) -> None:
        """List every version, newest first, with its time and digest."""
        later = T0 + timedelta(hours=1)
        store.apply(
            Batch(
                configs=[
                    ConfigRecord(FIRST, T0, "abc", '{"a.yaml": "x"}'),
                    ConfigRecord(SECOND, later, "def", "{}"),
                ]
            )
        )
        assert store.config_versions() == [
            ConfigVersion(SECOND, later, "def"),
            ConfigVersion(FIRST, T0, "abc"),
        ]
        store.close()

    def test_none_saved(self, store: Store) -> None:
        """A store with no saved config lists nothing."""
        assert store.config_versions() == []
        store.close()
