import assert from 'node:assert/strict'
import test from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

test('产品创建、消息、等待恢复和队列控制仅使用 Public Thread', async () => {
  const saved = new Map()
  globalThis.localStorage = {
    getItem: (key) => saved.get(key) ?? null,
    setItem: (key, value) => saved.set(key, String(value)),
    removeItem: (key) => saved.delete(key)
  }
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options })
    const body = String(url).endsWith('/threads')
      ? {
          object: 'agent.thread', id: 'thread-1', thread_id: 'thread-1',
          event_id: 'create-event', input_id: null, turn_id: null, run_id: null,
          status: 'accepted', title: '新对话', project_id: 'project-1'
        }
      : { status: 'accepted', event_id: 'event-1', input_id: 'input-1' }
    return new Response(JSON.stringify(body), {
      status: options?.method === 'POST' && String(url).endsWith('/events') ? 202 : 200,
      headers: { 'content-type': 'application/json' }
    })
  }
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  try {
    setActivePinia(createPinia())
    const { useUserStore } = await server.ssrLoadModule('/src/modules/identity/model/user.js')
    const userStore = useUserStore()
    userStore.token = 'test-token'
    userStore.userId = 1
    const { agentApi, threadApi } = await server.ssrLoadModule('/src/apis/agent_api.js')

    const thread = await threadApi.createThread(
      'agent-1', '新对话', { tool_approval_mode: 'default' },
      { requestId: 'create-key', projectId: 'project-1' }
    )
    assert.equal(thread.id, 'thread-1')
    assert.equal(thread.title, '新对话')
    assert.equal(thread.project_id, 'project-1')
    assert.equal(calls[0].url, '/api/v1/agents/threads')
    assert.equal(calls[0].options.headers['Idempotency-Key'], 'create-key')
    assert.equal(JSON.parse(calls[0].options.body).agent_id, 'agent-1')

    await agentApi.sendThreadMessage('thread-1', {
      idempotency_key: 'message-key',
      query: '继续',
      image_content: ['abc'],
      mode: 'follow_up',
      attachment_file_ids: ['file-1']
    })
    assert.equal(calls[1].url, '/api/v1/agents/threads/thread-1/events')
    assert.equal(calls[1].options.headers['Idempotency-Key'], 'message-key')
    const message = JSON.parse(calls[1].options.body).events[0]
    assert.equal(message.type, 'agent.session.input.message')
    assert.equal(message.yuxi.mode, 'follow_up')
    assert.equal(message.input[0].content[1].image_url, 'data:image/jpeg;base64,abc')

    await agentApi.sendThreadMessage('thread-1', {
      idempotency_key: 'steer-key', query: '改成英文',
      mode: 'steer', turn_id: 'turn-1'
    })
    assert.equal(JSON.parse(calls[2].options.body).events[0].yuxi.mode, 'steer')
    assert.ok(!('turn_id' in JSON.parse(calls[2].options.body).events[0].yuxi))

    await agentApi.resumeThreadTurn('thread-1', {
      turn_id: 'turn-1', waitpoint_id: 'wait-1',
      response: { type: 'answer', answers: [
        { question_id: 'q-1', answer: ['杭州', '上海'] },
        { question_id: 'q-2', answer: { type: 'other', text: '苏州', selected: ['杭州'] } }
      ] },
      idempotency_key: 'resume-key'
    })
    assert.deepEqual(JSON.parse(calls[3].options.body).events[0], {
      type: 'yuxi.session.input.resume',
      turn_id: 'turn-1',
      waitpoint_id: 'wait-1',
      response: { type: 'answer', answers: [
        { question_id: 'q-1', answer: ['杭州', '上海'] },
        { question_id: 'q-2', answer: { type: 'other', text: '苏州', selected: ['杭州'] } }
      ] }
    })

    await agentApi.cancelThreadTurn('thread-1', 'turn-1', 'cancel-key', 'run-1')
    assert.deepEqual(JSON.parse(calls[4].options.body).events[0], {
      type: 'agent.session.input.cancel', yuxi: { turn_id: 'turn-1', expected_run_id: 'run-1' }
    })
    await agentApi.continueThreadQueue('thread-1', 'continue-key')
    await agentApi.cancelThreadInput('thread-1', 'input-2', 'cancel-input-key')
    assert.equal(JSON.parse(calls[5].options.body).events[0].type, 'yuxi.session.input.continue')
    assert.equal(JSON.parse(calls[6].options.body).events[0].input_id, 'input-2')

    await agentApi.streamThreadEvents('thread-1', 'cursor-7')
    assert.equal(calls[7].url, '/api/v1/agents/threads/thread-1/events')
    assert.equal(calls[7].options.headers['Last-Event-ID'], 'cursor-7')

    await threadApi.updateThread('thread-1', null, undefined, 'default', 'model-a')
    assert.equal(calls[8].url, '/api/v1/agents/threads/thread-1')
    assert.deepEqual(JSON.parse(calls[8].options.body), {
      title: null, tool_approval_mode: 'default', model_spec: 'model-a'
    })
    await threadApi.archiveThread('thread-1')
    assert.equal(calls[9].url, '/api/v1/agents/threads/thread-1/archive')
    assert.equal(calls[9].options.method, 'POST')
    assert.ok(calls.every((call) => !call.url.includes('/api/chat/')))
  } finally {
    await server.close()
    delete globalThis.fetch
    delete globalThis.localStorage
  }
})
