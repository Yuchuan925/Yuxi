import { reactive, ref, onScopeDispose } from 'vue'
import { defineStore } from 'pinia'
import { agentApi } from '@/apis'
import { handleChatError } from '@/shared/lib/errorHandler'
import { useChatThreadsStore } from './chatThreads'
import { useAgentThreadState } from './useAgentThreadState'
import { useAgentRunStream } from './useAgentRunStream'
import { useAgentInputQueue } from './useAgentInputQueue'
import { useAgentStreamHandler } from './useAgentStreamHandler'
import { useStreamSmoother } from './useStreamSmoother'
import { itemsToMessages, mergeItemSnapshot } from './agentItems'
import { bindMessageInputRun } from './messageDebug'
import { extractPendingInterrupt, pendingInterruptFromWaitpoint } from './useApproval'

/** 每个 Thread 共享运行数据和订阅，视图仅注册展示回调。 */
export const useSessionRuntimeStore = defineStore('sessionRuntime', () => {
  const chatThreads = useChatThreadsStore()
  const chatState = reactive({ threadStates: {} })
  const threadMessages = ref({})
  const threadRuns = ref({})
  const observers = new Map()
  const requests = new Map()
  const streamSmoother = useStreamSmoother({
    getThreadState: (id) => chatState.threadStates[id] || null
  })
  const { getThreadState, cleanupThreadState, resetOngoingRunGroup } = useAgentThreadState({
    chatState,
    onBeforeResetThread: streamSmoother.resetThread,
    onBeforeCleanupThread: streamSmoother.resetThread
  })

  /** 同一类在途读取共用 Promise，释放视图后旧结果不能写回新状态。 */
  const once = (key, action) => {
    if (requests.has(key)) return requests.get(key)
    const request = Promise.resolve()
      .then(action)
      .finally(() => {
        if (requests.get(key) === request) requests.delete(key)
      })
    requests.set(key, request)
    return request
  }
  const notify = (name, payload) => {
    for (const observer of observers.get(payload.threadId) || []) observer[name]?.(payload)
  }
  const isCurrent = (threadId, state) => chatState.threadStates[threadId] === state

  const fetchThreadMessages = async ({ threadId, fresh = false }) => {
    if (!threadId || !observers.has(threadId)) return
    const state = getThreadState(threadId)
    if (fresh && requests.has(`history:${threadId}`)) {
      // 终态必须读取发生在完成事件之后的快照，不能共用更早的在途历史。
      try {
        await requests.get(`history:${threadId}`)
      } catch {
        /* 新请求负责重试。 */
      }
      if (!observers.has(threadId) || !isCurrent(threadId, state)) return
    }
    return once(`history:${threadId}`, async () => {
      if (!observers.has(threadId)) return
      const state = getThreadState(threadId)
      try {
        const response = await agentApi.getAgentHistory(threadId)
        if (!isCurrent(threadId, state)) return
        mergeItemSnapshot(state.ongoingRunGroup, response.items || [])
        streamSmoother.flushThread(threadId)
        threadMessages.value[threadId] = itemsToMessages(response.items || [])
        threadRuns.value[threadId] = response.runs
        if (!state.isStreaming) state.turnStatus = response.thread?.current_turn?.status || null
        chatThreads.upsertThread(response.thread)
      } catch (error) {
        handleChatError(error, 'load')
        throw error
      }
    })
  }

  const fetchAgentState = async (_agentId, threadId, { required = false } = {}) => {
    const success = await once(`state:${threadId}`, async () => {
      if (!threadId) return false
      const state = getThreadState(threadId)
      const runVersion = state.runStateVersion
      const version = (state.agentStateRequestVersion || 0) + 1
      state.agentStateRequestVersion = version
      try {
        const response = await agentApi.getAgentState(threadId, { includeRelations: false })
        if (
          !isCurrent(threadId, state) ||
          state.agentStateRequestVersion !== version ||
          state.runStateVersion !== runVersion
        )
          return false
        state.agentState = response.agent_state || null
        const interrupt = extractPendingInterrupt(response.interrupt, threadId)
        if (
          interrupt &&
          !state.isStreaming &&
          (!interrupt.interruptedRunId ||
            !state.activeRunId ||
            interrupt.interruptedRunId === state.activeRunId)
        ) {
          state.pendingInterrupt = interrupt
          notify('onInterruptDetected', { threadId })
        }
        return true
      } catch (error) {
        if (required) throw error
        return false
      }
    })
    if (required && (!success || !getThreadState(threadId)?.pendingInterrupt)) {
      throw new Error('checkpoint 中没有可恢复的审批状态')
    }
    return success
  }

  const { handlePublicEvent } = useAgentStreamHandler({
    getThreadState,
    streamSmoother,
    processApprovalInStream: (event, threadId) => {
      const state = getThreadState(threadId)
      state.pendingInterrupt = pendingInterruptFromWaitpoint(event.waitpoint, threadId)
      notify('onInterruptDetected', { threadId })
      return Boolean(state.pendingInterrupt)
    }
  })
  const stream = useAgentRunStream({
    getThreadState,
    isThreadActive: (threadId) => observers.has(threadId),
    handlePublicEvent,
    fetchThreadMessages,
    fetchAgentState,
    resetOngoingRunGroup,
    streamSmoother,
    onScrollToBottom: (threadId) => notify('onScrollToBottom', { threadId }),
    onInterruptDetected: ({ threadId, turn }) => {
      void (async () => {
        const state = getThreadState(threadId)
        const turnId = state.currentTurnId
        const version = state.runStateVersion
        const waiting = turn || (turnId && (await agentApi.getThreadTurn(threadId, turnId)))
        if (
          !isCurrent(threadId, state) ||
          state.currentTurnId !== turnId ||
          state.runStateVersion !== version ||
          waiting?.status !== 'waiting'
        )
          return
        state.pendingInterrupt = pendingInterruptFromWaitpoint(waiting.waitpoint, threadId)
        notify('onInterruptDetected', { threadId })
      })().catch((error) => console.warn('Failed to restore Turn waitpoint:', error))
      void resumeQueuedInputs(threadId)
    },
    onTerminalDetected: (payload) => {
      void resumeQueuedInputs(payload.threadId)
      notify('onTerminalDetected', payload)
    },
    onRunStarted: ({ threadId, runId, inputId }) => {
      const chunks = getThreadState(threadId)?.ongoingRunGroup?.optimisticMessages || {}
      bindMessageInputRun(Object.values(chunks).flat(), inputId, runId)
      bindMessageInputRun(threadMessages.value[threadId], inputId, runId)
      chatThreads.setThreadStatus(threadId, 'loading')
    }
  })
  const queue = useAgentInputQueue({
    getThreadState,
    isThreadActive: (threadId) => observers.has(threadId),
    resetOngoingRunGroup,
    startRunStream: stream.startRunStream
  })
  const resumeQueuedInputs = (threadId) =>
    once(`queue:${threadId}`, () => queue.resumeQueuedInputs(threadId))
  const resumeActiveRunForThread = (threadId) =>
    once(`resume:${threadId}`, () => stream.resumeActiveRunForThread(threadId))

  /** 最后一个视图释放时才终止观察；隐藏视图仍保有运行状态。 */
  const observeThread = (threadId, observer) => {
    if (!threadId) return () => {}
    if (!observers.has(threadId)) observers.set(threadId, new Set())
    const listeners = observers.get(threadId)
    listeners.add(observer)
    getThreadState(threadId)
    return () => {
      listeners.delete(observer)
      if (listeners.size || observers.get(threadId) !== listeners) return
      observers.delete(threadId)
      cleanupThreadState(threadId)
      delete threadMessages.value[threadId]
      delete threadRuns.value[threadId]
      for (const key of requests.keys()) {
        if (key.endsWith(`:${threadId}`)) requests.delete(key)
      }
    }
  }
  onScopeDispose(() => {
    for (const id of Object.keys(chatState.threadStates)) cleanupThreadState(id)
    observers.clear()
    requests.clear()
  })
  return {
    threadMessages,
    threadRuns,
    getThreadState,
    resetOngoingRunGroup,
    fetchThreadMessages,
    fetchAgentState,
    observeThread,
    startRunStream: stream.startRunStream,
    resumeActiveRunForThread,
    resumeQueuedInputs,
    startInputMonitor: queue.startInputMonitor,
    cancelInput: queue.cancelInput,
    continueQueue: queue.continueQueue
  }
})
