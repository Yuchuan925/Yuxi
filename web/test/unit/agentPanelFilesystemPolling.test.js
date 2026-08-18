import assert from 'node:assert/strict'
import test from 'node:test'

import {
  FILESYSTEM_REFRESH_INTERVAL_MS,
  createFilesystemRefreshGate,
  expandedKeysAfterFilesystemRefresh,
  refreshExpandedTree,
  shouldRefreshActivePreview,
  startAgentPanelFilesystemPolling
} from '../../src/utils/agentPanelFilesystemPolling.js'

test('agent panel polls live files every second and stops on cleanup', async () => {
  let callback
  let interval
  let cleared
  let allowed = false
  let refreshes = 0

  const stop = startAgentPanelFilesystemPolling({
    canRefresh: () => allowed,
    refresh: async () => {
      refreshes += 1
    },
    setIntervalFn: (next, milliseconds) => {
      callback = next
      interval = milliseconds
      return 42
    },
    clearIntervalFn: (timer) => {
      cleared = timer
    }
  })

  assert.equal(interval, FILESYSTEM_REFRESH_INTERVAL_MS)
  callback()
  assert.equal(refreshes, 0)

  allowed = true
  callback()
  await Promise.resolve()
  assert.equal(refreshes, 1)

  stop()
  assert.equal(cleared, 42)
})

test('filesystem refreshes are isolated by thread and stale responses cannot commit', () => {
  const gate = createFilesystemRefreshGate()

  assert.equal(gate.begin('thread-a'), true)
  assert.equal(gate.begin('thread-a'), false)
  assert.equal(gate.begin('thread-b'), true)
  assert.equal(gate.canCommit('thread-a', 'thread-b'), false)
  assert.equal(gate.canCommit('thread-b', 'thread-b'), true)

  gate.finish('thread-a')
  assert.equal(gate.begin('thread-a'), true)
})

test('silent filesystem polling preserves expanded directories', () => {
  const expanded = ['/project/outputs', '/project/uploads']

  assert.equal(expandedKeysAfterFilesystemRefresh(expanded, { silent: true }), expanded)
  assert.deepEqual(expandedKeysAfterFilesystemRefresh(expanded, { silent: false }), [])
})

test('silent filesystem polling reloads every visible expanded subtree', async () => {
  const initial = [
    {
      key: '/project/outputs',
      children: [
        {
          key: '/project/outputs/nested',
          children: [{ key: '/project/outputs/nested/old.txt', fileData: { size: 1 } }]
        }
      ]
    }
  ]
  const requested = []
  const refreshed = await refreshExpandedTree(
    initial,
    ['/project/outputs/nested', '/project/outputs'],
    async (path) => {
      requested.push(path)
      if (path === '/project/outputs') {
        return [{ key: '/project/outputs/nested', children: [] }]
      }
      return [{ key: '/project/outputs/nested/new.txt', fileData: { size: 8 } }]
    }
  )

  assert.deepEqual(requested, ['/project/outputs', '/project/outputs/nested'])
  assert.deepEqual(refreshed[0].children[0].children, [
    { key: '/project/outputs/nested/new.txt', fileData: { size: 8 } }
  ])
})

test('active preview refreshes only when Project metadata changes', () => {
  const current = { path: '/project/report.txt', size: 3, modified_at: 'old' }

  assert.equal(
    shouldRefreshActivePreview(current, {
      path: current.path,
      size: 3,
      modified_at: 'new'
    }),
    true
  )
  assert.equal(shouldRefreshActivePreview(current, { ...current }), false)
  assert.equal(shouldRefreshActivePreview({ ...current, artifact: true }, null), false)
  assert.equal(
    shouldRefreshActivePreview(
      { ...current, artifact: true },
      { ...current, modified_at: 'new' }
    ),
    true
  )
})
