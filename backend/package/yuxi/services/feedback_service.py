import asyncio

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.services.langfuse_service import submit_user_feedback_score
from yuxi.storage.postgres.models_business import AgentRun, AgentTurn, Conversation, Message, MessageFeedback
from yuxi.utils.logging_config import logger


async def submit_message_feedback_view(
    *,
    message_id: int,
    rating: str,
    reason: str | None,
    db: AsyncSession,
    current_uid: str,
    thread_id: str,
    app_id: str | None,
) -> dict:
    """仅给作用域内的结果消息保存反馈。"""
    if rating not in ["like", "dislike"]:
        raise HTTPException(status_code=422, detail="Rating must be 'like' or 'dislike'")

    try:
        message_result = await db.execute(
            select(Message, Conversation)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(
                AgentRun,
                and_(AgentRun.id == Message.run_id, AgentRun.output_message_id == Message.id),
            )
            .join(AgentTurn, AgentTurn.id == AgentRun.turn_id)
            .where(
                Message.id == message_id,
                Message.role == "assistant",
                Message.turn_id == AgentTurn.id,
                Conversation.thread_id == thread_id,
                Conversation.uid == str(current_uid),
                Conversation.app_id == app_id,
                AgentRun.conversation_id == Conversation.id,
                AgentRun.conversation_thread_id == thread_id,
                AgentRun.uid == str(current_uid),
                AgentRun.app_id == app_id,
                AgentRun.run_type.in_(("chat", "resume")),
                AgentRun.status == "completed",
                AgentTurn.result_run_id == AgentRun.id,
                AgentTurn.conversation_thread_id == thread_id,
                AgentTurn.uid == str(current_uid),
                AgentTurn.app_id == app_id,
                AgentTurn.status == "completed",
            )
        )
        row = message_result.one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Message not found")
        message = row[0]

        existing_feedback_result = await db.execute(
            select(MessageFeedback).filter_by(message_id=message_id, uid=str(current_uid))
        )
        existing_feedback = existing_feedback_result.scalar_one_or_none()
        if existing_feedback:
            raise HTTPException(status_code=409, detail="Feedback already submitted for this message")

        new_feedback = MessageFeedback(
            message_id=message_id,
            uid=str(current_uid),
            rating=rating,
            reason=reason,
        )

        db.add(new_feedback)
        await db.commit()
        await db.refresh(new_feedback)

        trace_id = (message.extra_metadata or {}).get("langfuse_trace_id")
        if trace_id:
            # submit_user_feedback_score 内部会同步调用 client.flush() 发起阻塞网络请求，
            # 放到线程池执行避免阻塞事件循环；本地反馈已落库，上传失败不影响主流程。
            await asyncio.to_thread(
                submit_user_feedback_score,
                trace_id=trace_id,
                feedback_id=new_feedback.id,
                message_id=new_feedback.message_id,
                conversation_id=message.conversation_id,
                uid=str(current_uid),
                rating=rating,
                reason=reason,
            )

        logger.info(f"User {current_uid} submitted {rating} feedback for message {message_id}")

        return {
            "id": new_feedback.id,
            "message_id": new_feedback.message_id,
            "rating": new_feedback.rating,
            "reason": new_feedback.reason,
            "created_at": new_feedback.created_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error submitting message feedback: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to submit feedback: {str(e)}")


async def get_message_feedback_view(
    *,
    message_id: int,
    db: AsyncSession,
    current_uid: str,
    thread_id: str,
    app_id: str | None,
) -> dict:
    """按 Thread 和 APP 作用域读取用户反馈。"""
    try:
        feedback_result = await db.execute(
            select(MessageFeedback)
            .join(Message, Message.id == MessageFeedback.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                MessageFeedback.message_id == message_id,
                MessageFeedback.uid == str(current_uid),
                Conversation.thread_id == thread_id,
                Conversation.uid == str(current_uid),
                Conversation.app_id == app_id,
            )
        )
        feedback = feedback_result.scalar_one_or_none()

        if not feedback:
            return {"has_feedback": False, "feedback": None}

        return {
            "has_feedback": True,
            "feedback": {
                "id": feedback.id,
                "rating": feedback.rating,
                "reason": feedback.reason,
                "created_at": feedback.created_at.isoformat(),
            },
        }

    except Exception as e:
        logger.exception(f"Error getting message feedback: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get feedback: {str(e)}")
