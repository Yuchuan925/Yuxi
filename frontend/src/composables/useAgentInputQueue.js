import { agentApi } from '@/apis'
import { IDLE_QUEUE_SNAPSHOT } from '@/composables/useAgentThreadState'
import { handleChatError } from '@/utils/errorHandler'

const controlKey = () =>
  typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `input-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`

/** 将持久 Input 队列投影到当前 Thread，并跟踪领取结果。 */
export function useAgentInputQueue({
  getThreadState,
  resetOnGoingConv,
  startRunStream,
  onStreamError
}) {
  const stopInputMonitor = (threadId, inputId) => {
    const ts = getThreadState(threadId)
    const entry = ts?.inputMonitors?.[inputId]
    if (!entry) return
    clearTimeout(entry.timer)
    entry.controller.abort()
    delete ts.inputMonitors[inputId]
  }

  const stopAllInputMonitors = (threadId) => {
    for (const inputId of Object.keys(getThreadState(threadId)?.inputMonitors || {})) {
      stopInputMonitor(threadId, inputId)
    }
  }

  const removeInput = (ts, inputId) => {
    if (ts) ts.queuedInputs = (ts.queuedInputs || []).filter((item) => item.input_id !== inputId)
  }

  const startInputMonitor = (threadId, inputId) => {
    const ts = getThreadState(threadId)
    if (!ts || !inputId || ts.inputMonitors?.[inputId]) return
    ts.inputMonitors ||= {}
    const entry = { controller: new AbortController(), timer: null }
    ts.inputMonitors[inputId] = entry

    const poll = async () => {
      if (entry.controller.signal.aborted) return
      try {
        const input = await agentApi.getThreadInput(threadId, inputId)
        if (entry.controller.signal.aborted) return
        if (input.status === 'consumed' && input.run_id) {
          const state = getThreadState(threadId)
          const message = state?.queuedInputs?.find((item) => item.input_id === inputId)?.message
          removeInput(state, inputId)
          stopInputMonitor(threadId, inputId)
          if (!state?.activeRunId) resetOnGoingConv(threadId, { preserveInputMonitors: true })
          if (message && state?.onGoingConv?.msgChunks) {
            state.onGoingConv.msgChunks[inputId] = [message]
          }
          state.pendingInputId = inputId
          void startRunStream(threadId, input.run_id, null, {
            turnId: input.turn_id, inputId
          })
          return
        }
        if (input.status === 'cancelled') {
          const state = getThreadState(threadId)
          removeInput(state, inputId)
          if (state?.onGoingConv?.msgChunks) delete state.onGoingConv.msgChunks[inputId]
          stopInputMonitor(threadId, inputId)
          onStreamError?.(threadId, inputId, 'cancelled')
          return
        }
      } catch (error) {
        if (error?.status >= 400 && error.status < 500 && error.status !== 429) {
          stopInputMonitor(threadId, inputId)
          onStreamError?.(threadId, inputId, 'unavailable')
          handleChatError(error, 'stream')
          return
        }
        entry.timer = setTimeout(poll, 5000)
        return
      }
      entry.timer = setTimeout(poll, 1000)
    }
    void poll()
  }

  const syncQueuedInputs = async (threadId) => {
    const ts = getThreadState(threadId)
    if (!ts) return
    try {
      const snapshot = await agentApi.getThreadQueue(threadId)
      const inputs = snapshot?.inputs || []
      const knownIds = new Set(inputs.map((input) => input.input_id))
      ts.queuedInputs = [
        ...inputs.map((input) => {
          const existing = ts.queuedInputs?.find((item) => item.input_id === input.input_id)
          return {
            ...input,
            content: input.content || existing?.content,
            message: existing?.message
          }
        }),
        ...(ts.queuedInputs || []).filter((input) =>
          !knownIds.has(input.input_id) &&
          (input.status === 'sending' || (input.kind === 'steer' && ts.inputMonitors?.[input.input_id]))
        )
      ]
      ts.queueSnapshot = snapshot || { ...IDLE_QUEUE_SNAPSHOT }
      for (const input of inputs) startInputMonitor(threadId, input.input_id)
    } catch (error) {
      console.warn('Failed to sync queued inputs:', error)
    }
  }

  const resumeQueuedInputs = async (threadId) => {
    if (threadId) await syncQueuedInputs(threadId)
  }

  const cancelInput = async (threadId, inputId) => {
    const ts = getThreadState(threadId)
    if (!ts || !inputId || ts.queuedInputs?.some(
      (input) => input.input_id === inputId && input.status === 'sending'
    )) return false
    try {
      await agentApi.cancelThreadInput(threadId, inputId, controlKey())
      stopInputMonitor(threadId, inputId)
      removeInput(ts, inputId)
      if (ts.onGoingConv?.msgChunks) delete ts.onGoingConv.msgChunks[inputId]
      return true
    } catch (error) {
      handleChatError(error, 'cancel')
      return false
    }
  }

  const continueQueue = async (threadId) => {
    const ts = getThreadState(threadId)
    if (!ts || ts.continueQueueInFlight) return false
    ts.continueQueueInFlight = true
    try {
      await agentApi.continueThreadQueue(threadId, controlKey())
      await syncQueuedInputs(threadId)
      return true
    } catch (error) {
      handleChatError(error, 'continue_queue')
      return false
    } finally {
      ts.continueQueueInFlight = false
    }
  }

  return {
    stopAllInputMonitors,
    startInputMonitor,
    cancelInput,
    syncQueuedInputs,
    resumeQueuedInputs,
    continueQueue
  }
}
