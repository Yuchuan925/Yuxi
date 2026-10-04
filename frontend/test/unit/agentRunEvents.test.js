import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { setImmediate } from 'node:timers'
import { createServer } from 'vite'
import { createItemState } from '../../src/modules/session/model/agentItems.js'

let server, api, useAgentRunStream, processRunSseResponse, useAgentInputQueue
before(async () => {
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  server = await createServer({ server: { middlewareMode: true, hmr: false } })
  ;({ agentApi: api } = await server.ssrLoadModule('/src/apis/index.js'))
  ;({ useAgentRunStream, processRunSseResponse } = await server.ssrLoadModule('/src/modules/session/model/useAgentRunStream.js'))
  ;({ useAgentInputQueue } = await server.ssrLoadModule('/src/modules/session/model/useAgentInputQueue.js'))
})
after(async () => { await server?.close(); delete globalThis.localStorage })
const tick = () => new Promise((resolve) => setImmediate(resolve))
const event = (type, runId = 'run', turnId = 'turn') => ({
  type, event_id: `${type}:${runId}`, session_id: 'thread', turn_id: turnId, yuxi: { run_id: runId }
})
const response = (events) => new Response(new ReadableStream({ start(controller) {
  events.forEach((value, index) => controller.enqueue(new TextEncoder().encode(
    `id: 2.0.1.${index + 1}-0.3\nevent: ${value.type}\ndata: ${JSON.stringify(value)}\n\n`)))
  controller.close()
} }))
function harness(t, events, { status = 'running', currentRun = 'run', snapshot = async () => {} } = {}) {
  const state = { currentTurnId: 'turn', activeRunId: currentRun, ongoingRunGroup: createItemState() }
  const delivered = [], terminal = []
  t.mock.method(api, 'getThreadTurn', async () => ({ turn_id: 'turn', current_run_id: currentRun, status }))
  t.mock.method(api, 'streamThreadEvents', async () => response(events))
  const stream = useAgentRunStream({
    getThreadState: () => state, currentAgentId: 'agent',
    handlePublicEvent: (value) => delivered.push(value), fetchThreadMessages: snapshot,
    fetchAgentState() {}, resetOngoingRunGroup() {},
    onTerminalDetected: (value) => terminal.push(value)
  })
  t.after(() => stream.stopRunStreamSubscription('thread'))
  return { state, stream, delivered, terminal }
}

test('SSE 每条 data 直接消费；等待异步快照时缓冲后续事件', async () => {
  const order = []
  const events = [event('yuxi.session.resync'), event('agent.session.turn.output_text.delta')]
  await processRunSseResponse(response(events), async (_type, data) => {
    order.push(data.type)
    if (data.type === 'yuxi.session.resync') { await tick(); order.push('snapshot-applied') }
  })
  assert.deepEqual(order, ['yuxi.session.resync', 'snapshot-applied', 'agent.session.turn.output_text.delta'])
})

test('resync 回读 items 和 Turn 后继续消费终态，cursor 保持订阅位置', async (t) => {
  const order = []
  const h = harness(t, [event('yuxi.session.resync'), event('agent.session.turn.completed')], {
    snapshot: async () => { order.push('snapshot'); await tick() }
  })
  await h.stream.startRunStream('thread', 'run', null, { turnId: 'turn' })
  await tick()
  assert.equal(order[0], 'snapshot')
  assert.equal(h.state.turnStatus, 'completed')
  assert.match(h.state.threadCursor, /^2\./)
  assert.equal(h.terminal.length, 1)
})

test('waiting 立即禁发并保留自身 Run，父终态回调不包含子 Thread', async (t) => {
  const h = harness(t, [event('yuxi.session.turn.waiting')])
  await h.stream.startRunStream('thread', 'run', null, { turnId: 'turn' })
  await tick()
  assert.equal(h.state.turnStatus, 'waiting')
  assert.equal(h.state.activeRunSteerable, false)
  assert.equal(h.state.activeRunId, 'run')
  assert.equal(h.terminal.length, 0)
})

