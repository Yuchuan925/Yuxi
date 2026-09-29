"""线程历史里的图片投影：多图从 raw_message 取，旧行退化到单值列。"""

from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yuxi.services.agents.messages import get_thread_history
from yuxi.services.agents.scope import ActorScope
from yuxi.services.agents.input_messages import build_chat_input_message
from yuxi.storage.postgres.models_business import AgentRun, AgentTurn, Base, Conversation, Message, Project

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

STARTED_AT = datetime(2026, 9, 25, 9, 0, 0)


@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(
            Project(
                id="project-image-1",
                uid="user-1",
                selection_status="implicit",
                directory_mode="managed",
                workdir_path="projects/project-image-1",
            )
        )
        db.add(
            Conversation(
                id=1,
                thread_id="thread-images",
                project_id="project-image-1",
                uid="user-1",
                agent_id="main",
                status="active",
            )
        )
        db.add(
            AgentTurn(
                id="turn-images",
                conversation_thread_id="thread-images",
                uid="user-1",
                status="completed",
                current_run_id="run-images",
                result_run_id="run-images",
            )
        )
        db.add(
            AgentRun(
                id="run-images",
                conversation_thread_id="thread-images",
                runtime_scope_id="thread-images",
                agent_slug="main",
                uid="user-1",
                turn_id="turn-images",
                conversation_id=1,
                input_payload={},
                status="completed",
                created_at=STARTED_AT,
            )
        )
        await db.commit()
        yield db
    await engine.dispose()


async def _history(session) -> list[dict]:
    view = await get_thread_history(thread_id="thread-images", scope=ActorScope(uid="user-1", app_id=None), db=session)
    return view["history"]


async def test_多图历史行的投影按顺序给出全部图片(session):
    built = build_chat_input_message("三张图", ["A", "B", "C"])
    session.add(
        Message(
            id=1,
            conversation_id=1,
            role="user",
            content=built.content,
            turn_id="turn-images",
            run_id="run-images",
            message_type=built.message_type,
            image_content=built.image_content,
            extra_metadata={"raw_message": built.raw_message()},
            delivery_status="dispatched",
            created_at=STARTED_AT,
        )
    )
    await session.commit()

    user_message = (await _history(session))[0]

    assert user_message["image_contents"] == ["A", "B", "C"]
    # 单值字段原样保留：CLI 与 API-key 用户仍依赖它
    assert user_message["image_content"] == "A"


async def test_旧单值历史行退化为一张图(session):
    """更早的历史行没有 raw_message，只有 image_content 列。"""
    session.add(
        Message(
            id=2,
            conversation_id=1,
            role="user",
            content="看图",
            turn_id="turn-images",
            run_id="run-images",
            message_type="multimodal_image",
            image_content="OLD",
            extra_metadata={},
            delivery_status="dispatched",
            created_at=STARTED_AT,
        )
    )
    await session.commit()

    user_message = (await _history(session))[0]

    assert user_message["image_contents"] == ["OLD"]
    assert user_message["image_content"] == "OLD"


async def test_纯文本历史行不产生图片(session):
    """有 raw_message 但没有 image part 时不得凭空造出图片。"""
    built = build_chat_input_message("只有文字")
    session.add(
        Message(
            id=3,
            conversation_id=1,
            role="user",
            content=built.content,
            turn_id="turn-images",
            run_id="run-images",
            message_type=built.message_type,
            extra_metadata={"raw_message": built.raw_message()},
            delivery_status="dispatched",
            created_at=STARTED_AT,
        )
    )
    await session.commit()

    user_message = (await _history(session))[0]

    assert user_message["image_contents"] == []
    assert user_message["message_type"] == "text"


async def test_投影不外泄_raw_message_的原始形状(session):
    """DTO 给的是窄形状字符串数组，浏览器不必理解 LangChain 的 content parts。"""
    built = build_chat_input_message("两张图", ["A", "B"])
    session.add(
        Message(
            id=4,
            conversation_id=1,
            role="user",
            content=built.content,
            turn_id="turn-images",
            run_id="run-images",
            message_type=built.message_type,
            image_content=built.image_content,
            extra_metadata={"raw_message": built.raw_message()},
            delivery_status="dispatched",
            created_at=STARTED_AT,
        )
    )
    await session.commit()

    contents = (await _history(session))[0]["image_contents"]

    assert contents == ["A", "B"]
    assert all(isinstance(item, str) for item in contents)
