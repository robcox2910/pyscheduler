"""Test the demo's seeding guards: main thread only, the ``seeded`` marker, odd folder names."""

import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from pyscheduler import demo
from pyscheduler.config import load_config
from pyscheduler.demo import SEEDED_MARK, SeedInterruptedError, seed_demo, serve_demo
from pyscheduler.demo_config import config_text

INTERRUPTED = 130
"""The exit code for a seed stopped by Ctrl-C (128 + SIGINT)."""
DEMO_JOBS = 12


class _StopError(Exception):
    """Raised by a stand-in to stop ``serve_demo`` once it has decided."""


def _off_main(target: Callable[[], object]) -> BaseException | None:
    """Run ``target()`` on a worker thread; return what it raised (``None``: nothing)."""
    raised: list[BaseException] = []

    def work() -> None:
        """Call the target, keeping what it raises."""
        try:
            target()
        except BaseException as exc:  # noqa: BLE001 -- handed back to the test
            raised.append(exc)

    worker = threading.Thread(target=work)
    worker.start()
    worker.join()
    return raised[0] if raised else None


class TestMainThread:
    """Seeding needs the main thread (pynet's child watcher only installs there)."""

    def test_off_main_thread_is_refused(self, tmp_path: Path) -> None:
        """Off the main thread ``seed_demo`` raises a clear error and writes nothing."""
        folder = tmp_path / "demo"
        raised = _off_main(lambda: seed_demo(folder))
        assert isinstance(raised, RuntimeError)
        assert str(raised).startswith("seed_demo must run on the main thread")
        assert not folder.exists()


class TestMarker:
    """``serve_demo`` seeds unless the ``seeded`` marker says a seed finished."""

    def test_config_without_marker_is_reseeded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A folder with a config but no marker (a failed seed) is seeded again."""
        folder = tmp_path / "demo"
        folder.mkdir()
        (folder / demo.CONFIG_NAME).write_text("scheduler: {}\n", encoding="utf-8")
        seeded: list[Path] = []

        def fake_seed(where: Path) -> Path:
            """Note the call, then stop ``serve_demo``."""
            seeded.append(where)
            raise _StopError

        monkeypatch.setattr(demo, "seed_demo", fake_seed)
        with pytest.raises(_StopError):
            serve_demo(folder, seconds=0)
        assert seeded == [folder]

    def test_marker_skips_the_seed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With the marker there, ``serve_demo`` serves without seeding."""
        folder = tmp_path / "demo"
        folder.mkdir()
        (folder / SEEDED_MARK).write_text("", encoding="utf-8")

        def fake_seed(where: Path) -> Path:
            """Fail: no seed was wanted."""
            raise AssertionError(where)

        async def fake_serve(path: Path, **kwargs: object) -> None:
            """Stand in for ``serve``: just check the path."""
            del kwargs
            assert path == folder.absolute() / demo.CONFIG_NAME
            raise _StopError

        monkeypatch.setattr(demo, "seed_demo", fake_seed)
        monkeypatch.setattr(demo, "serve", fake_serve)
        with pytest.raises(_StopError):
            serve_demo(folder, seconds=0)

    def test_partial_folder_is_seeded_cleanly(self, tmp_path: Path) -> None:
        """A seed over a half-made folder (broken config, stray state) finishes with the marker."""
        folder = tmp_path / "demo"
        (folder / "state").mkdir(parents=True)
        (folder / "state" / "stray").write_text("half a seed", encoding="utf-8")
        (folder / demo.CONFIG_NAME).write_text("scheduler: [\n", encoding="utf-8")
        path = seed_demo(folder)
        assert (folder / SEEDED_MARK).exists()
        assert not (folder / "state" / "stray").exists()
        assert len(load_config(path, env={}).jobs) == DEMO_JOBS


class TestFolderNames:
    """Folder paths are quoted in the YAML; ones the shell or YAML cannot hold are refused."""

    @pytest.mark.parametrize("name", ["a: b", "c #d", "plain", "x [y]"])
    def test_awkward_but_fine(self, tmp_path: Path, name: str) -> None:
        """A folder with ``: ``, `` #`` or brackets still gives a config naming it exactly."""
        folder = tmp_path / name
        folder.mkdir()
        path = folder / demo.CONFIG_NAME
        path.write_text(config_text(folder, folder / "t.yaml", folder / "u.yaml"), encoding="utf-8")
        settings = load_config(path, env={}).settings
        assert settings.state_dir == folder / "state"
        assert settings.users_file == folder / "u.yaml"

    @pytest.mark.parametrize("name", ["it's", 'say "hi"', "cost $5", "back`tick", "back\\slash"])
    def test_refused(self, tmp_path: Path, name: str) -> None:
        """A folder the generated shell command or YAML cannot hold is refused before writing."""
        folder = tmp_path / name
        with pytest.raises(ValueError, match="demo folder"):
            seed_demo(folder)
        assert not folder.exists()


class TestInterrupted:
    """Ctrl-C during the seed prints one line, not a traceback."""

    def test_main_says_seed_interrupted(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``main`` turns ``SeedInterruptedError`` into ``seed interrupted`` and exit 130."""

        def interrupted(where: Path) -> Path:
            """Stand in for a seed stopped by Ctrl-C."""
            raise SeedInterruptedError(str(where))

        monkeypatch.setattr(demo, "seed_demo", interrupted)
        assert demo.main(["--seed-only", "--folder", "unused"]) == INTERRUPTED
        assert capsys.readouterr().err == "seed interrupted\n"
