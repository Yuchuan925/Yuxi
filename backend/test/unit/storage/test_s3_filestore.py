import asyncio
from datetime import UTC, datetime
from io import BytesIO

import pytest
from botocore.exceptions import ClientError

from yuxi.storage.filestore import FileStoreError, S3FileStore


NOW = datetime(2026, 8, 12, tzinfo=UTC)


class FakeBody:
    def __init__(self, data: bytes):
        self._data = BytesIO(data)
        self.closed = False

    async def read(self, size: int = -1) -> bytes:
        return self._data.read(size)

    def close(self) -> None:
        self.closed = True


class FakePaginator:
    def __init__(self, client):
        self.client = client

    async def paginate(self, *, Bucket: str, Prefix: str):
        contents = [
            {"Key": key, "Size": len(value[0]), "LastModified": NOW}
            for key, value in sorted(self.client.objects.items())
            if key.startswith(Prefix)
        ]
        yield {"Contents": contents[:1]}
        yield {"Contents": contents[1:]}


class FakeS3Client:
    def __init__(self):
        self.bucket_exists = False
        self.head_bucket_calls = 0
        self.create_bucket_calls = 0
        self.create_bucket_kwargs: list[dict] = []
        self.objects: dict[str, tuple[bytes, str | None]] = {}
        self.last_body: FakeBody | None = None

    async def head_bucket(self, *, Bucket: str):
        self.head_bucket_calls += 1
        if not self.bucket_exists:
            raise _client_error("404", "HeadBucket")

    async def create_bucket(self, **kwargs):
        self.create_bucket_calls += 1
        self.create_bucket_kwargs.append(kwargs)
        self.bucket_exists = True

    async def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str | None = None):
        self.objects[Key] = (Body, ContentType)

    async def get_object(self, *, Bucket: str, Key: str):
        if Key not in self.objects:
            raise _client_error("NoSuchKey", "GetObject")
        data, content_type = self.objects[Key]
        self.last_body = FakeBody(data)
        return {"Body": self.last_body, "ContentLength": len(data), "LastModified": NOW, "ContentType": content_type}

    async def head_object(self, *, Bucket: str, Key: str):
        if Key not in self.objects:
            raise _client_error("404", "HeadObject")
        data, content_type = self.objects[Key]
        return {"ContentLength": len(data), "LastModified": NOW, "ContentType": content_type}

    def get_paginator(self, operation: str):
        assert operation == "list_objects_v2"
        return FakePaginator(self)

    async def delete_object(self, *, Bucket: str, Key: str):
        self.objects.pop(Key, None)

    async def delete_objects(self, *, Bucket: str, Delete: dict):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)


class FakeClientContext:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeSession:
    def __init__(self, client):
        self.s3 = client
        self.options = None

    def client(self, service: str, **options):
        assert service == "s3"
        self.options = options
        return FakeClientContext(self.s3)


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


@pytest.mark.asyncio
async def test_s3_filestore_supports_core_operations_and_path_style():
    client = FakeS3Client()
    session = FakeSession(client)
    store = S3FileStore(bucket="files", endpoint_url="http://minio:9000", session=session)

    await store.put("threads/t1/uploads/a.txt", b"abc", content_type="text/plain")
    await store.put("threads/t1/uploads/b.bin", b"defg")
    loaded = await store.read("threads/t1/uploads/a.txt")
    streamed = b"".join([chunk async for chunk in store.stream("threads/t1/uploads/a.txt", chunk_size=2)])
    listed = await store.list("threads/t1/uploads/")
    deleted = await store.delete_prefix("threads/t1/uploads/")

    assert loaded.data == b"abc"
    assert streamed == b"abc"
    assert [item.key for item in listed] == ["threads/t1/uploads/a.txt", "threads/t1/uploads/b.bin"]
    assert listed[0].content_type == "text/plain"
    assert listed[1].content_type == "application/octet-stream"
    assert deleted == 2
    assert client.objects == {}
    assert session.options["config"].s3["addressing_style"] == "path"
    assert client.last_body is not None and client.last_body.closed


@pytest.mark.asyncio
async def test_s3_filestore_concurrent_first_put_creates_bucket_once():
    client = FakeS3Client()
    store = S3FileStore(bucket="empty-files", session=FakeSession(client))

    await asyncio.gather(
        store.put("threads/t1/uploads/a.txt", b"a"),
        store.put("threads/t1/uploads/b.txt", b"b"),
    )

    assert client.head_bucket_calls == 1
    assert client.create_bucket_calls == 1
    assert sorted(client.objects) == ["threads/t1/uploads/a.txt", "threads/t1/uploads/b.txt"]


@pytest.mark.asyncio
async def test_s3_filestore_first_list_creates_empty_bucket():
    client = FakeS3Client()
    store = S3FileStore(bucket="empty-files", session=FakeSession(client))

    objects = await store.list("threads/t1/")

    assert objects == []
    assert client.create_bucket_calls == 1


@pytest.mark.asyncio
async def test_s3_filestore_missing_object_raises_filestore_error_after_bucket_creation():
    client = FakeS3Client()
    store = S3FileStore(bucket="empty-files", session=FakeSession(client))

    with pytest.raises(FileStoreError, match="对象不存在"):
        await store.stat("missing.txt")
    with pytest.raises(FileStoreError, match="对象不存在"):
        await store.read("missing.txt")

    assert client.create_bucket_calls == 1


@pytest.mark.asyncio
async def test_s3_filestore_aws_region_create_bucket_includes_location_constraint():
    client = FakeS3Client()
    store = S3FileStore(bucket="regional-files", region_name="ap-southeast-1", session=FakeSession(client))

    await store.list("")

    assert client.create_bucket_kwargs == [
        {
            "Bucket": "regional-files",
            "CreateBucketConfiguration": {"LocationConstraint": "ap-southeast-1"},
        }
    ]


@pytest.mark.asyncio
async def test_s3_filestore_custom_endpoint_omits_location_constraint():
    client = FakeS3Client()
    store = S3FileStore(
        bucket="minio-files",
        endpoint_url="http://minio:9000",
        region_name="ap-southeast-1",
        session=FakeSession(client),
    )

    await store.list("")

    assert client.create_bucket_kwargs == [{"Bucket": "minio-files"}]


@pytest.mark.asyncio
async def test_s3_filestore_aws_china_endpoint_includes_location_constraint():
    client = FakeS3Client()
    store = S3FileStore(
        bucket="china-files",
        endpoint_url="https://s3.cn-north-1.amazonaws.com.cn",
        region_name="cn-north-1",
        session=FakeSession(client),
    )

    await store.list("")

    assert client.create_bucket_kwargs == [
        {
            "Bucket": "china-files",
            "CreateBucketConfiguration": {"LocationConstraint": "cn-north-1"},
        }
    ]
