from __future__ import annotations

import asyncio
import io
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault(
    "SAVE_DIR", os.path.join(os.environ.get("CLAUDE_JOB_DIR", tempfile.gettempdir()), "yuxi-test-saves")
)

from yuxi.services import attachment_service as service
from yuxi.services import project_workdir_service

pytestmark = pytest.mark.unit


class FakeUpload:
    def __init__(self, filename: str, content: bytes, content_type: str | None = None):
        self.filename = filename
        self.content_type = content_type
        self._content = content
        self._offset = 0

    async def seek(self, offset: int) -> None:
        self._offset = offset

    async def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._content):
            return b""
        end = len(self._content) if size < 0 else min(len(self._content), self._offset + size)
        chunk = self._content[self._offset : end]
        self._offset = end
        return chunk


class FakeMinioClient:
    KB_BUCKETS = {"documents": "knowledgebases"}

    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.uploads: list[dict] = []
        self.deleted: list[tuple[str, str]] = []
        self.deleted_prefixes: list[tuple[str, str]] = []

    async def aupload_file(self, bucket_name: str, object_name: str, data: bytes, content_type: str | None = None):
        self.objects[(bucket_name, object_name)] = data
        self.uploads.append(
            {
                "bucket_name": bucket_name,
                "object_name": object_name,
                "data": data,
                "content_type": content_type,
            }
        )
        return SimpleNamespace(
            bucket_name=bucket_name,
            object_name=object_name,
            url=f"http://minio:9000/{bucket_name}/{object_name}",
        )

    async def adownload_file(self, bucket_name: str, object_name: str) -> bytes:
        try:
            return self.objects[(bucket_name, object_name)]
        except KeyError as exc:
            raise service.StorageError("missing object") from exc

    async def adownload_response(self, bucket_name: str, object_name: str):
        try:
            content = self.objects[(bucket_name, object_name)]
        except KeyError as exc:
            raise service.StorageError("missing object") from exc

        class Response:
            def __init__(self):
                self.stream = io.BytesIO(content)

            def read(self, size):
                return self.stream.read(size)

            def close(self):
                return None

            def release_conn(self):
                return None

        return Response()

    async def adelete_file(self, bucket_name: str, object_name: str) -> bool:
        self.objects.pop((bucket_name, object_name), None)
        self.deleted.append((bucket_name, object_name))
        return True

    async def adelete_objects_by_prefix(self, bucket_name: str, prefix: str) -> int:
        keys = [key for key in self.objects if key[0] == bucket_name and key[1].startswith(prefix)]
        for key in keys:
            self.objects.pop(key)
        self.deleted_prefixes.append((bucket_name, prefix))
        return len(keys)


class _ScopedBackendAdapter:
    def clear_scope_files(self, scope):
        assert scope == "uploads"
        return self.clear_upload_files()

    def upload_scope_file_from_path(self, scope, path, source_path):
        assert scope == "uploads"
        return self.write_upload_file(path, Path(source_path).read_bytes())


@dataclass
class FakeConversation:
    id: int = 1
    uid: str = "user-1"
    agent_id: str = "agent-1"
    status: str = "active"
    extra_metadata: dict | None = None


class FakeConversationRepository:
    def __init__(self, db):
        self.conversation = FakeConversation()
        self.attachments: list[dict] = []

    async def get_conversation_by_thread_id(self, thread_id: str):
        return self.conversation

    async def add_attachment(self, conversation_id: int, attachment_info: dict):
        self.attachments.append(attachment_info)
        return attachment_info

    async def add_attachments(self, conversation_id: int, attachment_infos: list[dict]):
        self.attachments.extend(attachment_infos)
        return attachment_infos

    async def get_attachments(self, conversation_id: int):
        return list(self.attachments)

    async def lock_attachments(self, conversation_id: int):
        return list(self.attachments)

    async def remove_attachment(self, conversation_id: int, file_id: str):
        before = len(self.attachments)
        self.attachments = [item for item in self.attachments if item.get("file_id") != file_id]
        return len(self.attachments) != before


class FakeDB:
    def __init__(self):
        self.commit_count = 0
        self.rollback_count = 0

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1


