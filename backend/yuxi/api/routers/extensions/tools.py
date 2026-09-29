from fastapi import APIRouter, Depends

from yuxi.modules.extensions.tools.catalog import get_tool_metadata
from yuxi.api.dependencies.auth import get_required_user
from yuxi.modules.identity.models import User

tools = APIRouter(prefix="/system/tools", tags=["tools"])


@tools.get("")
async def list_tools(
    category: str = None,
    user: User = Depends(get_required_user),
):
    """获取工具列表"""
    return {"success": True, "data": get_tool_metadata(category)}


@tools.get("/options")
async def get_tool_options(
    user: User = Depends(get_required_user),
):
    """获取工具选项（前端下拉框用）"""
    all_tools = get_tool_metadata()
    return {"success": True, "data": [{"label": t["name"], "value": t["slug"]} for t in all_tools]}
