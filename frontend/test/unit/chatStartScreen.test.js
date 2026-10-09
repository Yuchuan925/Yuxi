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
  source.lastIndexOf('<div', source.indexOf('ref="messageInputDockRef"')),
  source.indexOf('<section', source.indexOf('ref="messageInputDockRef"'))
)
const render = Vue.compile(`${dock}</div></div>`)
const layoutExpression = source.slice(
  source.indexOf('const showStartScreen ='),
  source.indexOf('const currentThread =')
)

test('新建路由且没有线程才显示欢迎布局，加载和空消息不改变布局', async () => {
  assert.ok(view.includes(':is-new-session="!threadId"'), '新建布局由路由传入')
  for (const isNewSession of [false, true]) {
    for (const threadId of [null, 'created-thread']) {
      const showStartScreen = new Function(
        'computed',
        'props',
        'currentThreadId',
        `${layoutExpression}; return showStartScreen`
      )(Vue.computed, { isNewSession }, Vue.ref(threadId))
      for (const isLoadingMessages of [false, true]) {
        for (const runGroups of [[], [{ id: 'history' }]]) {
          const html = await renderToString(
            Vue.createSSRApp({
              data: () => ({
                isNewSession,
                currentChatId: threadId,
                pendingSends: {},
                showStartScreen: showStartScreen.value,
                isLoadingMessages,
                runGroups,
                randomGreeting: '欢迎测试'
              }),
              render
            })
          )
          assert.equal(html.includes('start-screen'), isNewSession && !threadId)
          assert.equal(html.includes('欢迎测试'), isNewSession && !threadId)
          assert.equal(html.includes('正在加载消息'), isLoadingMessages)
        }
      }
    }
  }
})
