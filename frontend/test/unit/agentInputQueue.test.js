import assert from 'node:assert/strict'
import path from 'node:path'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
let server
let agentApi
let useAgentInputQueue
let MessageProcessor
let getConversationDisplayItems
let groupConversationContinuations

before(async () => {
  const storage = new Map()
  globalThis.localStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key)
  }
  server = await createServer({ root: webRoot, server: { middlewareMode: true } })
  ;({ agentApi } = await server.ssrLoadModule('/src/apis/index.js'))
  ;({ useAgentInputQueue } = await server.ssrLoadModule(
    '/src/modules/conversation/model/useAgentInputQueue.js'
  ))
  ;({ default: MessageProcessor } = await server.ssrLoadModule('/src/modules/conversation/model/messageProcessor.js'))
  ;({ getConversationDisplayItems } = await server.ssrLoadModule('/src/modules/conversation/model/messageGrouping.js'))
  ;({ groupConversationContinuations } = await server.ssrLoadModule(
    '/src/modules/conversation/model/conversationProcessGrouping.js'
  ))
})

after(async () => {
  await server?.close()
  delete globalThis.localStorage
})

test('审批前后的工具和思考跨关联 Run 连续展示，正文仍独立', () => {
  const runs = [
    { run_id: 'first', turn_id: 'turn-1', status: 'interrupted', timing: { created_at: '2026-09-16T00:00:00Z' } },
    {
      run_id: 'resume',
      turn_id: 'turn-1',
      run_type: 'resume',
      status: 'completed',
      timing: { created_at: '2026-09-16T00:01:00Z' }
    }
  ]
  const history = [
    { id: 'human', run_id: 'first', type: 'human', content: '完成任务' },
    { id: 'm1', run_id: 'first', type: 'ai', tool_calls: [{ id: 't1', name: 'ls', args: {} }] },
    {
      id: 'm2',
      run_id: 'first',
      type: 'ai',
      reasoning_content: 'thinking1',
      tool_calls: [
        { id: 't2', name: 'read_file', args: {} },
        { id: 't3', name: 'ask_user_question', args: {}, status: 'success' }
      ]
    },
    {
      id: 'answer',
      run_id: 'resume',
      type: 'human',
      content: '{"type":"approval","decisions":[{"call_id":"t3","decision":"approve"}]}',
      message_type: 'resume'
    },
    {
      id: 'm3',
      run_id: 'resume',
      type: 'ai',
      reasoning_content: 'thinking2',
      tool_calls: [{ id: 't4', name: 'ls', args: {} }]
    },
    { id: 'm4', run_id: 'resume', type: 'ai', content: '最终回答' }
  ]
  const runGroups = MessageProcessor.convertServerHistoryToMessages(history, runs)
  const groups = groupConversationContinuations(runGroups)
  assert.equal(groups.length, 1)
  const items = getConversationDisplayItems(groups[0])
  assert.deepEqual(
    items.map((item) => item.type),
    ['message', 'tool-group', 'message']
  )
  assert.deepEqual(
    items[1].entries.map((entry) => (entry.type === 'tool' ? entry.toolCall.id : entry.content)),
    ['t1', 'thinking1', 't2', 't3', 'thinking2', 't4']
  )
  assert.equal(items[2].message.content, '最终回答')
  assert.equal(runGroups.length, 2)
  const streaming = groupConversationContinuations([
    runGroups[0],
    { ...runGroups[1], status: 'streaming', messages: runGroups[1].messages.slice(0, 1) }
  ])
  assert.equal(streaming[0].status, 'streaming')
  assert.equal(getConversationDisplayItems(streaming[0])[1].key, items[1].key)
})

test('thinking 与相邻工具按顺序合并，正文和错误仍独立显示', () => {
  const messages = [
    { id: 'a1', type: 'ai', reasoning_content: '先检查', tool_calls: [{ id: 't1', name: 'ls', args: {} }] },
    { id: 'a2', type: 'ai', reasoning_content: '再确认', tool_calls: [{ id: 't2', name: 'read_file', args: {} }] },
    { id: 'a3', type: 'ai', reasoning_content: '得到结论', content: '最终回答' }
  ]
  const items = getConversationDisplayItems({ messages })
  assert.deepEqual(items.map((item) => item.type), ['tool-group', 'message'])
  assert.deepEqual(items[0].entries.map((entry) => entry.type), ['reasoning', 'tool', 'reasoning', 'tool', 'reasoning'])
  assert.deepEqual(items[0].entries.filter((entry) => entry.type === 'reasoning').map((entry) => entry.content), ['先检查', '再确认', '得到结论'])
  assert.equal(items[0].toolCalls.length, 2)
  assert.equal(items[1].message.content, '最终回答')
  assert.equal(items[1].message.reasoning_content, '')
  assert.equal(messages[2].reasoning_content, '得到结论')

  const thinkingOnly = getConversationDisplayItems({ messages: [{ id: 'a', type: 'ai', reasoning_content: '思考中' }] })
  assert.equal(thinkingOnly.length, 1)
  assert.equal(thinkingOnly[0].type, 'tool-group')
  assert.equal(thinkingOnly[0].entries[0].content, '思考中')
  assert.deepEqual(thinkingOnly[0].toolCalls, [])
  assert.deepEqual(getConversationDisplayItems({ messages: [{ type: 'ai', content: '' }] }), [])

  const failed = getConversationDisplayItems({ messages: [{ type: 'ai', reasoning_content: '检查中断', error_type: 'interrupted' }] })
  assert.deepEqual(failed.map((item) => item.type), ['tool-group', 'message'])
  assert.equal(failed[1].message.error_type, 'interrupted')
})

