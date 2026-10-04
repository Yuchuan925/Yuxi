import { agentApi } from '@/apis'
import { mergeItemSnapshot } from '@/modules/session/model/agentItems'
import { IDLE_QUEUE_SNAPSHOT } from '@/modules/session/model/useAgentThreadState'
import { handleChatError } from '@/shared/lib/errorHandler'

const controlKey = () =>
  typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `input-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`

/** 将持久 Input 队列投影到当前 Thread，并跟踪领取结果。 */
export function useAgentInputQueue({
  getThreadState,
  resetOngoingRunGroup,
  startRunStream,
  onStreamError
}) {
  const stopInputQueueMonitor = (ts) => {
    if (!ts?.inputQueueMonitor) return
    clearTimeout(ts.inputQueueMonitor.timer)
    ts.inputQueueMonitor.controller.abort()
    ts.inputQueueMonitor = null
  }

  const stopInputMonitor = (threadId, inputId) => {
    const ts = getThreadState(threadId)
    const entry = ts?.inputMonitors?.[inputId]
    if (!entry) return
    delete ts.inputMonitors[inputId]
    if (Object.keys(ts.inputMonitors).length === 0) stopInputQueueMonitor(ts)
  }

  const stopAllInputMonitors = (threadId) => {
    const ts = getThreadState(threadId)
    if (!ts) return
    for (const entry of Object.values(ts.inputMonitors || {})) {
      clearTimeout(entry.timer)
      entry.controller?.abort()
    }
    ts.inputMonitors = {}
    stopInputQueueMonitor(ts)
  }

  const removeInput = (ts, inputId) => {
    if (ts) ts.queuedInputs = (ts.queuedInputs || []).filter((item) => item.input_id !== inputId)
  }

  const startInputMonitor = (threadId, inputId) => {
    const ts = getThreadState(threadId)
    if (!ts || !inputId || ts.inputMonitors?.[inputId]) return
    ts.inputMonitors ||= {}
    ts.inputQueueMonitor ||= { controller: new AbortController(), timer: null }
    const monitor = ts.inputQueueMonitor
    ts.inputMonitors[inputId] = { controller: monitor.controller, timer: null }
    if (Object.keys(ts.inputMonitors).length > 1) return

    const poll = async () => {
      if (monitor.controller.signal.aborted) return
      let retryDelay = 1000
      for (const currentInputId of Object.keys(ts.inputMonitors)) {
        if (monitor.controller.signal.aborted) return
        try {
          const input = await agentApi.getThreadInput(threadId, currentInputId, {
            signal: monitor.controller.signal
          })
          if (monitor.controller.signal.aborted) return
          if (input.status === 'consumed' && input.run_id) {
            const state = getThreadState(threadId)
            removeInput(state, currentInputId)
            stopInputMonitor(threadId, currentInputId)
            if (!state?.activeRunId) resetOngoingRunGroup(threadId, { preserveInputMonitors: true })
            mergeItemSnapshot(state.ongoingRunGroup, input.items || [])
            delete state.ongoingRunGroup.optimisticMessages[currentInputId]
            state.pendingInputId = currentInputId
            void startRunStream(threadId, input.run_id, null, {
              turnId: input.turn_id, inputId: currentInputId
            })
            continue
          }
          if (input.status === 'cancelled') {
            const state = getThreadState(threadId)
            removeInput(state, currentInputId)
            if (state?.ongoingRunGroup?.optimisticMessages) {
              delete state.ongoingRunGroup.optimisticMessages[currentInputId]
            }
            stopInputMonitor(threadId, currentInputId)
            onStreamError?.(threadId, currentInputId, 'cancelled')
          }
        } catch (error) {
          if (error?.name === 'AbortError') return
          if (error?.status >= 400 && error.status < 500 && error.status !== 429) {
            stopInputMonitor(threadId, currentInputId)
            onStreamError?.(threadId, currentInputId, 'unavailable')
            handleChatError(error, 'stream')
          } else {
            retryDelay = 5000
          }
        }
      }
      if (!monitor.controller.signal.aborted && Object.keys(ts.inputMonitors).length > 0) {
        monitor.timer = setTimeout(poll, retryDelay)
      } else {
        stopInputQueueMonitor(ts)
      }
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
          input.status === 'sending'
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
      if (ts.ongoingRunGroup?.optimisticMessages) delete ts.ongoingRunGroup.optimisticMessages[inputId]
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
