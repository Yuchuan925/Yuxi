import { unref } from 'vue'
import { message } from 'ant-design-vue'
import { applyAgentEvent } from '@/modules/session/model/agentItems'

/** 主、子 Thread 共用公开事件消费入口。 */
export function useAgentStreamHandler({ getThreadState, processApprovalInStream, currentAgentId, streamSmoother }) {
  const handlePublicEvent = (event, threadId) => {
    const subagentCreated = event.type === 'agent.session.subagent.created'
    if ((subagentCreated ? event.yuxi?.session_id : event.session_id) !== threadId) return false
    const state = getThreadState(threadId)
    if (!state) return false
    const type = event.type
    if (subagentCreated) {
      const delegated = event.yuxi
      const runs = state.agentState?.subagent_runs || []
      if (!runs.some((run) => run.run_id === delegated.child_run_id)) {
        state.agentState = { ...state.agentState, subagent_runs: [...runs, {
          run_id: delegated.child_run_id,
          child_thread_id: delegated.child_thread_id,
          turn_id: delegated.child_turn_id,
          agent_id: delegated.child_agent_id,
          created_by_run_id: delegated.created_by_run_id,
          status: 'pending',
          created_at: new Date(event.subagent.opened_at * 1000).toISOString()
        }] }
      }
      return false
    }
    if (type === 'yuxi.session.turn.state' && state.activeRunId && event.yuxi?.run_id !== state.activeRunId) return false
    applyAgentEvent(state.ongoingRunGroup, event)
    if (type.endsWith('output_text.delta') || type.endsWith('reasoning.delta')) {
      state.replyLoadingVisible = false
      streamSmoother?.updateText(event.item_id, threadId)
    } else if (type.endsWith('.done')) {
      streamSmoother?.flushThread(threadId)
    }
    if (type === 'yuxi.session.turn.capability_limited') {
      message.warning(event.message)
    }
    if (type === 'yuxi.session.turn.state') {
      state.agentStateRequestVersion = (state.agentStateRequestVersion || 0) + 1
      state.agentState = event.agent_state
    } else if (type === 'yuxi.session.turn.context_compression') {
      state.contextCompressing = event.compression?.status === 'started'
    } else if (type === 'yuxi.session.turn.waiting') {
      state.currentTurnId = event.turn_id
      state.activeRunId = event.yuxi.run_id
      state.turnStatus = 'waiting'
      state.replyLoadingVisible = false
      return processApprovalInStream?.(event, threadId, unref(currentAgentId)) || false
    }
    return false
  }
  return { handlePublicEvent }
}