test('旧 Run 的 waiting 和其他 Turn 的终态不能结束恢复后的 Run', async (t) => {
  const h = harness(t, [event('yuxi.session.turn.waiting', 'old'),
    event('agent.session.turn.completed', 'foreign', 'foreign-turn'), event('agent.session.turn.completed', 'new')],
    { currentRun: 'new' })
  await h.stream.startRunStream('thread', 'new', null, { turnId: 'turn' })
  await tick()
  assert.equal(h.delivered.length, 1)
  assert.equal(h.delivered[0].yuxi.run_id, 'new')
  assert.equal(h.state.turnStatus, 'completed')
  assert.deepEqual(h.terminal[0].touchedThreadIds, ['thread'])
})

test('Run settled 不结束 Turn；只有 Turn 终态收尾', async (t) => {
  const h = harness(t, [event('yuxi.session.run.settled'), event('agent.session.turn.completed')])
  await h.stream.startRunStream('thread', 'run', null, { turnId: 'turn' })
  await tick()
  assert.equal(h.delivered.length, 2)
  assert.equal(h.terminal.length, 1)
})

test('FIFO 领取后用真实 item 替换乐观输入并订阅所属 Turn', async (t) => {
  const state = { queuedInputs: [], inputMonitors: {}, ongoingRunGroup: {
    ...createItemState(), optimisticMessages: { input: { id: 'local' } }
  } }
  const item = { id: 'input_1', type: 'message', role: 'user', status: 'completed', content: [], yuxi: { input_id: 'input' } }
  t.mock.method(api, 'getThreadQueue', async () => ({ inputs: [{ input_id: 'input', status: 'pending' }] }))
  t.mock.method(api, 'getThreadInput', async () => ({ status: 'consumed', run_id: 'run', turn_id: 'turn', items: [item] }))
  const started = []
  const queue = useAgentInputQueue({ getThreadState: () => state,
    resetOngoingRunGroup() {}, startRunStream: (...args) => started.push(args) })
  t.after(() => queue.stopAllInputMonitors('thread'))
  await queue.syncQueuedInputs('thread')
  await tick()
  assert.deepEqual(state.ongoingRunGroup.items.input_1, item)
  assert.deepEqual(state.ongoingRunGroup.optimisticMessages, {})
  assert.deepEqual(started, [['thread', 'run', null, { turnId: 'turn', inputId: 'input' }]])
})


test('旧 Turn 等待历史回读时启动新 FIFO 流，旧收尾不能中止新订阅或审批', async (t) => {
  let releaseHistory
  const history = new Promise((resolve) => { releaseHistory = resolve })
  let secondController, resetCount = 0, terminalCount = 0
  const state = { currentTurnId: 'turn', activeRunId: 'run', ongoingRunGroup: createItemState() }
  t.mock.method(api, 'streamThreadEvents', async (_thread, _cursor, { signal }) => {
    if (!secondController) {
      secondController = signal
      return response([event('agent.session.turn.completed')])
    }
    secondController = signal
    return new Response(new ReadableStream({ start(controller) {
      signal.addEventListener('abort', () => controller.close(), { once: true })
    } }))
  })
  const stream = useAgentRunStream({ getThreadState: () => state, currentAgentId: 'agent',
    handlePublicEvent() {}, fetchThreadMessages: () => history, fetchAgentState() {},
    resetOngoingRunGroup: () => { resetCount++; state.runStreamAbortController?.abort() },
    onTerminalDetected: () => terminalCount++ })
  await stream.startRunStream('thread', 'run', null, { turnId: 'turn' })
  const following = stream.startRunStream('thread', 'next', null, { turnId: 'next-turn' })
  await tick()
  releaseHistory()
  await tick()
  assert.equal(state.currentTurnId, 'next-turn')
  assert.equal(secondController.aborted, false)
  assert.equal(resetCount, 0)
  assert.equal(terminalCount, 0)
  stream.stopRunStreamSubscription('thread')
  await following
})