class FakeWorkdirBackend:
    def __init__(self):
        self.files: dict[str, bytes] = {}

    def ensure_available(self):
        return "sandbox-1"

    def upload_authorized_file_from_path(self, path: str, source_path: str):
        self.files[path] = Path(source_path).read_bytes()

    def delete_authorized_path(self, path: str, *, root: str):
        assert root == "/home/gem/projects/project-workdir-1"
        if self.files.pop(path, None) is None:
            raise FileNotFoundError(path)


@pytest.mark.asyncio
async def test_upload_tmp_attachment_writes_user_scoped_minio_object(monkeypatch):
    fake_minio = FakeMinioClient()
    monkeypatch.setattr(service, "get_minio_client", lambda: fake_minio)

    response = await service.upload_tmp_attachment_view(
        file=FakeUpload("demo.pdf", b"pdf-bytes", "application/pdf"),
        current_uid="user-1",
    )

    assert response["bucket_name"] == "knowledgebases"
    assert response["object_name"].startswith("tmp/chat_attachments/user-1/")
    assert response["parse_methods"][0] == "disable"
    assert fake_minio.objects[("knowledgebases", response["object_name"])] == b"pdf-bytes"


@pytest.mark.asyncio
async def test_parse_tmp_attachment_uses_selected_method_and_uploads_markdown(monkeypatch):
    fake_minio = FakeMinioClient()
    object_name = "tmp/chat_attachments/user-1/tmp-1/original/demo.pdf"
    fake_minio.objects[("knowledgebases", object_name)] = b"pdf-bytes"
    monkeypatch.setattr(service, "get_minio_client", lambda: fake_minio)

    parse_calls = []

    async def fake_parse(source: str, params: dict | None = None) -> str:
        parse_calls.append({"source": source, "params": params})
        return "# parsed"

    monkeypatch.setattr(service, "parse_document", fake_parse)

    response = await service.parse_tmp_attachment_view(
        object_name=object_name,
        file_name="demo.pdf",
        parse_method="disable",
        bucket_name="knowledgebases",
        current_uid="user-1",
    )

    assert parse_calls == [
        {
            "source": f"minio://knowledgebases/{object_name}",
            "params": {"ocr_engine": "disable"},
        }
    ]
    assert response["parsed_object_name"] == "tmp/chat_attachments/user-1/tmp-1/parsed/demo.md"
    assert fake_minio.objects[("knowledgebases", response["parsed_object_name"])] == b"# parsed"


@pytest.fixture
def confirm_attachment_env(monkeypatch: pytest.MonkeyPatch):
    """构造 confirm 流程所需的 MinIO 与仓库假实现，并挂载到 service 模块。"""
    fake_minio = FakeMinioClient()
    fake_repo = FakeConversationRepository(db=None)
    backend = FakeWorkdirBackend()

    monkeypatch.setattr(service, "get_minio_client", lambda: fake_minio)
    monkeypatch.setattr(service, "ConversationRepository", lambda db: fake_repo)

    async def resolve_binding(**kwargs):
        del kwargs
        return SimpleNamespace(
            workdir_path="/home/gem/projects/project-workdir-1",
            create_file_backend=lambda **_kwargs: backend,
        )

    monkeypatch.setattr(project_workdir_service, "resolve_project_workdir_binding", resolve_binding)
    fake_repo.workdir_backend = backend

    async def noop_invalidate(thread_id: str):
        return None

    monkeypatch.setattr(service, "invalidate_mention_cache", noop_invalidate)

    return fake_minio, fake_repo


