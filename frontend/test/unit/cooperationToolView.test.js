import assert from 'node:assert/strict'
import test from 'node:test'
import { collectCooperationTasks, getCooperationToolTargets } from '../../src/modules/session/model/cooperationToolView.js'

const sessions = [
  { session_id: 'child-1', name: 'tester', path: '/root/tester' },
  { session_id: 'child-2', name: 'tester', path: '/root/other/tester' }
]
test('协作入口用精确回执匹配多个等待目标，相同名称保持不同会话', () => {
  const messages = [{ tool_calls: [
    { name: 'create_session', tool_call_result: { content: JSON.stringify({ session_id: 'child-1', input_id: 'input-1' }) } },
    { name: 'submit_input', tool_call_result: { content: { session_id: 'child-2', input_id: 'input-2' } } }
  ] }]
  const tasks = collectCooperationTasks(messages)
  assert.deepEqual(getCooperationToolTargets({ name: 'wait_inputs', args: { input_ids: ['input-1', 'input-2'] } }, { sessions, tasks }), [
    { sessionId: 'child-1', name: 'tester' }, { sessionId: 'child-2', name: 'tester' }
  ])
  assert.deepEqual(getCooperationToolTargets({ name: 'wait_sessions', args: { targets: ['/root/other/tester'] } }, { sessions, tasks }), [
    { sessionId: 'child-2', name: 'tester' }
  ])
})
test('未知任务不猜测会话，相同名称也不作为导航身份', () => {
  const context = { sessions, tasks: new Map() }
  assert.deepEqual(getCooperationToolTargets({ name: 'wait_inputs', args: { input_ids: ['missing'] } }, context), [{ sessionId: undefined, name: '' }])
  assert.deepEqual(getCooperationToolTargets({ name: 'submit_input', args: { target: 'tester' } }, context), [{ sessionId: undefined, name: '' }])
})
