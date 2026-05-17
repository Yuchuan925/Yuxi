import os

from ..config import config
from .factory import KnowledgeBaseFactory
from .implementations.dify import DifyKB
from .implementations.milvus import MilvusKB
from .manager import KnowledgeBaseManager

_LITE_MODE = os.environ.get("LITE_MODE", "").lower() in ("true", "1")
_SKIP_APP_INIT = os.environ.get("YUXI_SKIP_APP_INIT") == "1"

if not _LITE_MODE:
    # 注册知识库类型
    KnowledgeBaseFactory.register("milvus", MilvusKB, {"description": "基于 Milvus 的生产级向量知识库，适合高性能部署"})

KnowledgeBaseFactory.register("dify", DifyKB, {"description": "连接 Dify Dataset 的只读检索知识库"})

# 创建知识库管理器
work_dir = os.path.join(config.save_dir, "knowledge_base_data")
knowledge_base = KnowledgeBaseManager(work_dir)

__all__ = ["knowledge_base"]
