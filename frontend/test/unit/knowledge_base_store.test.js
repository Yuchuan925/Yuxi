import test from 'node:test'
import assert from 'node:assert/strict'
import { setImmediate } from 'node:timers/promises'

import { createPinia, setActivePinia } from 'pinia'
import { createApp } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createServer } from 'vite'
import { message } from 'ant-design-vue'

const storageValues = new Map()
globalThis.localStorage = {
  getItem: (key) => storageValues.get(key) ?? null,
  setItem: (key, value) => storageValues.set(key, String(value)),
  removeItem: (key) => storageValues.delete(key),
  clear: () => storageValues.clear()
}

test('从二级目录点击全部文件会清空 parent_id 并返回根目录', async (t) => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })

  try {
    const warnings = []
    t.mock.method(console, 'warn', (...args) => warnings.push(args))
    const pinia = createPinia()
    const app = createApp({})
    app.use(pinia)
    app.use(createRouter({ history: createMemoryHistory(), routes: [] }))
    setActivePinia(pinia)
    const { documentApi } = await server.ssrLoadModule('/src/apis/knowledge_api.js')
    const { useKnowledgeBaseStore } = await server.ssrLoadModule('/src/modules/knowledge/model/knowledgeBase.js')
    const requests = []

    documentApi.listDocuments = async (kbId, params) => {
      requests.push({ kbId, params })
      return {
        items: [],
        page: 1,
        page_size: 100,
        total: 0,
        has_more: false,
        path_prefix: ''
      }
    }

    const store = app.runWithContext(() => useKnowledgeBaseStore())
    store.kbId = 'kb_1'
    store.fileBrowser.parentId = 'folder_2'
    store.folderBreadcrumbs = [
      { file_id: null, filename: '全部文件', path_prefix: '' },
      { file_id: 'folder_2', filename: '二级目录', path_prefix: '' }
    ]

    await store.goToFolder(0)

    assert.equal(store.fileBrowser.parentId, null)
    assert.deepEqual(store.folderBreadcrumbs, [
      { file_id: null, filename: '全部文件', path_prefix: '' }
    ])
    assert.deepEqual(requests, [
      {
        kbId: 'kb_1',
        params: {
          page: 1,
          page_size: 100,
          status: 'all',
          recursive: false
        }
      }
    ])
    assert.deepEqual(warnings, [])
  } finally {
    await server.close()
  }
})

test('知识库提交跨账号返回时，不把旧入队任务登记给新账号', async (t) => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })
  const pinia = createPinia()
  const app = createApp({})
  app.use(pinia)
  app.use(createRouter({ history: createMemoryHistory(), routes: [] }))
  setActivePinia(pinia)
  const { documentApi, knowledgeBaseApi } = await server.ssrLoadModule('/src/apis/knowledge_api.js')
  const { useKnowledgeBaseStore } = await server.ssrLoadModule('/src/modules/knowledge/model/knowledgeBase.js')
  const { useUserStore } = await server.ssrLoadModule('/src/modules/identity/model/user.js')
  const { useBackgroundJobsStore } = await server.ssrLoadModule('/src/modules/background-jobs/model/jobs.js')
  const { backgroundJobsApi } = await server.ssrLoadModule('/src/apis/background_jobs.js')
  const knowledgeBase = app.runWithContext(() => useKnowledgeBaseStore())
  const user = useUserStore()
  const job_tracker = useBackgroundJobsStore()
  const detailRequests = []
  t.mock.method(backgroundJobsApi, 'fetchJobDetail', async (id) => {
    detailRequests.push(id)
    return { job: { id, status: 'success', progress: 100 } }
  })
  t.mock.method(message, 'success', () => {})
  t.mock.method(knowledgeBaseApi, 'getKnowledgeBaseInfo', async () => ({ stats: { processing_count: 0 } }))
  t.mock.method(documentApi, 'listDocuments', async () => ({
    items: [],
    stats: { processing_count: 0 }
  }))
  try {
    for (const [action, apiMethod, args] of [
      ['addFiles', 'addDocuments', [{ items: ['fixture'], contentType: 'file', params: {} }]],
      ['parseFiles', 'parseDocuments', [['file']]],
      ['parsePendingFiles', 'parsePendingDocuments', []],
      ['indexFiles', 'indexDocuments', [['file']]],
      ['indexPendingFiles', 'indexPendingDocuments', []]
    ]) {
      await t.test(action, async () => {
        knowledgeBase.kbId = 'test-kb'
        user.token = `old-${action}`
        user.userRole = 'admin'
        let resolve
        t.mock.method(
          documentApi,
          apiMethod,
          () =>
            new Promise((done) => {
              resolve = done
            })
        )
        const pending = knowledgeBase[action](...args)
        user.logout()
        user.token = `new-${action}`
        user.userRole = 'admin'
        resolve({ status: 'queued', job_id: 'old-job' })
        assert.equal(await pending, true)
        assert.deepEqual(job_tracker.jobs, [])
        assert.equal(detailRequests.includes('old-job'), false)
        const current = knowledgeBase[action](...args)
        resolve({ status: 'queued', job_id: `new-${action}` })
        assert.equal(await current, true)
        await setImmediate()
        assert.equal(job_tracker.jobs[0].id, `new-${action}`)
        assert.equal(job_tracker.jobs[0].status, 'success')
        job_tracker.reset()
      })
    }
  } finally {
    knowledgeBase.stopAutoRefresh()
    job_tracker.$dispose()
    await server.close()
  }
})
