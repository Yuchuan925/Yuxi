import assert from 'node:assert/strict'
import test from 'node:test'
import { setImmediate } from 'node:timers'

import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {}
}

test('线程创建期间共享 Store 拒绝外层切换，创建结果可显式提交', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  setActivePinia(createPinia())
  try {
    const { useChatThreadsStore } = await server.ssrLoadModule(
      '/src/modules/session/model/chatThreads.js'
    )
    const store = useChatThreadsStore()
    store.setCurrentThreadId('thread-before')
    store.setThreadCreationInFlight(true)

    assert.equal(store.setCurrentThreadId('thread-sidebar'), false)
    assert.equal(store.currentThreadId, 'thread-before')
    assert.equal(store.setCurrentThreadId('thread-created', { force: true }), true)
    assert.equal(store.currentThreadId, 'thread-created')

    store.setThreadCreationInFlight(false)
  } finally {
    await server.close()
  }
})

test('Project 删除后 Store 只移除对应线程并清空当前选择', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  setActivePinia(createPinia())
  try {
    const { useChatThreadsStore } = await server.ssrLoadModule(
      '/src/modules/session/model/chatThreads.js'
    )
    const store = useChatThreadsStore()
    store.threads = [
      { id: 'thread-a', project_id: 'project-a' },
      { id: 'thread-b', project_id: 'project-b' }
    ]
    store.setCurrentThreadId('thread-a')

    assert.deepEqual(store.removeThreadsByProject('project-a'), ['thread-a'])
    assert.deepEqual(store.threads, [{ id: 'thread-b', project_id: 'project-b' }])
    assert.equal(store.currentThreadId, null)
  } finally {
    await server.close()
  }
})

test('置顶线程不占用普通线程分页 offset 且不会提前结束加载', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  setActivePinia(createPinia())
  try {
    const { threadApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
    const calls = []
    const pinned = { id: 'thread-pinned', is_pinned: true }
    threadApi.getThreads = async (_agentId, limit, offset) => {
      calls.push({ limit, offset })
      const count = offset < 200 ? 100 : 50
      return [
        pinned,
        ...Array.from({ length: count }, (_, index) => ({
          id: `thread-${offset + index}`,
          is_pinned: false
        }))
      ]
    }

    const { useChatThreadsStore } = await server.ssrLoadModule(
      '/src/modules/session/model/chatThreads.js'
    )
    const store = useChatThreadsStore()
    await store.loadThreads()
    await store.loadMoreThreads()
    await store.loadMoreThreads()

    assert.deepEqual(calls, [
      { limit: 100, offset: 0 },
      { limit: 100, offset: 100 },
      { limit: 100, offset: 200 }
    ])
    assert.equal(store.threads.length, 251)
    assert.equal(store.hasMoreThreads, false)
  } finally {
    await server.close()
  }
})

test('首屏之外的成员详情缓存不会推进列表分页或跳过边界主会话', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })
  setActivePinia(createPinia())
  try {
    const { threadApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
    const offsets = []
    threadApi.getThreads = async (_agentId, _limit, offset) => {
      offsets.push(offset)
      if (offset === 0) return Array.from({ length: 100 }, (_, i) => ({ id: `root-${i}` }))
      return offset === 100
        ? [{ id: 'boundary-root' }, { id: 'distant-child', parent_session_id: 'root-0' }]
        : []
    }
    const { useChatThreadsStore } = await server.ssrLoadModule(
      '/src/modules/session/model/chatThreads.js'
    )
    const store = useChatThreadsStore()
    await store.loadThreads()
    store.upsertThread({ id: 'distant-child', parent_session_id: 'root-0' })
    store.setCurrentThreadId('root-outside-page')
    store.upsertThread({ id: 'root-outside-page' })
    await store.loadThreads()
    assert.equal(store.currentThreadId, 'root-outside-page')
    assert.ok(store.threads.some((thread) => thread.id === 'distant-child'))
    await store.loadMoreThreads()
    assert.deepEqual(offsets, [0, 0, 100])
    assert.ok(store.threads.some((thread) => thread.id === 'boundary-root'))
    assert.equal(store.threads.filter((thread) => thread.id === 'distant-child').length, 1)
  } finally {
    await server.close()
  }
})

test('归档和置顶已加载线程后分页仍保留下一条边界会话', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })
  setActivePinia(createPinia())
  try {
    const { threadApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
    const rows = Array.from({ length: 110 }, (_, i) => ({ id: `root-${i}`, is_pinned: false }))
    const offsets = []
    threadApi.getThreads = async (_agentId, limit, offset) => {
      offsets.push(offset)
      return [
        ...rows.filter((row) => row.is_pinned),
        ...rows.filter((row) => !row.is_pinned).slice(offset, offset + limit)
      ].map((row) => ({ ...row }))
    }
    threadApi.archiveThread = async (id) =>
      rows.splice(
        rows.findIndex((row) => row.id === id),
        1
      )
    threadApi.updateThread = async (id, _title, isPinned) => {
      const row = rows.find((row) => row.id === id)
      row.is_pinned = isPinned
      return { ...row }
    }
    const { useChatThreadsStore } = await server.ssrLoadModule(
      '/src/modules/session/model/chatThreads.js'
    )
    const store = useChatThreadsStore()
    await store.loadThreads()
    store.upsertThread({ id: 'unpaged-child', parent_session_id: 'root-0' })
    await store.archiveThread('root-0')
    await store.updateThread('root-1', null, true)
    await store.loadMoreThreads()
    assert.deepEqual(offsets, [0, 98])
    assert.ok(store.threads.some((thread) => thread.id === 'root-100'))
    assert.ok(store.threads.some((thread) => thread.id === 'root-109'))
  } finally {
    await server.close()
  }
})

test('在途下一页与归档交错不能漏掉边界会话', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })
  setActivePinia(createPinia())
  try {
    const { threadApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
    const rows = Array.from({ length: 110 }, (_, i) => ({ id: `root-${i}` }))
    let releasePage, pageStarted
    const gate = new Promise((resolve) => {
      releasePage = resolve
    })
    const started = new Promise((resolve) => {
      pageStarted = resolve
    })
    threadApi.getThreads = async (_agentId, limit, offset) => {
      if (offset) {
        pageStarted()
        await gate
      }
      return rows.slice(offset, offset + limit).map((row) => ({ ...row }))
    }
    threadApi.archiveThread = async (id) =>
      rows.splice(
        rows.findIndex((row) => row.id === id),
        1
      )
    const { useChatThreadsStore } = await server.ssrLoadModule(
      '/src/modules/session/model/chatThreads.js'
    )
    const store = useChatThreadsStore()
    await store.loadThreads()
    const loading = store.loadMoreThreads()
    await started
    const archiving = store.archiveThread('root-0')
    await new Promise((resolve) => setImmediate(resolve))
    releasePage()
    await Promise.all([loading, archiving])
    assert.ok(
      store.threads.some((thread) => thread.id === 'root-100'),
      '在途归档不能让边界会话永久缺席'
    )
    assert.equal(store.threads.length, 109)
    assert.equal(store.hasMoreThreads, false)
  } finally {
    await server.close()
  }
})
