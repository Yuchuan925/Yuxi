"""文件流消费者在取消时必须先等待 I/O 结束。"""

import asyncio
from io import BytesIO
from threading import Event

import pytest

from yuxi.modules.workspace import filesystem
from yuxi.modules.workspace.filesystem import Workspace
from yuxi.modules.workspace.services.files import _write_workspace_upload
from yuxi.shared.files import FileInput


@pytest.mark.asyncio
async def test_cancelled_upload_waits_for_borrowed_stream_before_returning(tmp_path, monkeypatch):
    """请求取消不能让输入流在写入线程仍消费时被关闭。"""
    monkeypatch.setattr(filesystem, "user_workspace_dir", lambda _uid: tmp_path)
    started = Event()
    release = Event()

    class BlockingSource(BytesIO):
        """在首次读取时暂停线程，模拟慢磁盘。"""

        def read(self, size=-1):
            """让测试在 I/O 中途取消服务调用。"""
            started.set()
            assert release.wait(5)
            assert not self.closed
            return super().read(size)

    source = BlockingSource(b"content")
    task = asyncio.create_task(_write_workspace_upload(FileInput("file.txt", source), Workspace("user-1"), "/file.txt"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        source.close()
        assert (tmp_path / "file.txt").read_bytes() == b"content"
        assert not list(tmp_path.glob(".yuxi-write-*"))
    finally:
        release.set()
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        source.close()
