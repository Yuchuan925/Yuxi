import { unref } from 'vue'
import { agentApi } from '@/apis'
import { handleChatError } from '@/shared/lib/errorHandler'
import { getReconnectDelay } from './reconnectBackoff.js'

export const processRunSseResponse = async (response, onEvent) => {
  if (!response || !response.body) return
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let eventType = 'message'
  let eventId = null
  let dataLines = []

  const dispatch = async () => {
    if (dataLines.length === 0) return
    const dataText = dataLines.join('\n')
    let parsed
    try {
      parsed = JSON.parse(dataText)
    } catch (e) {
      console.warn('Failed to parse run SSE data:', e, dataText)
      return
    }
    await onEvent(eventType, parsed, eventId)
  }

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const rawLine of lines) {
        const line = rawLine.replace(/\r$/, '')
        if (!line) {
          await dispatch()
          eventType = 'message'
          eventId = null
          dataLines = []
          continue
        }

        if (line.startsWith(':')) {
          continue
        }
        if (line.startsWith('event:')) {
          eventType = line.slice(6).trim() || 'message'
        } else if (line.startsWith('data:')) {
          dataLines.push(line.slice(5).trimStart())
        } else if (line.startsWith('id:')) {
          eventId = line.slice(3).trim()
        }
      }
    }

    await dispatch()
  } finally {
    try {
      reader.releaseLock()
    } catch {
      // ignore
    }
  }
}

