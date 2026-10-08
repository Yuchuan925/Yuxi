from yuxi.modules.agents.runtime.state import BaseState


class ChatBotState(BaseState):
    """保存与 checkpoint 一起提交的协作消息消费位置。"""

    cooperation_cursor: int
