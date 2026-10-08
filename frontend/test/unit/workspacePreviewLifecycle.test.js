import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, effectScope, nextTick, reactive, ref, unref, watch } from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'

const source = readFileSync(new URL('../../src/pages/WorkspaceView.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const compiled = compileScript(descriptor, { id: 'workspace-preview-lifecycle' })
const executable = compiled.content
  .replace(/^import[\s\S]*?from '[^']+'\n/gm, '')
  .replace('export default', 'return')
const modalExpression = source.match(/:open="([^"]*previewModalVisible[^"]*)"/)[1]
const isModalOpen = new Function('state', `with (state) { return ${modalExpression} }`)

/** 编译实际页面，用可控宽度和生命周期触发预览切换。 */
function setupWorkspace(t, width) {
  const hooks = {}
  const deps = Object.fromEntries(
    compiled.scriptSetupAst
      .filter((node) => node.type === 'ImportDeclaration')
      .flatMap((node) => node.specifiers.map((specifier) => [specifier.local.name, null]))
  )
  Object.assign(deps, {
    computed, reactive, ref, watch,
    window: { removeEventListener() {} },
    document: { body: { style: {} } },
    useRoute: () => ({ query: {} }),
    useRouter: () => ({}),
    useUserStore: () => ({}),
    onMounted: (callback) => { hooks.mounted = callback },
    onActivated: (callback) => { hooks.activated = callback },
    onDeactivated: (callback) => { hooks.deactivated = callback },
    onUnmounted: (callback) => { hooks.unmounted = callback }
  })
  const scope = effectScope()
  const component = new Function(...Object.keys(deps), executable)(...Object.values(deps))
  const state = scope.run(() => component.setup({}, { expose() {} }))
  state.workspaceMainRef.value = { clientWidth: width }
  state.workspaceMainWidth.value = width
  t.after(() => scope.stop())
  return { state, hooks, view: new Proxy(state, { get: (target, key) => unref(target[key]) }) }
}

test('侧栏预览离开缓存页面后不因零宽度变成弹窗，返回时恢复侧栏', async (t) => {
  const { state, hooks, view } = setupWorkspace(t, 1200)
  state.startPreviewRequest({ path: '/agents/MEMORY.md', name: 'MEMORY.md' })
  await nextTick()
  assert.equal(view.showInlinePreview, true)
  assert.equal(isModalOpen(view), false)

  hooks.deactivated?.()
  state.workspaceMainWidth.value = 0
  await nextTick()
  assert.equal(isModalOpen(view), false, '离开个人空间后不得出现预览弹窗')
  assert.equal(view.inlinePreviewVisible, true, '隐藏容器的零宽度不得改变侧栏预览模式')
  assert.equal(view.previewModalVisible, false)
  assert.equal(view.selectedPreviewPath, '/agents/MEMORY.md')

  await hooks.activated()
  await nextTick()
  assert.equal(view.showInlinePreview, true)
  assert.equal(isModalOpen(view), false)

  state.workspaceMainWidth.value = 700
  await nextTick()
  assert.equal(isModalOpen(view), true, '激活页面缩窄时仍可切换到弹窗')

  state.workspaceMainWidth.value = 1200
  await nextTick()
  state.activeSourceKey.value = 'database:test-kb'
  hooks.deactivated?.()
  state.workspaceMainWidth.value = 0
  state.workspaceMainRef.value.clientWidth = 700
  await nextTick()
  await hooks.activated()
  await nextTick()
  assert.equal(isModalOpen(view), true, '后台缩窄后返回也必须恢复预览')
})

test('窄屏预览弹窗离开缓存页面后隐藏，返回仍保留选中文件', async (t) => {
  const { state, hooks, view } = setupWorkspace(t, 700)
  state.startPreviewRequest({ path: '/agents/MEMORY.md', name: 'MEMORY.md' })
  await nextTick()
  assert.equal(isModalOpen(view), true)
  hooks.deactivated?.()
  await nextTick()
  assert.equal(isModalOpen(view), false)
  await hooks.activated()
  await nextTick()
  assert.equal(isModalOpen(view), true)
  assert.equal(view.selectedPreviewPath, '/agents/MEMORY.md')
})
