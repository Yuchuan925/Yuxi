from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxi.agents.toolkits.buildin import tools
from yuxi.storage.filestore import LocalFileStore, thread_output_key

pytestmark = pytest.mark.unit


def _runtime(*, file_thread_id: str | None = "file-thread-1") -> SimpleNamespace:
    return SimpleNamespace(context=SimpleNamespace(file_thread_id=file_thread_id, uid="user-1"))


@pytest.mark.asyncio
async def test_present_artifacts_validates_output_object_with_filestore(tmp_path: Path, monkeypatch) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    monkeypatch.setattr(tools, "get_file_store", lambda: store)
    await store.put(thread_output_key("file-thread-1", "reports/result.md"), b"# result")

    result = await tools.present_artifacts.coroutine(
        filepaths=["/home/gem/user-data/outputs/reports/result.md"],
        runtime=_runtime(),
        tool_call_id="call-1",
    )

    assert result.update["artifacts"] == ["/home/gem/user-data/outputs/reports/result.md"]
    assert result.update["messages"][0].content == "已将交付物展示给用户"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filepath",
    [
        "/tmp/result.md",
        "home/gem/user-data/outputs/result.md",
        "/home/gem/user-data/outputs/../result.md",
        "/home/gem/user-data/outputs//result.md",
        "/home/gem/user-data/outputs/large_tool_results/result.md",
    ],
)
async def test_present_artifacts_rejects_noncanonical_or_internal_path(tmp_path, monkeypatch, filepath) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    monkeypatch.setattr(tools, "get_file_store", lambda: store)

    result = await tools.present_artifacts.coroutine(
        filepaths=[filepath],
        runtime=_runtime(),
        tool_call_id="call-1",
    )

    assert result.update["messages"][0].content.startswith("Error:")


@pytest.mark.asyncio
async def test_present_artifacts_rejects_missing_filestore_object(tmp_path, monkeypatch) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    monkeypatch.setattr(tools, "get_file_store", lambda: store)

    result = await tools.present_artifacts.coroutine(
        filepaths=["/home/gem/user-data/outputs/missing.md"],
        runtime=_runtime(),
        tool_call_id="call-1",
    )

    assert "文件不存在" in result.update["messages"][0].content
