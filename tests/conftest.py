from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

import pytest

from anchor import Anchor

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """A directory of its own for every test.

    Not a shared one. The crash tests leave real child processes writing, and a
    shared directory turns one slow teardown into a failure somewhere else.
    """
    directory = tmp_path / "store"
    directory.mkdir()
    return directory


@pytest.fixture
def db(data_dir: pathlib.Path):
    store = Anchor.open(data_dir)
    yield store
    store.close()


@pytest.fixture
def small_db(data_dir: pathlib.Path):
    """Segments small enough that a handful of writes rotates them."""
    store = Anchor.open(data_dir, max_segment_bytes=256)
    yield store
    store.close()


@pytest.fixture
def spawn(data_dir: pathlib.Path):
    """Run a writer as a real child process, so it can really be killed."""
    started: list[subprocess.Popen] = []

    def start(script: str, *args: str) -> subprocess.Popen:
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(data_dir), *args],
            cwd=REPO_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        started.append(proc)
        return proc

    yield start

    # Teardown kills children itself rather than trusting them to have exited.
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def wait_for(predicate, *, timeout_s: float = 15.0, interval_s: float = 0.02, what: str = "condition"):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval_s)
    pytest.fail(f"timed out after {timeout_s:g}s waiting for {what}")


def data_files(directory: pathlib.Path) -> list[pathlib.Path]:
    return sorted(directory.glob("*.data"))


def hint_files(directory: pathlib.Path) -> list[pathlib.Path]:
    return sorted(directory.glob("*.hint"))
