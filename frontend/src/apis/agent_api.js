import { apiGet, apiPost, apiDelete, apiPut, apiRequest } from './base'
import { getApiAuthHeaders } from './auth_context'

/**
 * 智能体API模块
 * 包含智能体管理、聊天、配置等功能
 * 权限要求: 任何已登录用户（普通用户、管理员、超级管理员）
 */

// =============================================================================
// === 智能体聊天分组 ===
// =============================================================================

export const agentApi = {
  /**
   * 获取智能体列表
   * @returns {Promise} - 智能体列表
   */
  getAgents: ({ includeSubagents = false } = {}) => {
    const params = new URLSearchParams()
    if (includeSubagents) params.set('include_subagents', 'true')
    const query = params.toString()
    return apiGet(query ? `/api/agent?${query}` : '/api/agent')
  },

  getAgentBackends: () => apiGet('/api/agent/backends'),

  /**
   * 获取单个智能体详情
   * @param {string} agentId - 智能体ID
   * @returns {Promise} - 智能体详情
   */
  getAgentDetail: (agentId) => apiGet(`/api/agent/${agentId}`),

  /**
   * 获取智能体历史消息
   * @param {string} agentId - 智能体ID
   * @param {string} threadId - 会话ID
   * @returns {Promise} - 历史消息
   */
  // 线程阅读快照：消息绑定 Turn/Run，结果由 Turn 的 result_run_id 指定。
  getAgentHistory: (threadId, options = {}) =>
    apiGet(`/api/v1/agents/threads/${threadId}/history`, options),

  /**
   * 获取会话内持久化的 Model/Tool 生命周期审计
   * @param {string} threadId - 会话ID
   * @returns {Promise<{audits: Array, truncated: boolean}>}
   */
  getThreadMessageAudits: (threadId) => apiGet(`/api/v1/agents/threads/${threadId}/audits`),

  /**
   * 获取指定会话的 AgentState
   * @param {string} agentId - 智能体ID
   * @param {string} threadId - 会话ID
   * @returns {Promise} - AgentState
   */
  getAgentState: (threadId, { includeMessages = false } = {}) =>
    apiGet(`/api/v1/agents/threads/${threadId}/state${includeMessages ? '?include_messages=true' : ''}`),

  /**
   * 提交线程级主动上下文压缩
   */
  compressThreadContext: (threadId) => apiPost(`/api/v1/agents/threads/${threadId}/compress`, {}),

  createAgent: (payload) => apiPost('/api/agent', payload),

  updateAgent: (agentId, payload) => apiPut(`/api/agent/${agentId}`, payload),

  deleteAgent: (agentId) => apiDelete(`/api/agent/${agentId}`),

  /** 产品对话以明确的 follow-up 或 steer 模式提交 Input。 */
  sendThreadMessage: (threadId, data) => {
    const content = [
      ...(data.query ? [{ type: 'input_text', text: data.query }] : []),
      ...(data.image_content || []).map((image) => ({
        type: 'input_image',
        image_url: image.startsWith('data:image/') ? image : `data:image/jpeg;base64,${image}`
      }))
    ]
    return apiPost(
      `/api/v1/agents/threads/${threadId}/events`,
      {
        events: [{
          type: 'agent.session.input.message',
          input: [{ role: 'user', content }],
          yuxi: {
            mode: data.mode,
            ...(data.mode !== 'steer' ? {
              model_spec: data.model_spec, tool_approval_mode: data.tool_approval_mode
            } : {}),
            attachment_file_ids: data.attachment_file_ids || []
          }
        }]
      },
      { headers: { 'Idempotency-Key': data.idempotency_key } }
    )
  },

  resumeThreadTurn: (threadId, data) =>
    apiPost(
      `/api/v1/agents/threads/${threadId}/events`,
      { events: [{ type: 'yuxi.session.input.resume', turn_id: data.turn_id,
        waitpoint_id: data.waitpoint_id, response: data.response }] },
      { headers: { 'Idempotency-Key': data.idempotency_key } }
    ),

  cancelThreadTurn: (threadId, turnId, idempotencyKey, expectedRunId = null) =>
    apiPost(
      `/api/v1/agents/threads/${threadId}/events`,
      { events: [{ type: 'agent.session.input.cancel', yuxi: { turn_id: turnId,
        ...(expectedRunId ? { expected_run_id: expectedRunId } : {}) } }] },
      { headers: { 'Idempotency-Key': idempotencyKey } }
    ),

  getPublicThread: (threadId) => apiGet(`/api/v1/agents/threads/${threadId}`),

  getThreadTurn: (threadId, turnId, options = {}) =>
    apiGet(`/api/v1/agents/threads/${threadId}/turns/${turnId}`, options),

  getThreadInput: (threadId, inputId) =>
    apiGet(`/api/v1/agents/threads/${threadId}/inputs/${inputId}`),

  streamThreadEvents: (threadId, afterCursor = null, { signal } = {}) => {
    const headers = {
      ...getApiAuthHeaders()
    }
    if (afterCursor) headers['Last-Event-ID'] = afterCursor
    return fetch(`/api/v1/agents/threads/${threadId}/events`, {
      method: 'GET', headers, signal
    })
  },

  getThreadQueue: (threadId) => apiGet(`/api/v1/agents/threads/${threadId}/queue`),

  /**
   * 手动继续 failed/cancelled 后暂停的线程队列
   */
  continueThreadQueue: (threadId, idempotencyKey) => apiPost(
    `/api/v1/agents/threads/${threadId}/events`,
    { events: [{ type: 'yuxi.session.input.continue' }] },
    { headers: { 'Idempotency-Key': idempotencyKey } }
  ),

  /**
   * 取消排队中的请求
   */
  cancelThreadInput: (threadId, inputId, idempotencyKey) => apiPost(
    `/api/v1/agents/threads/${threadId}/events`,
    { events: [{ type: 'yuxi.session.input.cancel_input', input_id: inputId }] },
    { headers: { 'Idempotency-Key': idempotencyKey } }
  ),

  /**
   * 获取 Run 状态
   * @param {string} runId - run ID
   * @returns {Promise<Object>}
   */
  getAgentRun: (threadId, runId, options = {}) =>
    apiGet(`/api/v1/agents/threads/${threadId}/runs/${runId}`, options)
}

