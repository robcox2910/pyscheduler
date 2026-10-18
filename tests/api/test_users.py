"""Test the users file: who may log in to the web UI, and with which role."""

from pathlib import Path

import pytest
from api_helpers import PHRASE, TEST_ROUNDS, users_yaml
from pyweb.security import hash_password, verify_password

from pyscheduler.api.auth import ROLE_RANK, Role, allows
from pyscheduler.api.users import UserHolder, UsersFileError, UserTable

SECOND_LINE = 2


def _write(tmp_path: Path, text: str) -> Path:
    """Write a users file and return its path."""
    path = tmp_path / "users.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _entry(name: str, role: str = "viewer", digest: str | None = None) -> str:
    """Return one flow-style users file line."""
    digest = digest or hash_password(PHRASE, rounds=TEST_ROUNDS)
    return f"- {{name: {name}, password_hash: {digest}, role: {role}}}\n"


class TestUserTable:
    """Load a users file and look users up by name."""

    def test_users_file_loads_and_errors(self, tmp_path: Path) -> None:
        """Load a good file: each user has one role and a hash ``verify_password`` accepts."""
        table = UserTable.load(
            _write(tmp_path, users_yaml(vic="viewer", ola="operator", ada="admin"))
        )
        ada = table.get("ada")
        assert ada is not None
        assert ada.name == "ada"
        assert ada.roles == frozenset({"admin"})
        assert verify_password(PHRASE, ada.password_hash)
        vic = table.get("vic")
        assert vic is not None
        assert vic.roles == frozenset({"viewer"})
        assert table.get("nobody") is None

    def test_block_style(self, tmp_path: Path) -> None:
        """Accept the block style, with the hash pasted as ``hash-password`` prints it."""
        digest = hash_password(PHRASE, rounds=TEST_ROUNDS)
        text = f"- name: ola\n  password_hash: {digest}\n  role: operator\n"
        user = UserTable.load(_write(tmp_path, text)).get("ola")
        assert user is not None
        assert user.roles == frozenset({"operator"})

    def test_empty_list(self, tmp_path: Path) -> None:
        """Accept an empty list: nobody can log in."""
        assert UserTable.load(_write(tmp_path, "[]\n")).get("ada") is None

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            (_entry("ada") + _entry("ada"), f"users.yaml:{SECOND_LINE}: .*'ada' is listed twice"),
            ("- x\n" + _entry("ada", "root"), "users.yaml:1: .*mapping"),
            (_entry("ada", "root"), "users.yaml:1: role must be viewer, operator or admin"),
            (_entry("ada", digest="plain-text"), "users.yaml:1: .*password_hash"),
            (_entry("ada", digest="pbkdf2_sha256$0$00$00"), "users.yaml:1: .*password_hash"),
            (_entry("ada", digest="pbkdf2_sha256$x$00$00"), "users.yaml:1: .*password_hash"),
            (_entry("ada", digest="pbkdf2_sha256$10$zz$00"), "users.yaml:1: .*password_hash"),
            (_entry("ada", digest="md5$10$00$00"), "users.yaml:1: .*password_hash"),
            ("- {name: ada, role: viewer}\n", "users.yaml:1: missing 'password_hash'"),
            ("- {name: 'a b', password_hash: x, role: viewer}\n", "users.yaml:1: bad name"),
            ("- {name: ada, password_hash: x, role: viewer, x: 1}\n", "users.yaml:1: unknown"),
            ("name: ada\n", "users.yaml:1: .*a list"),
            ("- [unclosed\n", "users.yaml"),
        ],
    )
    def test_bad_files_raise(self, tmp_path: Path, text: str, message: str) -> None:
        """Raise ``UsersFileError`` naming the file, the line and the problem."""
        with pytest.raises(UsersFileError, match=message):
            UserTable.load(_write(tmp_path, text))

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """Raise ``UsersFileError`` for a file that cannot be read."""
        with pytest.raises(UsersFileError, match="cannot read users file"):
            UserTable.load(tmp_path / "missing.yaml")


class TestUserHolder:
    """The holder is the store the session middleware asks, so a reload takes effect at once."""

    def test_get_follows_the_table(self, tmp_path: Path) -> None:
        """Answer ``None`` with no table, and the current table's user after a swap."""
        holder = UserHolder(None)
        assert holder.get("ada") is None
        holder.table = UserTable.load(_write(tmp_path, users_yaml(ada="admin")))
        user = holder.get("ada")
        assert user is not None
        assert user.roles == frozenset({"admin"})


class TestRoles:
    """``admin`` >= ``operator`` >= ``viewer``."""

    @pytest.mark.parametrize(
        ("role", "needed", "expected"),
        [
            ("viewer", "viewer", True),
            ("viewer", "operator", False),
            ("operator", "operator", True),
            ("operator", "admin", False),
            ("admin", "operator", True),
            ("admin", "viewer", True),
        ],
    )
    def test_allows(self, role: Role, needed: Role, *, expected: bool) -> None:
        """A role allows what it needs and everything ranked below it."""
        assert role in ROLE_RANK
        assert needed in ROLE_RANK
        assert allows(role, needed) is expected
