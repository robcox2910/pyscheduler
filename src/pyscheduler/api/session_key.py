"""The session key: the secret that signs the web UI's login cookies.

A login cookie is like a cloakroom ticket stamped with the club's seal.
Anyone can print a ticket, but only the club has the seal, so the
doorman can tell a real ticket from a fake one. The seal is a random
secret kept in ``<state_dir>/session.key``: made on the first start,
readable only by the scheduler's user (mode ``0600``), and read again
on every later start, so a restart does not log everybody out.
"""

import contextlib
import os
import secrets
from pathlib import Path

__all__ = ["KEY_BYTES", "KEY_FILE", "SessionKeyError", "load_or_make_key"]

KEY_FILE = "session.key"
"""The key's file name inside the state folder."""

KEY_BYTES = 48
"""How many random bytes a new key has (written as hex)."""

PRIVATE = 0o600
"""The key file's mode: read and write for its owner only."""

MIN_KEY_CHARS = 32
"""The shortest key pyweb's ``SessionSigner`` accepts."""


class SessionKeyError(ValueError):
    """Raised when the key file exists but holds no usable key."""


def load_or_make_key(state_dir: Path) -> str:
    """Return the session key kept in ``state_dir``, making it on the first call.

    A new key is written to a private temporary file (mode 0600, so never
    readable by others, even for a moment) and then hard-linked into place:
    the real file appears whole or not at all, and of two starts racing
    only one key wins. An existing key file is set back to 0600 if its
    mode was loosened.

    This reads or writes a file: call it from a helper thread, not the
    event loop.

    Raises:
        SessionKeyError: If the existing key is empty, not ASCII text, or
            shorter than 32 characters.
        OSError: If the folder or the file cannot be made or read.

    """
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / KEY_FILE
    if not path.exists():
        _make(path)
    path.chmod(PRIVATE)
    try:
        key = path.read_text(encoding="ascii").strip()
    except UnicodeDecodeError:
        key = ""
    if len(key) < MIN_KEY_CHARS:
        msg = f"{path}: the session key is unusable; delete the file to make a new one"
        raise SessionKeyError(msg)
    return key


def _make(path: Path) -> None:
    """Write a new random key to ``path`` unless another start got there first."""
    temp = path.with_name(f"{KEY_FILE}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(secrets.token_hex(KEY_BYTES) + "\n")
        with contextlib.suppress(FileExistsError):  # another start's key is already in place
            os.link(temp, path)
    finally:
        temp.unlink(missing_ok=True)