@pytest.mark.asyncio
async def test_confirm_tmp_thread_attachments_writes_realtime_workdir(confirm_attachment_env):
    fake_minio, fake_repo = confirm_attachment_env
    original_object = "tmp/chat_attachments/user-1/tmp-1/original/demo.pdf"
    parsed_object = "tmp/chat_attachments/user-1/tmp-1/parsed/demo.md"
    fake_minio.objects[("knowledgebases", original_object)] = b"pdf-bytes"
    fake_minio.objects[("knowledgebases", parsed_object)] = b"# parsed"

    response = await service.confirm_tmp_thread_attachments_view(
        thread_id="thread-1",
        attachments=[
            {
                "file_name": "demo.pdf",
                "file_type": "application/pdf",
                "bucket_name": "knowledgebases",
                "object_name": original_object,
                "parsed_object_name": parsed_object,
                "truncated": False,
            }
        ],
        db=FakeDB(),
        current_uid="user-1",
    )

    [attachment] = response["attachments"]
    assert attachment["status"] == "parsed"
    stored = fake_repo.attachments[0]
    assert stored["original_path"].startswith("/home/gem/projects/project-workdir-1/uploads/")
    assert stored["path"].startswith("/home/gem/projects/project-workdir-1/uploads/attachments/")
    assert fake_repo.workdir_backend.files[stored["original_path"]] == b"pdf-bytes"
    assert fake_repo.workdir_backend.files[stored["path"]] == b"# parsed"
    assert fake_minio.deleted_prefixes == [("knowledgebases", "tmp/chat_attachments/user-1/tmp-1/")]


@pytest.mark.asyncio
async def test_parse_tmp_attachment_uses_object_name_for_type_validation(monkeypatch):
    fake_minio = FakeMinioClient()
    object_name = "tmp/chat_attachments/user-1/tmp-1/original/demo.docx"
    fake_minio.objects[("knowledgebases", object_name)] = b"docx-bytes"
    monkeypatch.setattr(service, "get_minio_client", lambda: fake_minio)

    with pytest.raises(service.HTTPException) as exc_info:
        await service.parse_tmp_attachment_view(
            object_name=object_name,
            file_name="demo.pdf",
            parse_method="disable",
            bucket_name="knowledgebases",
            current_uid="user-1",
        )

    assert exc_info.value.status_code == 400
    assert "PDF 和图片" in exc_info.value.detail


@pytest.mark.asyncio
async def test_parse_tmp_attachment_handles_url_metacharacters(monkeypatch):
    fake_minio = FakeMinioClient()
    object_name = "tmp/chat_attachments/user-1/tmp-1/original/q1?.pdf"
    fake_minio.objects[("knowledgebases", object_name)] = b"pdf-bytes"
    monkeypatch.setattr(service, "get_minio_client", lambda: fake_minio)

    parse_calls = []

    async def fake_parse(source: str, params: dict | None = None) -> str:
        parse_calls.append(source)
        return "# parsed"

    monkeypatch.setattr(service, "parse_document", fake_parse)

    response = await service.parse_tmp_attachment_view(
        object_name=object_name,
        file_name="ignored.pdf",
        parse_method="disable",
        bucket_name="knowledgebases",
        current_uid="user-1",
    )

    assert parse_calls == ["minio://knowledgebases/tmp/chat_attachments/user-1/tmp-1/original/q1%3F.pdf"]
    assert response["parsed_object_name"] == "tmp/chat_attachments/user-1/tmp-1/parsed/q1?.md"


@pytest.mark.asyncio
async def test_confirm_tmp_thread_attachments_rejects_non_parsed_object(confirm_attachment_env):
    fake_minio, fake_repo = confirm_attachment_env
    original_object = "tmp/chat_attachments/user-1/tmp-1/original/demo.pdf"
    fake_minio.objects[("knowledgebases", original_object)] = b"pdf-bytes"

    with pytest.raises(service.HTTPException) as exc_info:
        await service.confirm_tmp_thread_attachments_view(
            thread_id="thread-1",
            attachments=[
                {
                    "file_name": "demo.pdf",
                    "file_type": "application/pdf",
                    "bucket_name": "knowledgebases",
                    "object_name": original_object,
                    "parsed_object_name": original_object,
                }
            ],
            db=None,
            current_uid="user-1",
        )

    assert exc_info.value.status_code == 400
    assert fake_repo.attachments == []


@pytest.mark.asyncio
async def test_confirm_tmp_thread_attachments_validates_batch_before_commit(confirm_attachment_env):
    fake_minio, fake_repo = confirm_attachment_env
    valid_object = "tmp/chat_attachments/user-1/tmp-1/original/valid.pdf"
    missing_object = "tmp/chat_attachments/user-1/tmp-2/original/missing.pdf"
    fake_minio.objects[("knowledgebases", valid_object)] = b"pdf-bytes"

    with pytest.raises(service.HTTPException) as exc_info:
        await service.confirm_tmp_thread_attachments_view(
            thread_id="thread-1",
            attachments=[
                {"file_name": "valid.pdf", "bucket_name": "knowledgebases", "object_name": valid_object},
                {"file_name": "missing.pdf", "bucket_name": "knowledgebases", "object_name": missing_object},
            ],
            db=None,
            current_uid="user-1",
        )

    assert exc_info.value.status_code == 400
    assert fake_repo.attachments == []


