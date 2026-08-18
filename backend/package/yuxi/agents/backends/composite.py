from __future__ import annotations

from dataclasses import dataclass

from deepagents.backends.composite import (
    CompositeBackend,
    _remap_file_info_path,
    _route_for_path,
    _strip_route_from_pattern,
)
from deepagents.backends.protocol import FileInfo, GlobResult
from deepagents.middleware.filesystem import FilesystemMiddleware

from yuxi.agents.skills.service import (
    get_user_skills_root_dir,
    refresh_user_skill_projection_async,
)
from yuxi.agents.backends.sandbox.paths import project_workdir_virtual_dir
from yuxi.utils.paths import VIRTUAL_PATH_CONVERSATION_HISTORY, VIRTUAL_PATH_LARGE_TOOL_RESULTS

from .sandbox import ProvisionerSandboxBackend
from .skills_backend import SelectedSkillsReadonlyBackend

_TOOL_RESULT_EVICTION_EXEMPT_TOOLS = frozenset({"read_file", "open_kb_document"})


def _coerce_glob_result(result) -> GlobResult:
    if isinstance(result, GlobResult):
        return result
    return GlobResult(matches=result or [])


class CustomCompositeBackend(CompositeBackend):
    """修复 glob 路由逻辑的 CompositeBackend。"""

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        backend, backend_path, route_prefix = _route_for_path(
            default=self.default,
            sorted_routes=self.sorted_routes,
            path=path,
        )
        if route_prefix is not None:
            result = _coerce_glob_result(backend.glob(pattern, backend_path))
            if result.error:
                return result
            return GlobResult(matches=[_remap_file_info_path(fi, route_prefix) for fi in (result.matches or [])])

        if path is None or path == "/":
            results: list[FileInfo] = []
            default_result = _coerce_glob_result(self.default.glob(pattern, path))
            if default_result.error:
                return default_result
            results.extend(default_result.matches or [])
            for route_prefix, backend in self.routes.items():
                route_pattern = _strip_route_from_pattern(pattern, route_prefix)
                result = _coerce_glob_result(backend.glob(route_pattern, "/"))
                if result.error:
                    return result
                results.extend(_remap_file_info_path(fi, route_prefix) for fi in (result.matches or []))
            results.sort(key=lambda x: x.get("path", ""))
            return GlobResult(matches=results)

        return _coerce_glob_result(self.default.glob(pattern, path))

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        backend, backend_path, route_prefix = _route_for_path(
            default=self.default,
            sorted_routes=self.sorted_routes,
            path=path,
        )
        if route_prefix is not None:
            result = _coerce_glob_result(await backend.aglob(pattern, backend_path))
            if result.error:
                return result
            return GlobResult(matches=[_remap_file_info_path(fi, route_prefix) for fi in (result.matches or [])])

        if path is None or path == "/":
            results: list[FileInfo] = []
            default_result = _coerce_glob_result(await self.default.aglob(pattern, path))
            if default_result.error:
                return default_result
            results.extend(default_result.matches or [])
            for route_prefix, backend in self.routes.items():
                route_pattern = _strip_route_from_pattern(pattern, route_prefix)
                result = _coerce_glob_result(await backend.aglob(route_pattern, "/"))
                if result.error:
                    return result
                results.extend(_remap_file_info_path(fi, route_prefix) for fi in (result.matches or []))
            results.sort(key=lambda x: x.get("path", ""))
            return GlobResult(matches=results)

        return _coerce_glob_result(await self.default.aglob(pattern, path))


class YuxiFilesystemMiddleware(FilesystemMiddleware):
    """Filesystem middleware that budgets large tool outputs before they hit model context."""

    def wrap_tool_call(self, request, handler):
        tool_result = handler(request)

        if self._tool_token_limit_before_evict is None:
            return tool_result
        if request.tool_call["name"] in _TOOL_RESULT_EVICTION_EXEMPT_TOOLS:
            return tool_result

        return self._intercept_large_tool_result(tool_result, request.runtime)

    async def awrap_tool_call(self, request, handler):
        tool_result = await handler(request)

        if self._tool_token_limit_before_evict is None:
            return tool_result
        if request.tool_call["name"] in _TOOL_RESULT_EVICTION_EXEMPT_TOOLS:
            return tool_result

        return await self._aintercept_large_tool_result(tool_result, request.runtime)


