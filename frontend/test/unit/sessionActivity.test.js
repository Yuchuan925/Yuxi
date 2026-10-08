import assert from 'node:assert/strict'
import test from 'node:test'
import { activityFromTurnEvent } from '../../src/modules/session/model/sessionActivity.js'

test('等待事件区分协作、审批和回答，恢复及终态清除等待标记', () => {
  for (const [kind, expected] of Object.entries({
    cooperation: 'waiting_cooperation', approval: 'waiting_approval', answer: 'waiting_answer'
  })) {
    assert.equal(activityFromTurnEvent({ type: 'yuxi.session.turn.waiting', waitpoint: { kind } }), expected)
  }
  assert.equal(activityFromTurnEvent({ type: 'agent.session.turn.in_progress' }), 'running')
  assert.equal(activityFromTurnEvent({ type: 'agent.session.turn.completed' }), 'idle')
  assert.equal(activityFromTurnEvent({ type: 'agent.session.turn.failed' }), 'failed')
  assert.equal(activityFromTurnEvent({ type: 'agent.session.turn.output_text.delta' }), undefined)
})
