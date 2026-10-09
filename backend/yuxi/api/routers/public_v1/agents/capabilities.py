"""Thread 范围内的文件、上下文与状态能力。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.api.dependencies.auth import get_db
from yuxi.api.responses.files import render_file_result
from yuxi.api.routers.public_v1.agents.auth import PublicAgentContext, require_public_context
from yuxi.api.uploads import read_upload_with_limit
from yuxi.infrastructure.images import process_uploaded_image
from yuxi.modules.agents.services.artifacts import resolve_thread_artifact_view, save_thread_artifact_to_workspace_view
from yuxi.modules.agents.services.attachments import (
    MAX_ATTACHMENT_SIZE_BYTES,
    confirm_tmp_thread_attachments_view,
    delete_thread_attachment_view,
    list_thread_attachments_view,
    parse_tmp_attachment_view,
    upload_tmp_attachment_view,
)
from yuxi.modules.agents.services.compression import compress_thread_context as compress_context
from yuxi.modules.agents.services.cooperation import get_cooperation_summary
from yuxi.modules.agents.services.state import get_agent_state_view
from yuxi.modules.agents.services.threads import get_thread_snapshot

router = APIRouter(dependencies=[Depends(require_public_context)])


class TmpAttachmentParse(BaseModel):
    """指定已上传临时对象的解析方式。"""

    model_config = ConfigDict(extra="forbid")
    object_name: str
    parse_method: str | None = None


class TmpAttachmentConfirmItem(BaseModel):
    """指定一份要正式绑定的临时附件。"""

    model_config = ConfigDict(extra="forbid")
    file_type: str | None = None
    object_name: str
    parsed_object_name: str | None = None


class TmpAttachmentConfirm(BaseModel):
    """批量确认同一 Thread 的临时附件。"""

    model_config = ConfigDict(extra="forbid")
    attachments: list[TmpAttachmentConfirmItem] = Field(min_length=1, max_length=20)


class SaveArtifact(BaseModel):
    """把 Thread 产物保存到用户工作区。"""

    model_config = ConfigDict(extra="forbid")
    path: str
    destination_path: str | None = None


@router.post("/attachments/tmp")
async def upload_tmp_attachment(
    file: UploadFile = File(...), context: PublicAgentContext = Depends(require_public_context)
):
    """把待确认附件上传到当前资源用户的临时空间。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="无法识别的文件名")
    try:
        content = await read_upload_with_limit(
            file,
            max_size_bytes=MAX_ATTACHMENT_SIZE_BYTES,
            too_large_message="附件过大，当前仅支持 5 MB 以内的文件",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await upload_tmp_attachment_view(
        file_content=content,
        filename=file.filename,
        content_type=file.content_type,
        current_uid=context.scope.uid,
        app_id=context.scope.app_id,
    )


@router.post("/attachments/tmp/parse")
async def parse_tmp_attachment(
    payload: TmpAttachmentParse, context: PublicAgentContext = Depends(require_public_context)
):
    """仅解析当前资源用户的临时对象。"""
    return await parse_tmp_attachment_view(
        object_name=payload.object_name,
        parse_method=payload.parse_method,
        current_uid=context.scope.uid,
        app_id=context.scope.app_id,
    )


@router.post("/images")
async def upload_image(file: UploadFile = File(...)):
    """把用户图片处理为 Public 输入所需的内联内容。"""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="只支持图片文件上传")
    image_data = await file.read()
    if len(image_data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片文件过大，请上传小于10MB的图片")
    result = process_uploaded_image(image_data, file.filename)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=f"图片处理失败: {result['error']}")
    return result


@router.get("/sessions/{session_id}/cooperation")
async def retrieve_cooperation_summary(
    session_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """读取不包含 checkpoint 和结果正文的完整协作摘要。"""
    return await get_cooperation_summary(db=db, scope=context.scope, thread_id=session_id)


@router.get("/sessions/{session_id}/state")
async def retrieve_thread_state(
    session_id: str,
    include_messages: bool = Query(default=False),
    include_relations: bool = Query(default=True),
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """按 Thread 作用域读取 LangGraph 当前状态。"""
    await get_thread_snapshot(db=db, scope=context.scope, thread_id=session_id)
    return await get_agent_state_view(
        thread_id=session_id,
        current_user=context.user,
        db=db,
        include_messages=include_messages,
        include_relations=include_relations,
        app_id=context.scope.app_id,
    )


@router.post("/sessions/{session_id}/compress")
async def compress_thread_context(
    session_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """在作用域与空闲条件成立时压缩线程上下文。"""
    await get_thread_snapshot(db=db, scope=context.scope, thread_id=session_id)
    return await compress_context(thread_id=session_id, current_user=context.user, db=db, app_id=context.scope.app_id)


@router.post("/sessions/{session_id}/attachments/confirm")
async def confirm_thread_attachments(
    session_id: str,
    payload: TmpAttachmentConfirm,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """在完整作用域检查后将临时附件绑定到 Thread。"""
    await get_thread_snapshot(db=db, scope=context.scope, thread_id=session_id)
    return await confirm_tmp_thread_attachments_view(
        thread_id=session_id,
        attachments=[item.model_dump() for item in payload.attachments],
        db=db,
        current_uid=context.scope.uid,
        app_id=context.scope.app_id,
    )


@router.get("/sessions/{session_id}/attachments")
async def list_thread_attachments(
    session_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """读取当前资源用户的 Thread 附件。"""
    await get_thread_snapshot(db=db, scope=context.scope, thread_id=session_id)
    return await list_thread_attachments_view(
        thread_id=session_id, db=db, current_uid=context.scope.uid, app_id=context.scope.app_id
    )


@router.delete("/sessions/{session_id}/attachments/{file_id}")
async def delete_thread_attachment(
    session_id: str,
    file_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """拒绝删除仍被待消费 Input 使用的附件。"""
    await get_thread_snapshot(db=db, scope=context.scope, thread_id=session_id)
    return await delete_thread_attachment_view(
        thread_id=session_id,
        file_id=file_id,
        db=db,
        current_uid=context.scope.uid,
        app_id=context.scope.app_id,
    )


@router.post("/sessions/{session_id}/artifacts/save")
async def save_thread_artifact(
    session_id: str,
    payload: SaveArtifact,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """把已授权 Thread 产物保存到用户工作区。"""
    await get_thread_snapshot(db=db, scope=context.scope, thread_id=session_id)
    return await save_thread_artifact_to_workspace_view(
        thread_id=session_id,
        current_uid=context.scope.uid,
        db=db,
        path=payload.path,
        destination_path=payload.destination_path,
        app_id=context.scope.app_id,
    )


@router.get("/sessions/{session_id}/artifacts/{path:path}")
async def retrieve_thread_artifact(
    session_id: str,
    path: str,
    download: bool = Query(default=False),
    preview: bool = Query(default=False),
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """下载或预览已授权 Thread 的沙盒产物。"""
    await get_thread_snapshot(db=db, scope=context.scope, thread_id=session_id)
    return render_file_result(
        await resolve_thread_artifact_view(
            thread_id=session_id,
            current_uid=context.scope.uid,
            db=db,
            path=path,
            download=download,
            preview=preview,
            app_id=context.scope.app_id,
        )
    )
