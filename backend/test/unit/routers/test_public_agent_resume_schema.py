"""Public 等待点回答的 wire 类型边界。"""

import pytest
from pydantic import ValidationError

from server.routers.public_v1.agents.schemas import ThreadEventCreate


def _resume_body(answer):
    """构建单题恢复事件。"""
    return {
        "events": [
            {
                "type": "yuxi.thread.input.resume",
                "turn_id": "turn-1",
                "waitpoint_id": "wait-1",
                "response": {"type": "answer", "answers": [{"question_id": "q-1", "answer": answer}]},
            }
        ]
    }


@pytest.mark.parametrize(
    "answer",
    [
        ["杭州", "上海"],
        {"type": "other", "text": "苏州", "selected": ["杭州"]},
    ],
)
def test_resume_wire_preserves_web_multiselect_and_other_answers(answer):
    """Web 多选与其他选项按原类型通过严格 Public 协议。"""
    body = _resume_body(answer)
    assert ThreadEventCreate.model_validate(body).model_dump(mode="json") == body


@pytest.mark.parametrize(
    "answer",
    [
        42,
        ["杭州", 42],
        {"type": "other", "text": "苏州", "selected": [42]},
        {"type": "other", "text": "苏州", "selected": [], "extra": True},
        {"type": "unknown", "text": "苏州", "selected": []},
    ],
)
def test_resume_wire_rejects_unrelated_answer_shapes(answer):
    """数字、混合数组和无约束对象不能越过 wire 边界。"""
    with pytest.raises(ValidationError):
        ThreadEventCreate.model_validate(_resume_body(answer))
