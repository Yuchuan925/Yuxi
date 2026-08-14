from __future__ import annotations

from yuxi.services import conversation_service as cs


def test_thread_attachment_objects_are_scoped_by_thread_and_file_id() -> None:
    original, parsed = cs._make_thread_attachment_objects("t-1", "f-1", "demo.txt")

    assert original == "threads/t-1/attachments/f-1/original/demo.txt"
    assert parsed == "threads/t-1/attachments/f-1/parsed/demo.md"


def test_serialize_attachment_includes_original_file_fields() -> None:
    serialized = cs.serialize_attachment(
        {
            "file_id": "f-1",
            "file_name": "demo.txt",
            "file_type": "text/plain",
            "file_size": 5,
            "status": "parsed",
            "uploaded_at": "2026-03-25T00:00:00+00:00",
            "path": "/home/gem/user-data/uploads/attachments/demo.md",
            "artifact_url": "/api/chat/thread/t-1/artifacts/home/gem/user-data/uploads/attachments/demo.md",
            "original_path": "/home/gem/user-data/uploads/demo.txt",
            "original_artifact_url": "/api/chat/thread/t-1/artifacts/home/gem/user-data/uploads/demo.txt",
            "minio_url": None,
        }
    )

    assert serialized["path"] == "/home/gem/user-data/uploads/attachments/demo.md"
    assert serialized["original_path"] == "/home/gem/user-data/uploads/demo.txt"
    assert serialized["original_artifact_url"] == "/api/chat/thread/t-1/artifacts/home/gem/user-data/uploads/demo.txt"
