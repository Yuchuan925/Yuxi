from __future__ import annotations

from pathlib import Path

import pytest

import yuxi.services.legacy_workdir_importer as svc


def test_import_moves_verified_legacy_tree_into_user_workspace(monkeypatch, tmp_path: Path):
    legacy_projects = tmp_path / "legacy-projects"
    legacy_workdir = legacy_projects / "workdir-1"
    legacy_workdir.mkdir(parents=True)
    (legacy_workdir / "report.md").write_text("report", encoding="utf-8")
    legacy_storage = tmp_path / "legacy"
    uploads = legacy_storage / "threads" / "thread-1" / "user-data" / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "input.txt").write_text("input", encoding="utf-8")
    user_data = tmp_path / "user-data"

    monkeypatch.setenv("YUXI_LEGACY_PROJECTS_DIR", str(legacy_projects))
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(user_data))
    monkeypatch.setattr(svc, "get_legacy_storage_dir", lambda: legacy_storage)
    workdirs = (svc.LegacyWorkdirBinding("workdir-1", "user-1"),)
    conversations = (svc.LegacyConversationBinding("thread-1", "user-1", "workdir-1"),)

    svc.import_legacy_workdirs(workdirs, conversations)

    target = user_data / "shared" / "user-1" / "workspace" / "projects" / "workdir-1"
    assert (target / "report.md").read_text(encoding="utf-8") == "report"
    assert (target / "uploads" / "input.txt").read_text(encoding="utf-8") == "input"
    assert (target / "outputs").is_dir()
    assert legacy_workdir.is_dir()


def test_import_rejects_symlink_without_replacing_existing_target(monkeypatch, tmp_path: Path):
    legacy_projects = tmp_path / "legacy-projects"
    legacy_workdir = legacy_projects / "workdir-1"
    legacy_workdir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (legacy_workdir / "escape.txt").symlink_to(outside)
    user_data = tmp_path / "user-data"
    target = user_data / "shared" / "user-1" / "workspace" / "projects" / "workdir-1"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("keep", encoding="utf-8")

    monkeypatch.setenv("YUXI_LEGACY_PROJECTS_DIR", str(legacy_projects))
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(user_data))

    with pytest.raises(RuntimeError, match="symlink"):
        svc.import_legacy_workdirs((svc.LegacyWorkdirBinding("workdir-1", "user-1"),), ())

    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert outside.read_text(encoding="utf-8") == "secret"


def test_import_rejects_unsafe_legacy_identity(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("YUXI_LEGACY_PROJECTS_DIR", str(tmp_path / "legacy-projects"))
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "user-data"))

    with pytest.raises(RuntimeError, match="不安全"):
        svc.import_legacy_workdirs((svc.LegacyWorkdirBinding("../escape", "user-1"),), ())


def test_rewrite_attachment_paths_removes_legacy_object_metadata():
    record = {
        "file_id": "file-1",
        "path": "/home/gem/projects/project-workdir-1/uploads/report.md",
        "original_path": "/home/gem/user-data/uploads/report.txt",
        "bucket_name": "documents",
        "original_object_name": "threads/thread-1/attachments/file-1/original/report.txt",
        "minio_url": "minio://documents/object",
    }

    rewritten = svc._rewrite_attachment(
        "thread-1",
        "/home/gem/user-data/projects/workdir-1",
        record,
    )

    assert rewritten["path"] == "/home/gem/user-data/projects/workdir-1/uploads/report.md"
    assert rewritten["original_path"] == "/home/gem/user-data/projects/workdir-1/uploads/report.txt"
    assert "bucket_name" not in rewritten
    assert "original_object_name" not in rewritten
    assert "minio_url" not in rewritten


def test_cleanup_happens_only_when_called_after_import(monkeypatch, tmp_path: Path):
    legacy_projects = tmp_path / "legacy-projects"
    source = legacy_projects / "workdir-1"
    source.mkdir(parents=True)
    (source / "report.txt").write_text("report", encoding="utf-8")
    monkeypatch.setenv("YUXI_LEGACY_PROJECTS_DIR", str(legacy_projects))

    svc.cleanup_legacy_workdir_sources((svc.LegacyWorkdirBinding("workdir-1", "user-1"),), ())

    assert not source.exists()
