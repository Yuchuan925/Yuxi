from __future__ import annotations

import asyncio
import re
import shutil
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yuxi.infrastructure.document_parsing.engines as engines
import yuxi.infrastructure.document_parsing.parser as parser_unified
from docx import Document
from PIL import Image

from yuxi.infrastructure.document_parsing import DocumentParserException
from yuxi.infrastructure.document_parsing.engines.mineru import MinerUParser
from yuxi.infrastructure.document_parsing.engines.mineru_official import MinerUOfficialParser
from yuxi.infrastructure.document_parsing.engines.rapid_ocr import RapidOCRParser
from yuxi.modules.documents.service import parse
from yuxi.infrastructure.document_parsing import ParseOptions
import tempfile

PARSER_FIXTURES = Path(__file__).parents[2] / "data"


def test_mineru_parser_normalizes_trailing_slash():
    parser = MinerUParser(server_url="http://mineru-api:30001/")

    assert parser.server_url == "http://mineru-api:30001"
    assert parser.parse_endpoint == "http://mineru-api:30001/file_parse"


def test_mineru_official_health_check_does_not_create_task(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "yuxi.infrastructure.document_parsing.engines.mineru_official.requests.post",
        lambda *args, **kwargs: pytest.fail("健康检查不应创建解析任务"),
    )

    health = MinerUOfficialParser(api_key="test-key").check_health()

    assert health["status"] == "configured"


def test_mineru_official_parsing_uses_shared_zip_processor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    file_path = tmp_path / "mineru.pdf"
    file_path.write_bytes(b"pdf")
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"zip")
    parser = MinerUOfficialParser(api_key="test-key")

    monkeypatch.setattr(parser, "_upload_file", lambda *args, **kwargs: "batch-id")
    monkeypatch.setattr(
        parser,
        "_poll_batch_result",
        lambda *args, **kwargs: {"state": "done", "full_zip_url": "https://example.test/result.zip"},
    )
    monkeypatch.setattr(parser, "_download_zip", lambda *args, **kwargs: str(zip_path))
    processed_paths: list[str] = []

    def _process_zip_file(zip_file_path: str, output_dir: Path, **kwargs) -> str:
        del kwargs
        processed_paths.append(zip_file_path)
        return "parsed markdown"

    monkeypatch.setattr(
        "yuxi.infrastructure.document_parsing.engines.mineru_official.extract_markdown_archive",
        _process_zip_file,
    )

    assert parser.process_file(str(file_path), tmp_path) == "parsed markdown"
    assert processed_paths == [str(zip_path)]
    assert not zip_path.exists()


def test_mineru_official_does_not_fallback_when_shared_zip_processing_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    file_path = tmp_path / "mineru.pdf"
    file_path.write_bytes(b"pdf")
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"zip")
    parser = MinerUOfficialParser(api_key="test-key")

    monkeypatch.setattr(parser, "_upload_file", lambda *args, **kwargs: "batch-id")
    monkeypatch.setattr(
        parser,
        "_poll_batch_result",
        lambda *args, **kwargs: {"state": "done", "full_zip_url": "https://example.test/result.zip"},
    )
    monkeypatch.setattr(parser, "_download_zip", lambda *args, **kwargs: str(zip_path))

    def _raise_zip_processing_error(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("malformed result archive")

    monkeypatch.setattr(
        "yuxi.infrastructure.document_parsing.engines.mineru_official.extract_markdown_archive",
        _raise_zip_processing_error,
    )

    with pytest.raises(DocumentParserException, match="malformed result archive"):
        parser.process_file(str(file_path), tmp_path)

    assert not zip_path.exists()


def test_rapid_ocr_health_check_does_not_load_model(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "yuxi.infrastructure.document_parsing.engines.rapid_ocr.RapidOCR",
        lambda *args, **kwargs: pytest.fail("健康检查不应加载 OCR 模型"),
    )

    health = RapidOCRParser().check_health()

    assert health["status"] == "healthy"


def _build_pdf(file_path: Path, text: str | list[str]) -> None:
    """用标准库构造带文本的最小 PDF，避免测试引入额外 PDF 依赖。"""
    pages = [text] if isinstance(text, str) else text
    kids = " ".join(f"{4 + index * 2} 0 R" for index in range(len(pages)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for index, page_text in enumerate(pages):
        escaped = page_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
        objects.extend(
            [
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + index * 2} 0 R >>"
                ).encode(),
                b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            ]
        )
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number, object_body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_number} 0 obj\n".encode())
        pdf.extend(object_body)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode())
    pdf.extend(f"startxref\n{xref_offset}\n%%EOF\n".encode())
    file_path.write_bytes(pdf)


