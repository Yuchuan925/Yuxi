"""Agent 文件工具注册与大结果处理策略。"""

from deepagents.backends import CompositeBackend
from deepagents.middleware.filesystem import TOOLS_EXCLUDED_FROM_EVICTION, FilesystemMiddleware, FsToolName

# Yuxi 在 DeepAgents 内建排除集之上额外豁免知识库文档工具结果，
# 避免 read_file/offload 循环：该工具自带分页与引用语义。
_TOOL_RESULT_EVICTION_EXEMPT_TOOLS = frozenset(TOOLS_EXCLUDED_FROM_EVICTION) | {"open_kb_document"}

# 文件工具 allowlist：显式排除 destructive delete。Yuxi backend 未实现 delete，
# 且删除语义需要审批与审计设计，开放前不应让模型看到该工具。
_AGENT_FS_TOOLS: tuple[FsToolName, ...] = (
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "execute",
)


def create_agent_filesystem_middleware(
    tool_token_limit_before_evict: int | None = None,
    *,
    backend: CompositeBackend,
    disabled_tools: frozenset[str] = frozenset(),
) -> FilesystemMiddleware:
    """构造文件系统中间件，在 ToolNode 注册前排除禁用工具。"""
    return YuxiFilesystemMiddleware(
        backend=backend,
        tool_token_limit_before_evict=tool_token_limit_before_evict,
        tools=[name for name in _AGENT_FS_TOOLS if name not in disabled_tools],
    )


class YuxiFilesystemMiddleware(FilesystemMiddleware):
    """在进入模型上下文前处理大工具结果。"""

    def wrap_tool_call(self, request, handler):
        tool_result = handler(request)

        if request.tool_call["name"] in _TOOL_RESULT_EVICTION_EXEMPT_TOOLS:
            return tool_result
        if self._tool_token_limit_before_evict is None:
            return tool_result

        return self._intercept_large_tool_result(tool_result)

    async def awrap_tool_call(self, request, handler):
        tool_result = await handler(request)

        if request.tool_call["name"] in _TOOL_RESULT_EVICTION_EXEMPT_TOOLS:
            return tool_result
        if self._tool_token_limit_before_evict is None:
            return tool_result

        return await self._aintercept_large_tool_result(tool_result)
