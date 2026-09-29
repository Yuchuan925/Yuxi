"""MCP 服务配置的 PostgreSQL 查询和写入。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.modules.extensions.mcp.models import MCPServer


async def get_mcp_server(db: AsyncSession, slug: str) -> MCPServer | None:
    """Get single server configuration by slug."""
    result = await db.execute(select(MCPServer).filter(MCPServer.slug == slug))
    return result.scalar_one_or_none()


async def get_all_mcp_servers(db: AsyncSession) -> list[MCPServer]:
    """Get all server configurations."""
    result = await db.execute(select(MCPServer))
    return list(result.scalars().all())


async def save_mcp_server(db: AsyncSession, server: MCPServer, *, new: bool = False) -> None:
    """写入服务配置，事务仍由调用方提交。"""
    if new:
        db.add(server)
    await db.flush()


async def delete_mcp_server_row(db: AsyncSession, server: MCPServer) -> None:
    """删除服务配置，事务仍由调用方提交。"""
    await db.delete(server)
    await db.flush()