def _build_docx(file_path: Path, text: str) -> None:
    document = Document()
    document.add_paragraph(text)
    document.save(str(file_path))


def test_pdfreader_preserves_page_order_blank_pages_and_trimming(tmp_path: Path):
    """文本提取保留空页分隔并去除逐页首尾空白。"""
    from yuxi.infrastructure.document_parsing.parser import _parse_ocr

    file_path = tmp_path / "pages.pdf"
    _build_pdf(file_path, ["  First page  ", "", "Last page"])
    assert _parse_ocr(file_path, tmp_path, ParseOptions()) == "First page\n\n\n\nLast page"


def test_pdfreader_rejects_corrupt_pdf(tmp_path: Path):
    """损坏 PDF 显式失败，不返回伪成功空文本。"""
    from pypdf.errors import PdfReadError
    from yuxi.infrastructure.document_parsing.parser import _parse_ocr

    file_path = tmp_path / "broken.pdf"
    file_path.write_bytes(b"%PDF-1.4\ninvalid")
    with pytest.raises(PdfReadError):
        _parse_ocr(file_path, tmp_path, ParseOptions())


def _build_png(file_path: Path) -> None:
    image = Image.new("RGB", (120, 80), "white")
    image.save(str(file_path))


@pytest.mark.asyncio
async def test_parse_document_pdf_returns_markdown_text(tmp_path: Path):
    file_path = tmp_path / "parser_test.pdf"
    _build_pdf(file_path, "Parser PDF content")

    markdown = await _parse_markdown(str(file_path), params={"ocr_engine": "disable"})

    assert "Parser" in markdown
    assert "content" in markdown


@pytest.mark.parametrize(
    ("filename", "expected_fragments"),
    [
        ("测试文档.docx", ("20XX个人述职报告", "测试表格")),
        ("测试演示.pptx", ("BUSINESS REPORT TEMPLATE", "工作内容回顾")),
        ("测试表格.xlsx", ("个人所得税计算",)),
    ],
)
def test_slim_office_backends_convert_real_fixtures(
    filename: str,
    expected_fragments: tuple[str, ...],
) -> None:
    if filename.endswith(".xls") and shutil.which("libreoffice") is None:
        pytest.skip("旧版 Excel fixture 需要 LibreOffice 转换器")

    document = parser_unified._convert_office_document(PARSER_FIXTURES / filename)

    markdown = document.export_to_markdown()

    assert markdown.strip()
    assert all(fragment in markdown for fragment in expected_fragments)


def test_xls_parsed_with_pandas_without_libreoffice() -> None:
    """旧版 .xls 不走 Docling，直接用 pandas + xlrd 解析为 Markdown。"""
    markdown = parser_unified._convert_xls_to_markdown(PARSER_FIXTURES / "测试旧表格.xls")

    assert markdown.strip()
    assert "Docling Slim" in markdown
    assert "53" in markdown


async def test_parse_resolved_document_routes_xls_to_pandas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """parse_resolved_document 对 .xls 使用 pandas 路径而非 Docling。"""
    docling_calls: list[Path] = []

    def _fake_docling(file_path: Path, params: dict | None = None) -> str:
        docling_calls.append(file_path)
        return "docling"

    monkeypatch.setattr(parser_unified, "_convert_with_docling", _fake_docling)

    markdown = await _parse_markdown(str(PARSER_FIXTURES / "测试旧表格.xls"))

    assert "Docling Slim" in markdown
    assert not docling_calls


