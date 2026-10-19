"""Read the ``scheduler:`` section into ``Settings``.

The settings are the control panel of the whole scheduler: its name, where
it keeps files, and how many things it may do at once.
"""

import re
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pyscheduler.config.fields import (
    Reader,
    check_keys,
    read_bool,
    read_duration,
    read_int,
    read_str,
    read_zone,
)
from pyscheduler.config.model import Settings
from pyscheduler.yamlish import Map, Node

SCHEDULER_KEYS = (
    "name", "timezone", "state_dir", "log_dir", "max_running", "listen",
    "api_tokens_file", "users_file", "kill_grace", "retention", "memory_days",
    "watch_limit", "secure_cookies",
)  # fmt: skip
_PORT = re.compile(r"\d{1,5}", re.ASCII)
MAX_PORT = 65535
DEFAULT_NAME = "pysched"
DEFAULT_MAX_RUNNING = 32
DEFAULT_LISTEN = ("127.0.0.1", 8470)
DEFAULT_KILL_GRACE = timedelta(seconds=10)
DEFAULT_RETENTION = timedelta(days=90)
DEFAULT_MEMORY_DAYS = 7
DEFAULT_WATCH_LIMIT = 8
UTC_ZONE = ZoneInfo("UTC")


def _listen(r: Reader, node: Node) -> tuple[str, int] | None:
    """Read ``listen: host:port``."""
    text = read_str(r, node, "listen")
    if text is None:
        return None
    host, sep, port = text.rpartition(":")
    if not sep or not host or _PORT.fullmatch(port) is None or int(port) > MAX_PORT:
        r.add(node.line, f"listen must be host:port with a port 0-{MAX_PORT}, got {text!r}")
        return None
    return host, int(port)


def read_settings(r: Reader, node: Map, main: Path) -> Settings:
    """Read the scheduler settings, resolving paths next to the main file."""
    check_keys(r, node, SCHEDULER_KEYS, "scheduler")
    items = node.items
    base = main.absolute().parent

    def path(key: str) -> Path | None:
        """Read a path setting, relative to the main file's folder."""
        text = read_str(r, items[key], key) if key in items else None
        return None if text is None else base / text

    def count(key: str, default: int) -> int:
        """Read a positive whole-number setting."""
        value = read_int(r, items[key], key, minimum=1) if key in items else None
        return default if value is None else value

    name = read_str(r, items["name"], "name") if "name" in items else None
    zone = read_zone(r, items["timezone"], "timezone") if "timezone" in items else None
    listen = _listen(r, items["listen"]) if "listen" in items else None
    return Settings(
        name=name or DEFAULT_NAME,
        timezone=zone or UTC_ZONE,
        state_dir=path("state_dir") or base / "state",
        log_dir=path("log_dir") or base / "logs",
        max_running=count("max_running", DEFAULT_MAX_RUNNING),
        listen=listen or DEFAULT_LISTEN,
        api_tokens_file=path("api_tokens_file"),
        users_file=path("users_file"),
        kill_grace=_duration(r, items, "kill_grace") or DEFAULT_KILL_GRACE,
        retention=_duration(r, items, "retention") or DEFAULT_RETENTION,
        memory_days=count("memory_days", DEFAULT_MEMORY_DAYS),
        watch_limit=count("watch_limit", DEFAULT_WATCH_LIMIT),
        secure_cookies=(
            read_bool(r, items["secure_cookies"], "secure_cookies")
            if "secure_cookies" in items
            else None
        ),
    )


def _duration(r: Reader, items: Mapping[str, Node], key: str) -> timedelta | None:
    """Read an optional duration setting."""
    return read_duration(r, items[key], key) if key in items else None
