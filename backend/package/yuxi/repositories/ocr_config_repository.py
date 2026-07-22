"""OCR 引擎配置数据访问。"""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import OCREngineConfig


async def list_ocr_configs(db: AsyncSession, *, for_update: bool = False) -> list[OCREngineConfig]:
    """按稳定顺序读取全部 OCR 引擎配置。"""

    statement = select(OCREngineConfig).order_by(OCREngineConfig.id.asc())
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    return list(result.scalars().all())


async def get_ocr_config(db: AsyncSession, engine_id: str, *, for_update: bool = False) -> OCREngineConfig | None:
    """按引擎标识读取一条 OCR 配置。"""

    statement = select(OCREngineConfig).where(OCREngineConfig.engine_id == engine_id)
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    return result.scalar_one_or_none()


async def clear_default_ocr_config(db: AsyncSession) -> None:
    """清除当前默认项，为同一事务中的默认切换让位。"""

    await db.execute(update(OCREngineConfig).where(OCREngineConfig.is_default.is_(True)).values(is_default=False))
