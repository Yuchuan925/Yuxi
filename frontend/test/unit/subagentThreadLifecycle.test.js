import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { setImmediate } from 'node:timers'
import { createRenderer, getCurrentInstance, h, nextTick, reactive, ssrContextKey } from 'vue'
import { createServer } from 'vite'

let server, View, api, enrichSubagentToolCall, getSubagentRunStatus, useAgentThreadState, useApproval, useAgentStreamHandler
before(async () => {
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  server = await createServer({ server: { middlewareMode: true, hmr: false } })
  ;({ default: View } = await server.ssrLoadModule('/src/modules/session/ui/SubagentThreadView.vue'))
  ;({ useAgentThreadState } = await server.ssrLoadModule('/src/modules/session/model/useAgentThreadState.js'))
  ;({ useApproval } = await server.ssrLoadModule('/src/modules/session/model/useApproval.js'))
  ;({ useAgentStreamHandler } = await server.ssrLoadModule('/src/modules/session/model/useAgentStreamHandler.js'))
  ;({ agentApi: api } = await server.ssrLoadModule('/src/apis/index.js'))
  api.getThreadTurn = async () => ({ turn_id: 'child-turn', status: 'completed', current_run_id: 'selected' })
  ;({ enrichSubagentToolCall, getSubagentRunStatus } = await server.ssrLoadModule(
    '/src/modules/session/ui/tools/toolRegistry.js'
  ))
})
after(async () => {
  await server?.close()
  delete globalThis.localStorage
})
const settle = async () => {
  await nextTick()
  await new Promise((resolve) => setImmediate(resolve))
}
const renderer = createRenderer({
  createElement: () => ({}),
  createText: () => ({}),
  createComment: () => ({}),
  insert() {},
  remove() {},
  setText() {},
  setElementText() {},
  patchProp() {},
  parentNode: () => null,
  nextSibling: () => null
})
function mount(t, initial = {}) {
  const props = reactive({ threadId: 'child', runId: 'selected', active: true, ...initial })
  let instance
  const Component = {
    ...View,
    render() {
      instance = getCurrentInstance()
      return h('div')
    }
  }
  const app = renderer.createApp(() => h(Component, props))
  app.provide(ssrContextKey, { modules: new Set() })
  const { getThreadState } = useAgentThreadState({ chatState: reactive({ threadStates: {} }) })
  app.provide('getAgentThreadState', getThreadState)
  const { approvalState, processApprovalInStream } = useApproval({
    getThreadState, fetchThreadMessages: async () => {}, getVisibleThread: () => props.threadId
  })
  const { handlePublicEvent } = useAgentStreamHandler({ getThreadState, processApprovalInStream })
  app.provide('handleSubagentEvent', handlePublicEvent)
  app.mount({})
  t.after(() => app.unmount())
  return { props, state: () => instance.setupState, getThreadState, approvalState }
}
const history = (status = 'running') => ({
  runs: [{ run_id: 'selected', turn_id: 'child-turn', status }, { run_id: 'other', turn_id: 'other-turn', status: 'completed' }],
  items: ['selected', 'other'].map((id, index) => ({
    id, type: 'message', role: 'assistant', content: [{ type: 'output_text', text: index ? 'Other output' : 'Selected output' }],
    status: 'completed', turn_id: index ? 'other-turn' : 'child-turn', yuxi: { run_id: id, output_index: index }
  }))
})

test('等待工具未返回时以参数 Run ID 关联目标，而不选同线程另一 Run', () => {
  const selected = { run_id: 'selected', child_thread_id: 'child' }
  const tool = enrichSubagentToolCall(
    { name: 'subagent_await', args: { run_id: 'selected' } },
    {
      subagentRunById: new Map([['selected', selected]])
    }
  )
  assert.equal(tool.subagent_run, selected)
})

test('完成详情只读历史并严格显示指定 Run，不请求 checkpoint', async (t) => {
  t.mock.method(api, 'getAgentHistory', async () => history('completed'))
  t.mock.method(api, 'getAgentState', () => {
    throw new Error('不应加载 checkpoint')
  })
  const h = mount(t)
  await settle()
  assert.deepEqual(
    h.state().displayMessages.map((m) => m.content),
    ['Selected output']
  )
  assert.equal(h.state().error, '')
})

