from collections.abc import Awaitable, Callable
import asyncio
import zipfile
from pathlib import Path


DEFAULT_IMAGE_BUCKET = "kb-images"
DEFAULT_IMAGE_PREFIX = "unknown/kb-images"


def _normalize_object_prefix(prefix: str | None) -> str:
    normalized = (prefix or DEFAULT_IMAGE_PREFIX).strip("/")
    return normalized or DEFAULT_IMAGE_PREFIX


async def process_zip_file(
    zip_path: str,
    image_bucket: str = DEFAULT_IMAGE_BUCKET,
    image_prefix: str = DEFAULT_IMAGE_PREFIX,
    process_images: Callable[..., Awaitable[list[dict]]] | None = None,
    replace_links: Callable[[str, list[dict]], str] | None = None,
) -> str:
    """
    处理ZIP文件，提取markdown内容和图片

    Args:
        zip_path: ZIP文件路径
        image_bucket: 图片上传的目标 bucket
        image_prefix: 图片上传对象前缀

    Returns:
        str: 处理后的 Markdown 文本。
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if name.startswith("/") or name.startswith("\\"):
                raise ValueError(f"ZIP 包含不安全路径: {name}")
            if ".." in Path(name).parts:
                raise ValueError(f"ZIP 路径包含上级引用: {name}")

        md_files = [n for n in zf.namelist() if n.lower().endswith(".md")]
        if not md_files:
            raise ValueError("压缩包中未找到 .md 文件")

        md_file = next((n for n in md_files if Path(n).name == "full.md"), md_files[0])

        with zf.open(md_file) as f:
            markdown_content = f.read().decode("utf-8")

        images_info = []
        images_dir = find_images_directory(zf, md_file)
        normalized_prefix = _normalize_object_prefix(image_prefix)

        if images_dir:
            if process_images is None or replace_links is None:
                raise ValueError("ZIP 图片处理能力未装配")
            images_info = await process_images(
                zf,
                images_dir,
                image_bucket=image_bucket,
                image_prefix=normalized_prefix,
            )
            markdown_content = replace_links(markdown_content, images_info)

    return markdown_content


def process_zip_file_sync(
    zip_path: str,
    image_bucket: str = DEFAULT_IMAGE_BUCKET,
    image_prefix: str = DEFAULT_IMAGE_PREFIX,
    process_images: Callable[..., Awaitable[list[dict]]] | None = None,
    replace_links: Callable[[str, list[dict]], str] | None = None,
) -> str:
    """同步调用 ZIP 处理，供同步解析器使用。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            process_zip_file(
                zip_path,
                image_bucket=image_bucket,
                image_prefix=image_prefix,
                process_images=process_images,
                replace_links=replace_links,
            )
        )

    result: str | None = None
    error: Exception | None = None

    def runner() -> None:
        nonlocal result, error
        try:
            result = asyncio.run(
                process_zip_file(
                    zip_path,
                    image_bucket=image_bucket,
                    image_prefix=image_prefix,
                    process_images=process_images,
                    replace_links=replace_links,
                )
            )
        except Exception as exc:  # pragma: no cover - pass through outer raise
            error = exc

    import threading

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()

    if error is not None:
        raise error

    if result is None:
        raise RuntimeError("ZIP 处理失败: 未返回结果")

    return result


def find_images_directory(zip_file: zipfile.ZipFile, md_file_path: str) -> str | None:
    """查找images目录"""
    md_parent = Path(md_file_path).parent

    candidates = []
    if str(md_parent) != ".":
        candidates.extend([str(md_parent / "images"), str(md_parent.parent / "images")])
    candidates.append("images")

    for cand in candidates:
        cand_clean = cand.rstrip("/")
        if any(n.startswith(cand_clean + "/") for n in zip_file.namelist()):
            return cand_clean

    return None
