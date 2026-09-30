import assert from 'node:assert/strict'
import path from 'node:path'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'
import { isThreadWaitingForUserAction } from '../../src/utils/toolApproval.js'

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
let server
let agentApi
let useAgentInputQueue
let useAgentRunStream
let dispatchRunEventChunks
let useAgentStreamHandler
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
    '/src/composables/useAgentInputQueue.js'
  ))
  ;({ useAgentRunStream, dispatchRunEventChunks } = await server.ssrLoadModule(
    '/src/composables/useAgentRunStream.js'
  ))
  ;({ useAgentStreamHandler } = await server.ssrLoadModule(
    '/src/composables/useAgentStreamHandler.js'
  ))
  ;({ default: MessageProcessor } = await server.ssrLoadModule('/src/utils/messageProcessor.js'))
  ;({ getConversationDisplayItems } = await server.ssrLoadModule('/src/utils/messageGrouping.js'))
  ;({ groupConversationContinuations } = await server.ssrLoadModule(
    '/src/utils/conversationProcessGrouping.js'
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

test('当前工具语义流首块即显示，完整快照覆盖分片并关联同 Run 结果', () => {
  const threadState = { onGoingConv: { msgChunks: {} } }
  const { handleStreamChunk } = useAgentStreamHandler({ getThreadState: () => threadState })
  const send = (stream_event) =>
    handleStreamChunk({ status: 'loading', run_id: 'run-tools', stream_event }, 'thread-tools')
  const messages = () =>
    MessageProcessor.convertToolResultToMessages(
      Object.values(threadState.onGoingConv.msgChunks).map(MessageProcessor.mergeMessageChunk)
    ).filter((msg) => msg.type !== 'tool')
  send({
    type: 'tool_call_delta',
    message_id: 'model-tools',
    tool_call_id: 'call-1',
    name: 'read_file',
    index: 0,
    args_delta: '{"path":'
  })
  let items = getConversationDisplayItems({ messages: messages() })
  assert.equal(items.length, 1)
  assert.equal(items[0].type, 'tool-group')
  assert.equal(items[0].toolCalls[0].name, 'read_file')
  send({ type: 'tool_call_delta', message_id: 'model-tools', index: 0, args_delta: '"/notes.md"}' })
  send({
    type: 'tool_call',
    message_id: 'model-tools',
    tool_call_id: 'call-1',
    name: 'read_file',
    index: 0,
    args: { path: '/notes.md' }
  })
  send({
    type: 'tool_call',
    message_id: 'model-tools',
    tool_call_id: 'call-2',
    name: 'ls',
    index: 1,
    args: { path: '/' }
  })
  handleStreamChunk(
    {
      status: 'stream_event',
      run_id: 'run-tools',
      event: {
        method: 'tools',
        data: {
          event: 'tool-finished',
          tool_call_id: 'call-1',
          output: { type: 'tool', tool_call_id: 'call-1', content: 'notes', status: 'success' }
        }
      }
    },
    'thread-tools'
  )
  const toolCalls = messages()[0].tool_calls
  assert.equal(toolCalls.length, 2)
  assert.deepEqual(JSON.parse(toolCalls[0].args), { path: '/notes.md' })
  assert.equal(toolCalls[0].tool_call_result.content, 'notes')
  assert.equal(toolCalls[0].tool_call_result.run_id, 'run-tools')
  assert.equal(toolCalls[1].tool_call_result, null)
})

test('只有单个完整工具事件且无正文时仍显示工具，旧 loading msg 不再消费', () => {
  const threadState = { onGoingConv: { msgChunks: {} } }
  const { handleStreamChunk } = useAgentStreamHandler({ getThreadState: () => threadState })
  handleStreamChunk(
    { status: 'loading', msg: { id: 'legacy', type: 'ai', content: 'old' } },
    'thread-tools'
  )
  assert.deepEqual(threadState.onGoingConv.msgChunks, {})
  handleStreamChunk(
    {
      status: 'loading',
      run_id: 'run-tools',
      stream_event: {
        type: 'tool_call',
        message_id: 'only-tool',
        tool_call_id: 'call-1',
        name: 'ls',
        args: {}
      }
    },
    'thread-tools'
  )
  const message = MessageProcessor.mergeMessageChunk(threadState.onGoingConv.msgChunks['only-tool'])
  assert.equal(getConversationDisplayItems({ messages: [message] })[0].toolCalls[0].name, 'ls')
})

test('重复工具 ID 的结果不能跨 Run 绑定', () => {
  const [message] = MessageProcessor.convertToolResultToMessages([
    { type: 'ai', run_id: 'new-run', tool_calls: [{ id: 'same-call', name: 'ls', args: {} }] },
    { type: 'tool', run_id: 'old-run', tool_call_id: 'same-call', content: 'old result' }
  ])
  assert.equal(message.tool_calls[0].tool_call_result, null)
})

test('Run envelope 归一化同时保留批量与单 chunk 形状及线程上下文', () => {
  const batchData = {
    type: 'agent.thread.output',
    thread_id: 'parent-thread',
    input_id: 'input-1',
    run_id: 'run-1',
    payload: { items: [
      { status: 'loading', id: 'message-1' },
      { status: 'loading', id: 'message-child', thread_id: 'child-thread' }
    ] }
  }
  const singleData = {
    type: 'agent.thread.output',
    thread_id: 'parent-thread',
    input_id: 'input-2',
    payload: {
      chunk: {
        status: 'loading',
        id: 'message-2',
        metadata: { thread_id: 'child-thread' }
      }
    }
  }

  const dispatched = []
  dispatchRunEventChunks({
    data: batchData,
    runId: 'run-fallback',
    fallbackThreadId: 'parent-thread',
    streamRunId: 'run-1',
    streamThreadId: 'parent-thread',
    onChunk: (chunk, threadId) => dispatched.push({ chunk, threadId })
  })
  dispatchRunEventChunks({
    data: singleData,
    runId: 'run-fallback',
    fallbackThreadId: 'parent-thread',
    onChunk: (chunk, threadId) => dispatched.push({ chunk, threadId })
  })

  assert.deepEqual(dispatched, [
    {
      chunk: {
        status: 'loading',
        id: 'message-1',
        input_id: 'input-1',
        run_id: 'run-1',
        thread_id: 'parent-thread',
        stream_run_id: 'run-1',
        stream_thread_id: 'parent-thread'
      },
      threadId: 'parent-thread'
    },
    {
      chunk: {
        status: 'loading',
        id: 'message-child',
        input_id: 'input-1',
        run_id: 'run-1',
        thread_id: 'child-thread',
        stream_run_id: 'run-1',
        stream_thread_id: 'parent-thread'
      },
      threadId: 'child-thread'
    },
    {
      chunk: {
        status: 'loading',
        id: 'message-2',
        metadata: { thread_id: 'child-thread' },
        input_id: 'input-2',
        run_id: 'run-fallback',
        thread_id: 'child-thread'
      },
      threadId: 'child-thread'
    }
  ])
})

test('agent_state SSE 使在途状态请求失效', () => {
  const threadState = {
    agentState: null,
    agentStateRequestVersion: 4,
    onGoingConv: { msgChunks: {} }
  }
  const { handleStreamChunk } = useAgentStreamHandler({
    getThreadState: () => threadState,
    processApprovalInStream: () => false,
    currentAgentId: { value: 'agent-1' }
  })
  const agentState = { token_usage: { measured_at: '2026-08-09T00:00:00Z' } }

  handleStreamChunk({ status: 'agent_state', agent_state: agentState }, 'thread-1')

  assert.deepEqual(threadState.agentState, agentState)
  assert.equal(threadState.agentStateRequestVersion, 5)
})

test('Run init 将权威 run_id 绑定到实时 User Message', () => {
  const threadState = {
    pendingInputId: 'input-1',
    replyLoadingVisible: false,
    contextCompressing: false,
    onGoingConv: {
      msgChunks: {
        'input-1': [
          { id: 'input-1', type: 'human', content: '快排', created_at: '2026-09-05T06:32:00Z' }
        ]
      }
    }
  }
  const { handleStreamChunk } = useAgentStreamHandler({
    getThreadState: () => threadState,
    processApprovalInStream: () => false,
    currentAgentId: { value: 'agent-1' }
  })

  handleStreamChunk(
    {
      status: 'init',
      run_id: 'stale-body-run',
      stream_run_id: 'run-1',
      stream_thread_id: 'thread-1',
      input_id: 'input-1',
      msg: { type: 'human', content: '快排' }
    },
    'thread-1'
  )

  const [message] = threadState.onGoingConv.msgChunks['input-1']
  assert.equal(message.created_at, '2026-09-05T06:32:00Z')
  assert.equal(message.run_id, 'run-1')
  assert.equal(message.extra_metadata.run_id, 'run-1')
  assert.equal(message.extra_metadata.input_id, 'input-1')
})

test('缺少 SSE input_id 时不把 Run 关联到本地 pending User', () => {
  const threadState = {
    pendingInputId: 'input-old',
    replyLoadingVisible: false,
    contextCompressing: false,
    onGoingConv: { msgChunks: {} }
  }
  const { handleStreamChunk } = useAgentStreamHandler({
    getThreadState: () => threadState,
    processApprovalInStream: () => false,
    currentAgentId: { value: 'agent-1' }
  })

  handleStreamChunk(
    {
      status: 'init',
      run_id: 'run-new',
      msg: {
        type: 'human',
        content: '旧请求',
        run_id: 'run-from-msg',
        extra_metadata: { run_id: 'run-from-msg' }
      }
    },
    'thread-1'
  )

  const [message] = threadState.onGoingConv.msgChunks['input-old']
  assert.equal(message.run_id, undefined)
  assert.equal(message.extra_metadata.run_id, undefined)
})

test('子线程 init 不继承父订阅 Run 关联', () => {
  const threadState = {
    pendingInputId: 'input-child',
    replyLoadingVisible: false,
    contextCompressing: false,
    onGoingConv: { msgChunks: {} }
  }
  const { handleStreamChunk } = useAgentStreamHandler({
    getThreadState: () => threadState,
    processApprovalInStream: () => false,
    currentAgentId: { value: 'agent-1' }
  })

  handleStreamChunk(
    {
      status: 'init',
      input_id: 'input-child',
      stream_run_id: 'parent-run',
      stream_thread_id: 'parent-thread',
      msg: { type: 'human', content: '子线程输入' }
    },
    'child-thread'
  )

  const [message] = threadState.onGoingConv.msgChunks['input-child']
  assert.equal(message.run_id, undefined)
})


const flush = () => new Promise((resolve) => setTimeout(resolve, 0))

test('队列只展示持久 Input，消费关联确定后启动所属 Turn 的 Run', async () => {
  const state = {
    queuedInputs: [{ input_id: 'input-1', status: 'sending', message: { type: 'human', content: '图片' } }],
    inputMonitors: {},
    onGoingConv: { msgChunks: {} },
    activeRunId: null
  }
  const originalQueue = agentApi.getThreadQueue
  const originalInput = agentApi.getThreadInput
  const started = []
  agentApi.getThreadQueue = async () => ({
    inputs: [{ input_id: 'input-1', status: 'pending', content: '图片' }],
    status: 'running', queue_paused: false
  })
  agentApi.getThreadInput = async () => ({
    input_id: 'input-1', status: 'consumed', turn_id: 'turn-1', run_id: 'run-1'
  })
  try {
    const queue = useAgentInputQueue({
      getThreadState: () => state,
      resetOnGoingConv: () => { state.onGoingConv = { msgChunks: {} } },
      startRunStream: (...args) => started.push(args)
    })
    await queue.syncQueuedInputs('thread-1')
    await flush()
    assert.equal(state.queueSnapshot.status, 'running')
    assert.deepEqual(state.queuedInputs, [])
    assert.equal(state.onGoingConv.msgChunks['input-1'][0].content, '图片')
    assert.deepEqual(started, [[
      'thread-1', 'run-1', null, { turnId: 'turn-1', inputId: 'input-1' }
    ]])
  } finally {
    agentApi.getThreadQueue = originalQueue
    agentApi.getThreadInput = originalInput
  }
})

test('游标过期事件回读持久历史和 Turn 后继续接收终态', async () => {
  const state = {
    activeRunId: null, currentTurnId: null, threadCursor: null,
    runStreamAbortController: null, isStreaming: false,
    onGoingConv: { msgChunks: {} }
  }
  const originalStream = agentApi.streamThreadEvents
  const originalTurn = agentApi.getThreadTurn
  let controller
  let historyReads = 0
  agentApi.streamThreadEvents = async () => new Response(new ReadableStream({
    start(value) { controller = value }
  }))
  agentApi.getThreadTurn = async () => ({ status: 'running', current_run_id: 'run-1' })
  try {
    const stream = useAgentRunStream({
      getThreadState: () => state,
      currentAgentId: { value: 'agent-1' },
      handleStreamChunk: () => {},
      fetchThreadMessages: async () => { historyReads += 1 },
      fetchAgentState: () => {},
      resetOnGoingConv: () => {},
      streamSmoother: { flushThread: () => {} }
    })
    const running = stream.startRunStream('thread-1', 'run-1', null, { turnId: 'turn-1' })
    await flush()
    controller.enqueue(new TextEncoder().encode(
      'event: agent.thread.resync\nid: 4\ndata: {"type":"agent.thread.resync","cursor":"4","payload":{"reason":"run_events_expired"}}\n\n'
    ))
    await flush()
    assert.equal(historyReads, 1)
    assert.equal(state.threadCursor, '4')
    assert.equal(state.isStreaming, true)
    controller.enqueue(new TextEncoder().encode(
      'event: agent.thread.turn.completed\nid: 5\ndata: {"type":"agent.thread.turn.completed","turn_id":"turn-1","run_id":"run-1","payload":{}}\n\n'
    ))
    controller.close()
    await running
    await flush()
    assert.equal(state.isStreaming, false)
  } finally {
    agentApi.streamThreadEvents = originalStream
    agentApi.getThreadTurn = originalTurn
  }
})

test('尚未接收的本地输入不能被取消或订阅', async () => {
  const state = {
    queuedInputs: [{ input_id: 'local-key', status: 'sending' }],
    inputMonitors: {},
    onGoingConv: { msgChunks: {} }
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

test('Turn.waiting 到达后立即禁发，不依赖等待点界面恢复', async () => {
  const state = {
    activeRunId: null, currentTurnId: null, turnStatus: null, pendingInterrupt: null,
    runStreamAbortController: null, isStreaming: false,
    onGoingConv: { msgChunks: {} }
  }
  const originalStream = agentApi.streamThreadEvents
  agentApi.streamThreadEvents = async () => new Response(
    'event: agent.thread.turn.waiting\nid: 1\ndata: {"type":"agent.thread.turn.waiting","turn_id":"turn-1","run_id":"run-1","payload":{}}\n\n'
  )
  try {
    const stream = useAgentRunStream({
      getThreadState: () => state,
      currentAgentId: { value: 'agent-1' },
      handleStreamChunk: () => {},
      fetchThreadMessages: async () => {},
      fetchAgentState: () => {},
      resetOnGoingConv: () => {},
      streamSmoother: { flushThread: () => {} }
    })
    await stream.startRunStream('thread-1', 'run-1', null, { turnId: 'turn-1' })
    assert.equal(state.isStreaming, false)
    assert.equal(state.currentTurnId, 'turn-1')
    assert.equal(state.turnStatus, 'waiting')
    assert.equal(state.pendingInterrupt, null)
    assert.equal(isThreadWaitingForUserAction(state), true)
  } finally {
    agentApi.streamThreadEvents = originalStream
  }
})

test('Run yielded 不能结束 Turn，只有 Turn.completed 收尾', async () => {
  const state = {
    activeRunId: null, currentTurnId: null, threadCursor: null,
    runStreamAbortController: null, isStreaming: false,
    onGoingConv: { msgChunks: {} }
  }
  const originalStream = agentApi.streamThreadEvents
  let controller
  const response = new Response(new ReadableStream({
    start(value) { controller = value }
  }))
  agentApi.streamThreadEvents = async () => response
  const terminals = []
  try {
    const stream = useAgentRunStream({
      getThreadState: () => state,
      currentAgentId: { value: 'agent-1' },
      handleStreamChunk: () => {},
      fetchThreadMessages: async () => {},
      fetchAgentState: () => {},
      resetOnGoingConv: () => {},
      onScrollToBottom: () => {},
      streamSmoother: { flushThread: () => {} },
      onTerminalDetected: (value) => terminals.push(value)
    })
    const running = stream.startRunStream('thread-1', 'run-1', null, { turnId: 'turn-1' })
    controller.enqueue(new TextEncoder().encode(
      'event: agent.thread.run.yielded\nid: 1\ndata: {"type":"agent.thread.run.yielded","turn_id":"turn-1","run_id":"run-1","payload":{}}\n\n'
    ))
    await flush()
    assert.equal(state.isStreaming, true)
    assert.equal(terminals.length, 0)

    controller.enqueue(new TextEncoder().encode(
      'event: agent.thread.turn.completed\nid: 2\ndata: {"type":"agent.thread.turn.completed","turn_id":"turn-1","run_id":"run-2","payload":{}}\n\n'
    ))
    controller.close()
    await running
    await flush()
    assert.equal(state.isStreaming, false)
    assert.equal(state.activeRunId, null)
    assert.equal(state.threadCursor, '2')
    assert.deepEqual(terminals.map((item) => item.runId), ['run-2'])
  } finally {
    agentApi.streamThreadEvents = originalStream
  }
})

test('恢复 Run 首次订阅时忽略同 Turn 旧 Run 的 waiting 重放', async () => {
  const state = {
    activeRunId: null, currentTurnId: null, threadCursor: null,
    runStreamAbortController: null, isStreaming: false,
    onGoingConv: { msgChunks: {} }
  }
  const originalStream = agentApi.streamThreadEvents
  agentApi.streamThreadEvents = async () => new Response([
    'event: agent.thread.turn.resumed\nid: 1\ndata: {"type":"agent.thread.turn.resumed","turn_id":"turn-1","run_id":"run-2"}\n\n',
    'event: agent.thread.turn.waiting\nid: 2\ndata: {"type":"agent.thread.turn.waiting","turn_id":"turn-1","run_id":"run-1"}\n\n',
    'event: agent.thread.output\nid: 3\ndata: {"type":"agent.thread.output","turn_id":"turn-1","run_id":"run-2","payload":{"chunk":{"event":"text","content":"完成"}}}\n\n',
    'event: agent.thread.turn.completed\nid: 4\ndata: {"type":"agent.thread.turn.completed","turn_id":"turn-1","run_id":"run-2"}\n\n'
  ].join(''))
  const chunks = []
  const waiting = []
  try {
    const stream = useAgentRunStream({
      getThreadState: () => state,
      currentAgentId: { value: 'agent-1' },
      handleStreamChunk: (chunk) => chunks.push(chunk),
      fetchThreadMessages: async () => {},
      fetchAgentState: () => {},
      resetOnGoingConv: () => {},
      streamSmoother: { flushThread: () => {} },
      onInterruptDetected: (value) => waiting.push(value)
    })
    await stream.startRunStream('thread-1', 'run-2', null, { turnId: 'turn-1' })
    await flush()
    assert.equal(state.turnStatus, 'completed')
    assert.equal(state.currentTurnId, null)
    assert.equal(state.threadCursor, '4')
    assert.deepEqual(waiting, [])
    assert.deepEqual(chunks.map((chunk) => chunk.content), ['完成'])
  } finally {
    agentApi.streamThreadEvents = originalStream
  }
})

test('其他 Turn 的终态不能收尾当前 Turn', async () => {
  const state = {
    activeRunId: null, currentTurnId: null, threadCursor: null,
    runStreamAbortController: null, isStreaming: false,
    onGoingConv: { msgChunks: {} }
  }
  const originalStream = agentApi.streamThreadEvents
  const originalTurn = agentApi.getThreadTurn
  agentApi.streamThreadEvents = async () => new Response(
    'event: agent.thread.turn.completed\nid: 1\ndata: {"type":"agent.thread.turn.completed","turn_id":"other","run_id":"run-x","payload":{}}\n\n'
  )
  agentApi.getThreadTurn = async () => ({
    status: 'running', current_run_id: 'run-1'
  })
  try {
    const stream = useAgentRunStream({
      getThreadState: () => state,
      currentAgentId: { value: 'agent-1' },
      handleStreamChunk: () => {},
      fetchThreadMessages: async () => {},
      fetchAgentState: () => {},
      resetOnGoingConv: () => {},
      streamSmoother: { flushThread: () => {} }
    })
    await stream.startRunStream('thread-1', 'run-1', null, { turnId: 'turn-1' })
    assert.equal(state.isStreaming, true)
    assert.equal(state.currentTurnId, 'turn-1')
    stream.stopRunStreamSubscription('thread-1')
  } finally {
    agentApi.streamThreadEvents = originalStream
    agentApi.getThreadTurn = originalTurn
  }
})
