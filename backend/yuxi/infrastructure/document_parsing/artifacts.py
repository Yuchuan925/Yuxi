"""解析产物的相对资源路径与 Markdown 引用。"""

import posixpath
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt


def save_resource(output_dir: Path, relative_path: str, content: bytes) -> str:
    """在解析目录内保存资源，拒绝上级路径与符号链接。"""
    relative = resource_path(relative_path)
    target = output_dir / relative
    for part in (target, *target.parents):
        if part.is_symlink():
            raise ValueError(f"资源路径包含符号链接: {relative_path}")
        if part == output_dir:
            break
    # 输出目录由 parser 独占创建，不允许引擎覆盖已有资源。
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        stream.write(content)
    return relative


def resource_path(value: str) -> str:
    """校验解析产物中的相对 POSIX 资源路径。"""
    path = PurePosixPath(value)
    if not value or path.is_absolute() or "\\" in value or ":" in value or ".." in path.parts:
        raise ValueError(f"不安全的资源路径: {value}")
    return path.as_posix()


def local_image_links(markdown: str) -> list[str]:
    """读取 Markdown 图片引用，保留完整相对路径身份。"""
    links = []
    for token in MarkdownIt().parse(markdown):
        for child in token.children or []:
            if child.type == "image":
                src = child.attrGet("src") or ""
                if src and not urlsplit(src).scheme and not src.startswith("//"):
                    links.append(src)
    return list(dict.fromkeys(links))


def replace_image_links(markdown: str, mapping: dict[str, str]) -> str:
    """按完整链接替换内联图片及引用定义，保留标题。"""
    replacements = {
        source: target if target.startswith("/") or urlsplit(target).scheme else quote(target, safe="/")
        for source, target in mapping.items()
    }
    for source, target in list(replacements.items()):
        for variant in (source, unquote(source)):
            replacements.setdefault(variant, target)
            replacements.setdefault(re.sub(r"([()])", r"\\\1", variant), target)
    if not replacements:
        return markdown
    choices = "|".join(re.escape(value) for value in sorted(replacements, key=len, reverse=True))
    inline = r"(\]\(<?)(" + choices + r")(?=>?(?:\s|\)))"
    definitions = r"(?m)^( {0,3}\[[^\]]+\]:\s*<?)(" + choices + r")(?=>?(?:\s|$))"
    for pattern in (inline, definitions):
        markdown = re.sub(pattern, lambda match: match[1] + replacements[match[2]], markdown)
    return markdown


def extract_markdown_archive(zip_path: str, output_dir: Path) -> str:
    """提取 Markdown 引用的资源，拒绝归档越界、重名和符号链接。"""
    with zipfile.ZipFile(zip_path) as archive:
        names = set()
        for entry in archive.infolist():
            name = entry.filename
            resource_path(name)
            if stat.S_ISLNK(entry.external_attr >> 16) or name in names:
                raise ValueError(f"ZIP 包含符号链接或重复路径: {name}")
            names.add(name)
        md_files = sorted(name for name in names if name.lower().endswith(".md"))
        if not md_files:
            raise ValueError("压缩包中未找到 .md 文件")
        md_file = next((name for name in md_files if PurePosixPath(name).name == "full.md"), md_files[0])
        markdown = archive.read(md_file).decode("utf-8")
        mapping = {}
        copied = set()
        for link in local_image_links(markdown):
            source = posixpath.normpath(posixpath.join(posixpath.dirname(md_file), unquote(link)))
            resource_path(source)
            if source not in names:
                raise ValueError(f"ZIP 缺少 Markdown 资源: {link}")
            target = f"images/{source}"
            if target not in copied:
                save_resource(output_dir, target, archive.read(source))
                copied.add(target)
            mapping[link] = target
        return replace_image_links(markdown, mapping)