@pytest.mark.asyncio
async def test_confirm_tmp_thread_attachments_keeps_duplicate_names_separate(confirm_attachment_env):
    fake_minio, fake_repo = confirm_attachment_env
    first_object = "tmp/chat_attachments/user-1/tmp-1/original/report.pdf"
    second_object = "tmp/chat_attachments/user-1/tmp-2/original/report.pdf"
    fake_minio.objects[("knowledgebases", first_object)] = b"first"
    fake_minio.objects[("knowledgebases", second_object)] = b"second"

    response = await service.confirm_tmp_thread_attachments_view(
        thread_id="thread-1",
        attachments=[
            {"file_name": "report.pdf", "bucket_name": "knowledgebases", "object_name": first_object},
            {"file_name": "report.pdf", "bucket_name": "knowledgebases", "object_name": second_object},
        ],
        db=FakeDB(),
        current_uid="user-1",
    )

    first, second = response["attachments"]
    assert first["original_path"] != second["original_path"]
    first_record, second_record = fake_repo.attachments
    assert fake_repo.workdir_backend.files[first_record["original_path"]] == b"first"
    assert fake_repo.workdir_backend.files[second_record["original_path"]] == b"second"


@pytest.mark.asyncio
async def test_materialize_attachment_record_restores_missing_local_cache(monkeypatch, tmp_path: Path):
    fake_minio = FakeMinioClient()
    original_object = "threads/thread-1/attachments/file-1/original/demo.pdf"
    markdown_object = "threads/thread-1/attachments/file-1/parsed/demo.md"
    fake_minio.objects[("knowledgebases", original_object)] = b"pdf-bytes"
    fake_minio.objects[("knowledgebases", markdown_object)] = b"# parsed"
    monkeypatch.setattr(service, "get_minio_client", lambda: fake_minio)

    def fake_uploads_dir(thread_id: str) -> Path:
        path = tmp_path / thread_id / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.setattr(service, "ensure_thread_dirs", lambda thread_id, uid: fake_uploads_dir(thread_id))
    monkeypatch.setattr(service, "sandbox_uploads_dir", fake_uploads_dir)
    attachment = {
        "bucket_name": "knowledgebases",
        "original_object_name": original_object,
        "markdown_object_name": markdown_object,
        "original_path": "/home/gem/user-data/uploads/file-1_demo.pdf",
        "path": "/home/gem/user-data/uploads/attachments/file-1_demo.md",
    }

    await service.materialize_attachment_record("thread-1", "user-1", attachment)

    assert (tmp_path / "thread-1" / "uploads" / "file-1_demo.pdf").read_bytes() == b"pdf-bytes"
    assert (tmp_path / "thread-1" / "uploads" / "attachments" / "file-1_demo.md").read_text() == "# parsed"


def _hydrate_record(
    *,
    thread_id: str = "thread-1",
    file_id: str = "file-1",
    file_name: str = "demo.pdf",
    parsed: bool = False,
) -> dict:
    original_object, markdown_object = service._make_thread_attachment_objects(thread_id, file_id, file_name)
    storage_name = f"{file_id}_{file_name}"
    original_path = f"/home/gem/user-data/uploads/{storage_name}"
    record = {
        "file_id": file_id,
        "file_name": file_name,
        "file_size": 3,
        "bucket_name": "knowledgebases",
        "original_object_name": original_object,
        "original_path": original_path,
        "path": original_path,
    }
    if parsed:
        record.update(
            {
                "markdown_object_name": markdown_object,
                "path": f"/home/gem/user-data/uploads/attachments/{service._make_attachment_path(storage_name)}",
            }
        )
    return record