async def test_xls_preserves_single_row_text_and_blank_cells() -> None:
    """真实多 sheet 文件保留单行内容、文本编号与空白，跳过空表。"""
    markdown = await _parse_markdown(str(PARSER_FIXTURES / "xls-cell-preservation.xls"))

    assert "## 单行" in markdown
    assert "## 文本与空白" in markdown
    assert "## 空表" not in markdown
    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in markdown.splitlines() if line.startswith("|")]
    assert ["唯一内容", "53"] in rows
    assert ["编号", "标记", "备注"] in rows
    assert ["00123", "NA", ""] in rows
    assert ["00456", "NULL", "保留文本"] in rows


def test_slim_office_backend_unloads_after_conversion_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unloaded = False

    class FailingBackend:
        def convert(self):
            raise RuntimeError("conversion failed")

        def unload(self):
            nonlocal unloaded
            unloaded = True

    input_document = SimpleNamespace(valid=True, _backend=FailingBackend())
    monkeypatch.setattr(parser_unified, "InputDocument", lambda *_args: input_document)

    with pytest.raises(RuntimeError, match="conversion failed"):
        parser_unified._convert_office_document(tmp_path / "failure.docx")

    assert unloaded


@pytest.mark.asyncio
async def test_pdf_never_enters_office_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file_path = tmp_path / "parser_test.pdf"
    _build_pdf(file_path, "Existing PDF path")
    monkeypatch.setattr(
        parser_unified,
        "_convert_office_document",
        lambda *_args, **_kwargs: pytest.fail("PDF 不得进入 Office backend"),
    )

    markdown = await _parse_markdown(str(file_path), params={"ocr_engine": "disable"})

    assert "Existing PDF path" in markdown


def test_lock_excludes_full_docling_and_torch_runtime() -> None:
    lock_text = (Path(__file__).parents[3] / "uv.lock").read_text(encoding="utf-8")
    package_names = set(re.findall(r'^name = "([^"]+)"$', lock_text, flags=re.MULTILINE))

    assert {
        "docling",
        "docling-ibm-models",
        "docling-parse",
        "torch",
        "torchvision",
    }.isdisjoint(package_names)


