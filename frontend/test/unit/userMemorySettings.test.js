import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { ref } from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'

const source = readFileSync(new URL('../../src/modules/settings/ui/UserConfigSettingsCard.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const executable = compileScript(descriptor, { id: 'memory-settings' }).content
  .replace(/^import[\s\S]*?from '[^']+'\n/gm, '').replace('export default', 'return')

test('关闭 Memory 时仍可查看文件，导航成功后关闭设置且不改写偏好', async () => {
  const events = []
  const updates = []
  const deps = {
    ref, onMounted() {},
    inject: () => ({ closeSettingsModal: () => events.push('closed') }),
    useRouter: () => ({ push: async (location) => events.push(location) }),
    userConfigApi: {
      get: async () => ({ enable_memory: false }),
      update: async (config) => { updates.push(config); return config }
    },
    message: { success() {}, error() {} }
  }
  const component = new Function(...Object.keys(deps), executable)(...Object.values(deps))
  const state = component.setup({}, { expose() {} })
  await state.loadUserConfig()
  assert.equal(state.draftEnableMemory.value, false)
  await state.viewMemory()
  assert.deepEqual(events, [{ path: '/workspace', query: { open: '/agents/MEMORY.md' } }, 'closed'])
  assert.deepEqual(updates, [])
  assert.equal(state.draftEnableMemory.value, false)
})
