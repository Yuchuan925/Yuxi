import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, nextTick, reactive, ref } from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'
import { cloneShareConfig } from '../../src/modules/agents/model/shareConfig.js'
import { parseMcpManifest } from '../../src/modules/extensions/model/mcpManifest.js'

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
    updateAgentProfile: async (slug, payload) => writes.push({ slug, payload: JSON.parse(JSON.stringify(payload)) }),
    createAgent: async (payload, file) => {
      writes.push({ payload: JSON.parse(JSON.stringify(payload)), file })
      return { agent_id: payload.slug, can_run: true }
    }
  })
  const deps = {
    computed, nextTick, reactive, ref, cloneShareConfig, parseMcpManifest,
    mcpApi: { getMcpServers: async () => ({ data: [{ slug: 'ready', name: '可用', enabled: true }, { slug: 'off', enabled: false }] }) },
    useAgentStore: () => store,
    useRouter: () => ({ push: async (route) => { navigations.push(route) } }),
    useUserStore: () => ({ isAdmin, isSuperAdmin: isAdmin, uid: 'admin', departmentId: 1 }),
    isBuiltinAgent: () => false,
    message: { error: (value) => errors.push(value), warning: (value) => errors.push(value), success() {} },
    userApi: {}, generateAgentAvatar: () => '',
    MAX_IMAGE_UPLOAD_SIZE_BYTES: 1024, MAX_IMAGE_UPLOAD_SIZE_MB: 1
  }
  for (const name of ['Bot', 'FileArchive', 'Microscope', 'RefreshCw', 'Settings2', 'SlidersHorizontal', 'Upload', 'Wrench',
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

test('一次创建附带 ZIP、已有 MCP 与清单，校验失败保留完整草稿', async () => {
  const { form, writes, errors } = await setupForm({})
  form.openCreate()
  await form.loadCreateMcps()
  assert.deepEqual(form.createMcpOptions.value, [{ value: 'ready', label: '可用' }])
  const file = { name: 'guide.zip', size: 100 }
  assert.equal(form.beforeCreateSkillUpload(file), false)
  form.createMcpSelection.value = ['ready']
  form.createMcpManifest.value = '{invalid'
  await form.saveAgent()
  assert.equal(writes.length, 0)
  assert.equal(form.showAgentModal.value, true)
  assert.equal(form.createSkillFile.value.name, file.name)
  assert.equal(form.createOutcomeUnknown.value, false)
  assert.match(errors.pop(), /有效的 JSON/)
  form.createMcpManifest.value = '{"mcpServers":{"new":{"type":"http","url":"https://example.com/mcp"}}}'
  await form.saveAgent()
  assert.equal(writes.length, 1)
  assert.equal(writes[0].file.name, 'guide.zip')
  assert.deepEqual(writes[0].payload.config_json.context.mcps, ['ready'])
  assert.equal(writes[0].payload.mcp_servers[0].slug, 'new')
  assert.equal(form.showAgentModal.value, false)
})

test('等待时不重复创建，未知结果禁止在同一草稿直接重试，明确校验失败可修正', async () => {
  const { form, store, writes } = await setupForm({ agent_id: 'existing', name: 'Existing', can_manage: true, can_run: true })
  form.openCreate()
  let rejectRequest
  store.createAgent = () => {
    writes.push('sent')
    return new Promise((_, reject) => { rejectRequest = reject })
  }
  const request = form.saveAgent()
  await form.saveAgent()
  assert.equal(writes.length, 1)
  rejectRequest(Object.assign(new Error('invalid'), { status: 422 }))
  await request
  assert.equal(form.createOutcomeUnknown.value, false)
  assert.equal(form.showAgentModal.value, true)
  store.createAgent = async () => { writes.push('sent'); throw new TypeError('connection lost') }
  await form.saveAgent()
  assert.equal(form.createOutcomeUnknown.value, true)
  await form.saveAgent()
  assert.equal(writes.length, 2)
  await form.closeAgentModal()
  await form.openEdit({ agent_id: 'existing' })
  form.agentForm.name = 'Updated existing'
  await form.saveAgent()
  assert.equal(writes[2].slug, 'existing')
  assert.equal(writes[2].payload.name, 'Updated existing')
})

test('创建 ZIP 后缀和预算在上传前拒绝，普通用户不提交导入配置', async () => {
  const { form, errors } = await setupForm({}, false)
  form.openCreate()
  form.beforeCreateSkillUpload({ name: 'bad.md', size: 1 })
  form.beforeCreateSkillUpload({ name: 'big.zip', size: 10 * 1024 * 1024 + 1 })
  assert.equal(form.createSkillFile.value, null)
  assert.equal(errors.length, 2)
  form.createMcpManifest.value = 'ignored'
  assert.equal('mcp_servers' in form.buildAgentPayload(), false)
})

for (const outcome of ['pending', 'unknown', 'invalid']) {
  const description = { pending: '等待中的', unknown: '结果不明的', invalid: '校验失败后的' }[outcome]
  test(`旧编辑请求不能覆盖${description}创建草稿`, async () => {
    const detail = { agent_id: 'existing', name: 'Existing', can_manage: true, can_run: true }
    const { form, store } = await setupForm(detail)
    const detailReads = []
    store.fetchAgentDetail = () => new Promise((resolve) => detailReads.push(resolve))
    const opens = [form.openEdit('existing')]
    form.openCreate()
    form.agentForm.name = 'Creation draft'
    form.agentForm.slug = 'creation-draft'
    form.beforeCreateSkillUpload({ name: 'draft.zip', size: 100 })
    let rejectCreation
    store.createAgent = () => new Promise((_, reject) => { rejectCreation = reject })
    const request = form.saveAgent()
    try {
      form.openCreate()
      opens.push(form.openEdit('another'))
      assert.equal(form.agentForm.name, 'Creation draft')
      assert.equal(detailReads.length, 1)
      if (outcome !== 'pending') {
        rejectCreation(outcome === 'unknown'
          ? new TypeError('connection lost')
          : Object.assign(new Error('invalid'), { status: 422 }))
        await request
      }
      if (outcome === 'unknown') {
        form.openCreate()
        opens.push(form.openEdit('another'))
        assert.equal(detailReads.length, 1)
      }
      detailReads[0](detail)
      await opens[0]
      assert.equal(form.editingAgentId.value, null)
      assert.equal(form.agentForm.name, 'Creation draft')
      assert.equal(form.createSkillFile.value.name, 'draft.zip')
      assert.equal(form.createOutcomeUnknown.value, outcome === 'unknown')
      assert.equal(form.showAgentModal.value, true)
    } finally {
      for (const resolve of detailReads) resolve(detail)
      rejectCreation(Object.assign(new Error('invalid'), { status: 422 }))
      await Promise.allSettled([...opens, request])
    }
  })
}
