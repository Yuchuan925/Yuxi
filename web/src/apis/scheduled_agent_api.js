import { apiDelete, apiGet, apiPost, apiRequest } from './base'

/** 用户 Agent 定时任务 API。 */
export const scheduledAgentApi = {
  list: () => apiGet('/api/scheduled-tasks'),
  get: (jobId) => apiGet(`/api/scheduled-tasks/${jobId}`),
  create: (payload) => apiPost('/api/scheduled-tasks', payload),
  update: (jobId, payload) =>
    apiRequest(`/api/scheduled-tasks/${jobId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload)
    }),
  runNow: (jobId) => apiPost(`/api/scheduled-tasks/${jobId}/run-now`, {}),
  executions: (jobId) => apiGet(`/api/scheduled-tasks/${jobId}/executions`),
  remove: (jobId) => apiDelete(`/api/scheduled-tasks/${jobId}`)
}
