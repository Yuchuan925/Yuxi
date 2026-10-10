import assert from 'node:assert/strict'
import { before, after, test } from 'node:test'
import { createServer } from 'vite'

let server, api, submitSessionInput, resumeSessionWaitpoint
before(async () => {
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  server = await createServer({ server: { middlewareMode: true, hmr: false } })
  ;({ agentApi: api } = await server.ssrLoadModule('/src/apis/index.js'))
  ;({ submitSessionInput, resumeSessionWaitpoint } = await server.ssrLoadModule(
    '/src/modules/session/model/sessionCommands.js'
  ))
})
after(async () => {
  await server?.close()
  delete globalThis.localStorage
})

const frozenInput = { query: '原始消息', idempotency_key: 'receipt-1', model_spec: 'model:one', mode: 'follow_up' }

test('重试恢复已有Receipt，返回原Input而不再次提交', async (t) => {
  const accepted = { input_id: 'input-1', turn_id: 'turn-1', run_id: null }
  t.mock.method(api, 'getSessionReceipt', async (thread, key) => {
    assert.deepEqual([thread, key], ['thread-1', 'receipt-1'])
    return accepted
  })
  t.mock.method(api, 'sendThreadMessage', () => { throw Error('不应重复提交') })
  assert.equal(await submitSessionInput('thread-1', frozenInput, { retry: true }), accepted)
})

test('Receipt不存在才用同一冻结载荷重发', async (t) => {
  t.mock.method(api, 'getSessionReceipt', async () => { throw Object.assign(Error('not found'), { status: 404 }) })
  const writes = []
  t.mock.method(api, 'sendThreadMessage', async (thread, data) => {
    writes.push({ thread, data: structuredClone(data) })
    return { input_id: 'input-2' }
  })
  assert.equal((await submitSessionInput('thread-1', frozenInput, { retry: true })).input_id, 'input-2')
  assert.deepEqual(writes, [{ thread: 'thread-1', data: frozenInput }])
})

test('Receipt查询故障保持未知接收结果，不盲目重发', async (t) => {
  const unavailable = Object.assign(Error('unavailable'), { status: 503 })
  t.mock.method(api, 'getSessionReceipt', async () => { throw unavailable })
  t.mock.method(api, 'sendThreadMessage', () => { throw Error('不应重发') })
  await assert.rejects(submitSessionInput('thread-1', frozenInput, { retry: true }), (error) => error === unavailable)
})

test('首次发送不查回执且拒绝缺失input_id的响应', async (t) => {
  t.mock.method(api, 'getSessionReceipt', () => { throw Error('首次发送不查回执') })
  t.mock.method(api, 'sendThreadMessage', async () => ({}))
  await assert.rejects(submitSessionInput('thread-1', frozenInput), /未返回 input_id/)
})

const waitingTurn = () => ({
  status: 'requires_action',
  yuxi: {
    current_run_id: 'run-1',
    waitpoint: { id: 'wait-1', run_id: 'run-1', questions: [{ question_id: 'q1' }, { question_id: 'q2' }] }
  }
})
const answerCommand = () => ({
  threadId: 'thread-1', turnId: 'turn-1', runId: 'run-1', kind: 'question', answer: { q2: '二', q1: '一' }
})

test('问答按当前问题ID构造协议，恢复结果绑定同一Turn和等待点', async (t) => {
  const writes = []
  t.mock.method(api, 'getThreadTurn', async (thread, turn) => {
    assert.deepEqual([thread, turn], ['thread-1', 'turn-1'])
    return waitingTurn()
  })
  t.mock.method(api, 'resumeThreadTurn', async (thread, body) => {
    writes.push({ thread, body })
    return { run_id: 'run-2' }
  })
  assert.deepEqual(await resumeSessionWaitpoint(answerCommand()), { run_id: 'run-2' })
  assert.deepEqual(writes, [{ thread: 'thread-1', body: {
    turn_id: 'turn-1', waitpoint_id: 'wait-1', idempotency_key: 'resume-wait-1',
    response: { type: 'answer', answers: [{ question_id: 'q1', answer: '一' }, { question_id: 'q2', answer: '二' }] }
  } }])
})

test('审批使用持久call_id并保留每项允许或拒绝', async (t) => {
  const turn = waitingTurn()
  turn.yuxi.waitpoint.calls = [{ call_id: 'call-a' }, { call_id: 'call-b' }]
  t.mock.method(api, 'getThreadTurn', async () => turn)
  let response
  t.mock.method(api, 'resumeThreadTurn', async (_, body) => { response = body.response; return { run_id: 'run-2' } })
  await resumeSessionWaitpoint({ ...answerCommand(), kind: 'tool_approval', answer: { decisions: [{ type: 'approve' }, { type: 'reject' }] } })
  assert.deepEqual(response, { type: 'approval', decisions: [
    { call_id: 'call-a', decision: 'approve' }, { call_id: 'call-b', decision: 'reject' }
  ] })
})

for (const defect of ['finished', 'different-run', 'different-waitpoint-run', 'missing-waitpoint', 'missing-answer', 'missing-turn', 'changed-calls']) {
  test(`拒绝过期或不完整等待点：${defect}`, async (t) => {
    const turn = waitingTurn(), command = answerCommand()
    if (defect === 'finished') turn.status = 'completed'
    if (defect === 'different-run') turn.yuxi.current_run_id = 'run-other'
    if (defect === 'different-waitpoint-run') turn.yuxi.waitpoint.run_id = 'run-other'
    if (defect === 'missing-waitpoint') delete turn.yuxi.waitpoint
    if (defect === 'missing-answer') delete command.answer.q2
    if (defect === 'missing-turn') command.turnId = null
    if (defect === 'changed-calls') {
      command.kind = 'tool_approval'
      turn.yuxi.waitpoint.calls = [{ call_id: 'call-a' }]
      command.answer = { decisions: [] }
    }
    t.mock.method(api, 'getThreadTurn', async () => turn)
    const writes = []
    t.mock.method(api, 'resumeThreadTurn', async (_, body) => { writes.push(body); return { run_id: 'run-2' } })
    await assert.rejects(resumeSessionWaitpoint(command), /等待中的 Turn|已变化|回答全部问题/)
    assert.deepEqual(writes, [])
  })
}

test('恢复响应没有Run时不能开始观察相邻执行', async (t) => {
  t.mock.method(api, 'getThreadTurn', async () => waitingTurn())
  t.mock.method(api, 'resumeThreadTurn', async () => ({}))
  await assert.rejects(resumeSessionWaitpoint(answerCommand()), /未创建 Run/)
})
