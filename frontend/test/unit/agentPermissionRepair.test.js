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
  const navigations = []
  const store = reactive({
    hasConfigChanges: false,
    resetAgentConfig() {},
    selectedAgent: detail,
    agentConfig: {},
    fetchAgentDetail: async () => detail,
    selectAgent: async (slug) => selections.push(slug),
    updateAgentProfile: async (slug, payload) => writes.push({ slug, payload: JSON.parse(JSON.stringify(payload)) })
  })
  const deps = {
    computed, nextTick, reactive, ref, cloneShareConfig,
    useAgentStore: () => store,
    useRouter: () => ({ push: async (route) => { navigations.push(route) } }),
    useUserStore: () => ({ isAdmin, uid: 'admin', departmentId: 1 }),
    isBuiltinAgent: () => false,
    message: { error: (value) => errors.push(value), warning: (value) => errors.push(value), success() {} },
    userApi: {}, generateAgentAvatar: () => '',
    MAX_IMAGE_UPLOAD_SIZE_BYTES: 1024, MAX_IMAGE_UPLOAD_SIZE_MB: 1
  }
  for (const name of ['Bot', 'Microscope', 'RefreshCw', 'Settings2', 'SlidersHorizontal', 'Upload', 'Wrench',
    'AgentRuntimeConfigForm', 'AgentBoundSkillPanel', 'ShareConfigForm', 'FallbackAvatar']) deps[name] = {}
  const component = new Function(...Object.keys(deps), executable)(...Object.values(deps))
  const form = component.setup({ backendOptions: [] }, { expose() {}, emit() {} })
  return { form, store, writes, selections, errors, navigations }
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


test('私有智能体不提交共享配置，也不提供转换共享的状态', async () => {
  const { form } = await setupForm({
    id: 91, agent_id: 'private-admin', name: '私有', backend_id: 'ChatbotAgent',
    visibility: 'private', can_manage: true, can_share: false,
    share_config: { version: 2, read_scope: null, manage_scope: null }
  })
  await form.openEdit({ agent_id: 'private-admin' })
  assert.equal(form.hasAnyUnsavedChanges.value, false)
  assert.equal(Boolean(form.canEditAgentShareConfig.value), false)
  assert.equal('share_config' in form.buildAgentPayload(), false)
  assert.equal('publishing' in form, false)
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

test('编辑专属 Skill 保留未保存 Agent 配置，忙碌时不跳转，保存后才离开', async () => {
  const { form, store, navigations, errors } = await setupForm({
    id: 237, agent_id: 'guide-agent', name: '资料整理助手', backend_id: 'ChatbotAgent',
    visibility: 'private', can_manage: true, can_share: false, can_run: true
  })
  await form.openEdit({ agent_id: 'guide-agent' })
  const route = { name: 'ExtensionSkillDetail', params: { slug: 'self-guide' } }
  form.agentForm.name = '未保存名称'
  await form.navigateToSkill(route)
  assert.equal(form.showAgentModal.value, true)
  assert.equal(form.agentForm.name, '未保存名称')
  assert.deepEqual(navigations, [])
  assert.match(errors.pop(), /请先保存智能体配置/)
  form.agentForm.name = '资料整理助手'
  store.hasConfigChanges = true
  await form.navigateToSkill(route)
  assert.deepEqual(navigations, [])
  store.hasConfigChanges = false
  form.saving.value = true
  await form.navigateToSkill(route)
  assert.deepEqual(navigations, [])
  form.saving.value = false
  form.agentIconUploading.value = true
  await form.navigateToSkill(route)
  assert.deepEqual(navigations, [])
  form.agentIconUploading.value = false
  await form.navigateToSkill(route)
  assert.deepEqual(navigations, [route])
  assert.equal(form.showAgentModal.value, false)
})
