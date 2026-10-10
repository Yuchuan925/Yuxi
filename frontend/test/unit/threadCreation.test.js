import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

import { createThreadForContext } from '../../src/modules/session/model/threadCreation.js'

test('延迟创建响应遇到上下文切换时不被接受', async () => {
  const context = { agentId: 'agent-a', projectId: 'auto', threadId: null }
  let currentContext = { ...context }
  let resolveRequest
  const response = new Promise((resolve) => {
    resolveRequest = resolve
  })

  const pending = createThreadForContext({
    context,
    getCurrentContext: () => currentContext,
    requestId: 'stable-request',
    create: async (requestId) => {
      assert.equal(requestId, 'stable-request')
      return response
    }
  })

  currentContext = { ...context, agentId: 'agent-b' }
  resolveRequest({ id: 'thread-a' })

  assert.deepEqual(await pending, { thread: { id: 'thread-a' }, accepted: false })
})


test('普通会话不指定标题，创建响应丢失后仍重放原审批模式', async () => {
  const source = readFileSync(new URL('../../src/modules/session/ui/SessionWorkspace.vue', import.meta.url), 'utf8')
  const block = source.slice(source.indexOf('const createActiveThread ='), source.indexOf('const ensureActiveThread ='))
  const calls = []
  const currentChatId = { value: null }
  const approval = { value: 'default' }
  const request = { value: null }
  const dependencies = {
    currentChatId, currentAgentId: { value: 'agent' }, selectedProjectId: { value: 'auto' },
    threadCreationRequest: request, currentToolApprovalMode: approval, AUTO_PROJECT_ID: 'auto',
    createClientRequestId: () => 'stable-key', createThreadForContext,
    chatThreadsStore: { setThreadCreationInFlight() {}, async createThread(...args) {
      calls.push(structuredClone(args))
      if (calls.length === 1) throw new Error('response lost')
      return { id: 'same-session' }
    } },
    threadMessages: { value: {} }, threadAttachmentsMap: { value: {} }, draftFilesByThread: { value: {} },
    promoteDraftSelection() {}, setCurrentThreadId: (id) => { currentChatId.value = id }
  }
  const create = new Function(...Object.keys(dependencies), `${block}; return createActiveThread`)(...Object.values(dependencies))
  await assert.rejects(create(), /response lost/)
  approval.value = 'always_trust'
  assert.equal(await create(), 'same-session')
  assert.deepEqual(calls[1], calls[0])
  assert.equal(calls[1][1], null)
  assert.equal(calls[1][2].tool_approval_mode, 'default')
  assert.equal(request.value, null)
})
