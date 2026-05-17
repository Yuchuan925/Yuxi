from __future__ import annotations

from typing import Any

import json_repair

from yuxi.models.chat import select_model

from .base import GraphExtractor


DEFAULT_TRIPLE_EXTRACTION_PROMPT = """请从下面文本中抽取实体和实体关系，返回严格 JSON，不要输出解释。
JSON 格式：
{
  "relations": [
    {
      "source": {"text": "实体文本", "label": "实体类型", "attributes": [{"text": "属性值", "label": "属性名称"}]},
      "target": {"text": "实体文本", "label": "实体类型", "attributes": [{"text": "属性值", "label": "属性名称"}]},
      "text": "关系显示文本",
      "label": "关系类型"
    }
  ]
}
"""


class LLMGraphExtractor(GraphExtractor):
    extractor_type = "llm"

    def validate_options(self) -> None:
        if not self.options.get("model_spec"):
            raise ValueError("LLM 抽取器需要 model_spec")

    async def extract(self, text: str, *, chunk_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        self.validate_options()
        model = select_model(model_spec=self.options["model_spec"], timeout=60.0)
        prompt = self._build_prompt(text)
        response = await model.call(prompt, stream=False)
        parsed = json_repair.loads(response.content if response else "")
        return parsed

    def _build_prompt(self, text: str) -> str:
        extraction_prompt = self.options.get("prompt") or DEFAULT_TRIPLE_EXTRACTION_PROMPT
        return f"{extraction_prompt}\n\n文本：\n{text}"