test('同一消息的 thinking、正文和工具分段使用不同的稳定 key', () => {
  const message = { id: 'mixed', type: 'ai', reasoning_content: '计划', content: '先查文件', tool_calls: [{ id: 't1', name: 'ls', args: {} }] }
  const items = getConversationDisplayItems({ messages: [message] })
  assert.deepEqual(items.map((item) => item.type), ['tool-group', 'message', 'tool-group'])
  assert.equal(new Set(items.map((item) => item.key)).size, 3)
  assert.equal(items[0].entries[0].content, '计划')
  assert.equal(items[2].entries[0].toolCall.id, 't1')
  const updated = getConversationDisplayItems({ messages: [{ ...message, content: '先查文件，再整理' }] })
  assert.deepEqual(updated.map((item) => item.key), items.map((item) => item.key))
})

test('完整队列快照合并 steer 节点并遵循服务器优先顺序', async () => {
  const state = {
    queuedInputs: [
      { input_id: 'F1', kind: 'follow_up', status: 'pending', content: '普通消息' },
      { input_id: 'S1', kind: 'steer', status: 'pending', content: '旧内容' },
      { input_id: 'S1', kind: 'steer', status: 'pending', content: '重复投影' },
      { input_id: 'local', status: 'sending' }
    ],
    inputMonitors: {},
    onGoingConv: { items: {}, optimisticMessages: {} }
  }
  const originalQueue = agentApi.getThreadQueue
  const originalInput = agentApi.getThreadInput
  const queue = useAgentInputQueue({ getThreadState: () => state })
  agentApi.getThreadInput = async () => ({ status: 'pending' })
  agentApi.getThreadQueue = async () => ({ inputs: [
    { input_id: 'S1', kind: 'steer', status: 'pending', content: 'S1\nS2' },
    { input_id: 'F1', kind: 'follow_up', status: 'pending', content: '普通消息' }
  ] })
  try {
    await queue.syncQueuedInputs('thread-1')
    assert.deepEqual(state.queuedInputs.map((input) => input.input_id), ['S1', 'F1', 'local'])
    assert.equal(state.queuedInputs[0].content, 'S1\nS2')
    agentApi.getThreadQueue = async () => ({ inputs: [
      { input_id: 'F1', kind: 'follow_up', status: 'pending', content: '普通消息' }
    ] })
    await queue.syncQueuedInputs('thread-1')
    assert.deepEqual(state.queuedInputs.map((input) => input.input_id), ['F1', 'local'])
  } finally {
    queue.stopAllInputMonitors('thread-1')
    agentApi.getThreadQueue = originalQueue
    agentApi.getThreadInput = originalInput
  }
})

test('尚未接收的本地输入不能被取消或订阅', async () => {
  const state = {
    queuedInputs: [{ input_id: 'local-key', status: 'sending' }],
    inputMonitors: {},
    onGoingConv: { items: {}, optimisticMessages: {} }
  }
  const originalQueue = agentApi.getThreadQueue
  const originalInput = agentApi.getThreadInput
  const originalCancel = agentApi.cancelThreadInput
  const calls = []
  agentApi.getThreadQueue = async () => ({ inputs: [] })
  agentApi.getThreadInput = async () => { calls.push('input'); throw new Error('not persisted') }
  agentApi.cancelThreadInput = async () => { calls.push('cancel'); throw new Error('not persisted') }
  try {
    const queue = useAgentInputQueue({ getThreadState: () => state })
    await queue.resumeQueuedInputs('thread-1')
    assert.equal(await queue.cancelInput('thread-1', 'local-key'), false)
    assert.deepEqual(state.queuedInputs, [{ input_id: 'local-key', status: 'sending' }])
    assert.deepEqual(calls, [])
  } finally {
    agentApi.getThreadQueue = originalQueue
    agentApi.getThreadInput = originalInput
    agentApi.cancelThreadInput = originalCancel
  }
})
