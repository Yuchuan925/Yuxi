import { apiGet } from './base'

export const graphApi = {
  getGraphs: async () => {
    return await apiGet('/api/graph/list', {}, true)
  },

  getSubgraph: async (params) => {
    const { db_id, node_label = '*', max_depth = 2, max_nodes = 100 } = params

    if (!db_id) {
      throw new Error('db_id is required')
    }

    const queryParams = new URLSearchParams({
      db_id,
      node_label,
      max_depth: max_depth.toString(),
      max_nodes: max_nodes.toString()
    })

    return await apiGet(`/api/graph/subgraph?${queryParams.toString()}`, {}, true)
  },

  getStats: async (db_id) => {
    if (!db_id) {
      throw new Error('db_id is required')
    }

    const queryParams = new URLSearchParams({ db_id })
    return await apiGet(`/api/graph/stats?${queryParams.toString()}`, {}, true)
  },

  getLabels: async (db_id) => {
    if (!db_id) {
      throw new Error('db_id is required')
    }

    const queryParams = new URLSearchParams({ db_id })
    return await apiGet(`/api/graph/labels?${queryParams.toString()}`, {}, true)
  }
}

export const unifiedApi = graphApi
