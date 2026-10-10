import pytest

from yuxi.modules.agents.runtime.sandbox import backend as sandbox_backend
from yuxi.modules.agents.runtime.state import merge_artifacts
from pydantic import ValidationError

from yuxi.modules.extensions.tools.builtin.present_artifacts import (
    PresentArtifactsInput,
    _normalize_presented_artifact_path,
    present_artifacts,
)
from yuxi.modules.agents.runtime.sandbox.paths import CONVERSATION_HISTORY_DIR_NAME, LARGE_TOOL_RESULTS_DIR_NAME


def _runtime_with_thread(thread_id: str, uid: str = "user-1"):
    context = type(
        "RuntimeContext",
        (),
        {
            "thread_id": thread_id,
            "runtime_scope_id": thread_id,
            "workdir_relative_path": "projects/11111111-1111-4111-8111-111111111111",
            "workdir_path": "/home/gem/user-data/projects/11111111-1111-4111-8111-111111111111",
            "uid": uid,
        },
    )()
    return type("RuntimeStub", (), {"context": context})()


def _stub_output_exists(monkeypatch: pytest.MonkeyPatch, exists: bool = True) -> None:
    class FakeBackend:
        def __init__(self, **kwargs):
            assert kwargs["create_if_missing"] is True

        def regular_file_exists(self, _path: str) -> bool:
            return exists

    monkeypatch.setattr(sandbox_backend, "ProvisionerSandboxBackend", FakeBackend)


def test_merge_artifacts_deduplicates_and_preserves_order():
    assert merge_artifacts(
        ["/home/gem/user-data/outputs/a.md"],
        ["/home/gem/user-data/outputs/a.md", "/home/gem/user-data/outputs/b.md"],
    ) == [
        {"path": "/home/gem/user-data/outputs/a.md", "type": "file"},
        {"path": "/home/gem/user-data/outputs/b.md", "type": "file"},
    ]


def test_merge_artifacts_keeps_latest_type_without_duplicate_paths():
    """同一路径的展示类型以最近登记为准，旧 checkpoint 仍可合并。"""
    assert merge_artifacts(
        ["/a.png", {"path": "/b.md", "type": "file"}],
        [{"path": "/a.png", "type": "image"}, {"path": "/c.png", "type": "file"}],
    ) == [
        {"path": "/a.png", "type": "image"},
        {"path": "/b.md", "type": "file"},
        {"path": "/c.png", "type": "file"},
    ]
    assert merge_artifacts(None, None) == []
    assert merge_artifacts(["/a.png"], None) == [{"path": "/a.png", "type": "file"}]


@pytest.mark.parametrize("artifact_type", ["file", "image"])
def test_present_artifacts_registers_explicit_type(monkeypatch, artifact_type):
    """同一 PNG 可按指定类型交付。"""
    _stub_output_exists(monkeypatch)
    path = "/home/gem/user-data/outputs/screenshot.png"
    result = present_artifacts.func(filepaths=[path], runtime=_runtime_with_thread("thread-1"), tool_call_id="call-1", type=artifact_type)
    assert result.update["artifacts"] == [{"path": path, "type": artifact_type}]
    assert result.update["messages"][0].content == "已将交付物展示给用户"


def test_present_artifacts_defaults_to_file_and_rejects_unknown_type(monkeypatch):
    """省略类型按文件交付，模型输入中的未知类型被 schema 拒绝。"""
    _stub_output_exists(monkeypatch)
    path = "/home/gem/user-data/outputs/screenshot.png"
    assert PresentArtifactsInput(filepaths=[path]).type == "file"
    result = present_artifacts.func(filepaths=[path], runtime=_runtime_with_thread("thread-1"), tool_call_id="call-1")
    assert result.update["artifacts"] == [{"path": path, "type": "file"}]
    with pytest.raises(ValidationError, match="file.*image"):
        PresentArtifactsInput(filepaths=[path], type="video")


def test_present_artifacts_does_not_register_missing_image(monkeypatch):
    """图片声明仍需经过普通文件边界，失败结果明确标记错误。"""
    _stub_output_exists(monkeypatch, exists=False)
    result = present_artifacts.func(
        filepaths=["/home/gem/user-data/outputs/missing.png"],
        runtime=_runtime_with_thread("thread-1"),
        tool_call_id="call-1",
        type="image",
    )
    assert "artifacts" not in result.update
    assert result.update["messages"][0].status == "error"
    assert "文件不存在或不是普通文件" in result.update["messages"][0].content


def test_normalize_presented_artifact_path_rejects_host_path():
    with pytest.raises(ValueError, match="可见范围"):
        _normalize_presented_artifact_path(
            "saves/threads/thread-1/user-data/outputs/report.md",
            _runtime_with_thread("thread-1"),
        )


def test_normalize_presented_artifact_path_accepts_virtual_path(monkeypatch: pytest.MonkeyPatch):
    thread_id = "artifacts-virtual-path"
    _stub_output_exists(monkeypatch)

    normalized = _normalize_presented_artifact_path(
        "/home/gem/user-data/projects/11111111-1111-4111-8111-111111111111/outputs/summary.txt",
        _runtime_with_thread(thread_id),
    )

    assert normalized == "/home/gem/user-data/projects/11111111-1111-4111-8111-111111111111/outputs/summary.txt"


def test_normalize_presented_artifact_path_accepts_any_visible_regular_file(monkeypatch: pytest.MonkeyPatch):
    thread_id = "artifacts-reject-path"
    _stub_output_exists(monkeypatch)

    assert (
        _normalize_presented_artifact_path(
            "/home/gem/user-data/projects/11111111-1111-4111-8111-111111111111/uploads/note.txt",
            _runtime_with_thread(thread_id),
        )
        == "/home/gem/user-data/projects/11111111-1111-4111-8111-111111111111/uploads/note.txt"
    )


def test_normalize_presented_artifact_path_does_not_special_case_internal_names(monkeypatch: pytest.MonkeyPatch):
    thread_id = "artifacts-reject-internal"
    _stub_output_exists(monkeypatch)

    for dir_name in [LARGE_TOOL_RESULTS_DIR_NAME, CONVERSATION_HISTORY_DIR_NAME, "large_tool_history"]:
        path = f"/home/gem/user-data/projects/11111111-1111-4111-8111-111111111111/outputs/{dir_name}/stage.txt"
        assert _normalize_presented_artifact_path(path, _runtime_with_thread(thread_id)) == path
