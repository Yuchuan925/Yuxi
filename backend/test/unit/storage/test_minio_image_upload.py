from io import BytesIO

import pytest
from fastapi import UploadFile
from PIL import Image

from yuxi.storage.minio import utils


def create_image_bytes(image_format: str) -> bytes:
    """生成指定格式的最小合法测试图片。"""
    content = BytesIO()
    Image.new("RGB", (1, 1), color="red").save(content, format=image_format)
    return content.getvalue()


@pytest.mark.parametrize(
    ("image_format", "expected_extension"),
    [("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp"), ("GIF", "gif")],
)
async def test_upload_image_uses_detected_format_for_object_name(monkeypatch, image_format, expected_extension):
    uploaded = {}

    async def fake_upload(bucket_name: str, object_name: str, data: bytes) -> str:
        uploaded.update(bucket_name=bucket_name, object_name=object_name, data=data)
        return f"/minio/{bucket_name}/{object_name}"

    monkeypatch.setattr(utils, "aupload_file_to_minio", fake_upload)
    image_bytes = create_image_bytes(image_format)
    upload = UploadFile(filename="avatar.svg", file=BytesIO(image_bytes), headers={"content-type": "image/svg+xml"})

    result = await utils.upload_image_to_minio(
        upload,
        object_prefix="avatar/1",
        max_size_bytes=1024 * 1024,
        too_large_message="图片过大",
    )

    assert uploaded["bucket_name"] == "public"
    assert uploaded["object_name"].startswith("avatar/1/")
    assert uploaded["object_name"].endswith(f".{expected_extension}")
    assert uploaded["data"] == image_bytes
    assert result.endswith(f".{expected_extension}")


async def test_upload_image_rejects_svg_with_forged_image_content_type(monkeypatch):
    async def fail_if_uploaded(*_args, **_kwargs):
        pytest.fail("非法 SVG 不应上传到 MinIO")

    monkeypatch.setattr(utils, "aupload_file_to_minio", fail_if_uploaded)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    upload = UploadFile(filename="avatar.svg", file=BytesIO(svg), headers={"content-type": "image/png"})

    with pytest.raises(ValueError, match="只能上传 PNG、JPEG、WebP 或 GIF 图片"):
        await utils.upload_image_to_minio(
            upload,
            object_prefix="avatar/1",
            max_size_bytes=1024 * 1024,
            too_large_message="图片过大",
        )


async def test_upload_image_rejects_truncated_image(monkeypatch):
    async def fail_if_uploaded(*_args, **_kwargs):
        pytest.fail("损坏图片不应上传到 MinIO")

    monkeypatch.setattr(utils, "aupload_file_to_minio", fail_if_uploaded)
    truncated_jpeg = create_image_bytes("JPEG")[:-2]
    upload = UploadFile(filename="avatar.jpg", file=BytesIO(truncated_jpeg), headers={"content-type": "image/jpeg"})

    with pytest.raises(ValueError, match="只能上传 PNG、JPEG、WebP 或 GIF 图片"):
        await utils.upload_image_to_minio(
            upload,
            object_prefix="avatar/1",
            max_size_bytes=1024 * 1024,
            too_large_message="图片过大",
        )


async def test_upload_image_maps_decompression_bomb_to_validation_error(monkeypatch):
    async def fail_if_uploaded(*_args, **_kwargs):
        pytest.fail("像素数超限的图片不应上传到 MinIO")

    monkeypatch.setattr(utils, "aupload_file_to_minio", fail_if_uploaded)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 0)
    upload = UploadFile(
        filename="avatar.png",
        file=BytesIO(create_image_bytes("PNG")),
        headers={"content-type": "image/png"},
    )

    with pytest.raises(ValueError, match="只能上传 PNG、JPEG、WebP 或 GIF 图片"):
        await utils.upload_image_to_minio(
            upload,
            object_prefix="avatar/1",
            max_size_bytes=1024 * 1024,
            too_large_message="图片过大",
        )


async def test_upload_image_rejects_decompression_bomb_warning(monkeypatch):
    async def fail_if_uploaded(*_args, **_kwargs):
        pytest.fail("达到像素安全阈值的图片不应上传到 MinIO")

    monkeypatch.setattr(utils, "aupload_file_to_minio", fail_if_uploaded)
    image_bytes = BytesIO()
    Image.new("RGB", (2, 1), color="red").save(image_bytes, format="PNG")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    upload = UploadFile(
        filename="avatar.png",
        file=BytesIO(image_bytes.getvalue()),
        headers={"content-type": "image/png"},
    )

    with pytest.raises(ValueError, match="只能上传 PNG、JPEG、WebP 或 GIF 图片"):
        await utils.upload_image_to_minio(
            upload,
            object_prefix="avatar/1",
            max_size_bytes=1024 * 1024,
            too_large_message="图片过大",
        )
