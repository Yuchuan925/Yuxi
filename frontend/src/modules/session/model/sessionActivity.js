/** 将当前轮次事件映射为侧边栏活动状态，未读状态独立保留。 */
export const activityFromTurnEvent = (event) => {
  if (event.type === 'yuxi.session.turn.waiting') {
    return {
      cooperation: 'waiting_cooperation', approval: 'waiting_approval', answer: 'waiting_answer'
    }[event.waitpoint?.kind] || 'waiting'
  }
  return {
    'agent.session.turn.created': 'queued',
    'agent.session.turn.in_progress': 'running',
    'agent.session.turn.completed': 'idle',
    'agent.session.turn.cancelled': 'idle',
    'agent.session.turn.failed': 'failed'
  }[event.type]
}

export const SESSION_ACTIVITY_LABELS = {
  waiting_cooperation: '等待协作',
  waiting_approval: '等待批准', waiting_answer: '等待回答', waiting: '等待中'
}
