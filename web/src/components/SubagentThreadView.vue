<template>
  <div class="subagent-thread-view">
    <div ref="scrollContainerRef" class="subagent-thread-scroll" @scroll="handleScroll">
      <div ref="contentRef" class="subagent-thread-content">
        <div v-if="loading && !hasRenderableMessages" class="subagent-thread-state">
          正在加载子智能体消息...
        </div>
        <div v-if="error" class="subagent-thread-state is-error" role="alert">
          {{ error }}
          <button type="button" :disabled="loading" @click="loadThread">重试</button>
        </div>
        <ThreadMessageList
          v-if="hasRenderableMessages || (!loading && !error)"
          :messages="displayMessages"
          :runs="runs"
          :ongoing-messages="streamedMessages"
          :is-processing="streamActive"
        />
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { agentApi } from '@/apis'
import { dispatchRunEventChunks, processRunSseResponse } from '@/composables/useAgentRunStream'
import { getMessageRunId } from '@/utils/messageDebug'
import { useAgentStreamHandler } from '@/composables/useAgentStreamHandler'
import { useStreamSmoother } from '@/composables/useStreamSmoother'
import ThreadMessageList from '@/components/ThreadMessageList.vue'
import { MessageProcessor } from '@/utils/messageProcessor'
import ScrollController from '@/utils/scrollController'

const props = defineProps({
  threadId: { type: String, required: true },
  runId: { type: String, default: '' },
  active: { type: Boolean, default: false }
})

const RUN_TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled', 'interrupted'])
const loading = ref(false)
const error = ref('')
const messages = ref([])
const runs = ref([])
const currentRunId = ref('')
const currentRunStatus = ref('')
const streamActive = ref(false)
const lastEventId = ref(null)
const scrollContainerRef = ref(null)
const contentRef = ref(null)
const streamState = reactive({ threadStates: {} })
let streamAbortController = null
let runLookupAbortController = null
let resizeObserver = null
let reconnectTimer = null
let loadVersion = 0
let loadAbortController = null
let disposed = false

const normalizeRunStatus = (status) => String(status || '').trim()
const isTerminalRunStatus = (status) => RUN_TERMINAL_STATUSES.has(normalizeRunStatus(status))
const getStreamThreadState = (threadId) => {
  if (!streamState.threadStates[threadId]) {
    streamState.threadStates[threadId] = {
      isStreaming: false,
      replyLoadingVisible: false,
      pendingInputId: null,
      pendingInterrupt: null,
      onGoingConv: {
        msgChunks: {},
        currentInputKey: null,
        currentAssistantKey: null,
        toolCallBuffers: {}
      },
      agentState: null
    }
  }
  return streamState.threadStates[threadId]
}
const streamSmoother = useStreamSmoother({ getThreadState: getStreamThreadState })
const { handleStreamChunk } = useAgentStreamHandler({
  getThreadState: getStreamThreadState,
  processApprovalInStream: () => false,
  currentAgentId: ref(''),
  streamSmoother
})
const streamedMessages = computed(() => {
  const threadState = getStreamThreadState(props.threadId)
  const chunks = Object.values(threadState.onGoingConv.msgChunks)
    .map(MessageProcessor.mergeMessageChunk)
    .filter(Boolean)
  return chunks.length
    ? MessageProcessor.convertToolResultToMessages(chunks).filter(
        (message) => message.type !== 'tool'
      )
    : []
})
const displayMessages = computed(() => messages.value)
const hasRenderableMessages = computed(
  () => displayMessages.value.length > 0 || streamedMessages.value.length > 0
)
const scrollController = new ScrollController(() => scrollContainerRef.value, {
  threshold: 80,
  scrollDelay: 80
})
const handleScroll = (event) => {
  scrollController.handleScroll(event)
}

const flattenContent = (content) => {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return content ?? ''
  return content
    .filter((block) => block?.type === 'text')
    .map((block) => block.text || '')
    .join('')
}
const normalizeMessages = (items) =>
  (Array.isArray(items) ? items : []).map((message) => ({
    ...message,
    content: flattenContent(message.content)
  }))
