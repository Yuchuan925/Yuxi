import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import * as Vue from 'vue'
import { renderToString } from 'vue/server-renderer'

const source = readFileSync(
  new URL('../../src/modules/session/ui/SessionWorkspace.vue', import.meta.url),
  'utf8'
)
const view = readFileSync(new URL('../../src/pages/AgentView.vue', import.meta.url), 'utf8')
const dock = source.slice(
  source.indexOf('<div\n            ref="messageInputDockRef"'),
  source.indexOf('              <section\n                v-if="currentQueuedInputs.length"')
)
const render = Vue.compile(`${dock}</div></div>`)

test('路由决定新建布局，已有线程的加载和空消息不显示居中输入框或欢迎语', async () => {
  assert.ok(view.includes(':is-new-session="!threadId"'), '新建布局由路由传入')
  for (const isNewSession of [false, true]) {
    for (const isLoadingMessages of [false, true]) {
      for (const runGroups of [[], [{ id: 'history' }]]) {
        const html = await renderToString(
          Vue.createSSRApp({
            data: () => ({
              isNewSession,
              isLoadingMessages,
              runGroups,
              randomGreeting: '欢迎测试'
            }),
            render
          })
        )
        assert.equal(html.includes('start-screen'), isNewSession)
        assert.equal(html.includes('欢迎测试'), isNewSession)
        assert.equal(html.includes('正在加载消息'), isLoadingMessages)
      }
    }
  }
})
