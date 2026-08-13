from yuxi.agents.backends.sandbox import VIRTUAL_PATH_PREFIX
from yuxi.agents.buildin.chatbot.state import merge_subagent_runs
from yuxi.agents.state import merge_artifacts
from yuxi.agents.toolkits.buildin import tools
from yuxi.agents.toolkits.buildin.tools import _normalize_presented_artifact_path
from yuxi.services.chat_service import extract_agent_state
from yuxi.storage.filestore import LocalFileStore, thread_output_key
from yuxi.utils.paths import CONVERSATION_HISTORY_DIR_NAME, LARGE_TOOL_RESULTS_DIR_NAME


def _runtime_with_thread(thread_id: str, uid: str = "user-1"):
    context = type(
        "RuntimeContext",
        (),
        {"thread_id": thread_id, "file_thread_id": thread_id, "uid": uid},
    )()
    return type("RuntimeStub", (), {"context": context})()


def test_merge_artifacts_deduplicates_and_preserves_order():
    assert merge_artifacts(
        ["/home/gem/user-data/outputs/a.md"],
        ["/home/gem/user-data/outputs/a.md", "/home/gem/user-data/outputs/b.md"],
    ) == [
        "/home/gem/user-data/outputs/a.md",
        "/home/gem/user-data/outputs/b.md",
    ]


def test_merge_subagent_runs_does_not_merge_entries_without_run_id():
    assert merge_subagent_runs(
        [{"id": "run-1", "status": "completed"}],
        [
            {"id": "run-1", "status": "failed", "error": "boom"},
            {"id": "run-2", "status": "completed"},
        ],
    ) == [
        {"id": "run-1", "status": "completed"},
        {"id": "run-1", "status": "failed", "error": "boom"},
        {"id": "run-2", "status": "completed"},
    ]


def test_merge_subagent_runs_updates_existing_run_by_run_id():
    assert merge_subagent_runs(
        [
            {
                "id": "tool-1",
                "run_id": "agent-run-1",
                "child_thread_id": "child-thread",
                "status": "running",
            }
        ],
        [
            {
                "id": "tool-1",
                "run_id": "agent-run-1",
                "child_thread_id": "child-thread",
                "status": "completed",
            }
        ],
    ) == [
        {
            "id": "tool-1",
            "run_id": "agent-run-1",
            "child_thread_id": "child-thread",
            "status": "completed",
        }
    ]


def test_merge_subagent_runs_keeps_continuation_run_history():
    assert merge_subagent_runs(
        [
            {
                "id": "tool-old",
                "run_id": "agent-run-old",
                "child_thread_id": "child-thread",
                "status": "completed",
                "completed_at": "2026-06-20T01:00:00Z",
            }
        ],
        [
            {
                "id": "tool-new",
                "run_id": "agent-run-new",
                "child_thread_id": "child-thread",
                "status": "pending",
            }
        ],
    ) == [
        {
            "id": "tool-old",
            "run_id": "agent-run-old",
            "child_thread_id": "child-thread",
            "status": "completed",
            "completed_at": "2026-06-20T01:00:00Z",
        },
        {
            "id": "tool-new",
            "run_id": "agent-run-new",
            "child_thread_id": "child-thread",
            "status": "pending",
        },
    ]


def test_merge_subagent_runs_does_not_merge_different_run_ids_by_state_id():
    assert merge_subagent_runs(
        [
            {
                "id": "tool-1",
                "run_id": "agent-run-old",
                "child_thread_id": "child-thread",
                "status": "completed",
            }
        ],
        [
            {
                "id": "tool-1",
                "run_id": "agent-run-new",
                "child_thread_id": "child-thread",
                "status": "pending",
            }
        ],
    ) == [
        {
            "id": "tool-1",
            "run_id": "agent-run-old",
            "child_thread_id": "child-thread",
            "status": "completed",
        },
        {
            "id": "tool-1",
            "run_id": "agent-run-new",
            "child_thread_id": "child-thread",
            "status": "pending",
        },
    ]


async def test_normalize_presented_artifact_path_rejects_host_path():
    try:
        await _normalize_presented_artifact_path("/app/saves/outputs/report.md", _runtime_with_thread("thread-1"))
    except ValueError as exc:
        assert f"{VIRTUAL_PATH_PREFIX}/outputs/" in str(exc)
    else:
        raise AssertionError("expected ValueError for host path")


async def test_normalize_presented_artifact_path_accepts_virtual_path(monkeypatch, tmp_path):
    thread_id = "artifacts-virtual-path"
    store = LocalFileStore(tmp_path)
    await store.put(thread_output_key(thread_id, "summary.txt"), b"demo")
    monkeypatch.setattr(tools, "get_file_store", lambda: store)

    normalized = await _normalize_presented_artifact_path(
        f"{VIRTUAL_PATH_PREFIX}/outputs/summary.txt",
        _runtime_with_thread(thread_id),
    )

    assert normalized == f"{VIRTUAL_PATH_PREFIX}/outputs/summary.txt"


async def test_normalize_presented_artifact_path_rejects_non_outputs_path():
    try:
        await _normalize_presented_artifact_path(
            f"{VIRTUAL_PATH_PREFIX}/uploads/note.txt",
            _runtime_with_thread("artifacts-reject-path"),
        )
    except ValueError as exc:
        assert f"{VIRTUAL_PATH_PREFIX}/outputs/" in str(exc)
    else:
        raise AssertionError("expected ValueError for non-outputs file")


async def test_normalize_presented_artifact_path_rejects_internal_output_files():
    for dir_name in [LARGE_TOOL_RESULTS_DIR_NAME, CONVERSATION_HISTORY_DIR_NAME, "large_tool_history"]:
        try:
            await _normalize_presented_artifact_path(
                f"{VIRTUAL_PATH_PREFIX}/outputs/{dir_name}/stage.txt",
                _runtime_with_thread("artifacts-reject-internal"),
            )
        except ValueError as exc:
            assert "工具调用阶段文件" in str(exc)
        else:
            raise AssertionError(f"expected ValueError for internal output file under {dir_name}")


def test_extract_agent_state_includes_artifacts():
    state = extract_agent_state(
        {
            "todos": [{"content": "done", "status": "completed"}],
            "files": {"/tmp/demo.txt": {"content": ["x"]}},
            "artifacts": ["/home/gem/user-data/outputs/demo.txt"],
            "subagent_runs": [{"id": "tool-1", "status": "completed"}],
            "token_usage": {"llm_input_tokens": 42},
        }
    )

    assert state["todos"] == [{"content": "done", "status": "completed"}]
    assert state["files"] == {"/tmp/demo.txt": {"content": ["x"]}}
    assert state["artifacts"] == ["/home/gem/user-data/outputs/demo.txt"]
    assert state["subagent_runs"] == [{"id": "tool-1", "status": "completed"}]
    assert state["token_usage"] == {"llm_input_tokens": 42}