export function useAgentRunStream({
  getThreadState,
  currentAgentId,
  handlePublicEvent,
  fetchThreadMessages,
  fetchAgentState,
  resetOngoingRunGroup,
  onScrollToBottom,
  streamSmoother,
  onInterruptDetected = null,
  onTerminalDetected = null,
  onRunStarted = null
}) {
  const stopRunStreamSubscription = (threadId) => {
    const ts = getThreadState(threadId)
    if (!ts) return
    streamSmoother?.flushThread(threadId)
    ts.runStreamAbortController?.abort()
    ts.runStreamAbortController = null
    clearTimeout(ts.runReconnectTimer)
    ts.runReconnectTimer = null
  }

  const settleTurn = (threadId, turnId, status, runId, touchedThreadIds) => {
    const ts = getThreadState(threadId)
    if (!ts || ts.currentTurnId !== turnId) return
    const generation = ts.runStreamGeneration
    streamSmoother?.flushThread(threadId)
    ts.runStreamAbortController?.abort()
    ts.runStreamAbortController = null
    ts.isStreaming = false
    ts.turnStatus = status
    ts.activeRunSteerable = false
    ts.replyLoadingVisible = false
    ts.pendingInputId = null
    ts.runReconnectAttempts = 0
    if (status === 'waiting') {
      ts.activeRunId = runId
    } else {
      ts.activeRunId = null
      ts.currentTurnId = null
      ts.pendingInterrupt = null
    }
    void fetchThreadMessages({ agentId: unref(currentAgentId), threadId, delay: 0 })
      .finally(() => {
        if (ts.runStreamGeneration !== generation) return
        if (status !== 'waiting') resetOngoingRunGroup(threadId, { preserveInputMonitors: true })
        fetchAgentState(unref(currentAgentId), threadId)
        if (status === 'waiting') onInterruptDetected?.({ threadId, runId })
        else onTerminalDetected?.({ threadId, runId, touchedThreadIds: [...touchedThreadIds] })
        if (status === 'completed') onScrollToBottom?.()
      })
  }

  const reconcileTurn = async (threadId, turnId, fallbackRunId) => {
    const turn = await agentApi.getThreadTurn(threadId, turnId)
    const ts = getThreadState(threadId)
    if (!ts || ts.currentTurnId !== turnId) return true
    const currentRunId = turn.current_run_id || fallbackRunId
    if (['waiting', 'completed', 'failed', 'cancelled'].includes(turn.status)) {
      settleTurn(threadId, turnId, turn.status, currentRunId, new Set([threadId]))
      return true
    }
    ts.activeRunId = currentRunId
    ts.turnStatus = 'running'
    ts.activeRunSteerable = true
    return false
  }

  const startRunStream = async (threadId, runId, afterCursor = null, options = {}) => {
    if (!threadId) return
    const ts = getThreadState(threadId)
    if (!ts) return
    const turnId = options.turnId || ts.currentTurnId ||
      (await agentApi.getPublicThread(threadId)).current_turn?.turn_id
    if (!turnId) return

    stopRunStreamSubscription(threadId)
    ts.runStreamGeneration = (ts.runStreamGeneration || 0) + 1
    const controller = new AbortController()
    ts.runStreamAbortController = controller
    ts.currentTurnId = turnId
    ts.activeRunId = runId
    ts.turnStatus = 'running'
    ts.pendingInterrupt = null
    ts.activeRunSteerable = true
    ts.isStreaming = true
    if (options.inputId) ts.pendingInputId = options.inputId
    onRunStarted?.({ threadId, runId, inputId: options.inputId })

    let sawTurnEnd = false
    try {
      const response = await agentApi.streamThreadEvents(threadId, afterCursor || ts.threadCursor, {
        signal: controller.signal
      })
      if (!response.ok) throw new Error(`SSE response not ok: ${response.status}`)
      await processRunSseResponse(response, async (_event, data, eventId) => {
        if (!data || controller.signal.aborted || ts.currentTurnId !== turnId) return
        ts.runReconnectAttempts = 0
        if (eventId) ts.threadCursor = String(eventId)
        if (data.type === 'agent.session.subagent.created') {
          if (data.yuxi?.session_id === threadId && data.yuxi?.turn_id === turnId) handlePublicEvent(data, threadId)
          return
        }
        if (data.session_id !== threadId) return
        if (data.type === 'yuxi.session.resync') {
          // 暂停消费，让 ReadableStream 缓冲新事件，快照应用后再继续合并。
          await fetchThreadMessages({ agentId: unref(currentAgentId), threadId, delay: 0 })
          sawTurnEnd = await reconcileTurn(threadId, turnId, ts.activeRunId)
          return
        }
        if (data.turn_id !== turnId) return
        const status = data.type === 'yuxi.session.turn.waiting' ? 'waiting'
          : data.type.startsWith('agent.session.turn.') ? data.type.split('.').at(-1) : null
        const owner = data.yuxi?.current_run_id || data.current_run_id
        if (['waiting', 'completed', 'failed', 'cancelled', 'in_progress', 'created'].includes(status)) {
          if (owner && data.yuxi?.run_id !== owner) return
          if (data.yuxi?.run_id !== ts.activeRunId) {
            const turn = await agentApi.getThreadTurn(threadId, turnId)
            ts.activeRunId = turn.current_run_id
            if (data.yuxi?.run_id !== turn.current_run_id) return
          }
        }
        handlePublicEvent(data, threadId)
        if (['waiting', 'completed', 'failed', 'cancelled'].includes(status)) {
          sawTurnEnd = true
          settleTurn(threadId, turnId, status, data.yuxi?.run_id || ts.activeRunId, new Set([threadId]))
        }
      })
      if (!sawTurnEnd && !controller.signal.aborted) {
        sawTurnEnd = await reconcileTurn(threadId, turnId, runId)
      }
    } catch (error) {
      if (error?.name !== 'AbortError') {
        console.error('Thread SSE stream error:', error)
        try {
          sawTurnEnd = await reconcileTurn(threadId, turnId, runId)
        } catch (lookupError) {
          console.warn('Failed to reconcile Thread after SSE error:', lookupError)
        }
        if (!sawTurnEnd) handleChatError(error, 'stream')
      }
    } finally {
      if (ts.runStreamAbortController === controller) ts.runStreamAbortController = null
      if (!sawTurnEnd && !controller.signal.aborted && ts.currentTurnId === turnId) {
        const reconnectAttempt = ts.runReconnectAttempts || 0
        ts.runReconnectAttempts = reconnectAttempt + 1
        ts.runReconnectTimer = setTimeout(() => {
          ts.runReconnectTimer = null
          if (!controller.signal.aborted && ts.currentTurnId === turnId && !ts.runStreamAbortController) {
            void startRunStream(threadId, ts.activeRunId, ts.threadCursor, { turnId })
          }
        }, getReconnectDelay(reconnectAttempt))
      }
    }
  }

  const resumeActiveRunForThread = async (threadId) => {
    if (!threadId) return
    const ts = getThreadState(threadId)
    if (!ts) return
    const thread = await agentApi.getPublicThread(threadId)
    const turnId = thread.current_turn?.turn_id
    if (!turnId) {
      ts.activeRunId = null
      ts.currentTurnId = null
      ts.turnStatus = null
      ts.isStreaming = false
      return
    }
    const turn = await agentApi.getThreadTurn(threadId, turnId)
    ts.currentTurnId = turnId
    ts.activeRunId = turn.current_run_id
    ts.turnStatus = turn.status
    if (turn.status === 'waiting') {
      ts.isStreaming = false
      ts.activeRunSteerable = false
      onInterruptDetected?.({ threadId, runId: turn.current_run_id, turn })
    } else if (turn.status === 'running') {
      if (!ts.runStreamAbortController) {
        void startRunStream(threadId, turn.current_run_id, ts.threadCursor, { turnId })
      }
    } else {
      settleTurn(threadId, turnId, turn.status, turn.current_run_id, new Set([threadId]))
    }
  }

  return { startRunStream, resumeActiveRunForThread, stopRunStreamSubscription }
}
