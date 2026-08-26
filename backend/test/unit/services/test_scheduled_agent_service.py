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


def test_validate_schedule_rejects_expression_without_future_occurrence():
    with pytest.raises(HTTPException) as exc_info:
        validate_schedule("0 0 31 2 *", "UTC")

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_validate_project_rejects_project_owned_by_another_user(monkeypatch):
    class ProjectRepository:
        def __init__(self, db):
            del db

        async def get_for_user(self, project_id, uid):
            assert (project_id, uid) == ("project-1", "user-2")
            return None

    monkeypatch.setattr(service, "ProjectRepository", ProjectRepository)

    with pytest.raises(HTTPException) as exc_info:
        await service._validate_project("project-1", SimpleNamespace(uid="user-2"), object())

    assert exc_info.value.status_code == 404


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


def test_scheduled_run_model_owns_execution_configuration_snapshot():
    from yuxi.storage.postgres.models_business import ScheduledAgentRun

    assert {"project_id", "agent_slug", "conversation_title", "prompt", "tool_approval_mode"}.issubset(
        ScheduledAgentRun.__table__.c.keys()
    )


def test_scheduled_run_model_uses_unique_thread_per_execution_and_preserves_history():
    from yuxi.storage.postgres.models_business import ScheduledAgentJob, ScheduledAgentRun

    project_fk = next(
        constraint
        for constraint in ScheduledAgentJob.__table__.foreign_key_constraints
        if constraint.name == "fk_scheduled_agent_jobs_project_uid"
    )
    assert project_fk.ondelete == "RESTRICT"
    foreign_key = next(iter(ScheduledAgentRun.__table__.c.job_id.foreign_keys))
    assert foreign_key.ondelete is None
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in ScheduledAgentRun.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("thread_id",) in unique_columns