@dataclass(frozen=True)
class _BackendScope:
    thread_id: str
    runtime_scope_id: str
    workdir_id: str
    uid: str
    skill_sources: dict[str, str]
    sandbox_instance_id: str

    @classmethod
    def from_runtime(cls, runtime) -> _BackendScope:
        config = getattr(runtime, "config", None)
        configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
        context = getattr(runtime, "context", None)
        state = getattr(runtime, "state", None)
        return cls.from_sources(
            configurable if isinstance(configurable, dict) else {},
            context,
            state if isinstance(state, dict) else {},
            readable_skills_source=context,
            error_context="runtime configurable context",
        )

    @classmethod
    def from_sources(cls, *sources, readable_skills_source, error_context: str) -> _BackendScope:
        def string_value(key: str) -> str | None:
            for source in sources:
                value = source.get(key) if isinstance(source, dict) else getattr(source, key, None)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        thread_id = string_value("thread_id")
        if not thread_id:
            raise ValueError(f"thread_id is required in {error_context}")

        uid = string_value("uid")
        if not uid:
            raise ValueError(f"uid is required in {error_context}")

        if not hasattr(readable_skills_source, "_runtime_skill_sources"):
            raise ValueError(f"_runtime_skill_sources is required in {error_context}")
        raw_sources = getattr(readable_skills_source, "_runtime_skill_sources")
        if not isinstance(raw_sources, dict):
            raise ValueError(f"_runtime_skill_sources must be a dict in {error_context}")
        skill_sources: dict[str, str] = {}
        for slug, path in raw_sources.items():
            if not isinstance(slug, str) or not slug.strip() or not isinstance(path, str) or not path.strip():
                raise ValueError(f"_runtime_skill_sources contains an invalid entry in {error_context}")
            skill_sources[slug.strip()] = path.strip()
        runtime_scope_id = string_value("runtime_scope_id") or thread_id
        return cls(
            thread_id=thread_id,
            runtime_scope_id=runtime_scope_id,
            workdir_id=string_value("workdir_id") or "",
            uid=uid,
            skill_sources=skill_sources,
            sandbox_instance_id=string_value("sandbox_instance_id") or runtime_scope_id,
        )

    def create_backend(self) -> CompositeBackend:
        if not self.workdir_id:
            raise ValueError("workdir_id is required in runtime context")
        workdir_path = project_workdir_virtual_dir(self.workdir_id)
        user_skills_root = get_user_skills_root_dir(self.uid)
        return CustomCompositeBackend(
            default=ProvisionerSandboxBackend(
                thread_id=self.runtime_scope_id,
                uid=self.uid,
                sandbox_instance_id=self.sandbox_instance_id,
                workdir_id=self.workdir_id,
                create_if_missing=False,
            ),
            routes={
                "/skills/": SelectedSkillsReadonlyBackend(
                    selected_slugs=list(self.skill_sources),
                    root_dir=user_skills_root,
                ),
            },
            artifacts_root=workdir_path,
        )


async def sync_agent_context_skills(context) -> None:
    """在 Agent Run 初始化时同步当前用户的授权 Skill 投影。"""
    scope = _BackendScope.from_sources(
        context,
        readable_skills_source=context,
        error_context="runtime context",
    )
    current_sources = await refresh_user_skill_projection_async(scope.uid)
    setattr(context, "_runtime_skill_sources", current_sources)


def create_agent_composite_backend(runtime) -> CompositeBackend:
    return _BackendScope.from_runtime(runtime).create_backend()


def create_agent_filesystem_middleware(
    tool_token_limit_before_evict: int | None = None,
    *,
    context=None,
) -> FilesystemMiddleware:
    backend = create_agent_composite_backend
    if context is not None:

        def build_context_backend(_runtime):
            """按可变运行上下文重建文件作用域，读取已同步的 Skill 投影。"""
            return _BackendScope.from_sources(
                context,
                readable_skills_source=context,
                error_context="runtime context",
            ).create_backend()

        backend = build_context_backend
    middleware = YuxiFilesystemMiddleware(
        backend=backend,
        tool_token_limit_before_evict=tool_token_limit_before_evict,
    )
    middleware._large_tool_results_prefix = VIRTUAL_PATH_LARGE_TOOL_RESULTS
    middleware._conversation_history_prefix = VIRTUAL_PATH_CONVERSATION_HISTORY
    return middleware
