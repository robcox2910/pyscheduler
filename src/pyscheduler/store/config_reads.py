"""Look up a past config: the file texts the scheduler loaded as one version.

Think of a filing cabinet where every time the rules change, a copy of
the rulebook is filed under a version number. ``get_config`` pulls out
the copy for one version, so a page can show what the rules said when an
old run was made.
"""

from dataclasses import dataclass
from datetime import datetime

from pydb.database import Database
from pydb.table import Table

from pyscheduler.store.schema import ConfigRecord, config_from_row

__all__ = ["ConfigReads", "ConfigVersion"]


@dataclass(frozen=True)
class ConfigVersion:
    """One row of the filing cabinet's index: a version, when it was filed and its digest."""

    version: int
    loaded_at: datetime
    digest: str


class ConfigReads:
    """Give the ``Store`` its reads of stored config versions, under PyDB's lock."""

    _db: Database
    _configs: Table
    _config_ids: dict[int, int]

    def get_config(self, version: int) -> ConfigRecord | None:
        """Return the config record saved as ``version``, or ``None`` if there is none."""
        with self._db.lock:
            record_id = self._config_ids.get(version)
            if record_id is None:
                return None
            return config_from_row(self._configs.get(record_id).data)

    def config_versions(self) -> list[ConfigVersion]:
        """Return every stored version (no file texts), newest first."""
        with self._db.lock:
            records = [
                config_from_row(self._configs.get(r).data) for r in self._config_ids.values()
            ]
        found = [ConfigVersion(c.version, c.loaded_at, c.digest) for c in records]
        return sorted(found, key=lambda v: v.version, reverse=True)
