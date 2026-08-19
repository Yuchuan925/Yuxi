import asyncio
import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.config import get_runtime_dir
from yuxi.config.options import system_options
from yuxi.knowledge.parser.factory import DocumentProcessorFactory
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.storage.minio import StorageError, get_minio_client
from yuxi.utils.datetime_utils import utc_isoformat
from yuxi.utils.logging_config import logger
from yuxi.utils.upload_utils import read_upload_with_limit, write_upload_to_path

ATTACHMENT_ALLOWED_EXTENSIONS: tuple[str, ...] = ()
MAX_ATTACHMENT_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_ATTACHMENT_MARKDOWN_CHARS = 32_000  # TODO: 转 MARKDOWN的时候，不应该裁剪
TMP_ATTACHMENT_PREFIX = "tmp/chat_attachments"
TMP_ATTACHMENT_PARSE_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif")
TMP_ATTACHMENT_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif")
TMP_ATTACHMENT_OCR_METHODS = tuple(DocumentProcessorFactory.get_available_processors())
TMP_ATTACHMENT_PARSE_METHODS = ("disable", *TMP_ATTACHMENT_OCR_METHODS)


@dataclass(slots=True)
class ConversionResult:
    """表示上传附件转换后的标准化结果。"""

    file_id: str
    file_name: str
    file_type: str | None
    file_size: int
    markdown: str
    truncated: bool


async def parse_document(source: str, params: dict | None = None, db: AsyncSession | None = None) -> str:
    """仅在附件确实需要解析时加载文档/OCR 重运行时。"""

    from yuxi.services.ocr_service import parse_document as parse_runtime_document

    return await parse_runtime_document(source, params=params, db=db)


async def _require_user_conversation(conv_repo: ConversationRepository, thread_id: str, uid: str):
    conversation = await conv_repo.get_conversation_by_thread_id(thread_id)
    if not conversation or conversation.uid != str(uid) or conversation.status == "deleted":
        raise HTTPException(status_code=404, detail="对话线程不存在")
    return conversation


def _ensure_workdir() -> Path:
    workdir = get_runtime_dir() / "uploads" / "chat_attachments"
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


async def _write_upload_to_disk(upload: UploadFile, dest: Path) -> int:
    return await write_upload_to_path(
        upload,
        dest,
        max_size_bytes=MAX_ATTACHMENT_SIZE_BYTES,
        too_large_message="附件过大，当前仅支持 5 MB 以内的文件",
    )


def _truncate_markdown(markdown: str) -> tuple[str, bool]:
    if len(markdown) <= MAX_ATTACHMENT_MARKDOWN_CHARS:
        return markdown, False

    truncated_content = markdown[: MAX_ATTACHMENT_MARKDOWN_CHARS - 100].rstrip()
    truncated_content = f"{truncated_content}\n\n[内容已截断，超出 {MAX_ATTACHMENT_MARKDOWN_CHARS} 字符限制]"
    return truncated_content, True


