"""文档处理器注册表和配置策略。"""

from importlib import import_module

PROCESSOR_TYPES = {
    "rapid_ocr": ("yuxi.knowledge.parser.rapid_ocr", "RapidOCRParser"),
    "mineru_ocr": ("yuxi.knowledge.parser.mineru", "MinerUParser"),
    "mineru_official": ("yuxi.knowledge.parser.mineru_official", "MinerUOfficialParser"),
    "pp_structure_v3_ocr": ("yuxi.knowledge.parser.pp_structure_v3", "PPStructureV3Parser"),
    "deepseek_ocr": ("yuxi.knowledge.parser.deepseek_ocr", "DeepSeekOCRParser"),
    "paddleocr_vl_1_6": ("yuxi.knowledge.parser.paddleocr_api", "PaddleOCRVLParser"),
    "paddleocr_pp_ocrv6": ("yuxi.knowledge.parser.paddleocr_api", "PaddleOCRPPOCRv6Parser"),
}

PROCESSOR_METADATA = {
    "rapid_ocr": {
        "display_name": "RapidOCR (ONNX)",
        "enabled": True,
        "endpoint": None,
        "credential_source": None,
        "credential_ref": None,
    },
    "mineru_ocr": {
        "display_name": "MinerU OCR",
        "enabled": True,
        "endpoint": None,
        "credential_source": None,
        "credential_ref": None,
        "endpoint_editable": True,
    },
    "mineru_official": {
        "display_name": "MinerU Official API",
        "enabled": True,
        "endpoint": "https://mineru.net/api/v4",
        "credential_source": "environment",
        "credential_ref": "MINERU_API_KEY",
        "credential_sources": ["environment", "database"],
    },
    "pp_structure_v3_ocr": {
        "display_name": "PP-Structure-V3",
        "enabled": True,
        "endpoint": None,
        "credential_source": None,
        "credential_ref": None,
        "endpoint_editable": True,
    },
    "deepseek_ocr": {
        "display_name": "DeepSeek OCR",
        "enabled": True,
        "endpoint": "https://api.siliconflow.cn/v1/chat/completions",
        "credential_source": "environment",
        "credential_ref": "SILICONFLOW_API_KEY",
        "credential_sources": ["environment", "database"],
    },
    "paddleocr_vl_1_6": {
        "display_name": "PaddleOCR-VL-1.6",
        "enabled": True,
        "endpoint": None,
        "credential_source": "environment",
        "credential_ref": "PADDLEOCR_API_TOKEN",
        "credential_sources": ["environment", "database"],
    },
    "paddleocr_pp_ocrv6": {
        "display_name": "PP-OCRv6",
        "enabled": True,
        "endpoint": None,
        "credential_source": "environment",
        "credential_ref": "PADDLEOCR_API_TOKEN",
        "credential_sources": ["environment", "database"],
    },
}


def get_supported_extensions(engine_id: str) -> list[str]:
    """从解析器类读取文件能力，避免配置中心复制一份扩展名清单。"""

    module_path, class_name = PROCESSOR_TYPES[engine_id]
    processor_class = getattr(import_module(module_path), class_name)
    return list(processor_class.supported_extensions)
