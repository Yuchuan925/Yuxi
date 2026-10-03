import assert from 'node:assert/strict'
import test from 'node:test'
import { createItemState, applyAgentEvent, mergeItemSnapshot, itemsToMessages } from '../../src/modules/conversation/model/agentItems.js'

const message = (id = 'm', status = 'in_progress') => ({
  id, type: 'message', role: 'assistant', turn_id: 'turn', status, phase: 'commentary',
  content: [{ type: 'output_text', text: '' }, { type: 'output_text', text: '' }],
  yuxi: { run_id: 'run', output_index: 0, message_id: 1 }
})
const event = (type, fields = {}, id = type) => ({
  type: `agent.session.turn.${type}`, event_id: id, session_id: 'thread', turn_id: 'turn',
  item_id: 'm', output_index: 0, content_index: 0, ...fields
})

test('多内容块分别追加，重放按 event_id 去重，done 完整替换', () => {
  const state = createItemState()
  applyAgentEvent(state, event('item.added', { item: message() }))
  const first = event('output_text.delta', { delta: 'wrong' }, 'a')
  applyAgentEvent(state, first)
  applyAgentEvent(state, first)
  applyAgentEvent(state, event('output_text.delta', { content_index: 1, delta: 'second' }, 'b'))
  assert.deepEqual(state.items.m.content.map((part) => part.text), ['wrong', 'second'])
  applyAgentEvent(state, event('output_text.done', { text: 'complete' }, 'done'))
  applyAgentEvent(state, event('output_text.delta', { delta: 'late' }, 'late'))
  assert.equal(state.items.m.content[0].text, 'complete')
  const full = message('m', 'completed')
  full.content = [{ type: 'output_text', text: 'final' }]
  full.phase = 'final_answer'
  applyAgentEvent(state, event('item.done', { item: full }))
  applyAgentEvent(state, event('output_text.delta', { delta: 'late' }, 'after-item'))
  assert.equal(state.items.m.content[0].text, 'final')
  assert.equal(state.items.m.phase, 'final_answer')
})

test('快照克隆保留结构化值且不与输入对象共享引用', () => {
  const state = createItemState()
  const snapshot = message('structured')
  snapshot.optional = undefined
  mergeItemSnapshot(state, [snapshot])

  assert.equal(state.items.structured.optional, undefined)
  assert.notStrictEqual(state.items.structured, snapshot)
  assert.notStrictEqual(state.items.structured.content, snapshot.content)
})

test('Redis 过期重读快照，终态拒绝旧 added 和 delta，保存明确 incomplete', () => {
  const state = createItemState()
  const full = message('m', 'incomplete')
  full.content[0].text = 'partial'
  mergeItemSnapshot(state, [full])
  applyAgentEvent(state, event('item.added', { item: message() }))
  applyAgentEvent(state, event('output_text.delta', { delta: 'old' }))
  mergeItemSnapshot(state, [message()])
  assert.equal(state.items.m.status, 'incomplete')
  assert.equal(state.items.m.content[0].text, 'partial')
})

test('纯工具和失败结果刷新后可见，相同 call_id 不跨 Run 关联', () => {
  const call = { id: 'call', type: 'function_call', name: 'search', arguments: { q: 'x' },
    call_id: 'same', status: 'failed', turn_id: 'turn', yuxi: { run_id: 'run', output_index: 1, message_id: 2 } }
  const output = { id: 'result', type: 'function_call_output', call_id: 'same', output: null, error: 'failed',
    status: 'failed', turn_id: 'turn', yuxi: { run_id: 'run', output_index: 2, message_id: 3 } }
  const unrelated = { ...output, id: 'other', output: 'wrong', yuxi: { ...output.yuxi, run_id: 'other' } }
  const [view] = itemsToMessages([call, output, unrelated])
  assert.equal(view.tool_calls[0].tool_call_result.content, 'failed')
  assert.equal(view.tool_calls[0].status, 'error')
  assert.deepEqual(view.tool_calls[0].args, { q: 'x' })
})