async def _convert_upload_to_markdown(upload: UploadFile) -> ConversionResult:
    """临时保存上传文件，转换为 Markdown 后清理临时文件。"""
    if not upload.filename:
        raise ValueError("无法识别的文件名")

    file_name = Path(upload.filename).name
    suffix = Path(file_name).suffix.lower()

    if ATTACHMENT_ALLOWED_EXTENSIONS and suffix not in ATTACHMENT_ALLOWED_EXTENSIONS:
        allowed = ", ".join(ATTACHMENT_ALLOWED_EXTENSIONS)
        raise ValueError(f"不支持的文件类型: {suffix or '未知'}，当前仅支持 {allowed}")

    temp_dir = _ensure_workdir()
    temp_path = temp_dir / f"{uuid.uuid4().hex}{suffix}"

    try:
        file_size = await _write_upload_to_disk(upload, temp_path)
        markdown = await parse_document(str(temp_path))
        markdown, truncated = _truncate_markdown(markdown)
        return ConversionResult(
            file_id=uuid.uuid4().hex,
            file_name=file_name,
            file_type=upload.content_type,
            file_size=file_size,
            markdown=markdown,
            truncated=truncated,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Attachment conversion failed: {exc}")
        raise
    finally:
        temp_path.unlink(missing_ok=True)


def _safe_file_name(file_name: str | None, default: str = "attachment.bin") -> str:
    safe_name = Path(file_name or "").name.replace("/", "_").replace("\\", "_").strip(" .")
    return safe_name or default


def _make_attachment_path(file_name: str) -> str:
    """生成附件在沙盒用户目录中的统一路径。"""
    file_name = _safe_file_name(file_name)
    base_name = file_name
    for ext in [".docx", ".txt", ".html", ".htm", ".pdf", ".md"]:
        if file_name.lower().endswith(ext):
            base_name = file_name[: -len(ext)]
            break

    safe_name = base_name.replace("/", "_").replace("\\", "_")
    return f"{safe_name}.md"


def _artifact_url(thread_id: str, virtual_path: str) -> str:
    return f"/api/chat/thread/{thread_id}/artifacts/{virtual_path.lstrip('/')}"


def _tmp_attachment_prefix(uid: str, tmp_file_id: str) -> str:
    return f"{TMP_ATTACHMENT_PREFIX}/{uid}/{tmp_file_id}"


def _get_tmp_attachment_bucket() -> str:
    return get_minio_client().KB_BUCKETS["documents"]


def _make_tmp_attachment_object(uid: str, file_name: str) -> tuple[str, str]:
    """生成用户隔离的 tmp 对象路径。"""
    tmp_file_id = uuid.uuid4().hex
    safe_name = _safe_file_name(file_name)
    return tmp_file_id, f"{_tmp_attachment_prefix(uid, tmp_file_id)}/original/{safe_name}"


def _make_tmp_parsed_object(uid: str, tmp_file_id: str, file_name: str) -> str:
    stem = Path(_safe_file_name(file_name)).stem or "attachment"
    return f"{_tmp_attachment_prefix(uid, tmp_file_id)}/parsed/{stem}.md"


def _minio_source(bucket_name: str, object_name: str) -> str:
    return f"minio://{bucket_name}/{quote(object_name, safe='/')}"


def _parse_user_tmp_object(object_name: str, uid: str) -> tuple[str, str, str]:
    if not object_name or "\\" in object_name:
        raise HTTPException(status_code=400, detail="无效的临时附件路径")

    user_prefix = f"{TMP_ATTACHMENT_PREFIX}/{uid}/"
    if not object_name.startswith(user_prefix):
        raise HTTPException(status_code=403, detail="无权访问该临时附件")

    parts = object_name[len(user_prefix) :].split("/")
    if len(parts) != 3 or any(not part or part in {".", ".."} for part in parts):
        raise HTTPException(status_code=400, detail="无效的临时附件路径")

    return parts[0], parts[1], parts[2]


def _require_tmp_object_section(
    object_name: str,
    uid: str,
    section: str,
    tmp_file_id: str | None = None,
) -> tuple[str, str]:
    current_tmp_file_id, current_section, object_file_name = _parse_user_tmp_object(object_name, uid)
    if current_section != section or (tmp_file_id is not None and current_tmp_file_id != tmp_file_id):
        raise HTTPException(status_code=400, detail="无效的临时附件路径")
    if section == "parsed" and Path(object_file_name).suffix.lower() != ".md":
        raise HTTPException(status_code=400, detail="无效的解析附件路径")
    return current_tmp_file_id, object_file_name


def _normalize_parse_method(file_name: str, parse_method: str | None, default_ocr_engine: str) -> str:
    """按文件类型确定临时附件解析方式。"""
    suffix = Path(file_name).suffix.lower()
    if suffix not in TMP_ATTACHMENT_PARSE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="当前仅支持 PDF 和图片附件解析")

    if suffix in TMP_ATTACHMENT_IMAGE_EXTENSIONS:
        method = parse_method or ("rapid_ocr" if default_ocr_engine == "disable" else default_ocr_engine)
    else:
        method = parse_method or "disable"
    allowed_methods = (
        TMP_ATTACHMENT_OCR_METHODS if suffix in TMP_ATTACHMENT_IMAGE_EXTENSIONS else TMP_ATTACHMENT_PARSE_METHODS
    )

    if method not in allowed_methods:
        allowed = ", ".join(allowed_methods)
        raise HTTPException(status_code=400, detail=f"不支持的解析方法: {method}，可选: {allowed}")
    return method


