import { getToolCallId, parseToolCallArgs, parseToolCallResult } from './toolCallProjection.js'

export const COOPERATION_ACTIONS = {
  create_session: '创建',
  submit_input: '提交工作至',
  send_message: '发送消息至',
  cancel_turn: '停止',
  wait_sessions: '等待',
  wait_inputs: '等待',
  get_result: '查看结果',
  list_sessions: '查看协作会话'
}

/** 从明确的工具回执建立任务与会话的对应关系。 */
export const collectCooperationTasks = (messages) => {
  const tasks = new Map()
  for (const message of messages) {
    for (const call of message.tool_calls || []) {
      if (!(getToolCallId(call) in COOPERATION_ACTIONS)) continue
      const result = parseToolCallResult(call)
      for (const item of [result, ...(result?.results || [])]) {
        if (item?.input_id && item.session_id) tasks.set(item.input_id, item.session_id)
      }
    }
  }
  return tasks
}

/** 使用精确会话 ID、路径或任务回执生成协作入口，不按名称猜测归属。 */
export const getCooperationToolTargets = (call, { sessions, tasks }) => {
  const id = getToolCallId(call)
  const args = parseToolCallArgs(call)
  const result = parseToolCallResult(call)
  let targets
  if (id === 'list_sessions') targets = result?.sessions || []
  else if (id === 'wait_inputs') {
    targets = (args.input_ids || []).map((inputId) => ({
      session_id: result?.results?.find((item) => item.input_id === inputId)?.session_id || tasks.get(inputId)
    }))
  } else if (id === 'get_result') {
    targets = [{ session_id: result?.session_id || tasks.get(args.input_id) ||
      sessions.find((member) => args.turn_id && member.turn_id === args.turn_id)?.session_id }]
  } else if (id === 'wait_sessions') targets = (args.targets || []).map((target) => ({ target }))
  else targets = [{ session_id: result?.session_id, target: args.target, name: result?.name || args.name,
    path: result?.path }]

  return targets.map((target) => {
    const session = sessions.find((member) =>
      (target.session_id && member.session_id === target.session_id) ||
      (target.target && [member.session_id, member.path].includes(target.target))
    )
    return {
      sessionId: target.session_id || session?.session_id,
      name: session?.name || target.name || (target.path || (target.target?.startsWith('/') ? target.target : ''))?.split('/').pop() || ''
    }
  }).filter((target, index, list) => !target.sessionId ||
    list.findIndex((item) => item.sessionId === target.sessionId) === index)
}
