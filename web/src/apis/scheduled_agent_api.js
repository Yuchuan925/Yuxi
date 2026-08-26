import { apiDelete, apiGet, apiPost, apiRequest } from './base'

/** 用户 Agent 定时任务 API。 */
export const scheduledAgentApi = {
  list: () => apiGet('/api/scheduled-agents'),
  get: (jobId) => apiGet(`/api/scheduled-agents/${jobId}`),
  create: (payload) => apiPost('/api/scheduled-agents', payload),
  update: (jobId, payload) =>
    apiRequest(`/api/scheduled-agents/${jobId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload)
    }),
  remove: (jobId) => apiDelete(`/api/scheduled-agents/${jobId}`)
}
