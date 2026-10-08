"""协作工具只接受直属成员名称，路径由执行身份生成。"""

import pytest
from pydantic import ValidationError

from yuxi.modules.agents.runtime.middlewares.cooperation import CooperationMiddleware

pytestmark = pytest.mark.unit


def test_list_sessions_schema_has_no_pagination_arguments():
    """模型一次读取完整协作摘要，无需携带游标或页大小。"""
    tool = next(tool for tool in CooperationMiddleware().tools if tool.name == "list_sessions")
    assert tool.tool_call_schema.model_json_schema()["properties"] == {}


def test_create_session_schema_exposes_local_name_without_parent_or_path():
    """模型仅看到带局部名称约束的创建参数。"""
    tool = CooperationMiddleware().tools[0]
    schema = tool.tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"name", "description"}
    name = schema["properties"]["name"]
    assert name["pattern"] == "^[A-Za-z0-9_-]+$"
    assert name["minLength"] == 1 and name["maxLength"] == 64
    assert "直属" in name["description"] and "路径" in name["description"]


@pytest.mark.parametrize("name", ["/root/aaa/bbb", "aaa/bbb", "/root/name", "../bbb", r"aaa\bbb", "", "a" * 65])
def test_create_session_tool_arguments_reject_paths(name):
    """路径与无效名称在工具参数边界被拒绝。"""
    tool = CooperationMiddleware().tools[0]
    with pytest.raises(ValidationError) as rejected:
        tool.tool_call_schema.model_validate({"name": name, "description": "完成独立工作"})
    assert any(error["loc"] == ("name",) for error in rejected.value.errors())


@pytest.mark.parametrize("name", ["aaa", "worker-1", "review_2", "a" * 64])
def test_create_session_tool_arguments_accept_local_names(name):
    """合法局部名称保留原值供服务生成路径。"""
    tool = CooperationMiddleware().tools[0]
    arguments = tool.tool_call_schema.model_validate({"name": name, "description": "完成独立工作"})
    assert arguments.name == name
