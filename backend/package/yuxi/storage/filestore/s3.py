from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import aioboto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .keys import normalize_key, normalize_prefix
from .models import FileStoreError, ObjectStat, StoredObject


class S3FileStore:
    """使用 aioboto3 提供 S3 兼容异步文件存储。"""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        region_name: str = "us-east-1",
        session: Any | None = None,
    ):
        """初始化 S3 存储连接配置。"""
        if not bucket:
            raise FileStoreError("S3 bucket 不能为空")
        self.bucket = bucket
        self._endpoint_url = endpoint_url
        self._region_name = region_name
        self._session = session or aioboto3.Session()
        self._bucket_ready = False
        self._bucket_lock = asyncio.Lock()
        self._client_options = {
            "endpoint_url": endpoint_url,
            "aws_access_key_id": access_key_id,
            "aws_secret_access_key": secret_access_key,
            "region_name": region_name,
            "config": Config(s3={"addressing_style": "path"}),
        }

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> ObjectStat:
        """向 S3 写入完整字节对象。"""
        normalized = normalize_key(key)
        resolved_content_type = content_type or mimetypes.guess_type(normalized)[0] or "application/octet-stream"
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": normalized,
            "Body": data,
            "ContentType": resolved_content_type,
        }
        try:
            await self._ensure_bucket()
            async with self._client() as client:
                await client.put_object(**kwargs)
            return await self.stat(normalized)
        except (BotoCoreError, ClientError) as exc:
            raise self._error("写入", normalized, exc) from exc

    async def read(self, key: str) -> StoredObject:
        """从 S3 读取完整对象。"""
        normalized = normalize_key(key)
        try:
            await self._ensure_bucket()
            async with self._client() as client:
                response = await client.get_object(Bucket=self.bucket, Key=normalized)
                body = response["Body"]
                try:
                    data = await body.read()
                finally:
                    body.close()
        except (BotoCoreError, ClientError) as exc:
            raise self._error("读取", normalized, exc) from exc
        return StoredObject(
            key=normalized,
            data=data,
            size=response["ContentLength"],
            modified=response["LastModified"],
            content_type=response.get("ContentType"),
        )

    async def stream(self, key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        """从 S3 按块流式读取对象。"""
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")
        normalized = normalize_key(key)
        try:
            await self._ensure_bucket()
            async with self._client() as client:
                response = await client.get_object(Bucket=self.bucket, Key=normalized)
                body = response["Body"]
                try:
                    while chunk := await body.read(chunk_size):
                        yield chunk
                finally:
                    body.close()
        except (BotoCoreError, ClientError) as exc:
            raise self._error("读取", normalized, exc) from exc

    async def stat(self, key: str) -> ObjectStat:
        """读取 S3 对象元数据。"""
        normalized = normalize_key(key)
        try:
            await self._ensure_bucket()
            async with self._client() as client:
                response = await client.head_object(Bucket=self.bucket, Key=normalized)
        except (BotoCoreError, ClientError) as exc:
            raise self._error("查询", normalized, exc) from exc
        return ObjectStat(
            key=normalized,
            size=response["ContentLength"],
            modified=response["LastModified"],
            content_type=response.get("ContentType"),
        )

    async def list(self, prefix: str = "") -> list[ObjectStat]:
        """分页列出 S3 前缀下的对象及元数据。"""
        normalized_prefix = normalize_prefix(prefix)
        objects: list[ObjectStat] = []
        try:
            await self._ensure_bucket()
            async with self._client() as client:
                paginator = client.get_paginator("list_objects_v2")
                async for page in paginator.paginate(Bucket=self.bucket, Prefix=normalized_prefix):
                    for item in page.get("Contents", []):
                        metadata = await client.head_object(Bucket=self.bucket, Key=item["Key"])
                        objects.append(
                            ObjectStat(
                                key=item["Key"],
                                size=item["Size"],
                                modified=item["LastModified"],
                                content_type=metadata.get("ContentType"),
                            )
                        )
        except (BotoCoreError, ClientError) as exc:
            raise self._error("列出", normalized_prefix, exc) from exc
        return objects

    async def delete(self, key: str) -> None:
        """幂等删除单个 S3 对象。"""
        normalized = normalize_key(key)
        try:
            await self._ensure_bucket()
            async with self._client() as client:
                await client.delete_object(Bucket=self.bucket, Key=normalized)
        except (BotoCoreError, ClientError) as exc:
            raise self._error("删除", normalized, exc) from exc

    async def delete_prefix(self, prefix: str) -> int:
        """分页删除指定 S3 前缀下的对象。"""
        normalized_prefix = normalize_prefix(prefix)
        if not normalized_prefix:
            raise FileStoreError("批量删除前缀不能为空")
        deleted = 0
        try:
            await self._ensure_bucket()
            async with self._client() as client:
                paginator = client.get_paginator("list_objects_v2")
                async for page in paginator.paginate(Bucket=self.bucket, Prefix=normalized_prefix):
                    for item in page.get("Contents", []):
                        await client.delete_object(Bucket=self.bucket, Key=item["Key"])
                        deleted += 1
        except (BotoCoreError, ClientError) as exc:
            raise self._error("批量删除", normalized_prefix, exc) from exc
        return deleted

    async def _ensure_bucket(self) -> None:
        """首次访问时并发安全地确认或创建存储桶。"""
        if self._bucket_ready:
            return

        async with self._bucket_lock:
            if self._bucket_ready:
                return
            try:
                async with self._client() as client:
                    try:
                        await client.head_bucket(Bucket=self.bucket)
                    except ClientError as exc:
                        code = exc.response.get("Error", {}).get("Code")
                        if code not in {"404", "NoSuchBucket", "NotFound"}:
                            raise
                        create_kwargs: dict[str, Any] = {"Bucket": self.bucket}
                        if self._is_aws_standard_endpoint() and self._region_name != "us-east-1":
                            create_kwargs["CreateBucketConfiguration"] = {
                                "LocationConstraint": self._region_name
                            }
                        await client.create_bucket(**create_kwargs)
            except (BotoCoreError, ClientError) as exc:
                raise FileStoreError(f"S3 bucket 初始化失败: {self.bucket}") from exc
            self._bucket_ready = True

    def _client(self):
        """创建带 path-style 配置的短生命周期 S3 客户端。"""
        return self._session.client("s3", **self._client_options)

    def _is_aws_standard_endpoint(self) -> bool:
        """判断当前连接是否使用 AWS 标准 S3 endpoint。"""
        if not self._endpoint_url:
            return True
        hostname = (urlparse(self._endpoint_url).hostname or "").lower()
        return hostname == "s3.amazonaws.com" or hostname.endswith((".amazonaws.com", ".amazonaws.com.cn"))

    def _error(self, action: str, key: str, exc: Exception) -> FileStoreError:
        """将底层 S3 异常转换为统一文件存储异常。"""
        if isinstance(exc, ClientError):
            code = exc.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return FileStoreError(f"对象不存在: {key}")
        return FileStoreError(f"S3 对象{action}失败: {key}")
