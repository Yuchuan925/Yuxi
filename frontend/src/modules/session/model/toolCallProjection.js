export const TOOL_NAME_MAP = {
  create_session: '创建协作会话',
  send_message: '投递协作消息',
  submit_input: '提交工作',
  cancel_turn: '取消轮次',
  wait_sessions: '等待协作更新',
  list_sessions: '查看协作树',
  bash: '执行命令',
  cmd: '执行命令',
  execute: '执行命令',
  run_shell_command: '执行命令',
  ls: '列出目录',
  list_directory: '列出目录',
  glob: '匹配文件路径',
  grep: '搜索文件内容',
  read_file: '读取文件',
  remember_memory: '更新记忆',
  write_file: '写入文件',
  edit_file: '编辑文件',
  replace: '编辑文件',
  search_file: '搜索知识库文件',
  search_file_content: '搜索文件内容',
  write_todos: '更新任务清单',
  text_to_img_qwen_image: '生成图片',
  query_kb: '搜索知识库',
  list_kbs: '查看知识库列表',
  find_kb_document: '查找知识库文档',
  open_kb_document: '打开知识库文档',
  calculator: '计算器',
  web_search: '网络搜索',
  tavily_search: '网络搜索',
  doubao_search: '网络搜索',
  ocr_parse_file: 'OCR识别文件',
  mysql_list_tables: '查看数据库表',
  mysql_describe_table: '查看表结构',
  mysql_query: '执行SQL查询',
  ask_user_question: '向用户提问'
}

// Keep intentionally hidden tool calls centralized so group summaries and renderers stay consistent.
export const HIDDEN_TOOL_CALL_IDS = ['present_artifacts']

export const getToolCallId = (toolCall) => toolCall?.name || toolCall?.function?.name || ''

export const getToolName = (toolId) => TOOL_NAME_MAP[toolId] || toolId

// 从工具元数据列表（完整工具列表或 builtin options）中按工具 id 查找对应元数据
export const findToolInList = (toolId, toolsList) =>
  (toolsList || []).find((t) => (t.slug ?? t.key ?? t.id) === toolId)

export const isHiddenToolCall = (toolCall) => HIDDEN_TOOL_CALL_IDS.includes(getToolCallId(toolCall))

export const isValidToolCall = (toolCall) => {
  return Boolean(
    toolCall &&
    (toolCall.id || toolCall.name || toolCall.function?.name) &&
    (toolCall.args !== undefined ||
      toolCall.function?.arguments !== undefined ||
      toolCall.tool_call_result !== undefined)
  )
}

export const parseToolCallArgs = (toolCall) => {
  const args = toolCall?.args ?? toolCall?.function?.arguments
  if (!args) return {}
  if (typeof args === 'object') return args
  try {
    return JSON.parse(args)
  } catch {
    return {}
  }
}

export const parseToolCallResult = (toolCall) => {
  const content = toolCall?.tool_call_result?.content ?? toolCall?.result
  if (content == null || content === '') return null
  if (typeof content === 'object') return content
  try {
    return JSON.parse(content)
  } catch {
    return null
  }
}

/** 以调用、ToolMessage 和结果顶层的错误状态优先决定工具展示状态。 */
export const getToolCallStatus = (toolCall) => {
  const statuses = [
    toolCall?.status,
    toolCall?.tool_call_result?.status,
    parseToolCallResult(toolCall)?.status
  ]
  if (statuses.some((status) => status === 'error' || status === 'failed')) return 'error'
  if (statuses.includes('incomplete')) return 'incomplete'
  if (
    toolCall?.tool_call_result != null ||
    toolCall?.result != null ||
    toolCall?.status === 'success' ||
    toolCall?.status === 'completed'
  )
    return 'completed'
  return 'running'
}

export const getToolCallDisplayStatus = (toolCall) => getToolCallStatus(toolCall)

export const normalizeToolCalls = (toolCalls, { includeHidden = false, mapToolCall } = {}) => {
  if (!Array.isArray(toolCalls)) return []

  return toolCalls
    .filter((toolCall) => {
      if (!isValidToolCall(toolCall)) return false
      return includeHidden || !isHiddenToolCall(toolCall)
    })
    .map((toolCall) => (mapToolCall ? mapToolCall(toolCall) : toolCall))
}
