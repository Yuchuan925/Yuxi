from __future__ import annotations

import io
from types import SimpleNamespace
from typing import ClassVar

import pytest
from rich.console import Console
from yuxi_cli.agent_eval import (
    AgentEvalError,
    AgentEvalOptions,
    extract_query,
    run_langfuse_agent_experiment,
)
from yuxi_cli.config import ConfigStore, Remote


class FakeResult:
    def __init__(self, outputs: list[str]):
        self.item_results = outputs
        self.output = outputs[0] if outputs else ""

    @classmethod
    def single(cls, output: str):
        return cls([output])

    def format(self, *, include_item_results: bool) -> str:
        assert include_item_results is True
        return f"formatted: {self.output}"


class FakeDataset:
    def __init__(self, item, *, result: FakeResult | None = None):
        self.item = item
        self.items = [item]
        self.result = result
        self.run_kwargs = None

    def run_experiment(self, **kwargs):
        self.run_kwargs = kwargs
        output = kwargs["task"](item=self.item)
        return self.result or FakeResult.single(output)


class FakePartialDataset:
    def __init__(self):
        self.items = [
            SimpleNamespace(id="item-1", input="hello"),
            SimpleNamespace(id="item-2", input="world"),
        ]
        self.run_kwargs = None

    def run_experiment(self, **kwargs):
        self.run_kwargs = kwargs
        kwargs["task"](item=self.items[0])
        return FakeResult.single("final answer")


class FakeLangfuse:
    def __init__(self, dataset):
        self.dataset = dataset
        self.flushed = 0

    def get_dataset(self, name: str):
        assert name == "agent-eval-smoke"
        return self.dataset

    def flush(self):
        self.flushed += 1


class FakeYuxiClient:
    calls: ClassVar[list[dict]] = []

    def __init__(self, remote: Remote, timeout: float = 30.0):
        self.remote = remote
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def create_agent_thread(self, *, agent_slug, idempotency_key):
        self.calls.append({
            "remote": self.remote, "client_timeout": self.timeout,
            "method": "create", "agent_slug": agent_slug, "key": idempotency_key,
        })
        return {"thread_id": "thread-1"}

    def send_agent_message(self, thread_id, message, *, idempotency_key):
        self.calls.append({
            "method": "message", "thread_id": thread_id,
            "message": message, "key": idempotency_key,
        })
        return {"input_id": "input-1", "turn_id": "turn-1"}

    def get_agent_turn(self, thread_id, turn_id):
        self.calls.append({"method": "turn", "thread_id": thread_id, "turn_id": turn_id})
        return {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "final answer"}]}]}


def _console():
    return Console(file=io.StringIO(), force_terminal=False)


def test_extract_query_supports_string_and_common_fields():
    assert extract_query("hello") == "hello"
    assert extract_query({"query": "hello"}) == "hello"
    assert extract_query({"question": "hello"}) == "hello"
    assert extract_query({"prompt": "hello"}) == "hello"


def test_extract_query_rejects_unrecognized_input():
    with pytest.raises(AgentEvalError, match="无法从 Langfuse dataset item input 中提取 query"):
        extract_query({"text": "hello"})


def test_run_langfuse_agent_experiment_uses_remote_api_key(tmp_path):
    store = ConfigStore(tmp_path / "config.toml")
    config = store.load()
    remote = config.get_remote("local")
    remote.api_key = "yxkey_local"
    store.save(config)
    dataset = FakeDataset(SimpleNamespace(id="item-1", input={"query": "2+2=?"}))
    langfuse = FakeLangfuse(dataset)
    console = _console()
    FakeYuxiClient.calls = []

    run_langfuse_agent_experiment(
        store,
        None,
        AgentEvalOptions(
            dataset_name="agent-eval-smoke",
            agent_slug="default-chatbot",
            experiment_name="exp-1",
            max_concurrency=2,
            timeout_seconds=123,
        ),
        console,
        langfuse_factory=lambda: langfuse,
        client_factory=FakeYuxiClient,
    )

    assert dataset.run_kwargs["name"] == "exp-1"
    assert dataset.run_kwargs["max_concurrency"] == 2
    assert dataset.run_kwargs["metadata"] == {
        "source": "agent_evaluation",
        "agent_slug": "default-chatbot",
        "dataset_name": "agent-eval-smoke",
        "remote": "local",
    }
    call = FakeYuxiClient.calls[0]
    assert call["remote"].name == "local"
    assert call["client_timeout"] == 123
    assert call["agent_slug"] == "default-chatbot"
    assert call["key"].startswith("eval-")
    assert FakeYuxiClient.calls[1]["message"] == "2+2=?"
    assert FakeYuxiClient.calls[1]["key"] == f"{call['key']}-message"
    assert [entry["method"] for entry in FakeYuxiClient.calls] == ["create", "message", "turn"]
    assert "formatted: final answer" in console.file.getvalue()
    assert langfuse.flushed == 1


def test_run_langfuse_agent_experiment_requires_login(tmp_path):
    store = ConfigStore(tmp_path / "config.toml")

    with pytest.raises(AgentEvalError, match="remote 尚未登录"):
        run_langfuse_agent_experiment(
            store,
            None,
            AgentEvalOptions(dataset_name="agent-eval-smoke", agent_slug="default-chatbot"),
            _console(),
            langfuse_factory=lambda: FakeLangfuse(FakeDataset(SimpleNamespace(id="1", input="hello"))),
            client_factory=FakeYuxiClient,
        )


def test_run_langfuse_agent_experiment_rejects_partial_langfuse_results(tmp_path):
    store = ConfigStore(tmp_path / "config.toml")
    config = store.load()
    config.get_remote("local").api_key = "yxkey_local"
    store.save(config)
    langfuse = FakeLangfuse(FakePartialDataset())
    FakeYuxiClient.calls = []

    with pytest.raises(AgentEvalError, match="1/2 个 item 成功写入"):
        run_langfuse_agent_experiment(
            store,
            None,
            AgentEvalOptions(
                dataset_name="agent-eval-smoke",
                agent_slug="default-chatbot",
                experiment_name="exp-1",
            ),
            _console(),
            langfuse_factory=lambda: langfuse,
            client_factory=FakeYuxiClient,
        )
