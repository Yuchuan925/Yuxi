import { unref } from 'vue'
import { agentApi } from '@/apis'
import { handleChatError } from '@/utils/errorHandler'

const getThreadIdFromObject = (value) => {
  if (!value || typeof value !== 'object') return ''
  if (typeof value.thread_id === 'string' && value.thread_id.trim()) return value.thread_id.trim()
  const nestedSources = [value.meta, value.metadata, value.configurable, value.stream_event]
  for (const source of nestedSources) {
    const nestedThreadId = getThreadIdFromObject(source)
    if (nestedThreadId) return nestedThreadId
  }
  return ''
}

const resolveChunkThreadId = ({ envelope, payload, chunk, fallbackThreadId }) => {
  return (
    getThreadIdFromObject(chunk) ||
    getThreadIdFromObject(payload) ||
    getThreadIdFromObject(envelope) ||
    fallbackThreadId
  )
}

export function dispatchRunEventChunks({
  data,
  runId,
  fallbackThreadId,
  streamRunId = null,
  streamThreadId = null,
  onChunk
}) {
  if (typeof onChunk !== 'function') return
  const payload = data?.payload || {}
  const chunks = Array.isArray(payload.items) ? payload.items : payload.chunk ? [payload.chunk] : []
  chunks.forEach((chunk) => {
    const routeThreadId = resolveChunkThreadId({
      envelope: data,
      payload,
      chunk,
      fallbackThreadId
    })
    onChunk(
      {
        ...chunk,
        input_id: chunk.input_id || data?.input_id,
        run_id: chunk.run_id || data?.run_id || runId,
        thread_id: routeThreadId,
        ...(streamRunId ? { stream_run_id: streamRunId } : {}),
        ...(streamThreadId ? { stream_thread_id: streamThreadId } : {})
      },
      routeThreadId
    )
  })
}

export const processRunSseResponse = async (response, onEvent) => {
  if (!response || !response.body) return
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let eventType = 'message'
  let eventId = null
  let dataLines = []

  const dispatch = () => {
    if (dataLines.length === 0) return
    const dataText = dataLines.join('\n')
    try {
      const parsed = JSON.parse(dataText)
      onEvent(eventType, parsed, eventId)
    } catch (e) {
      console.warn('Failed to parse run SSE data:', e, dataText)
    }
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
          dispatch()
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

    dispatch()
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
  handleStreamChunk,
  fetchThreadMessages,
  fetchAgentState,
  resetOnGoingConv,
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
    streamSmoother?.flushThread(threadId)
    ts.runStreamAbortController?.abort()
    ts.runStreamAbortController = null
    ts.isStreaming = false
    ts.turnStatus = status
    ts.activeRunSteerable = false
    ts.replyLoadingVisible = false
    ts.pendingInputId = null
    if (status === 'waiting') {
      ts.activeRunId = runId
    } else {
      ts.activeRunId = null
      ts.currentTurnId = null
      ts.pendingInterrupt = null
    }
    void fetchThreadMessages({ agentId: unref(currentAgentId), threadId, delay: 0 })
      .finally(() => {
        if (status !== 'waiting') resetOnGoingConv(threadId, { preserveInputMonitors: true })
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

    const touchedThreadIds = new Set([threadId])
    let sawTurnEnd = false
    // 首次全流会回放旧 Run；见到本次 Run 后才允许同 Turn 的后继 steer Run。
    let replayingEarlierRuns = !afterCursor && !ts.threadCursor && Boolean(runId)
    try {
      const response = await agentApi.streamThreadEvents(threadId, afterCursor || ts.threadCursor, {
        signal: controller.signal
      })
      if (!response.ok) throw new Error(`SSE response not ok: ${response.status}`)
      await processRunSseResponse(response, (event, data, eventId) => {
        if (!data || controller.signal.aborted || ts.currentTurnId !== turnId) return
        if (eventId || data.cursor) ts.threadCursor = String(eventId || data.cursor)
        if (data.type === 'agent.thread.resync') {
          void fetchThreadMessages({ agentId: unref(currentAgentId), threadId, delay: 0 })
            .catch((error) => console.warn('Failed to refresh Thread after resync:', error))
          void reconcileTurn(threadId, turnId, ts.activeRunId)
            .catch((error) => console.warn('Failed to reconcile Turn after resync:', error))
          return
        }
        if (data.turn_id !== turnId) return
        const type = data.type || event
        const status = type.startsWith('agent.thread.turn.') ? type.split('.').at(-1) : null
        const runEvent = type === 'agent.thread.output' || type === 'agent.thread.input.consumed' ||
          type.startsWith('agent.thread.run.') ||
          ['waiting', 'completed', 'failed', 'cancelled'].includes(status)
        if (replayingEarlierRuns && runEvent) {
          if (data.run_id !== runId) return
          replayingEarlierRuns = false
        }
        if (data.run_id && runEvent) ts.activeRunId = data.run_id
        dispatchRunEventChunks({
          data,
          runId: data.run_id || runId,
          fallbackThreadId: threadId,
          streamRunId: data.run_id || runId,
          streamThreadId: threadId,
          onChunk: (chunk, routeThreadId) => {
            touchedThreadIds.add(routeThreadId)
            handleStreamChunk(chunk, routeThreadId)
          }
        })
        if (['waiting', 'completed', 'failed', 'cancelled'].includes(status)) {
          sawTurnEnd = true
          settleTurn(threadId, turnId, status, data.run_id || ts.activeRunId, touchedThreadIds)
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
        ts.runReconnectTimer = setTimeout(() => {
          ts.runReconnectTimer = null
          if (!controller.signal.aborted && ts.currentTurnId === turnId && !ts.runStreamAbortController) {
            void startRunStream(threadId, ts.activeRunId, ts.threadCursor, { turnId })
          }
        }, 1000)
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