def serialize_attachment(record: dict) -> dict:
    path = record.get("path")
    return {
        "file_id": record.get("file_id"),
        "file_name": record.get("file_name"),
        "file_type": record.get("file_type"),
        "file_size": record.get("file_size", 0),
        "status": record.get("status", "uploaded"),
        "uploaded_at": record.get("uploaded_at"),
        "path": path,
        "artifact_url": record.get("artifact_url"),
        "original_path": record.get("original_path"),
        "original_artifact_url": record.get("original_artifact_url"),
        "request_id": record.get("request_id"),
    }


async def _write_workdir_file(backend, path: str, content: bytes) -> None:
    """通过受信任 no-follow 文件边界写入实时 Workdir。"""
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="yuxi-attachment-", delete=False) as temp_file:
            temp_path = temp_file.name
            temp_file.write(content)
        await asyncio.to_thread(backend.upload_authorized_file_from_path, path, temp_path)
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


async def _store_attachment(
    *,
    thread_id: str,
    backend,
    workdir_path: str,
    file_id: str,
    file_name: str,
    file_type: str | None,
    file_content: bytes,
    parsed_markdown: str | None = None,
    truncated: bool = False,
) -> dict:
    """将正式附件直接写入实时 Project Workdir。"""
    file_name = _safe_file_name(file_name)
    storage_name = f"{file_id}_{_safe_file_name(file_name)}"
    original_path = f"{workdir_path}/uploads/{storage_name}"
    await _write_workdir_file(backend, original_path, file_content)
    record = {
        "file_id": file_id,
        "file_name": file_name,
        "file_type": file_type,
        "file_size": len(file_content),
        "original_sha256": hashlib.sha256(file_content).hexdigest(),
        "status": "uploaded",
        "uploaded_at": utc_isoformat(),
        "path": original_path,
        "artifact_url": _artifact_url(thread_id, original_path),
        "original_path": original_path,
        "original_artifact_url": _artifact_url(thread_id, original_path),
    }
    if parsed_markdown is None:
        return record

    markdown_path = f"{workdir_path}/uploads/attachments/{_make_attachment_path(storage_name)}"
    try:
        await _write_workdir_file(backend, markdown_path, parsed_markdown.encode("utf-8"))
    except Exception:
        await asyncio.to_thread(backend.delete_authorized_path, original_path, root=workdir_path)
        raise
    record.update(
        {
            "status": "parsed",
            "path": markdown_path,
            "artifact_url": _artifact_url(thread_id, markdown_path),
            "file_path": markdown_path,
            "markdown": parsed_markdown,
            "truncated": truncated,
            "markdown_size": len(parsed_markdown.encode("utf-8")),
            "markdown_sha256": hashlib.sha256(parsed_markdown.encode("utf-8")).hexdigest(),
        }
    )
    return record


