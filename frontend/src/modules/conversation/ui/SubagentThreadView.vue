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
import { computed, inject, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { agentApi } from '@/apis'
import { processRunSseResponse } from '@/modules/conversation/model/useAgentRunStream'
import { useAgentStreamHandler } from '@/modules/conversation/model/useAgentStreamHandler'
import { useStreamSmoother } from '@/modules/conversation/model/useStreamSmoother'
import { itemsToMessages, mergeItemSnapshot } from '@/modules/conversation/model/agentItems'
import ThreadMessageList from '@/modules/conversation/ui/ThreadMessageList.vue'
import ScrollController from '@/modules/conversation/model/scrollController'

const props = defineProps({
  threadId: { type: String, required: true },
  runId: { type: String, default: '' },
  active: { type: Boolean, default: false }
})
const loading = ref(false)
const error = ref('')
const runs = ref([])
const selectedTurnId = ref(null)
const streamActive = ref(false)
const scrollContainerRef = ref(null)
const contentRef = ref(null)
const getThreadState = inject('getAgentThreadState')
const streamSmoother = useStreamSmoother({ getThreadState })
const handleSubagentEvent = inject('handleSubagentEvent', () => {})
const { handlePublicEvent } = useAgentStreamHandler({
  getThreadState, currentAgentId: ref(''), streamSmoother,
  processApprovalInStream: (event, threadId) => handleSubagentEvent(event, threadId)
})
let controller = null
let reconnectTimer = null
let resizeObserver = null
let version = 0
let cursor = null
const displayMessages = computed(() => itemsToMessages(
  Object.values(getThreadState(props.threadId).onGoingConv.items)
    .filter((item) => !selectedTurnId.value || item.turn_id === selectedTurnId.value)
).map((item) => ({ ...item, content: getThreadState(props.threadId).displayText[item.id] ?? item.content })))
const streamedMessages = computed(() => [])
const hasRenderableMessages = computed(() => displayMessages.value.length > 0)
const scrollController = new ScrollController(() => scrollContainerRef.value, { threshold: 80, scrollDelay: 80 })
const handleScroll = (event) => scrollController.handleScroll(event)
const scrollToBottom = async () => {
  if (!props.active) return
  await nextTick()
  await scrollController.scrollToBottom()
}
const stop = () => {
  controller?.abort()
  controller = null
  clearTimeout(reconnectTimer)
  reconnectTimer = null
  streamActive.value = false
}
const applySnapshot = async (signal) => {
  const epoch = version
  const threadId = props.threadId
  const snapshot = await agentApi.getAgentHistory(threadId, { signal })
  if (signal.aborted || epoch !== version || threadId !== props.threadId) return null
  mergeItemSnapshot(getThreadState(props.threadId).onGoingConv, snapshot.items)
  const selectedRun = props.runId ? snapshot.runs.find((run) => run.run_id === props.runId) : snapshot.runs.at(-1)
  if (props.runId && !selectedRun) throw new Error('子任务的 Run 不存在')
  selectedTurnId.value = selectedRun?.turn_id || null
  runs.value = snapshot.runs.filter((run) => !selectedTurnId.value || run.turn_id === selectedTurnId.value)
  const turn = selectedTurnId.value ? await agentApi.getThreadTurn(threadId, selectedTurnId.value, { signal }) : null
  if (signal.aborted || epoch !== version) return null
  getThreadState(threadId).activeRunId = turn?.current_run_id
  return turn
}
/** 快照拥有当前等待点；断流或重同步漏收 waiting 事件时也恢复交互。 */
const settleTurn = (turn) => {
  if (turn?.status === 'waiting') {
    handleSubagentEvent({ type: 'yuxi.session.turn.waiting', session_id: props.threadId,
      turn_id: turn.turn_id, waitpoint: turn.waitpoint,
      yuxi: { run_id: turn.current_run_id } }, props.threadId)
  }
  if (turn?.status === 'waiting' || ['completed', 'failed', 'cancelled'].includes(turn?.status)) stop()
}
const observe = async (currentVersion, turnId) => {
  const subscription = controller
  streamActive.value = true
  try {
    const response = await agentApi.streamThreadEvents(props.threadId, cursor, { signal: subscription.signal })
    if (!response.ok) throw new Error(`SSE response not ok: ${response.status}`)
    await processRunSseResponse(response, async (_event, event, eventCursor) => {
      if (subscription.signal.aborted || currentVersion !== version) return
      if (eventCursor) cursor = eventCursor
      if (event.session_id !== props.threadId) return
      if (event.type === 'yuxi.session.resync') {
        const turn = await applySnapshot(subscription.signal)
        settleTurn(turn)
        return
      }
      if (event.turn_id !== turnId) return
      if (event.type === 'yuxi.session.turn.waiting' ||
          ['created', 'in_progress', 'completed', 'failed', 'cancelled'].some((status) => event.type === `agent.session.turn.${status}`)) {
        const turn = await applySnapshot(subscription.signal)
        if (!turn || event.yuxi?.run_id !== turn.current_run_id) return
      }
      handlePublicEvent(event, props.threadId)
      if (event.type === 'yuxi.session.turn.waiting' ||
          ['completed', 'failed', 'cancelled'].some((status) => event.type === `agent.session.turn.${status}`)) {
        await applySnapshot(subscription.signal)
        stop()
      }
    })
    if (!subscription.signal.aborted && currentVersion === version) {
      const turn = await applySnapshot(subscription.signal)
      settleTurn(turn)
    }
  } catch (failure) {
    if (failure.name !== 'AbortError') error.value = '连接已中断，正在恢复子任务。'
  } finally {
    if (!subscription.signal.aborted && currentVersion === version && props.active) {
      reconnectTimer = setTimeout(() => observe(currentVersion, turnId), 1000)
    }
  }
}
const loadThread = async () => {
  stop()
  const currentVersion = ++version
  if (!props.active || !props.threadId) return
  const request = new AbortController()
  controller = request
  loading.value = true
  error.value = ''
  try {
    const turn = await applySnapshot(request.signal)
    if (currentVersion !== version) return
    if (turn?.status === 'running' || turn?.status === 'cancelling') {
      void observe(currentVersion, turn.turn_id)
    } else settleTurn(turn)
    await scrollToBottom()
  } catch (failure) {
    if (failure.name !== 'AbortError' && currentVersion === version) error.value = '暂时无法加载子任务，请重试。'
  } finally {
    if (currentVersion === version) loading.value = false
  }
}
watch([() => props.threadId, () => props.runId], () => { cursor = null; loadThread() })
watch(() => props.active, (active) => { if (active) loadThread(); else { version++; stop() } })
watch(displayMessages, scrollToBottom, { deep: true, flush: 'post' })
onMounted(() => {
  loadThread()
  if (typeof ResizeObserver !== 'undefined' && contentRef.value) {
    resizeObserver = new ResizeObserver(scrollToBottom)
    resizeObserver.observe(contentRef.value)
  }
})
onUnmounted(() => {
  version++
  stop()
  streamSmoother.resetThread(props.threadId)
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
