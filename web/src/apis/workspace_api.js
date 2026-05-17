import { apiDelete, apiGet, apiPost, apiPut } from './base'

const buildQuery = (params) => {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      query.set(key, String(value))
    }
  })
  return query.toString()
}

export const getWorkspaceTree = (path = '/', recursive = false, filesOnly = false) => {
  const query = buildQuery({ path, recursive, files_only: filesOnly })
  return apiGet(`/api/workspace/tree?${query}`)
}

export const getWorkspaceFileContent = (path) => {
  const query = buildQuery({ path })
  return apiGet(`/api/workspace/file?${query}`)
}

export const getWorkspaceKnowledgeTree = (
  dbId,
  parentId = null,
  recursive = false,
  filesOnly = false
) => {
  const query = buildQuery({ db_id: dbId, parent_id: parentId, recursive, files_only: filesOnly })
  return apiGet(`/api/workspace/knowledge/tree?${query}`)
}

export const getWorkspaceKnowledgeFileContent = (dbId, fileId, variant = 'parsed') => {
  const query = buildQuery({ db_id: dbId, file_id: fileId, variant })
  return apiGet(`/api/workspace/knowledge/file?${query}`)
}

export const downloadWorkspaceKnowledgeFile = (dbId, fileId, variant = 'original') => {
  const query = buildQuery({ db_id: dbId, file_id: fileId, variant })
  return apiGet(`/api/workspace/knowledge/download?${query}`, {}, true, 'blob')
}

export const saveWorkspaceFileContent = (path, content) => {
  return apiPut('/api/workspace/file', { path, content })
}

export const deleteWorkspacePath = (path) => {
  const query = buildQuery({ path })
  return apiDelete(`/api/workspace/file?${query}`)
}

export const createWorkspaceDirectory = (parentPath, name) => {
  return apiPost('/api/workspace/directory', {
    parent_path: parentPath,
    name
  })
}

export const uploadWorkspaceFile = (parentPath, file) => {
  const formData = new FormData()
  formData.append('parent_path', parentPath)
  formData.append('file', file)
  return apiPost('/api/workspace/upload', formData)
}

export const downloadWorkspaceFile = (path) => {
  const query = buildQuery({ path })
  return apiGet(`/api/workspace/download?${query}`, {}, true, 'blob')
}