test('隐藏 Tab 不加载，激活订阅，停用关闭，重开从已有游标续接', async (t) => {
  t.mock.method(api, 'getThreadTurn', async () => ({ turn_id: 'child-turn', status: 'running', current_run_id: 'selected' }))
  const requests = []
  t.mock.method(api, 'getAgentHistory', async () => history())
  t.mock.method(api, 'streamThreadEvents', async (threadId, cursor, { signal }) => {
    requests.push({ threadId, cursor, signal })
    return new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(
            new TextEncoder().encode('id: 12-0\nevent: yuxi.session.heartbeat\ndata: {"type":"yuxi.session.heartbeat","run_id":"selected","payload":{}}\n\n')
          )
          signal.addEventListener(
            'abort',
            () => controller.error(new DOMException('aborted', 'AbortError')),
            { once: true }
          )
        }
      })
    )
  })
  const h = mount(t, { active: false })
  await settle()
  assert.equal(api.getAgentHistory.mock.callCount(), 0)
  h.props.active = true
  await settle()
  assert.equal(requests.length, 1)
  h.props.active = false
  await settle()
  assert.equal(requests[0].signal.aborted, true)
  h.props.active = true
  await settle()
  assert.equal(requests[1].cursor, '12-0')
})

test('失败可重试，保留已有消息，关闭后迟到的历史响应不覆盖', async (t) => {
  let fail = false,
    late
  t.mock.method(api, 'getAgentHistory', async () => {
    if (fail) throw new Error('network')
    if (late)
      return new Promise((resolve) => {
        late.resolve = resolve
      })
    return history('completed')
  })
  const h = mount(t)
  await settle()
  fail = true
  await h.state().loadThread()
  assert.match(h.state().error, /重试/)
  assert.equal(h.state().displayMessages.length, 1)
  fail = false
  await h.state().loadThread()
  assert.equal(h.state().error, '')
  late = {}
  const pending = h.state().loadThread()
  h.props.active = false
  await settle()
  late.resolve({ runs: [], items: [] })
  await pending
  assert.equal(h.state().displayMessages.length, 1)
})

test('工具行采用独立观察到的 Run 状态，启动快照不覆盖终态，调用错误仍保留', () => {
  const tool = {
    name: 'subagent_start',
    result: { status: 'started', run_status: 'pending', run_id: 'selected' },
    subagent_run: { run_id: 'selected', status: 'completed' }
  }
  assert.equal(getSubagentRunStatus(tool), 'completed')
  assert.equal(getSubagentRunStatus({ ...tool, status: 'error' }), 'error')
  assert.equal(getSubagentRunStatus({ ...tool, subagent_run: null }), 'pending')
})

test('SSE 结束后终态查询尚未返回时隐藏 Tab，仍取消在途查询', async (t) => {
  let signal, resolveRequest
  t.mock.method(api, 'getAgentHistory', async () => history())
  t.mock.method(api, 'streamThreadEvents', async () => new Response(''))
  t.mock.method(api, 'getThreadTurn', (_threadId, _id, options) => {
    signal = options?.signal
    return new Promise((resolve) => {
      resolveRequest = resolve
    })
  })
  const h = mount(t)
  await settle()
  h.props.active = false
  await settle()
  resolveRequest({ status: 'running' })
  assert.equal(signal?.aborted, true)
})


test('子详情与恢复订阅共用同一 item 状态，父终态后恢复内容仍直接展示', async (t) => {
  t.mock.method(api, 'getAgentHistory', async () => history('completed'))
  const mounted = mount(t)
  await settle()
  const state = mounted.getThreadState('child')
  state.ongoingRunGroup.items.resumed = {
    id: 'resumed', type: 'message', role: 'assistant', status: 'completed', turn_id: 'child-turn',
    content: [{ type: 'output_text', text: 'Resumed output' }], yuxi: { run_id: 'resume', message_id: 3 }
  }
  await settle()
  assert.deepEqual(mounted.state().displayMessages.map((m) => m.content), ['Selected output', 'Resumed output'])
})

for (const [name, stream] of [
  ['断流', ''],
  ['重同步', 'data: {"type":"yuxi.session.resync","session_id":"child"}\n\n']
]) {
  test(`子详情${name}快照恢复等待点，无需重开面板即可回答`, async (t) => {
    let currentTurn = { turn_id: 'child-turn', status: 'running', current_run_id: 'selected' }
    t.mock.method(api, 'getAgentHistory', async () => history())
    t.mock.method(api, 'getThreadTurn', async () => currentTurn)
    t.mock.method(api, 'streamThreadEvents', async () => {
      currentTurn = { ...currentTurn, status: 'waiting', waitpoint: {
        id: 'child-wait', run_id: 'selected', kind: 'answer',
        questions: [{ question_id: 'q-1', question: '子任务的问题？', options: [], allow_other: true }]
      } }
      return new Response(stream)
    })
    const mounted = mount(t)
    await settle()
    const child = mounted.getThreadState('child')
    assert.equal(child.currentTurnId, 'child-turn')
    assert.equal(child.pendingInterrupt.waitpointId, 'child-wait')
    assert.equal(mounted.approvalState.showModal, true)
    assert.equal(mounted.approvalState.threadId, 'child')
    assert.equal(mounted.approvalState.questions[0].question, '子任务的问题？')
    assert.equal(mounted.state().streamActive, false)
  })
}
