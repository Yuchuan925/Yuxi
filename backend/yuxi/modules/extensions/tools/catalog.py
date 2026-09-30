from typing import Any

from yuxi.infrastructure.observability.logging import logger

# 工具元数据缓存
_metadata_cache: list[dict] = []


def _extract_tool_info(tool_obj) -> dict:
    """从 tool_obj 提取基础信息"""
    metadata = getattr(tool_obj, "metadata", {}) or {}
    info = {
        "slug": tool_obj.name,
        "name": metadata.get("name", tool_obj.name),  # 显示名称优先从 metadata 获取
        "description": tool_obj.description,
        "metadata": metadata,
        "args": [],
    }

    if hasattr(tool_obj, "args_schema") and tool_obj.args_schema:
        schema = tool_obj.args_schema
        if hasattr(schema, "schema"):
            schema = schema.schema()
        for arg_name, arg_info in schema.get("properties", {}).items():
            info["args"].append(
                {
                    "name": arg_name,
                    "type": arg_info.get("type", ""),
                    "description": arg_info.get("description", ""),
                }
            )
    return info


def _ensure_metadata_loaded():
    """延迟加载工具元数据（首次调用时自动触发）"""
    global _metadata_cache

    if _metadata_cache:  # 已加载
        return

    from yuxi.modules.extensions.tools.registry import get_all_extra_metadata, get_all_tool_instances

    # 获取所有工具实例
    all_tools = get_all_tool_instances()
    extra_meta = get_all_extra_metadata()

    for tool in all_tools:
        tool_name = tool.name
        runtime_info = _extract_tool_info(tool)

        # 合并附加元数据
        if tool_name in extra_meta:
            extra = extra_meta[tool_name]
            runtime_info["category"] = extra.category
            runtime_info["tags"] = extra.tags
            runtime_info["config_guide"] = extra.config_guide
            # display_name 优先级高于 tool.name
            if extra.display_name:
                runtime_info["name"] = extra.display_name
        else:
            # 未注册，设为默认分类
            runtime_info["category"] = "builtin"
            runtime_info["tags"] = []
            runtime_info["config_guide"] = ""

        _metadata_cache.append(runtime_info)

    logger.info(f"Tool service loaded {len(_metadata_cache)} tools (lazy load)")


def get_tool_metadata(category: str = None) -> list[dict]:
    """获取工具元数据列表（延迟加载）"""
    _ensure_metadata_loaded()

    if category:
        return [t for t in _metadata_cache if t.get("category") == category]
    return _metadata_cache


def get_tool_instances_by_category(category: str) -> list[Any]:
    from yuxi.modules.extensions.tools.registry import get_all_extra_metadata, get_all_tool_instances

    extra_meta = get_all_extra_metadata()
    tools = []
    for tool in get_all_tool_instances():
        tool_meta = extra_meta.get(tool.name)
        tool_category = tool_meta.category if tool_meta else "builtin"
        if tool_category == category:
            tools.append(tool)
    return tools
