"""Public Thread 输入协议的严格 wire 模型。"""

from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from yuxi.modules.agents.services.input_messages import (
    AgentRunInputMessage,
    build_chat_input_message_from_openai_content,
    normalize_image_contents,
)


class WireModel(BaseModel):
    """拒绝未定义的 Public 输入字段。"""

    model_config = ConfigDict(extra="forbid")


class InputTextPart(WireModel):
    """文本输入内容块。"""

    type: Literal["input_text"]
    text: str = Field(min_length=1, max_length=32768)


class InputImagePart(WireModel):
    """内联图片输入内容块。"""

    type: Literal["input_image"]
    image_url: str = Field(min_length=1)


class InputMessage(WireModel):
    """一条有序用户消息。"""

    type: Literal["message"] = "message"
    role: Literal["user"]
    content: list[Annotated[InputTextPart | InputImagePart, Field(discriminator="type")]] = Field(
        min_length=1, max_length=18
    )


class ThreadCreate(WireModel):
    """创建空 Thread 或原子接收首批消息。"""

    agent_id: str = Field(min_length=1, max_length=64)
    input: list[InputMessage] | None = Field(default=None, min_length=1, max_length=20)
    stream: StrictBool = False
    project_id: str | None = None
    title: str | None = Field(default=None, max_length=255)
    model_spec: str | None = None
    tool_approval_mode: str | None = None


class ThreadUpdate(WireModel):
    """更新 Thread 的展示字段与后续输入默认审批模式。"""

    title: str | None = Field(default=None, max_length=255)
    is_pinned: StrictBool | None = None
    tool_approval_mode: str | None = None
    model_spec: str | None = None


class MessageOptions(WireModel):
    """普通输入的优先级、执行配置与附件。"""

    mode: Literal["follow_up", "steer"] | None = None
    model_spec: str | None = None
    tool_approval_mode: str | None = None
    attachment_file_ids: list[str] = Field(default_factory=list, max_length=20)


class MessageEvent(WireModel):
    """官方消息输入，业务扩展统一放入 yuxi。"""

    type: Literal["agent.session.input.message"]
    input: list[InputMessage] = Field(min_length=1, max_length=20)
    yuxi: MessageOptions = Field(default_factory=MessageOptions)


class OtherAnswer(WireModel):
    """选项外填写的文本及已选选项。"""

    type: Literal["other"]
    text: StrictStr = Field(min_length=1)
    selected: list[StrictStr]


class AnswerItem(WireModel):
    """回答等待点中的一个问题。"""

    question_id: str = Field(min_length=1)
    answer: StrictStr | Annotated[list[StrictStr], Field(min_length=1)] | OtherAnswer


class AnswerResponse(WireModel):
    """按等待点顺序回答全部问题。"""

    type: Literal["answer"]
    answers: list[AnswerItem] = Field(min_length=1)


class ApprovalDecision(WireModel):
    """对一个等待中的工具调用作出决定。"""

    call_id: str = Field(min_length=1)
    decision: Literal["approve", "reject"]


class ApprovalResponse(WireModel):
    """按等待点顺序决定全部工具调用。"""

    type: Literal["approval"]
    decisions: list[ApprovalDecision] = Field(min_length=1)


class ResumeEvent(WireModel):
    """消费指定 Turn 的一次等待点。"""

    type: Literal["yuxi.session.input.resume"]
    turn_id: str = Field(min_length=1)
    waitpoint_id: str = Field(min_length=1)
    response: Annotated[AnswerResponse | ApprovalResponse, Field(discriminator="type")]


class CancelOptions(WireModel):
    """显式取消目标，省略时由 owning transaction 选择当前 Turn。"""

    turn_id: str | None = Field(default=None, min_length=1)
    expected_run_id: str | None = None


class CancelEvent(WireModel):
    """官方取消输入和可选业务目标。"""

    type: Literal["agent.session.input.cancel"]
    yuxi: CancelOptions = Field(default_factory=CancelOptions)


class ContinueEvent(WireModel):
    """显式恢复暂停的输入队列。"""

    type: Literal["yuxi.session.input.continue"]


class CancelInputEvent(WireModel):
    """移除尚未领取的 Input 批次。"""

    type: Literal["yuxi.session.input.cancel_input"]
    input_id: str = Field(min_length=1)


ThreadEvent = Annotated[
    MessageEvent | ResumeEvent | CancelEvent | ContinueEvent | CancelInputEvent,
    Field(discriminator="type"),
]


class ThreadEventCreate(WireModel):
    """单次接收一个输入或控制事件。"""

    events: list[ThreadEvent] = Field(min_length=1, max_length=1)


def input_messages_to_domain(messages: list[InputMessage]) -> list[AgentRunInputMessage]:
    """在 HTTP 边界校验图片并保留消息与内容块顺序。"""
    converted = []
    for message in messages:
        parts = []
        images = []
        for part in message.content:
            if isinstance(part, InputTextPart):
                parts.append({"type": "text", "text": part.text})
                continue
            if not part.image_url.startswith("data:image/") or ";base64," not in part.image_url:
                raise HTTPException(status_code=422, detail="input_image 仅支持 data:image base64 URL")
            image_content = part.image_url.split(";base64,", 1)[1]
            if not image_content:
                raise HTTPException(status_code=422, detail="input_image 内容不能为空")
            images.append(image_content)
            parts.append({"type": "image_url", "image_url": part.image_url})
        try:
            normalize_image_contents(images)
            converted.append(build_chat_input_message_from_openai_content(parts))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return converted
