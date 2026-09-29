from __future__ import annotations

import importlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from yuxi.modules.extensions.skills import service as skill_service
from yuxi.modules.extensions.tools.builtin import install_skill as exported_install_skill

install_skill_module = importlib.import_module("yuxi.modules.extensions.tools.builtin.install_skill")
sandbox_backend_module = importlib.import_module("yuxi.modules.agents.runtime.backends.sandbox")


def _runtime(**context_values):
    return SimpleNamespace(context=SimpleNamespace(**context_values))


@pytest.mark.asyncio
async def test_install_personal_skill_does_not_require_agent_config_access(monkeypatch, tmp_path):
    """安装回读真实个人文件，数据库不可用也无需修改 Agent 配置。"""
    from yuxi.infrastructure.postgres.manager import pg_manager
    from yuxi.modules.workspace import paths

    def fail_get_session():
        """拒绝安装过程访问数据库。"""
        raise AssertionError("个人 Skill 安装不应访问 Agent 配置")

    monkeypatch.setattr(pg_manager, "get_async_session_context", fail_get_session)
    monkeypatch.setattr(paths, "get_user_data_dir", lambda: tmp_path / "user-data")
    source_dir = tmp_path / "demo-skill"
    source_dir.mkdir()
    content = "---\nname: demo-skill\ndescription: Personal skill\n---\n# Demo\n"
    (source_dir / "SKILL.md").write_text(content, encoding="utf-8")
    monkeypatch.setattr(install_skill_module, "_prepare_skill_from_sandbox", lambda *args: source_dir)
    runtime = _runtime(uid="user-1", thread_id="shared-agent-thread", skills=[])

    result = await install_skill_module._run_install_task("/home/gem/user-data/demo-skill", runtime, "tool-1")

    installed = skill_service.get_personal_skills_root_dir("user-1") / "demo-skill" / "SKILL.md"
    assert installed.read_text(encoding="utf-8") == content
    assert result.update["messages"][0].content.splitlines() == [
        "已安装 Skill: demo-skill",
        "Skill 路径: /home/gem/user-data/agents/skills/demo-skill/SKILL.md",
    ]
    assert runtime.context.skills == []


@pytest.mark.asyncio
async def test_install_skill_from_sandbox_installs_as_current_user_private_skill(monkeypatch, tmp_path: Path):
    assert exported_install_skill.name == "install_skill"

    calls = {}
    event_loop_thread_id = threading.get_ident()
    source_dir = tmp_path / "demo-skill"

    def prepare_skill_from_sandbox(
        source,
        thread_id,
        uid,
        staging_root,
        workdir_relative_path,
        workdir_path,
    ):
        calls["prepare_thread_id"] = threading.get_ident()
        calls["prepare"] = {
            "source": source,
            "thread_id": thread_id,
            "uid": uid,
            "staging_root": staging_root,
            "workdir_relative_path": workdir_relative_path,
            "workdir_path": workdir_path,
        }
        return source_dir

    async def install_personal_skill_dir(uid, source_dir_arg, **kwargs):
        calls["install"] = {"uid": uid, "source_dir": source_dir_arg, **kwargs}
        return SimpleNamespace(
            slug="demo-skill",
            name="Demo Skill",
            description="demo description",
            source_scope="personal",
            tool_dependencies=[],
            mcp_dependencies=[],
            skill_dependencies=[],
            source_dir=source_dir,
        )

    monkeypatch.setattr(
        install_skill_module,
        "_prepare_skill_from_sandbox",
        prepare_skill_from_sandbox,
    )
    monkeypatch.setattr(skill_service, "install_personal_skill_dir", install_personal_skill_dir)
    runtime = _runtime(
        uid="normal-user",
        thread_id="thread-1",
        workdir_relative_path="projects/11111111-1111-4111-8111-111111111111",
        workdir_path="/home/gem/user-data/projects/11111111-1111-4111-8111-111111111111",
        skills=["existing-skill"],
    )
    result = await install_skill_module._run_install_task(
        " /home/gem/user-data/demo-skill ",
        runtime,
        "tool-1",
    )

    assert "activated_skills" not in result.update
    assert calls["prepare"]["uid"] == "normal-user"
    assert calls["prepare_thread_id"] != event_loop_thread_id
    assert calls["install"] == {"uid": "normal-user", "source_dir": source_dir}
    assert calls["prepare"]["source"] == "/home/gem/user-data/demo-skill"
    assert result.update["messages"][0].content.splitlines() == [
        "已安装 Skill: demo-skill",
        "Skill 路径: /home/gem/user-data/agents/skills/demo-skill/SKILL.md",
    ]
    assert runtime.context.skills == ["existing-skill"]


