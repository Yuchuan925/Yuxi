from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from yuxi.agents.backends.sandbox.provider import sandbox_provisioner_token
from yuxi.agents.backends.sandbox.provisioner_client import ProvisionerClient


def test_provisioner_client_sends_bearer_token(monkeypatch):
    calls = []

    def fake_request(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, json=lambda: {"sandboxes": [], "count": 0})

    monkeypatch.setattr("yuxi.agents.backends.sandbox.provisioner_client.httpx.request", fake_request)
    client = ProvisionerClient(
        "http://sandbox-provisioner:8002",
        token="test-provisioner-token-that-is-long-enough",
    )

    client.health()

    assert calls == [
        {
            "method": "GET",
            "url": "http://sandbox-provisioner:8002/health",
            "timeout": client._timeout,
            "headers": {"Authorization": "Bearer test-provisioner-token-that-is-long-enough"},
        }
    ]


def test_provisioner_client_can_disable_sandbox_environment(monkeypatch):
    calls = []

    def fake_request(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"sandbox_id": "sandbox-1", "sandbox_url": "http://sandbox"},
        )

    monkeypatch.setattr("yuxi.agents.backends.sandbox.provisioner_client.httpx.request", fake_request)
    client = ProvisionerClient(
        "http://sandbox-provisioner:8002",
        token="test-provisioner-token-that-is-long-enough",
    )

    client.create("sandbox-1", "thread-1", "user-1", {"SECRET": "value"}, inherit_env=False)

    assert calls[0]["json"]["inherit_env"] is False


def test_provisioner_client_replaces_skills_through_management_api(monkeypatch):
    calls = []

    def fake_request(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr("yuxi.agents.backends.sandbox.provisioner_client.httpx.request", fake_request)
    client = ProvisionerClient(
        "http://sandbox-provisioner:8002",
        token="test-provisioner-token-that-is-long-enough",
    )

    client.replace_skills("sandbox-1", {"demo/SKILL.md": b"# demo"})

    assert calls[0]["method"] == "PUT"
    assert calls[0]["url"].endswith("/api/sandboxes/sandbox-1/skills")
    assert base64.b64decode(calls[0]["json"]["files"]["demo/SKILL.md"]) == b"# demo"


def test_sandbox_provisioner_token_reads_environment(monkeypatch):
    monkeypatch.setenv("SANDBOX_PROVISIONER_TOKEN", "test-provisioner-token-that-is-long-enough")

    assert sandbox_provisioner_token() == "test-provisioner-token-that-is-long-enough"


def test_sandbox_provisioner_token_is_required(monkeypatch):
    monkeypatch.delenv("SANDBOX_PROVISIONER_TOKEN", raising=False)

    with pytest.raises(ValueError, match="at least 32 characters"):
        sandbox_provisioner_token()
