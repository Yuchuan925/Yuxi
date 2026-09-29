"""解析产生的图片上传和 Markdown 链接处理。"""

import asyncio
import base64
import os
import re
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

from yuxi.infrastructure.minio import get_minio_client
from yuxi.infrastructure.observability.logging import logger


def upload_image_to_minio(
    image_data: bytes, filename: str, bucket_name: str, object_prefix: str, url_builder: Callable[[str], str]
) -> str:
    """上传图片到 MinIO，返回经后端鉴权代理访问的 URL。"""

    minio_client = get_minio_client()
    minio_client.ensure_bucket_exists(bucket_name)

    normalized_prefix = object_prefix.strip("/") or "unknown/kb-images"
    timestamp = int(time.time() * 1000000)
    object_name = f"{normalized_prefix}/{timestamp}_{Path(filename).name}"

    minio_client.upload_file(
        bucket_name=bucket_name,
        object_name=object_name,
        data=image_data,
    )
    return url_builder(object_name)


def parse_data_uri(data_uri: str) -> tuple[bytes, str]:
    """解析 data URI，返回 (image_data, mime_type)。"""
    header, base64_data = data_uri.split(",", 1)
    mime_type = header.split(":")[1].split(";")[0]
    image_data = base64.b64decode(base64_data)
    return image_data, mime_type


async def process_images(
    zip_file: zipfile.ZipFile,
    images_dir: str,
    image_bucket: str,
    image_prefix: str,
    url_builder: Callable[[str], str],
) -> list[dict]:
    """处理图片：上传到MinIO并返回信息"""
    supported_extensions = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

    images = []
    image_names = [n for n in zip_file.namelist() if n.startswith(images_dir + "/")]
    normalized_prefix = image_prefix.strip("/") or "unknown/kb-images"

    minio_client = get_minio_client()
    await asyncio.to_thread(minio_client.ensure_bucket_exists, image_bucket)

    for img_name in image_names:
        suffix = Path(img_name).suffix.lower()
        if suffix not in supported_extensions:
            continue

        try:
            with zip_file.open(img_name) as f:
                data = f.read()

            timestamp = int(time.time() * 1000000)
            object_name = f"{normalized_prefix}/{timestamp}_{Path(img_name).name}"

            await minio_client.aupload_file(
                bucket_name=image_bucket,
                object_name=object_name,
                data=data,
            )

            img_info = {
                "name": Path(img_name).name,
                "url": url_builder(object_name),
                "path": f"images/{Path(img_name).name}",
            }
            images.append(img_info)

            logger.debug(f"图片上传成功: {Path(img_name).name} -> {img_info['url']}")

        except Exception as e:
            logger.error(f"上传图片失败 {Path(img_name).name}: {e}")
            continue

    return images


def replace_image_links(markdown_content: str, images: list[dict]) -> str:
    """替换markdown中的图片链接为MinIO URL"""
    if not images:
        return markdown_content

    image_map = {}
    for img in images:
        path = img["path"]
        url = img["url"]
        image_map[path] = url
        image_map[f"/{path}"] = url
        image_map[img["name"]] = url

    def replace_link(match):
        alt_text = match.group(1) or ""
        img_path = match.group(2)

        for pattern, url in image_map.items():
            if img_path.endswith(pattern) or img_path == pattern:
                return f"![{alt_text}]({url})"

        filename = os.path.basename(img_path)
        if filename in image_map:
            return f"![{alt_text}]({image_map[filename]})"

        return match.group(0)

    pattern = r"!\[([^\]]*)\]\(([^)]+)\)"
    return re.sub(pattern, replace_link, markdown_content)
