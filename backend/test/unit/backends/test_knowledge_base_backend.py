from __future__ import annotations

from types import SimpleNamespace

import pytest

import yuxi.agents.backends.knowledge_base_backend as knowledge_base_backend


@pytest.mark.asyncio
async def test_resolve_visible_knowledge_bases_filters_by_slug(monkeypatch):
    async def fake_get_databases_by_uid(_uid):
        return {
            "databases": [
                {"slug": "kb-a", "name": "Same Name"},
                {"slug": "kb-b", "name": "Same Name"},
            ]
        }

    monkeypatch.setattr(knowledge_base_backend.knowledge_base, "get_databases_by_uid", fake_get_databases_by_uid)

    context = SimpleNamespace(uid="u1", knowledges=["kb-b"])

    databases = await knowledge_base_backend.resolve_visible_knowledge_bases_for_context(context)

    assert databases == [{"slug": "kb-b", "name": "Same Name"}]
    assert context._visible_knowledge_bases == databases


@pytest.mark.asyncio
async def test_resolve_visible_knowledge_bases_requires_slug(monkeypatch):
    async def fake_get_databases_by_uid(_uid):
        return {"databases": [{"id": "legacy-id", "name": "Legacy"}]}

    monkeypatch.setattr(knowledge_base_backend.knowledge_base, "get_databases_by_uid", fake_get_databases_by_uid)

    context = SimpleNamespace(uid="u1", knowledges=["legacy-id"])

    databases = await knowledge_base_backend.resolve_visible_knowledge_bases_for_context(context)

    assert databases == []
