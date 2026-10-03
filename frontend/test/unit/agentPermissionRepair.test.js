import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, nextTick, reactive, ref } from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'
import { cloneShareConfig } from '../../src/modules/agents/model/shareConfig.js'

const source = readFileSync(
  new URL('../../src/modules/agents/ui/AgentEditModal.vue', import.meta.url), 'utf8'
)
const { descriptor } = parse(source)
const executable = compileScript(descriptor, { id: 'agent-permission-repair' }).content
  .replace(/^import[\s\S]*?from '[^']+'\n/gm, '')
  .replace('export default', 'return')

/** 编译实际编辑组件并提供独立网络边界。 */
async function setupForm(detail, isAdmin = true) {
  const writes = []
  const selections = []
  const errors = []
  const store = {
    hasConfigChanges: false,
    resetAgentConfig() {},
    selectedAgent: detail,
    agentConfig: {},
    fetchAgentDetail: async () => detail,
    selectAgent: async (slug) => selections.push(slug),
    updateAgentProfile: async (slug, payload) => writes.push({ slug, payload: JSON.parse(JSON.stringify(payload)) })
  }
  const deps = {
    computed, nextTick, reactive, ref, cloneShareConfig,
    useAgentStore: () => store,
    useUserStore: () => ({ isAdmin, uid: 'admin', departmentId: 1 }),
    isBuiltinAgent: () => false,
    message: { error: (value) => errors.push(value), warning: (value) => errors.push(value), success() {} },
    userApi: {}, generatePixelAvatar: () => '',
    MAX_IMAGE_UPLOAD_SIZE_BYTES: 1024, MAX_IMAGE_UPLOAD_SIZE_MB: 1
  }
  for (const name of ['Bot', 'Microscope', 'RefreshCw', 'Settings2', 'SlidersHorizontal', 'Upload', 'Wrench',
    'AgentRuntimeConfigForm', 'ShareConfigForm', 'FallbackAvatar']) deps[name] = {}
  const component = new Function(...Object.keys(deps), executable)(...Object.values(deps))
  const form = component.setup({ backendOptions: [] }, { expose() {}, emit() {} })
  return { form, writes, selections, errors }
}

test('管理员打开坏配置不写入，明确保存以 slug 修复共享范围', async () => {
  const detail = {
    id: 90, agent_id: 'fixture-agent', slug: 'fixture-agent', name: '权限修复',
    backend_id: 'ChatbotAgent', can_manage: true, can_share: true, visibility: 'shared', share_config_invalid: true,
    share_config: { version: 2, read_scope: { access_level: 'department', department_ids: { 1: true } } }
  }
  const { form, writes, selections, errors } = await setupForm(detail)
  await form.openEdit({ agent_id: 'fixture-agent' })
  assert.equal(form.showAgentModal.value, true)
  assert.equal(form.shareConfigNeedsRepair.value, true)
  assert.deepEqual(writes, [])
  assert.deepEqual(selections, ['fixture-agent'])
  assert.deepEqual(form.agentShareConfig.value, { version: 2, read_scope: null, manage_scope: null })
  await form.saveAgent()
  assert.deepEqual(errors, [])
  assert.equal(writes.length, 1)
  assert.equal(writes[0].slug, 'fixture-agent')
  assert.deepEqual(writes[0].payload.share_config, { version: 2, read_scope: null, manage_scope: null })
  assert.equal(form.showAgentModal.value, false)
  assert.equal(form.shareConfigNeedsRepair.value, false)
})


test('仅选择发布也是待保存修改，取消发布恢复原状态', async () => {
  const { form } = await setupForm({
    id: 91, agent_id: 'private-admin', name: '待发布', backend_id: 'ChatbotAgent',
    visibility: 'private', can_manage: true, can_publish: true, can_share: false,
    share_config: { version: 2, read_scope: null, manage_scope: null }
  })
  await form.openEdit({ agent_id: 'private-admin' })
  assert.equal(form.hasAnyUnsavedChanges.value, false)
  form.publishing.value = true
  await nextTick()
  assert.equal(form.hasAnyUnsavedChanges.value, true)
  form.publishing.value = false
  assert.equal(form.hasAnyUnsavedChanges.value, false)
})

test('管理员创建共享 SubAgent 能显式保存限定范围，普通主 Agent 仍私有', async () => {
  const { form } = await setupForm({})
  form.openCreate()
  form.agentForm.name = '限定子 Agent'
  form.agentForm.backend_id = 'SubAgentBackend'
  assert.equal(Boolean(form.canEditAgentShareConfig.value), true)
  const payload = form.buildAgentPayload()
  assert.equal(payload.visibility, 'shared')
  assert.deepEqual(payload.share_config.read_scope.user_uids, ['admin'])
  form.agentForm.backend_id = 'ChatbotAgent'
  assert.equal(Boolean(form.canEditAgentShareConfig.value), false)
  assert.equal(form.buildAgentPayload().visibility, 'private')
  assert.equal('share_config' in form.buildAgentPayload(), false)
})
