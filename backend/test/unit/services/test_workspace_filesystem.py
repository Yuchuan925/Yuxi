from __future__ import annotations

import os
from pathlib import Path

import pytest

from yuxi.services import workspace_filesystem as workspace_filesystem_module
from yuxi.services.workspace_filesystem import WorkspaceFilesystem


def test_upload_authorized_file_uses_owner_only_mode(
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
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("existing_kind", ["file", "symlink"])
def test_upload_without_overwrite_atomically_preserves_existing_entry(
    tmp_path: Path,
    monkeypatch,
    existing_kind: str,
) -> None:
    workspace_root = tmp_path / "workspace"
    project_root = workspace_root / "projects" / "workdir-1"
    project_root.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    target = project_root / "occupied.txt"
    if existing_kind == "file":
        target.write_text("original", encoding="utf-8")
    else:
        target.symlink_to(outside)
    source = tmp_path / "source.txt"
    source.write_text("replacement", encoding="utf-8")
    monkeypatch.setattr(workspace_filesystem_module, "user_workspace_dir", lambda _uid: workspace_root)

    with pytest.raises(FileExistsError):
        WorkspaceFilesystem("user-1").upload_authorized_file_from_path(
            "/home/gem/user-data/projects/workdir-1/occupied.txt",
            str(source),
            overwrite=False,
        )

    if existing_kind == "file":
        assert target.read_text(encoding="utf-8") == "original"
    else:
        assert target.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside"


def test_create_authorized_directory_uses_owner_only_mode(
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
    assert (workspace_root / "project").stat().st_mode & 0o777 == 0o700
