from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from yuxi.services import scheduled_agent_service as service
from yuxi.services.scheduled_agent_service import next_run_at, validate_schedule


def test_next_run_at_uses_timezone_and_returns_utc_naive_datetime():
    result = next_run_at("0 9 * * *", "Asia/Shanghai", datetime(2026, 8, 26, 0, 0))

    assert result == datetime(2026, 8, 26, 1, 0)
    assert result.tzinfo is None


@pytest.mark.parametrize(
    ("expression", "timezone"),
    [("every day", "Asia/Shanghai"), ("0 9 * * * *", "Asia/Shanghai"), ("0 9 * * *", "Mars/Olympus")],
)
def test_validate_schedule_rejects_invalid_expression_or_timezone(expression, timezone):
    with pytest.raises(HTTPException) as exc_info:
        validate_schedule(expression, timezone)

    assert exc_info.value.status_code == 422


def test_validate_schedule_accepts_five_field_cron_and_iana_timezone():
    assert validate_schedule("*/15 * * * *", "UTC") == ("*/15 * * * *", "UTC")


@pytest.mark.asyncio
async def test_validate_agent_rejects_agent_outside_user_visibility(monkeypatch):
    class AgentRepository:
        def __init__(self, db):
            del db

        async def get_visible_by_slug(self, *, slug, user, kind):
            assert (slug, user.uid, kind) == ("private-agent", "user-2", "main")
            return None

    monkeypatch.setattr(service, "AgentRepository", AgentRepository)

    with pytest.raises(HTTPException) as exc_info:
        await service._validate_agent("private-agent", SimpleNamespace(uid="user-2"), object())

    assert exc_info.value.status_code == 404
