"""Define the state structures for the agent."""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain.agents import AgentState


class Artifact(TypedDict):
    """共享路径身份与展示类型的交付物。"""

    path: str
    type: Literal["file", "image"]


def merge_artifacts(existing: list[Artifact | str] | None, new: list[Artifact | str] | None) -> list[Artifact]:
    """按路径保序合并交付物，最近的展示类型覆盖旧值。"""
    artifacts = {}
    for item in (existing or []) + (new or []):
        artifact = Artifact(path=item, type="file") if isinstance(item, str) else item
        artifacts[artifact["path"]] = artifact
    return list(artifacts.values())


class BaseState(AgentState):
    """Shared state fields for Yuxi agents."""

    artifacts: Annotated[list[Artifact | str], merge_artifacts]


class AgentStatePayload(TypedDict):
    """Serialized agent state payload consumed by the frontend."""

    todos: list
    artifacts: list[Artifact | str]
    token_usage: dict | None
