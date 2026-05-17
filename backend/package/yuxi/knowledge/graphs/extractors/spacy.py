from __future__ import annotations

import threading
from typing import Any

from .base import GraphExtractor


_spacy_models: dict[str, Any] = {}
_spacy_model_lock = threading.Lock()


def _load_spacy_model(model_name: str) -> Any:
    cached = _spacy_models.get(model_name)
    if cached is not None:
        return cached

    try:
        import spacy
    except ImportError as exc:
        raise ValueError("spaCy 未安装，无法使用 spacy 抽取器") from exc

    with _spacy_model_lock:
        cached = _spacy_models.get(model_name)
        if cached is not None:
            return cached
        model = spacy.load(model_name)
        _spacy_models[model_name] = model
        return model


class SpacyGraphExtractor(GraphExtractor):
    extractor_type = "spacy"

    def _model_name(self) -> str:
        return str(self.options.get("model") or self.options.get("model_name") or "").strip()

    def validate_options(self) -> None:
        if not self._model_name():
            raise ValueError("spaCy 抽取器需要 model 或 model_name")

    async def extract(self, text: str, *, chunk_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        self.validate_options()
        model_name = self._model_name()
        allowed_labels = set(self.options.get("entity_labels") or [])
        doc = _load_spacy_model(model_name)(text)
        entities = []
        seen = set()
        for ent in doc.ents:
            entity_text = ent.text.strip()
            if not entity_text or entity_text in seen:
                continue
            if allowed_labels and ent.label_ not in allowed_labels:
                continue
            seen.add(entity_text)
            entities.append(
                {
                    "text": entity_text,
                    "label": ent.label_ or "Entity",
                    "attributes": [],
                }
            )

        return {"entities": entities, "relations": [], "metadata": {"model": model_name}}
