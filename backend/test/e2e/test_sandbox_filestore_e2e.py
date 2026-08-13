from __future__ import annotations

import uuid

import pytest

from yuxi.agents.backends.sandbox.backend import ProvisionerSandboxBackend
from yuxi.agents.backends.sandbox.provider import get_sandbox_provider
from yuxi.storage.filestore import (
    get_file_store,
    thread_output_key,
    thread_skill_key,
    thread_upload_key,
    user_workspace_key,
)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_real_sandbox_round_trips_filestore_changes_after_recreation() -> None:
    """验证真实 Sandbox 与 FileStore 的初始化、回写和重建恢复。"""
    run_id = uuid.uuid4().hex
    uid = f"e2e-user-{run_id}"
    thread_id = f"e2e-thread-{run_id}"
    skill_name = f"e2e-skill-{run_id}"
    store = get_file_store()
    provider = get_sandbox_provider()
    backend: ProvisionerSandboxBackend | None = None

    upload_key = thread_upload_key(thread_id, "input.txt")
    workspace_key = user_workspace_key(uid, "notes/state.txt")
    skill_key = thread_skill_key(thread_id, f"{skill_name}/SKILL.md")
    deleted_output_key = thread_output_key(thread_id, "delete-me.txt")
    text_output_key = thread_output_key(thread_id, "created/report.txt")
    binary_output_key = thread_output_key(thread_id, "created/payload.bin")

    try:
        await store.put(upload_key, b"uploaded text\n", content_type="text/plain")
        await store.put(workspace_key, b"workspace before\n", content_type="text/plain")
        await store.put(
            skill_key,
            f"---\nname: {skill_name}\ndescription: E2E skill\n---\n# E2E\n".encode(),
            content_type="text/markdown",
        )
        await store.put(deleted_output_key, b"delete this\n", content_type="text/plain")

        backend = ProvisionerSandboxBackend(thread_id=thread_id, uid=uid, inherit_env=False)
        assert backend.read("/home/gem/user-data/uploads/input.txt").file_data == {
            "content": "uploaded text",
            "encoding": "utf-8",
        }
        assert backend.read("/home/gem/user-data/workspace/notes/state.txt").file_data == {
            "content": "workspace before",
            "encoding": "utf-8",
        }
        assert backend.read(f"/home/gem/skills/{skill_name}/SKILL.md").error is None
        hidden_skills_path = backend.execute("test ! -e /home/gem/.yuxi-skills-rw")
        assert hidden_skills_path.exit_code == 0
        skills_write = backend.execute(f"printf hacked > /home/gem/skills/{skill_name}/SKILL.md")
        assert skills_write.exit_code != 0
        assert (await store.read(skill_key)).data.startswith(b"---\n")

        result = backend.execute(
            "mkdir -p /home/gem/user-data/outputs/created && "
            "printf 'created text\\n' > /home/gem/user-data/outputs/created/report.txt && "
            "python3 -c \"open('/home/gem/user-data/outputs/created/payload.bin', 'wb')"
            ".write(bytes([0, 255, 16, 128]))\" && "
            "printf 'workspace after\\n' > /home/gem/user-data/workspace/notes/state.txt && "
            "rm /home/gem/user-data/outputs/delete-me.txt && "
            "python3 -c 'raise SystemExit(23)'"
        )

        assert result.exit_code == 23
        assert (await store.read(workspace_key)).data == b"workspace after\n"
        assert (await store.read(text_output_key)).data == b"created text\n"
        assert (await store.read(binary_output_key)).data == bytes([0, 255, 16, 128])
        assert await store.list(deleted_output_key) == []

        provider.release(thread_id, uid=uid)
        backend = ProvisionerSandboxBackend(thread_id=thread_id, uid=uid, inherit_env=False)

        assert backend.read("/home/gem/user-data/workspace/notes/state.txt").file_data == {
            "content": "workspace after",
            "encoding": "utf-8",
        }
        assert backend.read("/home/gem/user-data/outputs/created/report.txt").file_data == {
            "content": "created text",
            "encoding": "utf-8",
        }
        binary_download = backend.download_files(["/home/gem/user-data/outputs/created/payload.bin"])[0]
        assert binary_download.error is None
        assert binary_download.content == bytes([0, 255, 16, 128])
        assert backend.read("/home/gem/user-data/outputs/delete-me.txt").file_data is None
        assert await store.list(deleted_output_key) == []
    finally:
        try:
            provider.release(thread_id, uid=uid)
        except Exception:  # noqa: BLE001
            pass
        await store.delete_prefix(f"threads/{thread_id}/")
        await store.delete_prefix(f"users/{uid}/workspace/")