@pytest.mark.asyncio
async def test_hydrate_attachment_records_streams_validated_minio_objects(monkeypatch, tmp_path: Path):
    fake_minio = FakeMinioClient()
    parsed = _hydrate_record(thread_id="parent-thread", parsed=True)
    plain = _hydrate_record(thread_id="parent-thread", file_id="file-2", file_name="plain.txt")
    fake_minio.objects[("knowledgebases", parsed["original_object_name"])] = b"pdf-bytes"
    fake_minio.objects[("knowledgebases", parsed["markdown_object_name"])] = b"# parsed"
    fake_minio.objects[("knowledgebases", plain["original_object_name"])] = b"plain"
    parsed["file_size"] = len(b"pdf-bytes")
    plain["file_size"] = len(b"plain")
    events = []
    original_download = fake_minio.adownload_response

    async def tracked_download(bucket_name, object_name):
        events.append(("download", object_name))
        return await original_download(bucket_name, object_name)

    fake_minio.adownload_response = tracked_download
    monkeypatch.setattr(service, "get_minio_client", lambda: fake_minio)
    monkeypatch.setattr(service, "sandbox_uploads_dir", lambda _thread_id: tmp_path / "legacy-uploads")
    calls = {}

    class FakeBackend(_ScopedBackendAdapter):
        def __init__(self, **kwargs):
            calls["scope"] = kwargs

        def clear_upload_files(self):
            events.append(("clear", None))

        def write_upload_file(self, path, content):
            events.append(("write", path, content))

    monkeypatch.setattr(service, "ProvisionerSandboxBackend", FakeBackend)

    await service.hydrate_attachment_records_to_sandbox(
        "child-thread",
        "user-1",
        [parsed, plain],
        file_thread_id="parent-thread",
    )

    assert calls["scope"] == {
        "thread_id": "child-thread",
        "uid": "user-1",
        "sandbox_instance_id": None,
        "create_if_missing": True,
    }
    assert events == [
        ("clear", None),
        ("download", parsed["original_object_name"]),
        ("write", parsed["original_path"], b"pdf-bytes"),
        ("download", parsed["markdown_object_name"]),
        ("write", parsed["path"], b"# parsed"),
        ("download", plain["original_object_name"]),
        ("write", plain["original_path"], b"plain"),
    ]


