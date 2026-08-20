import errno
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from yuxi.utils import paths as paths_module
from yuxi.utils.paths import ensure_within_root, open_directory_fd


def test_open_directory_fd_creates_nested_directories_without_taking_root_fd(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    previous_umask = os.umask(0o077)
    try:
        directory_fd = open_directory_fd(root_fd, ("first", "second"), create=True)
    finally:
        os.umask(previous_umask)
    try:
        assert os.fstat(directory_fd).st_ino == (root / "first" / "second").stat().st_ino
        assert os.fstat(root_fd).st_ino == root.stat().st_ino
        assert (root / "first").stat().st_mode & 0o777 == 0o777
        assert (root / "first" / "second").stat().st_mode & 0o777 == 0o777
    finally:
        os.close(directory_fd)
        os.close(root_fd)


def test_open_directory_fd_distinguishes_symlink_and_non_directory_components(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "target").mkdir()
    (root / "linked").symlink_to(root / "target", target_is_directory=True)
    (root / "file").write_text("not a directory", encoding="utf-8")

    with pytest.raises(OSError) as symlink_error:
        open_directory_fd(root, ("linked",))
    assert symlink_error.value.errno == errno.ELOOP

    with pytest.raises(NotADirectoryError) as non_directory_error:
        open_directory_fd(root, ("file",))
    assert non_directory_error.value.errno == errno.ENOTDIR


def test_open_directory_fd_closes_internal_fd_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []

    def fail_open(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(paths_module.os, "dup", lambda _fd: 101)
    monkeypatch.setattr(paths_module.os, "open", fail_open)
    monkeypatch.setattr(paths_module.os, "close", closed.append)

    with pytest.raises(OSError, match="boom"):
        open_directory_fd(7, ("child",))

    assert closed == [101]


def test_open_directory_fd_closes_new_child_when_permission_update_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    removed: list[tuple[str, int | None]] = []

    monkeypatch.setattr(paths_module.os, "dup", lambda _fd: 101)
    monkeypatch.setattr(paths_module.os, "mkdir", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(paths_module.os, "open", lambda *_args, **_kwargs: 202)
    monkeypatch.setattr(paths_module.os, "fchmod", Mock(side_effect=OSError("boom")))
    monkeypatch.setattr(paths_module.os, "close", closed.append)
    monkeypatch.setattr(
        paths_module.os,
        "rmdir",
        lambda path, *, dir_fd=None: removed.append((path, dir_fd)),
    )

    with pytest.raises(OSError, match="boom"):
        open_directory_fd(7, ("child",), create=True)

    assert closed == [202, 101]
    assert removed == [("child", 101)]


def test_open_directory_fd_removes_new_directory_when_permission_update_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    real_fchmod = os.fchmod
    attempts = 0

    def fail_first_permission_update(fd: int, mode: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("boom")
        real_fchmod(fd, mode)

    monkeypatch.setattr(paths_module.os, "fchmod", fail_first_permission_update)

    with pytest.raises(OSError, match="boom"):
        open_directory_fd(root, ("child",), create=True)
    assert not (root / "child").exists()

    directory_fd = open_directory_fd(root, ("child",), create=True)
    try:
        assert (root / "child").stat().st_mode & 0o777 == 0o777
    finally:
        os.close(directory_fd)


def test_ensure_within_root_returns_root_and_descendant(tmp_path: Path) -> None:
    root = tmp_path / "root"
    child = root / "nested" / "file.txt"

    assert ensure_within_root(root, root, error_message="outside") == root
    assert ensure_within_root(child, root, error_message="outside") == child


def test_ensure_within_root_rejects_sibling_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    sibling = tmp_path / "root-other" / "file.txt"

    with pytest.raises(ValueError, match="outside"):
        ensure_within_root(sibling, root, error_message="outside")
