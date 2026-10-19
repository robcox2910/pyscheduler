"""Look up a past config: the file texts the scheduler loaded as one version.

Think of a filing cabinet where every time the rules change, a copy of
the rulebook is filed under a version number. ``get_config`` pulls out
the copy for one version, so a page can show what the rules said when an
old run was made.
"""

from pydb.database import Database
from pydb.table import Table

from pyscheduler.store.schema import ConfigRecord, config_from_row

__all__ = ["ConfigReads"]


class ConfigReads:
    """Give the ``Store`` its read of one stored config version, under PyDB's lock."""

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
