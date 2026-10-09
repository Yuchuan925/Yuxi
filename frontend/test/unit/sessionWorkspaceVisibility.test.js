import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, reactive, ref } from 'vue'

const source = readFileSync(
  new URL('../../src/modules/session/ui/SessionWorkspace.vue', import.meta.url),
  'utf8'
)

test('新线程创建后立即退出欢迎布局，不等待路由或首个回复', () => {
  const props = reactive({ isNewSession: true })
  const currentThreadId = ref(null)
  const expression = source.slice(
    source.indexOf('const showStartScreen ='),
    source.indexOf('const currentThread =')
  )
  const showStartScreen = new Function(
    'computed',
    'props',
    'currentThreadId',
    `${expression}; return showStartScreen`
  )(computed, props, currentThreadId)
  assert.equal(showStartScreen.value, true)
  currentThreadId.value = 'created-thread'
  assert.equal(showStartScreen.value, false, '路由仍为新会话时也必须收起欢迎布局')
  props.isNewSession = false
  assert.equal(showStartScreen.value, false)
  currentThreadId.value = null
  assert.equal(showStartScreen.value, false, '已有会话路由加载期间不能显示欢迎布局')
  props.isNewSession = true
  assert.equal(showStartScreen.value, true, '回到新会话时恢复欢迎布局')
  assert.match(source, /'start-screen': showStartScreen/)
  assert.match(source, /v-if="showStartScreen" class="chat-greeting-input"/)
})

test('隐藏成员标签继续接收终态，但不标已读或滚动；显示后恢复可见副作用', () => {
  const props = reactive({ visible: false })
  const visibility = source.slice(
    source.indexOf('const workspaceActivated ='),
    source.indexOf('// ==================== LOCAL CHAT')
  )
  const { workspaceActive, workspaceActivated } = new Function(
    'computed',
    'ref',
    'props',
    `${visibility}; return { workspaceActive, workspaceActivated }`
  )(computed, ref, props)
  const viewed = [],
    resumed = []
  let scrolls = 0
  const dependencies = {
    getThreadState() {},
    currentAgentId: ref('agent'),
    handlePublicEvent() {},
    fetchThreadMessages() {},
    fetchAgentState() {},
    resetOngoingRunGroup() {},
    streamSmoother: {},
    workspaceActive,
    pageVisible: ref(true),
    scrollController: {
      scrollToBottom() {
        scrolls++
      }
    },
    approvalState: {},
    hideApprovalState() {},
    resumeQueuedInputs: (id) => resumed.push(id),
    currentThreadId: ref('child'),
    chatThreadsStore: { markThreadViewed: (id) => viewed.push(id) },
    agentPanelFilesystemRefreshVersion: ref(0),
    sessionRuntime: {}
  }
  const registration = source.slice(
    source.indexOf('const runtimeView ='),
    source.indexOf('let releaseRuntimeView')
  )
  const handlers = new Function(
    ...Object.keys(dependencies),
    `${registration}; return runtimeView`
  )(...Object.values(dependencies))
  const complete = () => {
    handlers.onTerminalDetected({ threadId: 'child', runId: 'run' })
    handlers.onScrollToBottom()
  }
  complete()
  assert.deepEqual(resumed, [], '恢复由共享运行内核负责')
  assert.deepEqual(viewed, [])
  assert.equal(scrolls, 0)
  props.visible = true
  complete()
  assert.deepEqual(viewed, ['child'])
  assert.equal(scrolls, 1)
  dependencies.pageVisible.value = false
  complete()
  assert.deepEqual(viewed, ['child'], '浏览器后台标签也不能标记已读')
  assert.equal(scrolls, 1)
  dependencies.pageVisible.value = true
  workspaceActivated.value = false
  complete()
  assert.deepEqual(viewed, ['child'], 'KeepAlive 后台页面也不能标记已读')
  assert.equal(scrolls, 1)
})

test('另一视图完成审批后，本视图立即解除旧等待点的输入遮罩', () => {
  const expression = source.slice(
    source.indexOf('const currentApprovalModalVisible ='),
    source.indexOf('const currentApprovalQuestions =')
  )
  const pending = { interruptedRunId: 'run', waitpointId: 'approval-1' }
  const shared = ref({ pendingInterrupt: pending })
  const local = reactive({ showModal: true, threadId: 'thread', ...pending })
  const visible = new Function(
    'computed',
    'approvalState',
    'visibleApprovalThreadId',
    'currentThreadState',
    `${expression}; return currentApprovalModalVisible`
  )(computed, local, ref('thread'), shared)
  assert.equal(visible.value, true)
  shared.value.pendingInterrupt = null
  assert.equal(visible.value, false, '其他视图提交并清除共享等待点后不再阻塞输入')
  shared.value.pendingInterrupt = { interruptedRunId: 'run2', waitpointId: 'approval-2' }
  assert.equal(visible.value, false, '旧面板也不能代表新 Run 的审批')
  Object.assign(local, shared.value.pendingInterrupt)
  assert.equal(visible.value, true)
})