const resetStreamState = () => {
  streamSmoother.resetThread(props.threadId)
  delete streamState.threadStates[props.threadId]
}
const stopRunStream = () => {
  streamAbortController?.abort()
  streamAbortController = null
  runLookupAbortController?.abort()
  runLookupAbortController = null
  streamActive.value = false
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
}
const scrollToBottom = async (force = false) => {
  if (!props.active) return
  await nextTick()
  if (force) await scrollController.scrollToBottomStaticForce()
  else await scrollController.scrollToBottom()
}
const loadThread = async () => {
  if (!props.threadId || !props.active || disposed) return
  const version = ++loadVersion
  loadAbortController?.abort()
  const controller = new AbortController()
  loadAbortController = controller
  const isCurrent = () => !disposed && props.active && version === loadVersion
  loading.value = true
  error.value = ''
  try {
    // 已知 Run 时无需加载 checkpoint；历史接口本身包含线程的 Run 列表。
    const history = await agentApi.getAgentHistory(props.threadId, { signal: controller.signal })
    if (!isCurrent()) return
    const threadRuns = history.runs || []
    const selectedRun = props.runId
      ? threadRuns.find((run) => run.run_id === props.runId)
      : threadRuns.at(-1)
    const run =
      props.runId && !selectedRun
        ? await agentApi.getAgentRun(props.threadId, props.runId, {
            signal: controller.signal
          })
        : selectedRun
    if (!isCurrent()) return
    currentRunId.value = run?.run_id || run?.id || props.runId || ''
    currentRunStatus.value = normalizeRunStatus(run?.status)
    runs.value = props.runId ? threadRuns.filter((item) => item.run_id === props.runId) : threadRuns
    messages.value = normalizeMessages(history.history || []).filter(
      (message) => !props.runId || getMessageRunId(message) === props.runId
    )
    if (!currentRunId.value || isTerminalRunStatus(currentRunStatus.value)) {
      stopRunStream()
      resetStreamState()
    } else {
      messages.value = messages.value.filter(
        (message) => getMessageRunId(message) !== currentRunId.value
      )
      void startRunStream(currentRunId.value, lastEventId.value, false)
    }
    await scrollToBottom(true)
  } catch (loadError) {
    if (!isCurrent() || loadError?.name === 'AbortError') return
    const status = loadError?.response?.status || loadError?.status
    error.value =
      status === 404 ? '子智能体记录不存在或已无权访问。' : '暂时无法加载子智能体消息，请重试。'
  } finally {
    if (isCurrent()) loading.value = false
  }
}
const scheduleReconnect = (runId) => {
  if (disposed || !props.active || reconnectTimer) return
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    void startRunStream(runId, lastEventId.value, false)
  }, 1000)
}
const startRunStream = async (runId, afterSeq = null, resetMessages = false) => {
  stopRunStream()
  if (disposed || !props.active || !runId) return
  if (resetMessages) resetStreamState()
  const controller = new AbortController()
  streamAbortController = controller
  streamActive.value = true
  getStreamThreadState(props.threadId).isStreaming = true
  let terminalSeen = false

  try {
    const response = await agentApi.streamThreadEvents(props.threadId, afterSeq, {
      signal: controller.signal
    })
    if (!response.ok) throw new Error(`SSE response not ok: ${response.status}`)
    await processRunSseResponse(response, (event, data, eventId) => {
      if (controller.signal.aborted || !props.active || !data) return
      if (eventId) lastEventId.value = String(eventId)
      if (data.type === 'agent.thread.resync') {
        void loadThread()
        return
      }
      if (data.run_id !== runId) return
      const payload = data.payload || {}
      const isRetryableError =
        event === 'error' && (payload.retryable === true || payload.chunk?.retryable === true)
      if (isRetryableError) return
      dispatchRunEventChunks({
        data,
        runId,
        fallbackThreadId: props.threadId,
        onChunk: (chunk, threadId) => {
          if (threadId !== props.threadId) return
          handleStreamChunk(chunk, threadId)
        }
      })
      if (data.type?.startsWith('agent.thread.run.') &&
          isTerminalRunStatus(data.type.split('.').at(-1))) {
        terminalSeen = true
        streamActive.value = false
        controller.abort()
      }
    })
  } catch (streamError) {
    if (streamError?.name !== 'AbortError') {
      console.error('Failed to stream subagent run messages:', streamError)
    }
  } finally {
    if (streamAbortController === controller) streamActive.value = false
    if ((terminalSeen || !controller.signal.aborted) && !disposed && props.active) {
      streamSmoother.flushThread(props.threadId)
      const lookupController = new AbortController()
      runLookupAbortController = lookupController
      try {
        const runResponse = await agentApi.getAgentRun(props.threadId, runId, {
          signal: lookupController.signal
        })
        if (!disposed && props.active) {
          const status = normalizeRunStatus(runResponse?.status)
          if (isTerminalRunStatus(status)) await loadThread()
          else scheduleReconnect(runId)
        }
      } catch {
        if (!disposed && props.active) scheduleReconnect(runId)
      } finally {
        if (runLookupAbortController === lookupController) runLookupAbortController = null
      }
    }
    if (streamAbortController === controller) streamAbortController = null
  }
}

watch([() => props.threadId, () => props.runId], () => {
  stopRunStream()
  resetStreamState()
  lastEventId.value = null
  messages.value = []
  loadThread()
})
watch(
  () => props.active,
  (active) => {
    if (active) loadThread()
    else {
      loadVersion += 1
      loadAbortController?.abort()
      stopRunStream()
      streamSmoother.flushThread(props.threadId)
    }
  }
)
watch(streamedMessages, () => scrollToBottom(), { deep: true, flush: 'post' })

onMounted(() => {
  loadThread()
  if (typeof ResizeObserver !== 'undefined' && contentRef.value) {
    resizeObserver = new ResizeObserver(() => scrollToBottom())
    resizeObserver.observe(contentRef.value)
  }
})
onUnmounted(() => {
  disposed = true
  loadVersion += 1
  loadAbortController?.abort()
  stopRunStream()
  resetStreamState()
  resizeObserver?.disconnect()
  scrollController.reset()
})
</script>

<style scoped lang="less">
.subagent-thread-view,
.subagent-thread-scroll {
  width: 100%;
  height: 100%;
  min-height: 0;
}

.subagent-thread-scroll {
  overflow-y: auto;
  padding: 16px 28px 28px;
}

.subagent-thread-content {
  width: min(100%, 800px);
  min-height: 100%;
  margin: 0 auto;
}

.subagent-thread-state {
  padding: 32px 0;
  color: var(--gray-500);
  font-size: 13px;
  text-align: center;

  &.is-error {
    color: var(--color-error-600);
  }
}
</style>
