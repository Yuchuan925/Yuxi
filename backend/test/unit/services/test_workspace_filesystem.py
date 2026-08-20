from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from yuxi.services import workspace_filesystem as workspace_filesystem_module
from yuxi.services.workspace_filesystem import WorkspaceFilesystem


def test_upload_authorized_file_is_writable_across_runtime_uid(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    source = tmp_path / "source.txt"
    source.write_text("content", encoding="utf-8")
    monkeypatch.setattr(workspace_filesystem_module, "user_workspace_dir", lambda _uid: workspace_root)

    previous_umask = os.umask(0o077)
    try:
        WorkspaceFilesystem("user-1").upload_authorized_file_from_path(
            "/home/gem/user-data/projects/workdir-1/file.txt",
            str(source),
        )
    finally:
        os.umask(previous_umask)

    target = workspace_root / "projects" / "workdir-1" / "file.txt"
    assert target.read_text(encoding="utf-8") == "content"
    assert target.stat().st_mode & 0o777 == 0o666
    assert target.parent.stat().st_mode & 0o777 == 0o777


def test_create_authorized_directory_is_writable_across_runtime_uid(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setattr(workspace_filesystem_module, "user_workspace_dir", lambda _uid: workspace_root)

    previous_umask = os.umask(0o077)
    try:
        path = WorkspaceFilesystem("user-1").create_authorized_directory(
            "/home/gem/user-data",
            "project",
            root="/home/gem/user-data",
        )
    finally:
        os.umask(previous_umask)

    assert path == "/home/gem/user-data/project"
    assert (workspace_root / "project").stat().st_mode & 0o777 == 0o777


def test_create_authorized_directory_removes_partial_directory_when_permission_update_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setattr(workspace_filesystem_module, "user_workspace_dir", lambda _uid: workspace_root)
    monkeypatch.setattr(workspace_filesystem_module.os, "fchmod", Mock(side_effect=OSError("boom")))

    with pytest.raises(OSError, match="boom"):
        WorkspaceFilesystem("user-1").create_authorized_directory(
            "/home/gem/user-data",
            "project",
            root="/home/gem/user-data",
        )

    assert not (workspace_root / "project").exists()
