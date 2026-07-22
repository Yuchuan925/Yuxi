"""OCR 引擎可持久化参数契约。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class OCRParams(BaseModel):
    """拒绝未声明字段的 OCR 参数基类。"""

    model_config = ConfigDict(extra="forbid")


class RapidOCRParams(OCRParams):
    """RapidOCR 可持久化参数。"""

    det_box_thresh: float = Field(0.3, ge=0, le=1)
    zoom_x: float = Field(2.0, gt=0, le=10)
    zoom_y: float = Field(2.0, gt=0, le=10)


class MinerUParams(OCRParams):
    """自托管 MinerU 可持久化参数。"""

    timeout_seconds: int = Field(1800, ge=1, le=7200)
    lang_list: list[str] = Field(default_factory=lambda: ["ch"])
    backend: str = "hybrid-auto-engine"
    parse_method: str = "auto"
    formula_enable: bool = True
    table_enable: bool = True
    image_analysis: bool = True


class MinerUOfficialParams(OCRParams):
    """MinerU 官方服务可持久化参数。"""

    max_wait_seconds: int = Field(600, ge=1, le=7200)
    poll_interval_seconds: float = Field(5, gt=0, le=60)
    language: str = "ch"
    is_ocr: bool = True
    enable_formula: bool = True
    enable_table: bool = True


class PPStructureParams(OCRParams):
    """PP-Structure-V3 可持久化参数。"""

    timeout_seconds: int = Field(300, ge=1, le=3600)
    use_table_recognition: bool = True
    use_formula_recognition: bool = True
    use_seal_recognition: bool = False


class DeepSeekOCRParams(OCRParams):
    """DeepSeek OCR 可持久化参数。"""

    pdf_dpi: int = Field(200, ge=72, le=600)
    max_tokens: int = Field(4096, ge=1, le=32768)
    temperature: float = Field(0.1, ge=0, le=2)
    timeout_seconds: int = Field(120, ge=1, le=1800)


class PaddleOCRVLParams(OCRParams):
    """PaddleOCR-VL 可持久化参数。"""

    poll_interval_seconds: float = Field(5, gt=0, le=60)
    max_wait_seconds: int = Field(600, ge=1, le=7200)
    useDocOrientationClassify: bool = False
    useDocUnwarping: bool = False
    useChartRecognition: bool = False


class PaddleOCRV6Params(OCRParams):
    """PP-OCRv6 可持久化参数。"""

    poll_interval_seconds: float = Field(5, gt=0, le=60)
    max_wait_seconds: int = Field(600, ge=1, le=7200)
    useDocOrientationClassify: bool = False
    useDocUnwarping: bool = False
    useTextlineOrientation: bool = False


PARAM_SCHEMAS: dict[str, type[OCRParams]] = {
    "disable": OCRParams,
    "rapid_ocr": RapidOCRParams,
    "mineru_ocr": MinerUParams,
    "mineru_official": MinerUOfficialParams,
    "pp_structure_v3_ocr": PPStructureParams,
    "deepseek_ocr": DeepSeekOCRParams,
    "paddleocr_vl_1_6": PaddleOCRVLParams,
    "paddleocr_pp_ocrv6": PaddleOCRV6Params,
}


def validate_default_params(engine_id: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """按引擎白名单校验并补全默认识别参数。"""

    schema = PARAM_SCHEMAS.get(engine_id)
    if schema is None:
        raise ValueError(f"不支持的 OCR 引擎: {engine_id}")
    return schema.model_validate(params or {}).model_dump()


def validate_endpoint(endpoint: str | None) -> str | None:
    """规范化自托管 OCR 的 HTTP(S) 端点。"""

    if endpoint is None or not endpoint.strip():
        return None
    return str(HttpUrl(endpoint.strip()))