@pytest.mark.asyncio
async def test_install_skill_rejects_subagent_runtime_before_install(monkeypatch):
    def fail_install(*args, **kwargs):
        raise AssertionError("子智能体运行态不应执行安装")

    monkeypatch.setattr(
        skill_service,
        "install_personal_skill_dir",
        fail_install,
    )

    result = await install_skill_module._run_install_task(
        "/home/gem/user-data/demo-skill",
        _runtime(uid="user-1", thread_id="child-thread", is_subagent_runtime=True),
        "tool-1",
    )

    assert "只能在主智能体中使用" in result.update["messages"][0].content
    assert "activated_skills" not in result.update


@pytest.mark.asyncio
async def test_install_skill_git_source_requires_skill_names():
    result = await install_skill_module._run_install_task(
        "owner/repo",
        _runtime(uid="user-1", thread_id="thread-1"),
        "tool-1",
    )

    assert "必须通过 skill_names 指定技能名称" in result.update["messages"][0].content


@pytest.mark.asyncio
async def test_install_skill_rejects_empty_source():
    result = await install_skill_module._run_install_task(
        " ",
        _runtime(uid="user-1", thread_id="thread-1"),
        "tool-1",
    )

    assert "Skill 来源不能为空" in result.update["messages"][0].content


def test_prepare_skill_from_sandbox_uses_sandbox_api_without_host_path_resolution(monkeypatch, tmp_path: Path):
    remote_dir = "/home/gem/user-data/demo-skill"

    class FakeProvisionerSandboxBackend:
        def __init__(self, *, thread_id, uid, workdir_path, create_if_missing):
            assert thread_id == "thread-1"
            assert uid == "user-1"
            assert workdir_path is None
            assert create_if_missing is True

        def ls(self, path):
            assert path == remote_dir
            return SimpleNamespace(
                error=None,
                entries=[{"path": f"{remote_dir}/SKILL.md", "is_dir": False, "size": 6}],
            )

        def download_files(self, paths):
            assert paths == [f"{remote_dir}/SKILL.md"]
            return [SimpleNamespace(error=None, content=b"# demo")]

    monkeypatch.setattr(sandbox_backend_module, "ProvisionerSandboxBackend", FakeProvisionerSandboxBackend)

    staging = install_skill_module._prepare_skill_from_sandbox(
        remote_dir,
        "thread-1",
        "user-1",
        tmp_path / "staging",
    )

    assert (staging / "SKILL.md").read_text(encoding="utf-8") == "# demo"


def test_prepare_skill_from_sandbox_preserves_download_error_message(monkeypatch, tmp_path: Path):
    remote_dir = "/home/gem/user-data/demo-skill"

    class FakeProvisionerSandboxBackend:
        def __init__(self, *, thread_id, uid, workdir_path, create_if_missing):
            assert thread_id == "thread-1"
            assert uid == "user-1"
            assert workdir_path is None
            assert create_if_missing is True

        def ls(self, _path):
            return SimpleNamespace(
                error=None,
                entries=[{"path": f"{remote_dir}/SKILL.md", "is_dir": False, "size": 1}],
            )

        def download_files(self, _paths):
            return [SimpleNamespace(error="read_failed", content=None)]

    monkeypatch.setattr(sandbox_backend_module, "ProvisionerSandboxBackend", FakeProvisionerSandboxBackend)

    with pytest.raises(ValueError, match="下载沙盒文件失败"):
        install_skill_module._prepare_skill_from_sandbox(
            remote_dir,
            "thread-1",
            "user-1",
            tmp_path / "staging",
        )
