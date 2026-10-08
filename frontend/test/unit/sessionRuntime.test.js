import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { setImmediate } from 'node:timers'
import { createServer } from 'vite'
import { createPinia, setActivePinia, disposePinia } from 'pinia'

let server, api, useRuntime, ErrorHandler
before(async () => {
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  server = await createServer({ server: { middlewareMode: true, hmr: false } })
  ;({ agentApi: api } = await server.ssrLoadModule('/src/apis/index.js'))
  ;({ ErrorHandler } = await server.ssrLoadModule('/src/shared/lib/errorHandler.js'))
  ;({ useSessionRuntimeStore: useRuntime } = await server.ssrLoadModule(
    '/src/modules/session/model/sessionRuntime.js'
  ))
})
after(async () => {
  await server?.close()
  delete globalThis.localStorage
})
const tick = () => new Promise((resolve) => setImmediate(resolve))

function setup(t) {
  t.mock.method(ErrorHandler, 'handleError', () => {})
  const pinia = createPinia()
  setActivePinia(pinia)
  const saved = { ...api }
  let subscriptions = 0,
    lookups = 0,
    aborted = 0,
    wire
  Object.assign(api, {
    getPublicThread: async () => {
      lookups++
      return { current_turn: { turn_id: 'turn' } }
    },
    getThreadTurn: async () => ({ current_run_id: 'run', status: 'running' }),
    getAgentHistory: async () => ({ items: [], runs: [], thread: { id: 'thread' } }),
    getAgentState: async () => ({ agent_state: {} }),
    getThreadQueue: async () => ({ inputs: [], status: 'ready' }),
    streamThreadEvents: async (_id, _after, { signal }) => {
      subscriptions++
      const body = new ReadableStream({
        start(controller) {
          wire = controller
        }
      })
      signal.addEventListener(
        'abort',
        () => {
          aborted++
          wire.close()
        },
        { once: true }
      )
      return new Response(body)
    }
  })
  const runtime = useRuntime()
  t.after(() => {
    disposePinia(pinia)
    Object.assign(api, saved)
  })
  return {
    runtime,
    counts: () => ({ subscriptions, lookups, aborted }),
    emit: (event) => wire.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`))
  }
}

test('页面和侧栏共享一个流及恢复请求，卸载一个视图不停止另一个', async (t) => {
  const h = setup(t),
    { runtime } = h
  const first = runtime.observeThread('thread', {})
  const second = runtime.observeThread('thread', {})
  await Promise.all([
    runtime.resumeActiveRunForThread('thread'),
    runtime.resumeActiveRunForThread('thread')
  ])
  await tick()
  assert.deepEqual(h.counts(), { subscriptions: 1, lookups: 1, aborted: 0 })
  const state = runtime.getThreadState('thread')
  first()
  h.emit({
    type: 'yuxi.session.turn.state',
    session_id: 'thread',
    turn_id: 'turn',
    yuxi: { run_id: 'run' },
    agent_state: { todos: ['shared after detach'] }
  })
  await tick()
  assert.deepEqual(state.agentState.todos, ['shared after detach'])
  assert.equal(h.counts().aborted, 0)
  second()
  await tick()
  assert.equal(h.counts().aborted, 1)
  assert.equal(runtime.threadMessages.thread, undefined)
})

test('重复启动同一轮不重连，另一视图读取同一份持久历史', async (t) => {
  const h = setup(t),
    { runtime } = h
  const detach = runtime.observeThread('thread', {})
  const detach2 = runtime.observeThread('thread', {})
  let histories = 0
  api.getAgentHistory = async () => {
    histories++
    return { items: [], runs: [{ id: 'result-run' }], thread: { id: 'thread' } }
  }
  const stream = runtime.startRunStream('thread', 'run', null, { turnId: 'turn' })
  await tick()
  await runtime.startRunStream('thread', 'run', null, { turnId: 'turn' })
  await Promise.all([
    runtime.fetchThreadMessages({ threadId: 'thread' }),
    runtime.fetchThreadMessages({ threadId: 'thread' })
  ])
  assert.equal(histories, 1)
  assert.equal(runtime.threadRuns.thread[0].id, 'result-run')
  assert.equal(h.counts().subscriptions, 1)
  detach()
  detach2()
  await stream
})

test('最后一个视图释放后，迟到恢复和历史不能启动流或覆盖新实例', async (t) => {
  const h = setup(t),
    { runtime } = h
  const detach = runtime.observeThread('thread', {})
  let releaseLookup, releaseHistory
  api.getPublicThread = () =>
    new Promise((resolve) => {
      releaseLookup = resolve
    })
  api.getAgentHistory = () =>
    new Promise((resolve) => {
      releaseHistory = resolve
    })
  const resume = runtime.resumeActiveRunForThread('thread')
  const history = runtime.fetchThreadMessages({ threadId: 'thread' })
  await tick()
  detach()
  const detachNew = runtime.observeThread('thread', {})
  runtime.threadMessages.thread = ['new view']
  releaseLookup({ current_turn: { turn_id: 'old-turn' } })
  releaseHistory({ items: [], runs: [{ id: 'old-run' }], thread: { id: 'thread' } })
  await Promise.all([resume, history])
  assert.equal(h.counts().subscriptions, 0)
  assert.deepEqual(runtime.threadMessages.thread, ['new view'])
  detachNew()
})

test('同一 Thread 的旧恢复快照不能覆盖刚开始的新执行', async (t) => {
  const h = setup(t),
    { runtime } = h
  const detach = runtime.observeThread('thread', {})
  let releaseLookup
  api.getPublicThread = () =>
    new Promise((resolve) => {
      releaseLookup = resolve
    })
  const restoring = runtime.resumeActiveRunForThread('thread')
  await tick()
  const stream = runtime.startRunStream('thread', 'new-run', null, { turnId: 'new-turn' })
  await tick()
  releaseLookup({ current_turn: null })
  await restoring
  const state = runtime.getThreadState('thread')
  assert.equal(state.activeRunId, 'new-run')
  assert.equal(state.currentTurnId, 'new-turn')
  assert.equal(state.isStreaming, true)
  detach()
  await stream
})

for (const historyFails of [false, true]) {
  test(`终态必须重读完成后的历史，读取${historyFails ? '失败保留流输出' : '成功保留最终输出'}`, async (t) => {
    const h = setup(t),
      { runtime } = h
    const detach = runtime.observeThread('thread', {})
    let resolveOld,
      requests = 0
    const item = {
      id: 'output',
      type: 'message',
      role: 'assistant',
      turn_id: 'turn',
      status: 'completed',
      content: [{ type: 'output_text', text: 'FINAL OUTPUT' }],
      yuxi: { run_id: 'run', message_id: 'output' }
    }
    api.getAgentHistory = async () => {
      requests++
      if (requests === 1)
        return new Promise((resolve) => {
          resolveOld = resolve
        })
      if (historyFails) throw new Error('history unavailable')
      return {
        items: [item],
        runs: [],
        thread: { id: 'thread', current_turn: { status: 'completed' } }
      }
    }
    const old = runtime.fetchThreadMessages({ threadId: 'thread' })
    await tick()
    const stream = runtime.startRunStream('thread', 'run', null, { turnId: 'turn' })
    await tick()
    h.emit({
      type: 'agent.session.turn.item.done',
      event_id: 'done-output',
      session_id: 'thread',
      turn_id: 'turn',
      yuxi: { run_id: 'run' },
      item
    })
    h.emit({
      type: 'agent.session.turn.completed',
      event_id: 'completed',
      session_id: 'thread',
      turn_id: 'turn',
      yuxi: { run_id: 'run', current_run_id: 'run' }
    })
    await stream
    resolveOld({ items: [], runs: [], thread: { id: 'thread' } })
    await old
    await tick()
    await tick()
    assert.equal(requests, 2, '终态不能共用完成事件之前的历史')
    const output = historyFails
      ? runtime.getThreadState('thread').ongoingRunGroup.items.output.content[0].text
      : runtime.threadMessages.thread[0].content
    assert.equal(output, 'FINAL OUTPUT')
    detach()
  })
}

test('旧终态历史完成时不能清空重挂载实例的新流', async (t) => {
  const h = setup(t),
    { runtime } = h
  const detach = runtime.observeThread('thread', {})
  let resolveHistory
  api.getAgentHistory = () =>
    new Promise((resolve) => {
      resolveHistory = resolve
    })
  const old = runtime.startRunStream('thread', 'run', null, { turnId: 'turn' })
  await tick()
  h.emit({
    type: 'agent.session.turn.completed',
    event_id: 'completed',
    session_id: 'thread',
    turn_id: 'turn',
    yuxi: { run_id: 'run', current_run_id: 'run' }
  })
  await old
  await tick()
  detach()
  const detachNew = runtime.observeThread('thread', {})
  const active = runtime.startRunStream('thread', 'new-run', null, { turnId: 'new-turn' })
  await tick()
  resolveHistory({ items: [], runs: [], thread: { id: 'thread' } })
  await tick()
  await tick()
  assert.equal(runtime.getThreadState('thread').activeRunId, 'new-run')
  assert.equal(runtime.getThreadState('thread').runStreamAbortController.signal.aborted, false)
  assert.equal(h.counts().aborted, 1, '旧终态只停止旧流')
  detachNew()
  await active
})

for (const source of ['resume', 'waitpoint']) {
  test(`同一 SSE 内进入新 Run 后，旧${source}快照不能恢复旧等待点`, async (t) => {
    const h = setup(t),
      { runtime } = h
    const detach = runtime.observeThread('thread', {})
    const stream = runtime.startRunStream('thread', 'run', null, { turnId: 'turn' })
    await tick()
    let resolveOld,
      reads = 0
    api.getThreadTurn = async () => {
      if (++reads === 1)
        return new Promise((resolve) => {
          resolveOld = resolve
        })
      return { current_run_id: 'run2', status: 'running' }
    }
    let restoring
    if (source === 'resume') restoring = runtime.resumeActiveRunForThread('thread')
    else
      h.emit({
        type: 'yuxi.session.turn.waiting',
        session_id: 'thread',
        turn_id: 'turn',
        yuxi: { run_id: 'run', current_run_id: 'run' },
        waitpoint: { kind: 'cooperation' }
      })
    await tick()
    await tick()
    assert.equal(typeof resolveOld, 'function')
    h.emit({
      type: 'agent.session.turn.in_progress',
      session_id: 'thread',
      turn_id: 'turn',
      yuxi: { run_id: 'run2', current_run_id: 'run2' }
    })
    await tick()
    await tick()
    assert.equal(runtime.getThreadState('thread').activeRunId, 'run2')
    resolveOld({
      current_run_id: 'run',
      status: 'waiting',
      waitpoint: {
        kind: 'answer',
        id: 'old-waitpoint',
        run_id: 'run',
        questions: [{ question: 'old question' }]
      }
    })
    await restoring
    await tick()
    await tick()
    const state = runtime.getThreadState('thread')
    assert.equal(state.activeRunId, 'run2')
    assert.equal(state.isStreaming, true)
    assert.equal(state.turnStatus, 'running')
    assert.equal(state.pendingInterrupt, null)
    assert.equal(h.counts().subscriptions, 1)
    detach()
    await stream
  })
}

test('SSE 校验旧 Run 期间的新恢复结果不能被旧事件覆盖', async (t) => {
  const h = setup(t),
    { runtime } = h
  const detach = runtime.observeThread('thread', {})
  const stream = runtime.startRunStream('thread', 'run', null, { turnId: 'turn' })
  await tick()
  let releaseOld,
    reads = 0
  api.getThreadTurn = async () => {
    if (++reads === 1)
      return new Promise((resolve) => {
        releaseOld = resolve
      })
    return { status: 'running', current_run_id: 'run3' }
  }
  h.emit({
    type: 'agent.session.turn.in_progress',
    session_id: 'thread',
    turn_id: 'turn',
    yuxi: { run_id: 'run2', current_run_id: 'run2' }
  })
  await tick()
  await runtime.resumeActiveRunForThread('thread')
  assert.equal(runtime.getThreadState('thread').activeRunId, 'run3')
  releaseOld({ status: 'running', current_run_id: 'run2' })
  await tick()
  await tick()
  assert.equal(runtime.getThreadState('thread').activeRunId, 'run3')
  detach()
  await stream
})

test('旧 checkpoint 查询不能覆盖新 Run 的状态事件', async (t) => {
  const h = setup(t),
    { runtime } = h
  const detach = runtime.observeThread('thread', {})
  let releaseState
  api.getAgentState = () =>
    new Promise((resolve) => {
      releaseState = resolve
    })
  const old = runtime.fetchAgentState(null, 'thread')
  await tick()
  const stream = runtime.startRunStream('thread', 'run', null, { turnId: 'turn' })
  await tick()
  h.emit({
    type: 'yuxi.session.turn.state',
    session_id: 'thread',
    turn_id: 'turn',
    yuxi: { run_id: 'run' },
    agent_state: { todos: ['new state'] }
  })
  await tick()
  releaseState({ agent_state: { todos: ['old state'] } })
  await old
  assert.deepEqual(runtime.getThreadState('thread').agentState.todos, ['new state'])
  detach()
  await stream
})