def test_convert_csv_to_markdown_preserves_column_dtypes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_path = tmp_path / "parser_test.csv"
    file_path.write_text("id,score\n9007199254740993,2.5\n", encoding="utf-8")
    captured_dtypes: list[dict[str, object]] = []
    original_to_markdown = pd.DataFrame.to_markdown

    def _capture_dtypes(dataframe: pd.DataFrame, *args, **kwargs) -> str:
        captured_dtypes.append(dataframe.dtypes.to_dict())
        return original_to_markdown(dataframe, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_markdown", _capture_dtypes)

    markdown = parser_unified._convert_csv_to_markdown(file_path)

    assert markdown
    assert str(captured_dtypes[0]["id"]) == "int64"


@pytest.mark.asyncio
async def test_parse_document_docx_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_path = tmp_path / "parser_test_async.docx"
    file_path.write_bytes(b"fake docx")
    completion_order: list[str] = []

    def _slow_docling_conversion(*args, **kwargs) -> str:
        time.sleep(0.1)
        return "Async DOCX content"

    async def __parse_markdown() -> None:
        await _parse_markdown(str(file_path))
        completion_order.append("parse")

    async def _record_event_loop_progress() -> None:
        await asyncio.sleep(0.01)
        completion_order.append("event_loop")

    monkeypatch.setattr(parser_unified, "_convert_with_docling", _slow_docling_conversion)

    await asyncio.gather(__parse_markdown(), _record_event_loop_progress())

    assert completion_order == ["event_loop", "parse"]


def test_rapid_ocr_resolves_model_dir_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target_dir = tmp_path / "custom_models"
    monkeypatch.setenv("RAPIDOCR_MODEL_DIR", str(target_dir))
    parser = RapidOCRParser()
    params = parser._get_model_params()

    assert params["Global.model_root_dir"] == str(target_dir)
    assert target_dir.exists()


async def _parse_markdown(source, params=None, db=None):
    """回读真实解析入口，保留文本格式测试的独立断言。"""
    with tempfile.TemporaryDirectory() as temporary:
        result = await parse(str(source), Path(temporary) / "parsed", params, db)
        return result.markdown_path.read_text()


@pytest.mark.asyncio
async def test_docx_directory_preserves_embedded_image_and_position(tmp_path):
    """真实 DOCX 产物保留图片字节及正文顺序。"""
    result = await parse(str(PARSER_FIXTURES / "测试文档.docx"), tmp_path / "parsed")
    markdown = result.markdown_path.read_text()
    assert result.resources == ("images/figure-001.png",)
    assert (result.directory / result.resources[0]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert re.search(r"20XX个人述职报告[\s\S]+!\[image\]\(images/figure-001.png\)[\s\S]+测试图片", markdown)


@pytest.mark.asyncio
async def test_office_resource_failure_rejects_partial_directory(tmp_path, monkeypatch):
    """资源失败不能退回纯文本并报告成功。"""
    source = tmp_path / "source.docx"
    source.write_bytes(b"fixture")
    fake = SimpleNamespace(pictures=[SimpleNamespace(image=None)], export_to_markdown=lambda: "before\n<!-- image -->")
    monkeypatch.setattr(parser_unified, "_convert_office_document", lambda _: fake)
    with pytest.raises(ValueError, match="缺少图像数据"):
        await parse(str(source), tmp_path / "parsed")
    assert not (tmp_path / "parsed").exists()


@pytest.mark.asyncio
async def test_zip_preserves_distinct_paths_and_reference_titles(tmp_path):
    """同名图片保持完整路径，引用式图片与标题也保持可读。"""
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("nested/full.md", '![](a/chart.png "first")\n![second][fig]\n\n[fig]: b/chart.png "second"')
        archive.writestr("nested/a/chart.png", b"first")
        archive.writestr("nested/b/chart.png", b"second")
    result = await parse(str(source), tmp_path / "parsed")
    markdown = result.markdown_path.read_text()
    assert '![](images/nested/a/chart.png "first")' in markdown
    assert '[fig]: images/nested/b/chart.png "second"' in markdown
    assert (result.directory / "images/nested/a/chart.png").read_bytes() == b"first"
    assert (result.directory / "images/nested/b/chart.png").read_bytes() == b"second"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_path", ["../outside.png", "/absolute.png", "images/../outside.png", "C:\\outside.png"])
async def test_zip_rejects_unsafe_paths_before_writing(tmp_path, bad_path):
    """恢复路径穿越缺陷时，测试必须因拒绝缺失而失败。"""
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("full.md", "# content")
        archive.writestr(bad_path, b"escape")
    with pytest.raises(ValueError, match="不安全的资源路径"):
        await parse(str(source), tmp_path / "parsed")
    assert not (tmp_path / "parsed").exists()
    assert not (tmp_path / "outside.png").exists()


@pytest.mark.asyncio
async def test_missing_image_rejects_successful_text(tmp_path):
    """正文引用不存在的图片时拒绝完整成功。"""
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("full.md", "![](missing.png)")
    with pytest.raises(ValueError, match="缺少 Markdown 资源"):
        await parse(str(source), tmp_path / "parsed")
    assert not (tmp_path / "parsed").exists()


@pytest.mark.asyncio
async def test_default_engine_resolves_system_ocr_config_before_conversion(tmp_path, monkeypatch):
    """应用层解析默认引擎，底层只接收有效值。"""
    source = tmp_path / "source.pdf"
    _build_pdf(source, "text")

    async def config(*args):
        return {"default_ocr_engine": "mineru_ocr"}

    async def kwargs(*args):
        return {"server_url": "http://configured.test"}

    captured = {}

    def get_engine(engine_id, **kwargs):
        captured.update(engine=engine_id, kwargs=kwargs)
        return SimpleNamespace(process_file=lambda *args: "configured OCR text")

    monkeypatch.setattr("yuxi.modules.system.options.Option.get", config)
    monkeypatch.setattr("yuxi.modules.documents.service._build_processor_kwargs", kwargs)
    monkeypatch.setattr(engines, "get_engine", get_engine)
    result = await parse(str(source), tmp_path / "parsed", db=object())
    assert result.markdown_path.read_text() == "configured OCR text"
    assert captured == {"engine": "mineru_ocr", "kwargs": {"server_url": "http://configured.test"}}


@pytest.mark.asyncio
async def test_cancel_waits_for_converter_before_removing_directory(tmp_path, monkeypatch):
    """取消后没有仍能重新创建资源目录的引擎线程。"""
    import threading
    from yuxi.infrastructure.document_parsing.artifacts import save_resource

    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    started, release = threading.Event(), threading.Event()

    def converter(source, output, options):
        started.set()
        assert release.wait(5)
        save_resource(output, "images/late.png", b"late")
        return "![](images/late.png)"

    monkeypatch.setattr(parser_unified, "_parse_ocr", converter)
    task = asyncio.create_task(parser_unified.parse(source, tmp_path / "parsed", ParseOptions(ocr_engine="rapid_ocr")))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path / "parsed").exists()


@pytest.mark.asyncio
async def test_zip_rewrite_keeps_identity_and_legal_destinations(tmp_path):
    """一次替换原始引用，编码空格和括号且保留两张不同图片。"""
    from yuxi.infrastructure.document_parsing.artifacts import local_image_links
    from urllib.parse import unquote

    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("full.md", r"![](chart.png)" + "\n![](images/chart.png)\n![](image%20one.png)\n" + r"![](image\(1\).png)")
        for name, data in {
            "chart.png": b"first",
            "images/chart.png": b"second",
            "image one.png": b"space",
            "image(1).png": b"parentheses",
        }.items():
            archive.writestr(name, data)
    result = await parser_unified.parse(source, tmp_path / "parsed")
    links = local_image_links(result.markdown_path.read_text())
    assert len(links) == 4
    assert [(result.directory / unquote(link)).read_bytes() for link in links] == [
        b"first",
        b"second",
        b"space",
        b"parentheses",
    ]


@pytest.mark.asyncio
async def test_local_destinations_encode_url_delimiters_without_changing_hosted_urls(tmp_path):
    """本地文件名与已编码代理 URL 保持各自身份。"""
    from urllib.parse import quote, unquote
    from yuxi.infrastructure.document_parsing.artifacts import local_image_links, replace_image_links

    names = ["chart#one.png", "chart?one.png", "chart%20one.png", "chart one.png"]
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("full.md", "\n".join(f"![]({quote(name, safe='/')})" for name in names))
        for name in names:
            archive.writestr(name, name.encode())
    result = await parser_unified.parse(source, tmp_path / "parsed")
    links = local_image_links(result.markdown_path.read_text())
    assert len(links) == len(names)
    assert [(result.directory / unquote(link)).read_bytes() for link in links] == [name.encode() for name in names]
    hosted = "/api/knowledge/knowledge-bases/kb/images/chart%23one.png"
    assert replace_image_links("![](chart.png)", {"chart.png": hosted}) == f"![]({hosted})"


@pytest.mark.asyncio
async def test_image_alt_brackets_do_not_change_resource_identity(tmp_path):
    """图片说明中的合法方括号不影响资源引用改写。"""
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("full.md", r"![chart [section A]](chart.png)" + "\n" + r"![chart \[section A\]](chart.png)")
        archive.writestr("chart.png", b"image")
    result = await parser_unified.parse(source, tmp_path / "parsed")
    markdown = result.markdown_path.read_text()
    assert markdown.count("(images/chart.png)") == 2
    assert "[section A]" in markdown
    assert (result.directory / "images/chart.png").read_bytes() == b"image"