async def upload_tmp_attachment_view(*, file: UploadFile, current_uid: str) -> dict:
    """上传附件到用户隔离的 MinIO tmp 路径。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="无法识别的文件名")

    file_name = _safe_file_name(file.filename)
    try:
        file_content = await read_upload_with_limit(
            file,
            max_size_bytes=MAX_ATTACHMENT_SIZE_BYTES,
            too_large_message="附件过大，当前仅支持 5 MB 以内的文件",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    file_size = len(file_content)
    tmp_file_id, object_name = _make_tmp_attachment_object(str(current_uid), file_name)
    minio_client = get_minio_client()
    bucket_name = _get_tmp_attachment_bucket()
    try:
        upload_result = await minio_client.aupload_file(
            bucket_name=bucket_name,
            object_name=object_name,
            data=file_content,
            content_type=file.content_type,
        )
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=f"临时附件上传失败: {exc}") from exc

    suffix = Path(file_name).suffix.lower()
    if suffix == ".pdf":
        parse_methods = list(TMP_ATTACHMENT_PARSE_METHODS)
    elif suffix in TMP_ATTACHMENT_IMAGE_EXTENSIONS:
        parse_methods = list(TMP_ATTACHMENT_OCR_METHODS)
    else:
        parse_methods = []

    return {
        "tmp_file_id": tmp_file_id,
        "file_name": file_name,
        "file_type": file.content_type,
        "file_size": file_size,
        "bucket_name": upload_result.bucket_name,
        "object_name": upload_result.object_name,
        "minio_url": upload_result.url,
        "uploaded_at": utc_isoformat(),
        "parse_supported": bool(parse_methods),
        "parse_methods": parse_methods,
    }


async def parse_tmp_attachment_view(
    *,
    object_name: str,
    file_name: str,
    parse_method: str | None,
    bucket_name: str | None,
    current_uid: str,
) -> dict:
    """解析用户 tmp 附件并把 markdown 写回 tmp。"""
    minio_client = get_minio_client()
    expected_bucket = _get_tmp_attachment_bucket()
    bucket_name = bucket_name or expected_bucket
    if bucket_name != expected_bucket:
        raise HTTPException(status_code=400, detail="无效的临时附件 bucket")

    tmp_file_id, safe_name = _require_tmp_object_section(object_name, str(current_uid), "original")
    default_ocr_engine = "rapid_ocr"
    if parse_method is None and Path(safe_name).suffix.lower() in TMP_ATTACHMENT_IMAGE_EXTENSIONS:
        default_ocr_engine = (await system_options.get())["default_ocr_engine"]
    method = _normalize_parse_method(safe_name, parse_method, default_ocr_engine)

    try:
        markdown = await parse_document(_minio_source(bucket_name, object_name), params={"ocr_engine": method})
        markdown, truncated = _truncate_markdown(markdown)
        parsed_object_name = _make_tmp_parsed_object(str(current_uid), tmp_file_id, safe_name)
        upload_result = await minio_client.aupload_file(
            bucket_name=bucket_name,
            object_name=parsed_object_name,
            data=markdown.encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
        )
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=f"读取临时附件失败: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Tmp attachment parse failed for {safe_name}: {exc}")
        raise HTTPException(status_code=400, detail=f"附件解析失败: {exc}") from exc

    return {
        "tmp_file_id": tmp_file_id,
        "file_name": safe_name,
        "bucket_name": upload_result.bucket_name,
        "object_name": object_name,
        "parsed_object_name": upload_result.object_name,
        "parsed_minio_url": upload_result.url,
        "parse_method": method,
        "status": "parsed",
        "truncated": truncated,
    }


async def confirm_tmp_thread_attachments_view(
    *,
    thread_id: str,
    attachments: list[dict],
    db: AsyncSession,
    current_uid: str,
) -> dict:
    """将选中的 tmp 附件正式关联到对话线程。"""
    if not attachments:
        raise HTTPException(status_code=400, detail="请选择要添加的附件")

    conv_repo = ConversationRepository(db)
    conversation = await _require_user_conversation(conv_repo, thread_id, str(current_uid))
    from yuxi.services.workdir_service import resolve_workdir_binding

    binding = await resolve_workdir_binding(thread_id=thread_id, uid=str(current_uid), db=db)
    backend = binding.create_file_backend()
    await asyncio.to_thread(backend.ensure_available)
    minio_client = get_minio_client()
    expected_bucket = _get_tmp_attachment_bucket()
    prepared_items: list[dict] = []

    for item in attachments:
        object_name = str(item.get("object_name") or "")
        bucket_name = str(item.get("bucket_name") or expected_bucket)
        if bucket_name != expected_bucket:
            raise HTTPException(status_code=400, detail="无效的临时附件 bucket")

        tmp_file_id, file_name = _require_tmp_object_section(object_name, str(current_uid), "original")
        try:
            file_content = await minio_client.adownload_file(bucket_name, object_name)
        except StorageError as exc:
            raise HTTPException(status_code=400, detail=f"读取临时附件失败: {exc}") from exc

        if len(file_content) > MAX_ATTACHMENT_SIZE_BYTES:
            max_size_mb = MAX_ATTACHMENT_SIZE_BYTES // (1024 * 1024)
            raise HTTPException(status_code=400, detail=f"附件过大，当前仅支持 {max_size_mb} MB 以内的文件")

        parsed_markdown = None
        parsed_object_name = str(item.get("parsed_object_name") or "")
        if parsed_object_name:
            _require_tmp_object_section(parsed_object_name, str(current_uid), "parsed", tmp_file_id)
            expected_parsed_object = _make_tmp_parsed_object(str(current_uid), tmp_file_id, file_name)
            if parsed_object_name != expected_parsed_object:
                raise HTTPException(status_code=400, detail="解析附件路径无效")
            try:
                parsed_bytes = await minio_client.adownload_file(bucket_name, parsed_object_name)
                parsed_markdown = parsed_bytes.decode("utf-8")
            except StorageError as exc:
                raise HTTPException(status_code=400, detail=f"读取解析附件失败: {exc}") from exc
            except UnicodeDecodeError as exc:
                raise HTTPException(status_code=400, detail="解析附件内容不是有效的 Markdown 文本") from exc

        prepared_items.append(
            {
                "file_name": file_name,
                "file_type": item.get("file_type"),
                "tmp_file_id": tmp_file_id,
                "file_content": file_content,
                "parsed_markdown": parsed_markdown,
                "truncated": bool(item.get("truncated")),
            }
        )

    added_records: list[dict] = []
    try:
        for prepared in prepared_items:
            file_id = uuid.uuid4().hex
            attachment_record = await _store_attachment(
                thread_id=thread_id,
                backend=backend,
                workdir_path=binding.virtual_path,
                file_id=file_id,
                file_name=prepared["file_name"],
                file_type=prepared["file_type"],
                file_content=prepared["file_content"],
                parsed_markdown=prepared["parsed_markdown"],
                truncated=prepared["truncated"],
            )
            added_records.append(attachment_record)
    except Exception:
        for record in added_records:
            for path in (record.get("path"), record.get("original_path")):
                if isinstance(path, str):
                    try:
                        await asyncio.to_thread(backend.delete_authorized_path, path, root=binding.virtual_path)
                    except Exception:
                        pass
        raise

    try:
        await conv_repo.add_attachments(conversation.id, added_records)
        await db.commit()
    except Exception:
        await db.rollback()
        for record in added_records:
            for path in {record.get("path"), record.get("original_path")}:
                if isinstance(path, str):
                    try:
                        await asyncio.to_thread(
                            backend.delete_authorized_path,
                            path,
                            root=binding.virtual_path,
                        )
                    except Exception:
                        pass
        raise

    delete_results = await asyncio.gather(
        *(
            minio_client.adelete_objects_by_prefix(
                expected_bucket,
                f"{_tmp_attachment_prefix(str(current_uid), prepared['tmp_file_id'])}/",
            )
            for prepared in prepared_items
        ),
        return_exceptions=True,
    )
    for prepared, result in zip(prepared_items, delete_results):
        if isinstance(result, StorageError):
            logger.warning(f"Failed to remove confirmed tmp attachment {prepared['tmp_file_id']}: {result}")

    return {"attachments": [serialize_attachment(item) for item in added_records]}


async def upload_thread_attachment_view(
    *,
    thread_id: str,
    file: UploadFile,
    db: AsyncSession,
    current_uid: str,
) -> dict:
    """上传原始附件并关联到指定对话线程。"""
    conv_repo = ConversationRepository(db)
    conversation = await _require_user_conversation(conv_repo, thread_id, str(current_uid))
    from yuxi.services.workdir_service import resolve_workdir_binding

    binding = await resolve_workdir_binding(thread_id=thread_id, uid=str(current_uid), db=db)
    backend = binding.create_file_backend()
    await asyncio.to_thread(backend.ensure_available)
    if not file.filename:
        raise HTTPException(status_code=400, detail="无法识别的文件名")

    file_name = Path(file.filename).name
    max_size_mb = MAX_ATTACHMENT_SIZE_BYTES // (1024 * 1024)
    try:
        file_content = await read_upload_with_limit(
            file,
            max_size_bytes=MAX_ATTACHMENT_SIZE_BYTES,
            too_large_message=f"附件过大，当前仅支持 {max_size_mb} MB 以内的文件",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    parsed_markdown = None
    truncated = False
    try:
        await file.seek(0)
        conversion = await _convert_upload_to_markdown(file)
        parsed_markdown = conversion.markdown
        truncated = conversion.truncated
    except ValueError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Attachment markdown conversion failed for {file_name}: {exc}")

    file_id = uuid.uuid4().hex
    attachment_record = await _store_attachment(
        thread_id=thread_id,
        backend=backend,
        workdir_path=binding.virtual_path,
        file_id=file_id,
        file_name=file_name,
        file_type=file.content_type,
        file_content=file_content,
        parsed_markdown=parsed_markdown,
        truncated=truncated,
    )

    try:
        await conv_repo.add_attachment(conversation.id, attachment_record)
        await db.commit()
    except Exception:
        await db.rollback()
        for path in {attachment_record.get("path"), attachment_record.get("original_path")}:
            if isinstance(path, str):
                try:
                    await asyncio.to_thread(backend.delete_authorized_path, path, root=binding.virtual_path)
                except Exception:
                    pass
        raise

    return serialize_attachment(attachment_record)


async def list_thread_attachments_view(
    *,
    thread_id: str,
    db: AsyncSession,
    current_uid: str,
) -> dict:
    """列出指定对话线程的附件。"""
    conv_repo = ConversationRepository(db)
    conversation = await _require_user_conversation(conv_repo, thread_id, str(current_uid))
    attachments = await conv_repo.get_attachments(conversation.id)
    return {
        "attachments": [serialize_attachment(item) for item in attachments],
        "limits": {
            "allowed_extensions": sorted(ATTACHMENT_ALLOWED_EXTENSIONS),
            "max_size_bytes": MAX_ATTACHMENT_SIZE_BYTES,
        },
    }


async def delete_thread_attachment_view(
    *,
    thread_id: str,
    file_id: str,
    db: AsyncSession,
    current_uid: str,
) -> dict:
    """删除指定对话线程的附件。"""
    conv_repo = ConversationRepository(db)
    conversation = await _require_user_conversation(conv_repo, thread_id, str(current_uid))
    from yuxi.services.workdir_service import resolve_workdir_binding

    binding = await resolve_workdir_binding(thread_id=thread_id, uid=str(current_uid), db=db)
    backend = binding.create_file_backend()
    await asyncio.to_thread(backend.ensure_available)

    existing_attachments = await conv_repo.lock_attachments(conversation.id)
    target_attachment = next((item for item in existing_attachments if item.get("file_id") == file_id), None)
    if target_attachment is None:
        raise HTTPException(status_code=404, detail="附件不存在或已被删除")

    removed = await conv_repo.remove_attachment(conversation.id, file_id)
    if not removed:
        raise HTTPException(status_code=404, detail="附件不存在或已被删除")
    await db.commit()

    for path in {target_attachment.get("path"), target_attachment.get("original_path")}:
        if not isinstance(path, str):
            continue
        try:
            await asyncio.to_thread(backend.delete_authorized_path, path, root=binding.virtual_path)
        except FileNotFoundError:
            pass
        except Exception:
            # PostgreSQL 已经移除 shipping 引用；残留文件仍留在用户可见 Workdir，后续可显式清理。
            logger.warning("附件元数据已删除，但 Workdir 文件清理失败: thread=%s path=%s", thread_id, path)

    return {"message": "附件已删除"}
