"""仅供等待取消装配原拓扑的禁止执行模型。"""

from langchain_core.language_models.chat_models import BaseChatModel


class CheckpointCleanupModel(BaseChatModel):
    """清理图只能使用状态 API，误调用模型立即失败。"""

    @property
    def _llm_type(self) -> str:
        """标识无外部依赖的 checkpoint 清理模型。"""
        return "checkpoint-cleanup"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """拒绝从维护路径执行模型。"""
        raise RuntimeError("Checkpoint 清理禁止模型执行")
