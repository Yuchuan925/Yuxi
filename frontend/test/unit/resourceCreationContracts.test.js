import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, defineComponent, h, nextTick, reactive, ref, watch } from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'
import { parseMcpManifest } from '../../src/modules/extensions/model/mcpManifest.js'

/** 编译真实资源弹窗，替换网络边界而非业务实现。 */
function setupModal(file, props, dependencies) {
  const source = readFileSync(new URL(`../../src/modules/extensions/ui/${file}.vue`, import.meta.url), 'utf8')
  const { descriptor } = parse(source)
  const executable = compileScript(descriptor, { id: file }).content
    .replace(/^import[\s\S]*?from '[^']+'\n/gm, '')
    .replace('export default', 'return')
  const deps = { computed, defineComponent, h, nextTick, reactive, ref, watch, ...dependencies }
  const events = []
  const component = new Function(...Object.keys(deps), executable)(...Object.values(deps))
  return { modal: component.setup(props, { expose() {}, emit: (...args) => events.push(args) }), events }
}

test('固定共享入口发布到共享 API，完成返回实际安装 slug，默认入口仍是个人', async () => {
  const calls = []
  const dependencies = {
    skillApi: {
      confirmPersonalSkillInstallDraft: () => assert.fail('共享入口不能发布到个人目录'),
      confirmSkillInstallDraft: async (draftId, share, slugs) => {
        calls.push({ draftId, share, slugs })
        return { data: [{ success: true, requested_slug: 'guide', slug: 'installed-guide' }] }
      },
      discardSkillInstallDraft: () => assert.fail('成功草稿已经消费')
    },
    useUserStore: () => ({ isAdmin: true }),
    ShareConfigForm: {}, CheckCircle2: {}, Circle: {}, LoaderCircle: {}, PackageOpen: {}, XIcon: {}, XCircle: {}
  }
  const flow = { kind: 'draft', drafts: [{ draft_id: 'draft', items: [{ slug: 'guide', success: true }],
    default_share_config: { version: 2, read_scope: { access_level: 'global' } }, allowed_access_levels: ['global'] }] }
  const { modal, events } = setupModal('SkillInstallFlowModal', { open: true, flow, target: 'shared' }, dependencies)
  assert.equal(modal.installTarget.value, 'shared')
  await modal.installDrafts()
  await modal.finishFlow()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].draftId, 'draft')
  assert.deepEqual(calls[0].slugs, ['guide'])
  assert.equal(calls[0].share.read_scope.access_level, 'global')
  assert.deepEqual(events, [['completed', { success: 1, failed: 0, slugs: ['installed-guide'] }], ['close']])
  const defaultModal = setupModal('SkillInstallFlowModal', { open: true, flow, target: '' }, dependencies).modal
  assert.equal(defaultModal.installTarget.value, 'personal')
})

test('独立 MCP 发布等待时禁止重复提交与关闭，成功才返回资源标识', async () => {
  let resolveCreate
  const calls = []
  const { modal, events } = setupModal('McpFormModal', { open: true, editMode: false, editData: null }, {
    CollapseTransition: {}, ChevronRight: {}, Plug: {},
    parseMcpManifest, message: { success() {}, error: (message) => assert.fail(message) },
    mcpApi: { createMcpServer: (data) => { calls.push(data); return new Promise((resolve) => { resolveCreate = resolve }) } }
  })
  Object.assign(modal.form, { slug: 'query', name: '查询', url: 'https://example.com/mcp' })
  const request = modal.handleFormSubmit()
  await modal.handleFormSubmit()
  modal.visible.value = false
  assert.equal(modal.formLoading.value, true)
  assert.equal(calls.length, 1)
  assert.deepEqual(events, [])
  assert.equal(calls[0].slug, 'query')
  resolveCreate({ success: true, data: { slug: 'query', enabled: true } })
  await request
  assert.equal(modal.formLoading.value, false)
  assert.deepEqual(events, [['update:open', false], ['submitted', { slug: 'query', enabled: true }]])
})
