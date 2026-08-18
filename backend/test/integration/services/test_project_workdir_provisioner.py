"""真实双 Sandbox 的 Project Workdir 挂载契约测试。"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from yuxi.agents.backends.sandbox import ProvisionerSandboxBackend, get_sandbox_provider
from yuxi.agents.skills.service import sync_user_accessible_skills_async

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_two_sandboxes_share_project_files_but_not_runtime_state():
    suffix = uuid.uuid4().hex
    uid = f"pytest-project-{suffix}"
    workdir_id = f"workdir-{suffix}"
    first_scope = f"pytest-runtime-a-{suffix}"
    second_scope = f"pytest-runtime-b-{suffix}"
    project_root = f"/home/gem/projects/project-{workdir_id}"
    project_file = f"{project_root}/outputs/shared.txt"
    runtime_file = f"/tmp/yuxi-runtime-{suffix}"

    first = ProvisionerSandboxBackend(thread_id=first_scope, uid=uid, workdir_id=workdir_id)
    second = ProvisionerSandboxBackend(thread_id=second_scope, uid=uid, workdir_id=workdir_id)

    try:
        first_result = await asyncio.to_thread(
            first.execute,
            f"mkdir -p {project_root}/outputs && printf shared-bytes > {project_file} "
            f"&& printf private-runtime > {runtime_file}",
        )
        assert first_result.exit_code == 0, first_result.output

        read_result = await asyncio.to_thread(second.execute, f"cat {project_file}")
        assert read_result.exit_code == 0, read_result.output
        assert read_result.output == "shared-bytes"

        runtime_result = await asyncio.to_thread(second.execute, f"test ! -e {runtime_file}")
        assert runtime_result.exit_code == 0, runtime_result.output

        first_connection = get_sandbox_provider().get(
            first_scope,
            uid=uid,
            workdir_id=workdir_id,
        )
        second_connection = get_sandbox_provider().get(
            second_scope,
            uid=uid,
            workdir_id=workdir_id,
        )
        assert first_connection is not None and second_connection is not None
        assert first_connection.sandbox_id != second_connection.sandbox_id
        assert first_connection.generation and second_connection.generation
        assert first_connection.generation != second_connection.generation
    finally:
        try:
            await asyncio.to_thread(first.execute, f"rm -f {project_file} {runtime_file}")
        except Exception:
            pass
        for scope in (first_scope, second_scope):
            try:
                await asyncio.to_thread(
                    get_sandbox_provider().release,
                    scope,
                    uid=uid,
                    workdir_id=workdir_id,
                    clear_cache_on_delete_failure=True,
                )
            except Exception:
                pass


async def test_recreated_runtime_keeps_project_files_and_drops_process_state():
    suffix = uuid.uuid4().hex
    uid = f"pytest-project-recreate-{suffix}"
    workdir_id = f"workdir-{suffix}"
    first_scope = f"pytest-runtime-before-{suffix}"
    second_scope = f"pytest-runtime-after-{suffix}"
    project_root = f"/home/gem/projects/project-{workdir_id}"
    project_file = f"{project_root}/outputs/persistent.txt"
    runtime_file = f"/tmp/yuxi-runtime-{suffix}"
    provider = get_sandbox_provider()

    first = ProvisionerSandboxBackend(thread_id=first_scope, uid=uid, workdir_id=workdir_id)
    second = ProvisionerSandboxBackend(thread_id=second_scope, uid=uid, workdir_id=workdir_id)
    try:
        result = await asyncio.to_thread(
            first.execute,
            f"mkdir -p {project_root}/outputs && printf persistent > {project_file} "
            f"&& printf transient > {runtime_file}",
        )
        assert result.exit_code == 0, result.output
        await asyncio.to_thread(
            provider.release,
            first_scope,
            uid=uid,
            workdir_id=workdir_id,
            clear_cache_on_delete_failure=True,
        )

        result = await asyncio.to_thread(second.execute, f"cat {project_file} && test ! -e {runtime_file}")
        assert result.exit_code == 0, result.output
        assert result.output == "persistent"
    finally:
        try:
            await asyncio.to_thread(second.execute, f"rm -f {project_file}")
        except Exception:
            pass
        for scope in (first_scope, second_scope):
            try:
                await asyncio.to_thread(
                    provider.release,
                    scope,
                    uid=uid,
                    workdir_id=workdir_id,
                    clear_cache_on_delete_failure=True,
                )
            except Exception:
                pass


async def test_file_bridge_remains_available_when_execution_runtime_is_released():
    """Viewer 文件桥接与执行 runtime 共享字节，但不共享删除生命周期。"""
    suffix = uuid.uuid4().hex
    uid = f"pytest-file-bridge-{suffix}"
    workdir_id = f"workdir-{suffix}"
    runtime_scope = f"pytest-runtime-{suffix}"
    file_scope = f"workdir-files-{workdir_id}"
    project_root = f"/home/gem/projects/project-{workdir_id}"
    project_file = f"{project_root}/outputs/realtime.txt"
    provider = get_sandbox_provider()
    runtime_backend = ProvisionerSandboxBackend(
        thread_id=runtime_scope,
        uid=uid,
        workdir_id=workdir_id,
    )
    file_backend = ProvisionerSandboxBackend(
        thread_id=file_scope,
        uid=uid,
        sandbox_instance_id=file_scope,
        workdir_id=workdir_id,
    )
    try:
        result = await asyncio.to_thread(
            runtime_backend.execute,
            f"mkdir -p {project_root}/outputs && printf realtime > {project_file}",
        )
        assert result.exit_code == 0, result.output
        before_release = await asyncio.to_thread(
            file_backend.list_authorized_directory,
            f"{project_root}/outputs",
            root=project_root,
        )
        assert [item["name"] for item in before_release] == ["realtime.txt"]

        await asyncio.to_thread(
            provider.release,
            runtime_scope,
            uid=uid,
            workdir_id=workdir_id,
            clear_cache_on_delete_failure=True,
        )

        read_result = await asyncio.to_thread(file_backend.read, project_file)
        assert read_result.error is None
        assert read_result.file_data == {"content": "realtime", "encoding": "utf-8"}
    finally:
        try:
            await asyncio.to_thread(file_backend.execute, f"rm -f {project_file}")
        except Exception:
            pass
        for scope in (runtime_scope, file_scope):
            try:
                await asyncio.to_thread(
                    provider.release,
                    scope,
                    uid=uid,
                    sandbox_instance_id=scope,
                    workdir_id=workdir_id,
                    clear_cache_on_delete_failure=True,
                )
            except Exception:
                pass


async def test_user_skill_projection_is_shared_across_sandboxes_but_isolated_by_uid(tmp_path):
    """同一用户的 Sandbox 共享授权 Skill 文件，不同用户不可读。"""
    suffix = uuid.uuid4().hex
    uid = f"pytest-skills-{suffix}"
    other_uid = f"pytest-skills-other-{suffix}"
    first_scope = f"pytest-skills-runtime-a-{suffix}"
    second_scope = f"pytest-skills-runtime-b-{suffix}"
    other_scope = f"pytest-skills-runtime-other-{suffix}"
    selected_source = tmp_path / "selected"
    unselected_source = tmp_path / "authorized-unselected"
    selected_source.mkdir()
    unselected_source.mkdir()
    (selected_source / "SKILL.md").write_text("selected-skill", encoding="utf-8")
    (unselected_source / "SKILL.md").write_text("authorized-unselected-skill", encoding="utf-8")

    await sync_user_accessible_skills_async(
        uid,
        {
            "selected": selected_source,
            "authorized-unselected": unselected_source,
        },
    )
    await sync_user_accessible_skills_async(other_uid, {})

    first = ProvisionerSandboxBackend(thread_id=first_scope, uid=uid)
    second = ProvisionerSandboxBackend(thread_id=second_scope, uid=uid)
    other = ProvisionerSandboxBackend(thread_id=other_scope, uid=other_uid)
    try:
        write_result = await asyncio.to_thread(
            first.execute,
            "printf tampered > /home/gem/skills/selected/SKILL.md",
        )
        first_selected = await asyncio.to_thread(first.read, "/home/gem/skills/selected/SKILL.md")
        second_unselected = await asyncio.to_thread(
            second.read,
            "/home/gem/skills/authorized-unselected/SKILL.md",
        )
        other_selected = await asyncio.to_thread(other.read, "/home/gem/skills/selected/SKILL.md")

        assert write_result.exit_code != 0
        assert first_selected.error is None
        assert first_selected.file_data == {"content": "selected-skill", "encoding": "utf-8"}
        assert second_unselected.error is None
        assert second_unselected.file_data == {
            "content": "authorized-unselected-skill",
            "encoding": "utf-8",
        }
        assert other_selected.error and "does not exist" in other_selected.error.lower()
    finally:
        await sync_user_accessible_skills_async(uid, {})
        await sync_user_accessible_skills_async(other_uid, {})
        for scope, scope_uid in (
            (first_scope, uid),
            (second_scope, uid),
            (other_scope, other_uid),
        ):
            try:
                await asyncio.to_thread(
                    get_sandbox_provider().release,
                    scope,
                    uid=scope_uid,
                    clear_cache_on_delete_failure=True,
                )
            except Exception:
                pass
