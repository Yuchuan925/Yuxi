import assert from 'node:assert/strict'
import test from 'node:test'
import { ESLint } from 'eslint'

const eslint = new ESLint()
for (const [file, source] of [
  ['src/modules/conversation/model/invalid.js', "import '@/pages/AgentView.vue'"],
  ['src/modules/agents/ui/invalid.js', "export { default } from '../../../app/App.vue'"],
  ['src/shared/ui/invalid.js', "const load = () => import('@/modules/agents/model/agent'); load()"],
  ['src/shared/lib/invalid.js', "export * from '@/apis/base'"],
  ['src/pages/invalid.js', "import '@/components/AgentChatComponent.vue'"]
,
  ['src/modules/agents/model/invalid.js', "import '@/modules/../pages/AgentView.vue'"],
  ['src/shared/lib/invalid.js', "import '@/shared/../modules/agents/model/agent'"],
  ['src/modules/conversation/model/invalid.js', 'const load = () => import(`@/pages/AgentView.vue`); load()'],
  ['src/modules/conversation/model/invalid.js', "import '/src/pages/AgentView.vue'"]
]) {
  test(`拒绝越界引用：${file} → ${source}`, async () => {
    const [result] = await eslint.lintText(source, { filePath: file })
    assert.ok(
      result.messages.some((message) => message.ruleId === 'architecture/boundaries'),
      JSON.stringify(result.messages)
    )
  })
}
test('页面装配业务模块、领域复用 shared、领域之间引用不被误拒绝', async () => {
  for (const [file, source] of [
    ['src/pages/example.js', "import '@/modules/conversation/ui/ConversationWorkspace.vue'"],
    ['src/modules/agents/ui/example.js', "import '@/shared/ui/ActionDropdown.vue'"],
    ['src/modules/conversation/model/example.js', "import '@/modules/agents/model/agent'"]
  ]) {
    const [result] = await eslint.lintText(source, { filePath: file })
    assert.equal(result.errorCount, 0, JSON.stringify(result.messages))
  }
})
