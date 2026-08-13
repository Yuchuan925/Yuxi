from io import BytesIO

from PIL import Image
from yuxi.storage.minio import get_minio_client


def create_png_bytes() -> bytes:
    """生成用于真实上传链路的最小 PNG 图片。"""
    content = BytesIO()
    Image.new("RGB", (1, 1), color="red").save(content, format="PNG")
    return content.getvalue()


async def test_image_upload_rejects_forged_svg_and_accepts_png(test_client, admin_headers):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    rejected_response = await test_client.post(
        "/api/user/upload-image",
        files={"file": ("avatar.svg", svg, "image/png")},
        headers=admin_headers,
    )

    assert rejected_response.status_code == 400
    assert rejected_response.json()["detail"] == "只能上传 PNG、JPEG、WebP 或 GIF 图片"

    accepted_response = await test_client.post(
        "/api/user/upload-image",
        files={"file": ("avatar.svg", create_png_bytes(), "image/svg+xml")},
        headers=admin_headers,
    )

    assert accepted_response.status_code == 200, accepted_response.text
    image_url = accepted_response.json()["image_url"]
    assert image_url.startswith("/minio/public/images/")
    assert image_url.endswith(".png")

    object_name = image_url.removeprefix("/minio/public/")
    minio_client = get_minio_client()
    try:
        assert await minio_client.astat_file("public", object_name) == len(create_png_bytes())
    finally:
        await minio_client.adelete_file("public", object_name)
