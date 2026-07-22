import os
from pathlib import Path

import aiofiles
import yaml
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi import config, get_version
from yuxi.storage.postgres.models_business import User
from yuxi.utils.logging_config import logger

from server.utils.auth_middleware import get_admin_user, get_db, get_required_user

system = APIRouter(prefix="/system", tags=["system"])

# =============================================================================
# === 健康检查分组 ===
# =============================================================================


@system.get("/health")
async def health_check():
    """系统健康检查接口（公开接口）"""
    return {"status": "ok", "message": "服务正常运行", "version": get_version()}


@system.get("/discovery")
async def discovery():
    """系统能力发现接口（公开接口）"""
    return {
        "name": "Yuxi",
        "version": get_version(),
        "api_prefix": "/api",
        "capabilities": {
            "cli": {
                "min_cli_version": "0.1.0",
                "browser_login": True,
                "api_key_auth": True,
                "remote_config": True,
                "kb_upload": True,
                "kb_list": True,
                "kb_files": True,
                "kb_query": True,
                "kb_open": True,
                "kb_find": True,
            }
        },
        "endpoints": {
            "health": "/api/system/health",
            "auth_me": "/api/auth/me",
            "cli_auth_sessions": "/api/auth/cli/sessions",
            "cli_auth_authorize": "/auth/cli/authorize",
        },
    }


# =============================================================================
# === 配置管理分组 ===
# =============================================================================


@system.get("/config")
async def get_config(current_user: User = Depends(get_required_user)):
    """获取系统配置"""
    return config.dump_config()


@system.post("/config")
async def update_config_single(key=Body(...), value=Body(...), current_user: User = Depends(get_admin_user)) -> dict:
    """更新单个配置项"""
    if not isinstance(key, str) or key not in type(config).model_fields:
        raise HTTPException(status_code=400, detail=f"未知配置项: {key}")
    if not config.can_update(key):
        raise HTTPException(status_code=400, detail=f"配置项不可修改: {key}")
    try:
        config.set_value(key, value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    config.save()
    return config.dump_config()


@system.post("/config/update")
async def update_config_batch(items: dict = Body(...), current_user: User = Depends(get_admin_user)) -> dict:
    """批量更新配置项"""
    try:
        config.update(items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    config.save()
    return config.dump_config()


@system.get("/logs")
async def get_system_logs(levels: str | None = None, current_user: User = Depends(get_admin_user)):
    """获取系统日志

    Args:
        levels: 可选的日志级别过滤，多个级别用逗号分隔，如 "INFO,ERROR,DEBUG,WARNING"
    """
    try:
        from yuxi.utils.logging_config import LOG_FILE

        # 解析日志级别过滤条件
        level_filter = None
        if levels:
            level_filter = set(level.strip().upper() for level in levels.split(",") if level.strip())

        #  修复 GBK 编码报错：强制 utf-8 读取，忽略错误
        async with aiofiles.open(LOG_FILE, encoding="utf-8", errors="ignore") as f:
            # 读取最后1000行
            lines = []
            async for line in f:
                filtered_line = line.rstrip("\n\r")
                # 如果指定了日志级别过滤，则按级别过滤
                if level_filter:
                    # 日志格式: 2025-03-10 08:26:37,269 - INFO - module - message
                    # 提取日志级别
                    parts = filtered_line.split(" - ")
                    if len(parts) >= 2 and parts[1].strip() in level_filter:
                        lines.append(filtered_line + "\n")
                    # 继续读取以保持行数统计准确
                    if len(lines) > 1000:
                        lines.pop(0)
                else:
                    lines.append(filtered_line + "\n")
                    if len(lines) > 1000:
                        lines.pop(0)

        log = "".join(lines)
        return {"log": log, "message": "success", "log_file": LOG_FILE}
    except Exception as e:
        logger.error(f"获取系统日志失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取系统日志失败: {str(e)}")


# =============================================================================
# === 信息管理分组 ===
# =============================================================================


async def load_info_config():
    """加载信息配置文件"""
    try:
        # 配置文件路径
        brand_file_path = os.environ.get("YUXI_BRAND_FILE_PATH", "package/yuxi/config/static/info.local.yaml")
        config_path = Path(brand_file_path)

        # 检查文件是否存在
        if not config_path.exists():
            logger.debug(f"The config file {config_path} does not exist, using default config")
            config_path = Path("package/yuxi/config/static/info.template.yaml")

        # 异步读取配置文件
        async with aiofiles.open(config_path, encoding="utf-8") as file:
            content = await file.read()

        # 注入版本号占位符
        content = content.replace("{{YUXI_VERSION}}", get_version())

        config = yaml.safe_load(content)

        return config

    except Exception as e:
        logger.error(f"Failed to load info config: {e}")
        return {}


@system.get("/info")
async def get_info_config():
    """获取系统信息配置（公开接口，无需认证）"""
    try:
        config = await load_info_config()
        return {"success": True, "data": config}
    except Exception as e:
        logger.error(f"获取信息配置失败: {e}")
        raise HTTPException(status_code=500, detail="获取信息配置失败")


@system.post("/info/reload")
async def reload_info_config(current_user: User = Depends(get_admin_user)):
    """重新加载信息配置"""
    try:
        config = await load_info_config()
        return {"success": True, "message": "配置重新加载成功", "data": config}
    except Exception as e:
        logger.error(f"重新加载信息配置失败: {e}")
        raise HTTPException(status_code=500, detail="重新加载信息配置失败")


# =============================================================================
# === OCR服务分组 ===
# =============================================================================


class OCREngineConfigPayload(BaseModel):
    """管理员可更新的 OCR 配置字段。"""

    enabled: bool | None = None
    is_default: bool | None = None
    endpoint: str | None = None
    credential_source: str | None = None
    credential_value: str | None = None


@system.get("/ocr/options")
async def get_ocr_engine_options(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """返回普通用户可见的脱敏 OCR 引擎选项。"""

    from yuxi.services.ocr_config_service import get_ocr_options

    return await get_ocr_options(db)


@system.get("/ocr/configs")
async def get_ocr_engine_configs(
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """返回管理员可编辑的 OCR 配置与凭证状态。"""

    from yuxi.repositories.ocr_config_repository import list_ocr_configs
    from yuxi.services.ocr_config_service import serialize_admin_config

    records = await list_ocr_configs(db)
    return {"configs": [serialize_admin_config(record) for record in records]}


@system.put("/ocr/configs/{engine_id}")
async def put_ocr_engine_config(
    engine_id: str,
    payload: OCREngineConfigPayload,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """保存单个 OCR 引擎配置。后续 OCR 任务会直接读取最新数据库配置。"""

    from yuxi.services.ocr_config_service import serialize_admin_config, update_ocr_config

    try:
        record = await update_ocr_config(
            db,
            engine_id,
            payload.model_dump(exclude_unset=True),
            current_user.username,
        )
        if record is None:
            raise HTTPException(status_code=404, detail=f"OCR 引擎配置不存在: {engine_id}")
        # 先提交事实来源；后续 OCR 任务会直接读取已提交的数据库配置。
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        await db.refresh(record)
        return {"config": serialize_admin_config(record)}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
