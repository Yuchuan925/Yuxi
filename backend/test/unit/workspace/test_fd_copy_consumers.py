"""Workspace 与共享 Skill 的真实 fd 复制回归。"""

import os

import pytest

from yuxi.infrastructure.filesystem import open_directory_fd
from yuxi.modules.agents.services import artifacts
from yuxi.modules.workspace import filesystem
from yuxi.modules.workspace.errors import FileTransferLimitError


@pytest.fixture(params=["workspace", "skill"])
def copy_file(request, tmp_path, monkeypatch):
    """通过两个真实消费者复制，授权与文件打开仍由消费者拥有。"""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(filesystem, "user_workspace_dir", lambda _uid: root)

    def copy(name, target, limit):
        """保持入口各自的 no-follow 路径边界。"""
        if request.param == "workspace":
            return filesystem.Workspace("user-1").download_authorized_file_to_path(f"/{name}", str(target), limit)
        root_fd = open_directory_fd(root, ())
        try:
            return artifacts._copy_from_shared_skill_fd(root_fd, (name,), str(target), limit)
        finally:
            os.close(root_fd)

    return root, copy


def test_copy_consumer_preserves_bytes_and_handles_short_writes(copy_file, tmp_path, monkeypatch):
    """两个入口在短写下都完整复制并截断已有目标。"""
    root, copy = copy_file
    (root / "source").write_bytes(b"abcdef")
    target = tmp_path / "target"
    target.write_bytes(b"long original content")
    original_write = os.write

    def short_write(fd, content):
        """每次写入两个字节，保留真实文件结果 oracle。"""
        return original_write(fd, content[:2])

    monkeypatch.setattr(os, "write", short_write)
    assert copy("source", target, 6) == 6
    assert target.read_bytes() == b"abcdef"


def test_copy_consumer_preserves_transfer_error(copy_file, tmp_path):
    """公共超限异常仍被原有 Workspace 异常入口识别。"""
    root, copy = copy_file
    (root / "source").write_bytes(b"abcdef")
    target = tmp_path / "target"
    target.touch()
    with pytest.raises(FileTransferLimitError):
        copy("source", target, 5)
    assert (root / "source").read_bytes() == b"abcdef"
    assert len(target.read_bytes()) <= 5


@pytest.mark.parametrize("link_location", ["source", "target"])
def test_copy_consumer_rejects_symlink_without_changing_outside(copy_file, tmp_path, link_location):
    """源及目标链接均不能绕过原有 no-follow 边界。"""
    root, copy = copy_file
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    source = root / "source"
    target = tmp_path / "target"
    if link_location == "source":
        source.symlink_to(outside)
        target.write_bytes(b"original")
    else:
        source.write_bytes(b"source")
        target.symlink_to(outside)
    with pytest.raises((PermissionError, OSError)):
        copy("source", target, 100)
    assert outside.read_bytes() == b"outside"
    if link_location == "source":
        assert target.read_bytes() == b"original"
