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

test('管理员打开坏配置不写入，明确保存以 slug 修复共享范围', async () => {
  const writes = []
  const selections = []
  const errors = []
  const detail = {
    id: 90, agent_id: 'fixture-agent', slug: 'fixture-agent', name: '权限修复',
    backend_id: 'ChatbotAgent', can_manage: true, share_config_invalid: true,
    share_config: { version: 2, read_scope: { access_level: 'department', department_ids: { 1: true } } }
  }
  const store = {
    hasConfigChanges: false,
    selectedAgent: detail,
    agentConfig: {},
    fetchAgentDetail: async () => detail,
    selectAgent: async (slug) => selections.push(slug),
    updateAgentProfile: async (slug, payload) => writes.push({ slug, payload: JSON.parse(JSON.stringify(payload)) })
  }
  const deps = {
    computed, nextTick, reactive, ref, cloneShareConfig,
    useAgentStore: () => store,
    useUserStore: () => ({ isAdmin: true, uid: 'admin', departmentId: 1 }),
    isBuiltinAgent: () => false,
    message: { error: (value) => errors.push(value), warning: (value) => errors.push(value), success() {} },
    userApi: {}, generatePixelAvatar: () => '',
    MAX_IMAGE_UPLOAD_SIZE_BYTES: 1024, MAX_IMAGE_UPLOAD_SIZE_MB: 1
  }
  for (const name of ['Bot', 'Microscope', 'RefreshCw', 'Settings2', 'SlidersHorizontal', 'Upload', 'Wrench',
    'AgentRuntimeConfigForm', 'ShareConfigForm', 'FallbackAvatar']) deps[name] = {}
  const component = new Function(...Object.keys(deps), executable)(...Object.values(deps))
  const form = component.setup({ backendOptions: [] }, { expose() {}, emit() {} })
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
