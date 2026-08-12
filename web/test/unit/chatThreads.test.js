import test from 'node:test'
import assert from 'node:assert/strict'

import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

const storageValues = new Map()
globalThis.localStorage = {
  getItem: (key) => storageValues.get(key) ?? null,
  setItem: (key, value) => storageValues.set(key, String(value)),
  removeItem: (key) => storageValues.delete(key),
  clear: () => storageValues.clear()
}

async function withViteContext(fn) {
  const server = await createServer({
    server: { middlewareMode: true },
    appType: 'custom'
  })
  try {
    setActivePinia(createPinia())
    const { threadApi } = await server.ssrLoadModule('/src/apis/agent_api.js')
    const { useChatThreadsStore } = await server.ssrLoadModule('/src/stores/chatThreads.js')
    await fn({ threadApi, useChatThreadsStore })
  } finally {
    await server.close()
  }
}

test('setThreadStatus 原地更新指定线程状态', async () => {
  await withViteContext(async ({ useChatThreadsStore }) => {
    const store = useChatThreadsStore()
    store.upsertThread({ id: 't1', title: 'A', thread_status: 'done' })
    store.upsertThread({ id: 't2', title: 'B', thread_status: 'done' })

    store.setThreadStatus('t1', 'loading')

    const statusById = new Map(store.threads.map((t) => [t.id, t.thread_status]))
    assert.equal(statusById.get('t1'), 'loading')
    assert.equal(statusById.get('t2'), 'done')
    assert.equal(store.threads.length, 2)
  })
})

test('setThreadStatus 忽略不存在的线程', async () => {
  await withViteContext(async ({ useChatThreadsStore }) => {
    const store = useChatThreadsStore()
    store.upsertThread({ id: 't1', title: 'A', thread_status: 'done' })

    store.setThreadStatus('missing', 'loading')

    assert.equal(store.threads.length, 1)
    assert.equal(store.threads[0].thread_status, 'done')
  })
})

test('markThreadViewed 调用接口并合并返回的已读线程', async () => {
  await withViteContext(async ({ threadApi, useChatThreadsStore }) => {
    const store = useChatThreadsStore()
    store.upsertThread({ id: 't1', title: 'A', thread_status: 'ready' })

    threadApi.markThreadViewed = async (threadId) => {
      assert.equal(threadId, 't1')
      return { id: 't1', title: 'A', thread_status: 'done' }
    }

    const updated = await store.markThreadViewed('t1')

    assert.equal(updated.thread_status, 'done')
    assert.equal(store.threads[0].thread_status, 'done')
  })
})

test('markThreadViewed 接口失败时保留本地状态且不抛错', async () => {
  await withViteContext(async ({ threadApi, useChatThreadsStore }) => {
    const store = useChatThreadsStore()
    store.upsertThread({ id: 't1', title: 'A', thread_status: 'ready' })

    threadApi.markThreadViewed = async () => {
      throw new Error('network down')
    }

    const updated = await store.markThreadViewed('t1')

    assert.equal(updated, null)
    assert.equal(store.threads[0].thread_status, 'ready')
  })
})

test('syncThreadStatuses 合并服务端最新状态且不重建列表', async () => {
  await withViteContext(async ({ threadApi, useChatThreadsStore }) => {
    const store = useChatThreadsStore()
    store.upsertThread({ id: 't1', title: 'A', thread_status: 'done' })
    store.upsertThread({ id: 't2', title: 'B', thread_status: 'done' })
    store.upsertThread({ id: 't3', title: 'C', thread_status: 'ready' })

    threadApi.getThreads = async () => [
      { id: 't1', thread_status: 'loading' },
      { id: 't2', thread_status: 'ready' }
    ]

    await store.syncThreadStatuses()

    const statusById = new Map(store.threads.map((t) => [t.id, t.thread_status]))
    assert.equal(statusById.get('t1'), 'loading')
    assert.equal(statusById.get('t2'), 'ready')
    assert.equal(statusById.get('t3'), 'ready')
    assert.equal(store.threads.length, 3)
  })
})
