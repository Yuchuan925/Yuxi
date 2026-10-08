import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, reactive, ref } from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'

const { descriptor } = parse(readFileSync(new URL('../../src/pages/WorkspaceView.vue', import.meta.url), 'utf8'))
const compiled = compileScript(descriptor, { id: 'workspace-open-file' })
const executable = compiled.content
  .replace(/^import[\s\S]*?from '[^']+'\n/gm, '').replace('export default', 'return')

test('工作区消费文件打开参数，保留其他参数且后续可再次请求同一文件', async () => {
  const path = '/agents/MEMORY.md'
  const route = { path: '/workspace', query: { open: path, other: 'retained' } }
  const replacements = []
  const deps = Object.fromEntries(compiled.scriptSetupAst
    .filter((node) => node.type === 'ImportDeclaration')
    .flatMap((node) => node.specifiers.map((specifier) => [specifier.local.name, null])))
  Object.assign(deps, {
    computed, reactive, ref, watch() {}, onMounted() {}, onActivated() {}, onDeactivated() {}, onUnmounted() {},
    useUserStore: () => ({}), useRoute: () => route,
    useRouter: () => ({ replace: async (location) => { replacements.push(location); route.query = location.query } }),
    getWorkspaceTree: async () => ({ entries: [{ path, name: 'MEMORY.md', is_dir: false }] }),
    getWorkspaceFileContent: async () => ({}),
    normalizePreviewResponse: async () => ({ path, content: '# MEMORY', previewType: 'text' }),
    window: { URL: { revokeObjectURL() {} } }, message: { error() {} }
  })
  const component = new Function(...Object.keys(deps), executable)(...Object.values(deps))
  const state = component.setup({}, { expose() {} })
  await state.openFileByPath(path)
  assert.equal(state.selectedPreviewPath.value, path)
  assert.deepEqual(route.query, { other: 'retained' })
  state.closePreview()
  route.query.open = path
  await state.openFileByPath(path)
  assert.equal(state.selectedPreviewPath.value, path)
  assert.equal(replacements.length, 2)

  route.query.open = '/agents/USER.md'
  await state.openFileByPath(path)
  assert.equal(route.query.open, '/agents/USER.md', '不消费更新后的另一个打开请求')
  assert.equal(replacements.length, 2)
})
