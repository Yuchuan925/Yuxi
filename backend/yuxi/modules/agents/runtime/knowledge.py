"""Agent Context 的可见知识资源解析。"""

from __future__ import annotations

from typing import Any


async def resolve_visible_knowledge_bases_for_context(context) -> list[dict[str, Any]]:
    """解析当前用户可见且被 Context 选中的知识库。"""
    from yuxi.modules.knowledge.services.tools import visible_knowledge_bases

    uid = getattr(context, "uid", None)
    if not uid:
        setattr(context, "_visible_knowledge_bases", [])
        return []

    databases = await visible_knowledge_bases(str(uid))
    enabled_knowledges = getattr(context, "knowledges", None)
    if enabled_knowledges is not None:
        enabled_ids = {str(value).strip() for value in enabled_knowledges if str(value).strip()}
        databases = [db for db in databases if str(db.get("kb_id") or "").strip() in enabled_ids]

    setattr(context, "_visible_knowledge_bases", databases)
    return databases