@pytest.mark.asyncio
async def test_hydrate_attachment_records_clears_sandbox_when_current_set_is_empty(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(service, "get_minio_client", FakeMinioClient)
    monkeypatch.setattr(service, "sandbox_uploads_dir", lambda _thread_id: tmp_path / "legacy-uploads")
    clear_calls = []

    class FakeBackend(_ScopedBackendAdapter):
        def __init__(self, **_kwargs):
            pass

        def clear_upload_files(self):
            clear_calls.append(True)

        def write_upload_file(self, _path, _content):
            pytest.fail("空附件集不得写入")

    monkeypatch.setattr(service, "ProvisionerSandboxBackend", FakeBackend)

    await service.hydrate_attachment_records_to_sandbox("thread-1", "user-1", [])

    assert clear_calls == [True]


@pytest.mark.asyncio
async def test_hydrate_attachment_records_fails_before_sandbox_on_missing_object(monkeypatch, tmp_path: Path):
    record = _hydrate_record()
    clear_calls = []
    monkeypatch.setattr(service, "get_minio_client", FakeMinioClient)
    monkeypatch.setattr(service, "sandbox_uploads_dir", lambda _thread_id: tmp_path / "legacy-uploads")

    class FakeBackend(_ScopedBackendAdapter):
        def __init__(self, **_kwargs):
            pass

        def clear_upload_files(self):
            clear_calls.append(True)

        def write_upload_file(self, _path, _content):
            pytest.fail("缺失对象时不得写入 sandbox")

    monkeypatch.setattr(service, "ProvisionerSandboxBackend", FakeBackend)

    with pytest.raises(service.HTTPException, match="附件对象不存在"):
        await service.hydrate_attachment_records_to_sandbox("thread-1", "user-1", [record])
    assert clear_calls == [True, True]


@pytest.mark.asyncio
async def test_hydrate_attachment_records_rejects_cross_thread_object_before_download(monkeypatch, tmp_path: Path):
    fake_minio = FakeMinioClient()
    record = _hydrate_record()
    record["original_object_name"] = "threads/other-thread/attachments/file-1/original/demo.pdf"
    monkeypatch.setattr(service, "get_minio_client", lambda: fake_minio)
    monkeypatch.setattr(service, "sandbox_uploads_dir", lambda _thread_id: tmp_path / "legacy-uploads")
    monkeypatch.setattr(
        service,
        "ProvisionerSandboxBackend",
        lambda **_kwargs: pytest.fail("跨线程对象不得触达 sandbox"),
    )

    with pytest.raises(service.HTTPException, match="附件对象作用域无效"):
        await service.hydrate_attachment_records_to_sandbox("thread-1", "user-1", [record])
    assert fake_minio.objects == {}


@pytest.mark.asyncio
async def test_hydrate_accepts_existing_record_with_pre_normalized_file_name(monkeypatch, tmp_path: Path):
    fake_minio = FakeMinioClient()
    object_name = "threads/thread-1/attachments/file-1/original/report.txt"
    fake_minio.objects[("knowledgebases", object_name)] = b"content"
    record = {
        "file_id": "file-1",
        "file_name": " report.txt",
        "file_size": 7,
        "bucket_name": "knowledgebases",
        "original_object_name": object_name,
        "original_path": "/home/gem/user-data/uploads/file-1_report.txt",
        "path": "/home/gem/user-data/uploads/file-1_report.txt",
    }
    writes = []
    monkeypatch.setattr(service, "get_minio_client", lambda: fake_minio)
    monkeypatch.setattr(service, "sandbox_uploads_dir", lambda _thread_id: tmp_path / "legacy-uploads")

    class FakeBackend(_ScopedBackendAdapter):
        def __init__(self, **_kwargs):
            pass

        def clear_upload_files(self):
            pass

        def write_upload_file(self, path, content):
            writes.append((path, content))

    monkeypatch.setattr(service, "ProvisionerSandboxBackend", FakeBackend)

    await service.hydrate_attachment_records_to_sandbox("thread-1", "user-1", [record])

    assert writes == [("/home/gem/user-data/uploads/file-1_report.txt", b"content")]


@pytest.mark.asyncio
async def test_store_attachment_normalizes_persisted_file_name(monkeypatch):
    del monkeypatch
    backend = FakeWorkdirBackend()

    record = await service._store_attachment(
        thread_id="thread-1",
        backend=backend,
        workdir_path="/home/gem/projects/project-workdir-1",
        file_id="file-1",
        file_name=" report.txt",
        file_type="text/plain",
        file_content=b"content",
    )

    assert record["file_name"] == "report.txt"
    assert record["original_path"] == "/home/gem/projects/project-workdir-1/uploads/file-1_report.txt"
    assert backend.files[record["original_path"]] == b"content"


@pytest.mark.asyncio
async def test_hydrate_attachment_records_uses_legacy_files_without_minio_metadata(monkeypatch, tmp_path: Path):
    uploads_dir = tmp_path / "legacy-uploads"
    attachments_dir = uploads_dir / "attachments"
    attachments_dir.mkdir(parents=True)
    record = _hydrate_record(file_id="legacy", file_name="legacy.pdf", parsed=True)
    record.pop("bucket_name")
    record.pop("original_object_name")
    record.pop("markdown_object_name")
    record["original_path"] = "/home/gem/user-data/uploads/legacy.pdf"
    record["path"] = "/home/gem/user-data/uploads/attachments/legacy.md"
    (uploads_dir / "legacy.pdf").write_bytes(b"legacy-pdf")
    (attachments_dir / "legacy.md").write_bytes(b"# legacy")
    monkeypatch.setattr(service, "get_minio_client", FakeMinioClient)
    monkeypatch.setattr(service, "sandbox_uploads_dir", lambda _thread_id: uploads_dir)
    calls = []

    class FakeBackend(_ScopedBackendAdapter):
        def __init__(self, **_kwargs):
            pass

        def clear_upload_files(self):
            calls.append(("clear",))

        def write_upload_file(self, path, content):
            calls.append(("write", path, content))

    monkeypatch.setattr(service, "ProvisionerSandboxBackend", FakeBackend)

    await service.hydrate_attachment_records_to_sandbox(
        "thread-1",
        "user-1",
        [record],
    )

    assert calls == [
        ("clear",),
        ("write", record["original_path"], b"legacy-pdf"),
        ("write", record["path"], b"# legacy"),
    ]


@pytest.mark.asyncio
async def test_hydrate_attachment_records_rejects_missing_legacy_file_before_sandbox(monkeypatch, tmp_path: Path):
    record = _hydrate_record(file_id="missing", file_name="missing.pdf")
    record.pop("bucket_name")
    record.pop("original_object_name")
    record["original_path"] = "/home/gem/user-data/uploads/missing.pdf"
    record["path"] = record["original_path"]
    clear_calls = []
    monkeypatch.setattr(service, "get_minio_client", FakeMinioClient)
    monkeypatch.setattr(service, "sandbox_uploads_dir", lambda _thread_id: tmp_path / "legacy-uploads")

    class FakeBackend(_ScopedBackendAdapter):
        def __init__(self, **_kwargs):
            pass

        def clear_upload_files(self):
            clear_calls.append(True)

        def write_upload_file(self, _path, _content):
            pytest.fail("历史附件缺失时不得写入 sandbox")

    monkeypatch.setattr(service, "ProvisionerSandboxBackend", FakeBackend)

    with pytest.raises(service.HTTPException, match="不存在或不安全"):
        await service.hydrate_attachment_records_to_sandbox("thread-1", "user-1", [record])
    assert clear_calls == [True, True]


@pytest.mark.asyncio
async def test_hydrate_attachment_records_rejects_legacy_symlink(monkeypatch, tmp_path: Path):
    uploads_dir = tmp_path / "legacy-uploads"
    uploads_dir.mkdir()
    secret = tmp_path / "secret"
    secret.write_bytes(b"worker-secret")
    record = _hydrate_record(file_id="legacy", file_name="secret.txt")
    record.pop("bucket_name")
    record.pop("original_object_name")
    record["original_path"] = "/home/gem/user-data/uploads/secret.txt"
    record["path"] = record["original_path"]
    (uploads_dir / "secret.txt").symlink_to(secret)
    clear_calls = []
    monkeypatch.setattr(service, "get_minio_client", FakeMinioClient)
    monkeypatch.setattr(service, "sandbox_uploads_dir", lambda _thread_id: uploads_dir)

    class FakeBackend(_ScopedBackendAdapter):
        def __init__(self, **_kwargs):
            pass

        def clear_upload_files(self):
            clear_calls.append(True)

        def write_upload_file(self, _path, _content):
            pytest.fail("符号链接内容不得写入 sandbox")

    monkeypatch.setattr(service, "ProvisionerSandboxBackend", FakeBackend)

    with pytest.raises(service.HTTPException, match="不存在或不安全"):
        await service.hydrate_attachment_records_to_sandbox("thread-1", "user-1", [record])
    assert clear_calls == [True, True]


@pytest.mark.asyncio
async def test_hydrate_cancellation_waits_for_write_then_clears(monkeypatch, tmp_path: Path):
    fake_minio = FakeMinioClient()
    record = _hydrate_record()
    fake_minio.objects[("knowledgebases", record["original_object_name"])] = b"content"
    record["file_size"] = len(b"content")
    write_started = threading.Event()
    allow_write_finish = threading.Event()
    calls = []
    monkeypatch.setattr(service, "get_minio_client", lambda: fake_minio)
    monkeypatch.setattr(service, "sandbox_uploads_dir", lambda _thread_id: tmp_path / "legacy-uploads")

    class FakeBackend(_ScopedBackendAdapter):
        def __init__(self, **_kwargs):
            pass

        def clear_upload_files(self):
            calls.append("clear")

        def write_upload_file(self, _path, _content):
            calls.append("write-start")
            write_started.set()
            allow_write_finish.wait(timeout=5)
            calls.append("write-finish")

    monkeypatch.setattr(service, "ProvisionerSandboxBackend", FakeBackend)
    task = asyncio.create_task(service.hydrate_attachment_records_to_sandbox("thread-1", "user-1", [record]))
    await asyncio.to_thread(write_started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    allow_write_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["clear", "write-start", "write-finish", "clear"]


def test_delete_materialized_attachment_files_removes_only_target(monkeypatch, tmp_path: Path):
    uploads_dir = tmp_path / "thread-1" / "uploads"
    attachments_dir = uploads_dir / "attachments"
    attachments_dir.mkdir(parents=True)
    original = uploads_dir / "file-1_demo.pdf"
    markdown = attachments_dir / "file-1_demo.md"
    unrelated = uploads_dir / "keep.txt"
    original.write_bytes(b"pdf")
    markdown.write_text("parsed", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(service, "sandbox_uploads_dir", lambda _thread_id: uploads_dir)

    service._delete_materialized_attachment_files(
        "thread-1",
        {
            "original_path": "/home/gem/user-data/uploads/file-1_demo.pdf",
            "path": "/home/gem/user-data/uploads/attachments/file-1_demo.md",
            "markdown_object_name": "threads/thread-1/attachments/file-1/parsed/demo.md",
        },
    )

    assert not original.exists()
    assert not markdown.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_delete_recorded_objects_is_best_effort():
    class FailingMinio:
        async def adownload_file(self, _bucket_name, object_name):
            return object_name.encode()

        async def adelete_file(self, _bucket_name, _object_name):
            raise service.StorageError("storage unavailable")

    await service._delete_recorded_objects(
        {"original_object_name": "threads/thread-1/attachments/file-1/original/demo.pdf"},
        "knowledgebases",
        FailingMinio(),
    )


@pytest.mark.asyncio
async def test_delete_thread_attachment_updates_live_workdir_even_during_runtime(monkeypatch):
    fake_repo = FakeConversationRepository(db=None)
    backend = FakeWorkdirBackend()
    original = "/home/gem/projects/project-workdir-1/uploads/file-1_demo.pdf"
    parsed = "/home/gem/projects/project-workdir-1/uploads/attachments/file-1_demo.md"
    backend.files = {original: b"pdf", parsed: b"markdown"}
    fake_repo.attachments = [{"file_id": "file-1", "file_name": "demo.pdf", "original_path": original, "path": parsed}]

    async def resolve_binding(**kwargs):
        del kwargs
        return SimpleNamespace(
            workdir_path="/home/gem/projects/project-workdir-1",
            create_file_backend=lambda **_kwargs: backend,
        )

    monkeypatch.setattr(service, "ConversationRepository", lambda _db: fake_repo)
    monkeypatch.setattr(project_workdir_service, "resolve_project_workdir_binding", resolve_binding)
    monkeypatch.setattr(service, "invalidate_mention_cache", AsyncMock())

    result = await service.delete_thread_attachment_view(
        thread_id="thread-1", file_id="file-1", db=FakeDB(), current_uid="user-1"
    )

    assert result == {"message": "附件已删除"}
    assert fake_repo.attachments == []
    assert backend.files == {}


@pytest.mark.asyncio
async def test_delete_thread_attachment_does_not_delete_bytes_before_metadata_commit(monkeypatch):
    fake_repo = FakeConversationRepository(db=None)
    backend = FakeWorkdirBackend()
    original = "/home/gem/projects/project-workdir-1/uploads/file-1_demo.pdf"
    backend.files = {original: b"pdf"}
    fake_repo.attachments = [
        {"file_id": "file-1", "file_name": "demo.pdf", "original_path": original, "path": original}
    ]

    async def fail_remove(_conversation_id: int, _file_id: str):
        raise RuntimeError("database unavailable")

    fake_repo.remove_attachment = fail_remove

    async def resolve_binding(**kwargs):
        del kwargs
        return SimpleNamespace(
            workdir_path="/home/gem/projects/project-workdir-1",
            create_file_backend=lambda **_kwargs: backend,
        )

    monkeypatch.setattr(service, "ConversationRepository", lambda _db: fake_repo)
    monkeypatch.setattr(project_workdir_service, "resolve_project_workdir_binding", resolve_binding)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.delete_thread_attachment_view(
            thread_id="thread-1",
            file_id="file-1",
            db=FakeDB(),
            current_uid="user-1",
        )

    assert backend.files == {original: b"pdf"}
