from typing import Any

from yuxi.knowledge.base import KnowledgeBase


class ReadOnlyConnectors(KnowledgeBase):
    """只读外部检索连接器基类。

    这类知识库只负责保存连接参数和执行 Query，不承载文档上传、解析、索引和文件预览能力。
    """

    requires_embedding_model = False
    supports_documents = False
    apply_chunk_defaults = False

    @staticmethod
    def _readonly_error() -> ValueError:
        return ValueError("只读检索连接器不支持该操作")

    async def _create_kb_instance(self, db_id: str, config: dict) -> Any:
        del db_id, config
        return None

    async def _initialize_kb_instance(self, instance: Any) -> None:
        del instance
        return None

    async def add_file_record(
        self, db_id: str, item: str, params: dict | None = None, operator_id: str | None = None
    ) -> dict:
        raise self._readonly_error()

    async def parse_file(self, db_id: str, file_id: str, operator_id: str | None = None) -> dict:
        raise self._readonly_error()

    async def update_file_params(self, db_id: str, file_id: str, params: dict, operator_id: str | None = None) -> None:
        raise self._readonly_error()

    async def create_folder(self, db_id: str, folder_name: str, parent_id: str | None = None) -> dict:
        raise self._readonly_error()

    async def move_file(self, db_id: str, file_id: str, new_parent_id: str | None) -> dict:
        raise self._readonly_error()

    async def delete_folder(self, db_id: str, folder_id: str) -> None:
        raise self._readonly_error()

    async def index_file(
        self,
        db_id: str,
        file_id: str,
        operator_id: str | None = None,
        params: dict | None = None,
    ) -> dict:
        raise self._readonly_error()

    async def update_content(self, db_id: str, file_ids: list[str], params: dict | None = None) -> list[dict]:
        raise self._readonly_error()

    async def delete_file(self, db_id: str, file_id: str) -> None:
        raise self._readonly_error()

    async def get_file_basic_info(self, db_id: str, file_id: str) -> dict:
        raise self._readonly_error()

    async def get_file_content(self, db_id: str, file_id: str) -> dict:
        raise self._readonly_error()

    async def open_file_content(self, db_id: str, file_id: str, offset: int = 0, limit: int = 800) -> dict:
        del offset, limit
        raise self._readonly_error()

    async def get_file_info(self, db_id: str, file_id: str) -> dict:
        raise self._readonly_error()

    async def list_file_tree(
        self,
        db_id: str,
        parent_id: str | None = None,
        recursive: bool = False,
        files_only: bool = False,
    ) -> dict:
        del db_id, parent_id, recursive, files_only
        raise ValueError("只读检索连接器不支持文件树预览")

    async def read_file_preview(self, db_id: str, file_id: str, variant: str = "parsed") -> dict:
        del db_id, file_id, variant
        raise ValueError("只读检索连接器不支持文件预览")

    async def get_file_download(self, db_id: str, file_id: str, variant: str = "original") -> dict:
        del db_id, file_id, variant
        raise ValueError("只读检索连接器不支持文件下载")
