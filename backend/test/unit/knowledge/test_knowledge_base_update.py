import asyncio

from yuxi.knowledge.base import KnowledgeBase


class FakeKnowledgeBase(KnowledgeBase):
    @property
    def kb_type(self) -> str:
        return "fake"

    async def _create_kb_instance(self, slug: str, config: dict):
        return None

    async def _initialize_kb_instance(self, instance) -> None:
        pass

    async def index_file(self, slug: str, file_id: str, operator_id: str | None = None) -> dict:
        return {}

    async def update_content(self, slug: str, file_ids: list[str], params: dict | None = None) -> list[dict]:
        return []

    async def aquery(self, query_text: str, slug: str, **kwargs) -> list[dict]:
        return []

    def get_query_params_config(self, slug: str, **kwargs) -> dict:
        return {"options": []}

    async def delete_file(self, slug: str, file_id: str) -> None:
        pass

    async def get_file_basic_info(self, slug: str, file_id: str) -> dict:
        return {}

    async def get_file_content(self, slug: str, file_id: str) -> dict:
        return {}

    async def get_file_info(self, slug: str, file_id: str) -> dict:
        return {}

    async def _save_metadata(self) -> None:
        pass


def make_kb(tmp_path):
    kb = FakeKnowledgeBase(str(tmp_path))
    kb.databases_meta = {
        "db": {
            "name": "Old name",
            "description": "Old description",
            "kb_type": "fake",
            "llm_model_spec": "provider:model-a",
        }
    }
    return kb


async def test_update_database_keeps_llm_spec_when_field_is_omitted(tmp_path):
    kb = make_kb(tmp_path)

    result = kb.update_database("db", "New name", "New description")
    await asyncio.sleep(0)

    assert result["llm_model_spec"] == "provider:model-a"
    assert kb.databases_meta["db"]["llm_model_spec"] == "provider:model-a"


async def test_update_database_clears_llm_spec_when_field_is_explicit(tmp_path):
    kb = make_kb(tmp_path)

    result = kb.update_database("db", "New name", "New description", None, update_llm_model_spec=True)
    await asyncio.sleep(0)

    assert result["llm_model_spec"] is None
    assert kb.databases_meta["db"]["llm_model_spec"] is None
