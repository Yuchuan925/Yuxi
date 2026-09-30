"""图片格式转换、压缩与缩略图生成。"""

import base64
import io

from PIL import ExifTags, Image

from yuxi.infrastructure.observability.logging import logger

SUPPORTED_FORMATS = {"JPEG", "PNG", "WebP", "GIF", "BMP"}
MAX_FILE_SIZE = 5 * 1024 * 1024


def process_uploaded_image(image_data: bytes, filename: str = "") -> dict:
    """处理上传图片，返回图片内容、缩略图与元数据或错误信息。"""
    try:
        img_format, _ = _validate_image_format(image_data)
        if img_format not in SUPPORTED_FORMATS:
            raise ValueError(f"不支持的图片格式: {img_format}")

        with Image.open(io.BytesIO(image_data)) as img:
            img = _fix_image_orientation(img)
            thumbnail_data = _generate_thumbnail(img)
            processed_data, final_format = _compress_image(img, img_format)

            base64_data = base64.b64encode(processed_data).decode("utf-8")
            base64_thumbnail = base64.b64encode(thumbnail_data).decode("utf-8")
            width, height = img.size
            mime_type = f"image/{final_format.lower()}"

            return {
                "success": True,
                "image_content": base64_data,
                "thumbnail_content": base64_thumbnail,
                "width": width,
                "height": height,
                "format": final_format,
                "mime_type": mime_type,
                "size_bytes": len(processed_data),
                "original_filename": filename,
            }

    except Exception as e:
        logger.error(f"图片处理失败: {str(e)}")
        return {"success": False, "error": str(e)}


def _validate_image_format(image_data: bytes) -> tuple[str, str]:
    """验证图片格式并返回格式信息。"""
    try:
        with Image.open(io.BytesIO(image_data)) as img:
            return img.format, img.mode
    except Exception as e:
        raise ValueError(f"无效的图片格式: {str(e)}")


def _fix_image_orientation(img: Image.Image) -> Image.Image:
    """根据 EXIF 信息修正图片方向。"""
    try:
        if hasattr(img, "_getexif"):
            exif = img._getexif()
            if exif is not None:
                for tag, value in exif.items():
                    if tag in ExifTags.TAGS and ExifTags.TAGS[tag] == "Orientation":
                        if value == 3:
                            img = img.rotate(180, expand=True)
                        elif value == 6:
                            img = img.rotate(270, expand=True)
                        elif value == 8:
                            img = img.rotate(90, expand=True)
                        break
    except Exception as e:
        logger.warning(f"修正图片方向失败，使用原始方向: {str(e)}")

    return img


def _generate_thumbnail(img: Image.Image) -> bytes:
    """生成保持宽高比的 JPEG 缩略图。"""
    try:
        thumbnail = _convert_to_rgb_for_export(img)
        thumbnail.thumbnail((200, 200), Image.Resampling.LANCZOS)

        with io.BytesIO() as output:
            thumbnail.save(output, format="JPEG", quality=85, optimize=True)
            return output.getvalue()

    except Exception as e:
        logger.error(f"生成缩略图失败: {str(e)}")
        with io.BytesIO() as output:
            empty_img = Image.new("RGB", (1, 1), color="white")
            empty_img.save(output, format="JPEG", quality=85)
            return output.getvalue()


def _compress_image(img: Image.Image, original_format: str) -> tuple[bytes, str]:
    """压缩图片，超出大小限制时降低质量或缩小尺寸。"""
    processed_img = _convert_to_rgb_for_export(img)
    target_format = "JPEG" if original_format != "PNG" else "PNG"
    quality = 85

    with io.BytesIO() as output:
        processed_img.save(output, format=target_format, quality=quality, optimize=True)
        compressed_data = output.getvalue()

        if len(compressed_data) <= MAX_FILE_SIZE:
            return compressed_data, target_format

        while len(compressed_data) > MAX_FILE_SIZE and quality > 10:
            quality -= 10
            output.seek(0)
            output.truncate(0)
            processed_img.save(output, format=target_format, quality=quality, optimize=True)
            compressed_data = output.getvalue()

        if len(compressed_data) > MAX_FILE_SIZE:
            scale_factor = 0.9
            while len(compressed_data) > MAX_FILE_SIZE and scale_factor > 0.3:
                new_width = int(processed_img.width * scale_factor)
                new_height = int(processed_img.height * scale_factor)
                resized_img = processed_img.resize((new_width, new_height), Image.Resampling.LANCZOS)

                output.seek(0)
                output.truncate(0)
                resized_img.save(output, format=target_format, quality=85, optimize=True)
                compressed_data = output.getvalue()

                scale_factor -= 0.1

        return compressed_data, target_format


def _convert_to_rgb_for_export(img: Image.Image) -> Image.Image:
    """转换为 RGB，同时把透明像素按白底合成，避免隐藏颜色变成可见像素。"""
    if img.mode == "RGB":
        return img.copy()

    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if not has_alpha:
        return img.convert("RGB")

    rgba_img = img.convert("RGBA")
    background = Image.new("RGBA", rgba_img.size, (255, 255, 255, 255))
    background.alpha_composite(rgba_img)
    return background.convert("RGB")