// =============================================================================
// === 多模态图片支持分组 ===
// =============================================================================

export const multimodalApi = {
  /**
   * 上传图片并获取base64编码
   * @param {File} file - 图片文件
   * @returns {Promise} - 上传结果
   */
  uploadImage: (file) => {
    const formData = new FormData()
    formData.append('file', file)

    return apiRequest(
      '/api/v1/agents/images',
      {
        method: 'POST',
        body: formData
      },
      true
    )
  }
}

// =============================================================================
// === 对话线程分组 ===
// =============================================================================

export const threadApi = {
  /**
   * 获取对话线程列表
   * @param {string | null | undefined} agentId - 智能体ID，可选；不传时返回全部智能体对话
   * @param {number} limit - 返回数量限制，默认100
   * @param {number} offset - 偏移量，默认0
   * @returns {Promise} - 对话线程列表
   */
  getThreads: (agentId = null, limit = 100, offset = 0) => {
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset)
    })
    if (agentId) {
      params.set('agent_id', agentId)
    }
    const url = `/api/v1/agents/threads?${params.toString()}`
    return apiGet(url)
  },

  /**
   * 搜索历史对话
   * @param {string} query - 搜索关键词
   * @param {Object} options - 搜索选项
   * @param {string | null | undefined} options.agentId - 智能体ID，可选
   * @param {number} options.limit - 返回数量限制
   * @param {number} options.offset - 偏移量
   * @returns {Promise} - 搜索结果
   */
  searchThreads: (query, { agentId = null, limit = 20, offset = 0 } = {}) => {
    const params = new URLSearchParams({
      q: query,
      limit: String(limit),
      offset: String(offset)
    })
    if (agentId) {
      params.set('agent_id', agentId)
    }
    return apiGet(`/api/v1/agents/threads/search?${params.toString()}`)
  },

  /**
   * 创建新对话线程
   * @param {string} agentId - 智能体ID
   * @param {string} title - 对话标题
   * @param {Object} metadata - 元数据
   * @returns {Promise} - 创建结果
   */
  createThread: async (agentId, title, metadata, { requestId, projectId } = {}) => {
    const thread = await apiPost(
      '/api/v1/agents/threads',
      {
        agent_id: agentId,
        title: title || '新的对话',
        tool_approval_mode: metadata?.tool_approval_mode,
        ...(projectId ? { project_id: projectId } : {})
      },
      { headers: { 'Idempotency-Key': requestId } }
    )
    return {
      id: thread.id,
      agent_id: agentId,
      title: thread.title,
      project_id: thread.project_id,
      metadata: metadata || {},
      thread_status: 'active'
    }
  },

  /**
   * 更新对话线程
   * @param {string} threadId - 对话线程ID
   * @param {string} title - 对话标题
   * @param {boolean} is_pinned - 是否置顶
   * @param {string} toolApprovalMode - 工具审批模式
   * @returns {Promise} - 更新结果
   */
  updateThread: (threadId, title, is_pinned, toolApprovalMode, modelSpec) =>
    apiRequest(`/api/v1/agents/threads/${threadId}`, {
      method: 'PATCH',
      body: JSON.stringify({ title, is_pinned, tool_approval_mode: toolApprovalMode,
        model_spec: modelSpec })
    }),

  /**
   * 记录用户已查看该线程的最新顶层 run，清除侧边栏未读状态
   * @param {string} threadId - 对话线程ID
   * @returns {Promise} - 更新后的线程
   */
  markThreadViewed: (threadId) => apiPost(`/api/v1/agents/threads/${threadId}/viewed`),

  /**
   * 删除对话线程
   * @param {string} threadId - 对话线程ID
   * @returns {Promise} - 删除结果
   */
  archiveThread: (threadId) => apiPost(`/api/v1/agents/threads/${threadId}/archive`),

  /**
   * 获取线程附件列表
   * @param {string} threadId - 对话线程ID
   * @returns {Promise}
   */
  getThreadAttachments: (threadId) => apiGet(`/api/v1/agents/threads/${threadId}/attachments`),

  /**
   * 获取线程文件下载/预览 URL
   * @param {string} threadId
   * @param {string} path
   * @param {boolean} download
   * @returns {string}
   */
  getThreadArtifactUrl: (threadId, path, download = false) => {
    const encodedPath = path
      .split('/')
      .filter(Boolean)
      .map((segment) => encodeURIComponent(segment))
      .join('/')
    const query = download ? '?download=true' : ''
    return `/api/v1/agents/threads/${threadId}/artifacts/${encodedPath}${query}`
  },

  /**
   * 下载线程文件（带鉴权）
   * @param {string} threadId
   * @param {string} path
   * @returns {Promise<Response>}
   */
  downloadThreadArtifact: (threadId, path) =>
    apiGet(threadApi.getThreadArtifactUrl(threadId, path, true), {}, true, 'blob'),

  /** 读取允许跨 Project/User Data/Skills 的 artifact 预览字节。 */
  previewThreadArtifact: (threadId, path) =>
    apiGet(
      `${threadApi.getThreadArtifactUrl(threadId, path, false)}?preview=true`,
      {},
      true,
      'blob'
    ),

  /**
   * 保存交付物到指定 workspace 目录
   * @param {string} threadId
   * @param {string} path
   * @param {string} destinationPath
   * @returns {Promise}
   */
  saveThreadArtifactToWorkspace: (threadId, path, destinationPath) =>
    apiPost(`/api/v1/agents/threads/${threadId}/artifacts/save`, {
      path,
      destination_path: destinationPath
    }),

  /**
   * 上传临时附件
   * @param {File} file
   * @returns {Promise}
   */
  uploadTmpAttachment: (file) => {
    const formData = new FormData()
    formData.append('file', file)
    return apiRequest('/api/v1/agents/attachments/tmp', {
      method: 'POST',
      body: formData
    })
  },

  /**
   * 解析临时附件
   * @param {Object} payload
   * @returns {Promise}
   */
  parseTmpAttachment: (payload) => apiPost('/api/v1/agents/attachments/tmp/parse', payload),

  /**
   * 确认添加临时附件到线程
   * @param {string} threadId
   * @param {Array} attachments
   * @returns {Promise}
   */
  confirmTmpThreadAttachments: (threadId, attachments) =>
    apiPost(`/api/v1/agents/threads/${threadId}/attachments/confirm`, { attachments }),

  /**
   * 删除附件
   * @param {string} threadId
   * @param {string} fileId
   * @returns {Promise}
   */
  deleteThreadAttachment: (threadId, fileId) =>
    apiDelete(`/api/v1/agents/threads/${threadId}/attachments/${fileId}`)
}