test('原始推理 done 替换并关闭增量，缺失推理不制造占位', () => {
  const state = createItemState()
  mergeItemSnapshot(state, [message()])
  const reasoning = (type, fields) => ({ ...event(type, fields), type: `yuxi.session.turn.${type}` })
  applyAgentEvent(state, reasoning('reasoning.delta', { delta: 'part' }))
  applyAgentEvent(state, reasoning('reasoning.done', { text: 'full' }))
  applyAgentEvent(state, { ...reasoning('reasoning.delta', { delta: 'late' }), event_id: 'late-reasoning' })
  assert.equal(itemsToMessages(Object.values(state.items))[0].reasoning_content, 'full')
  assert.equal(itemsToMessages([message('empty')])[0].reasoning_content, '')
})


test('恢复 Run 的工具结果只通过明确 call_item_id 关联原调用', () => {
  const call = { id: 'original-call', type: 'function_call', name: 'ask_user_question', arguments: {},
    call_id: 'same', status: 'completed', turn_id: 'turn', yuxi: { run_id: 'original', message_id: 1 } }
  const output = { id: 'resumed-result', type: 'function_call_output', call_id: 'same', output: 'yes',
    status: 'completed', turn_id: 'turn', yuxi: { run_id: 'resume', message_id: 2,
      call_item_id: 'original-call', call_run_id: 'original' } }
  const [view] = itemsToMessages([call, output])
  assert.equal(view.tool_calls[0].tool_call_result.content, 'yes')
  assert.equal(view.tool_calls[0].status, 'success')
})

test('恢复完成的工具拒绝迟到 incomplete，最终回答不被旧 commentary 降级', () => {
  const state = createItemState()
  const call = { id: 'call', type: 'function_call', status: 'incomplete', yuxi: {} }
  mergeItemSnapshot(state, [call])
  applyAgentEvent(state, event('item.done', { item: { ...call, status: 'completed' } }, 'resumed'))
  mergeItemSnapshot(state, [call])
  applyAgentEvent(state, event('item.done', { item: call }, 'old-call'))
  assert.equal(state.items.call.status, 'completed')
  const final = { ...message('m', 'completed'), phase: 'final_answer' }
  mergeItemSnapshot(state, [final])
  applyAgentEvent(state, event('item.done', { item: message('m', 'completed') }, 'old-message'))
  assert.equal(state.items.m.phase, 'final_answer')
})

test('活动多块 resync 恢复已结束块，保留新块增量并拒绝旧块迟到 delta', () => {
  const state = createItemState()
  const local = message()
  local.content[0].text = 'part'
  local.content[1].text = 'newer'
  local.yuxi.reasoning = { 0: 'raw part', 1: 'new raw' }
  mergeItemSnapshot(state, [local])
  const snapshot = message()
  snapshot.content[0].text = 'whole'
  snapshot.yuxi.completed_content_indices = [0]
  snapshot.yuxi.completed_reasoning_indices = [0]
  snapshot.yuxi.reasoning = { 0: 'raw whole' }
  mergeItemSnapshot(state, [snapshot])
  applyAgentEvent(state, event('output_text.delta', { delta: 'late' }, 'late-block'))
  applyAgentEvent(state, event('output_text.delta', { content_index: 1, delta: ' tail' }, 'new-block'))
  applyAgentEvent(state, { ...event('reasoning.delta', { delta: 'late' }, 'late-raw'),
    type: 'yuxi.session.turn.reasoning.delta' })
  assert.deepEqual(state.items.m.content.map((part) => part.text), ['whole', 'newer tail'])
  assert.deepEqual(state.items.m.yuxi.reasoning, { 0: 'raw whole', 1: 'new raw' })
  const fresh = createItemState()
  mergeItemSnapshot(fresh, [snapshot])
  applyAgentEvent(fresh, event('output_text.delta', { delta: 'late' }, 'fresh-late'))
  assert.equal(fresh.items.m.content[0].text, 'whole')
})

test('未完成调用保留明确中断状态，不派生为仍在运行', () => {
  const call = { id: 'stopped', type: 'function_call', name: 'execute', arguments: {},
    call_id: 'stopped', status: 'incomplete', turn_id: 'turn', yuxi: { run_id: 'run', message_id: 1 } }
  const [view] = itemsToMessages([call])
  assert.equal(view.tool_calls[0].status, 'incomplete')
})
