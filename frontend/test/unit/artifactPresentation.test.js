import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { setImmediate } from 'node:timers/promises'
import { compileScript, parse } from 'vue/compiler-sfc'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { normalizeArtifacts } from '../../src/modules/session/model/artifacts.js'

const source = readFileSync(
  new URL('../../src/modules/session/ui/AgentArtifactsCard.vue', import.meta.url),
  'utf8'
)
const { descriptor } = parse(source)
const executable = compileScript(descriptor, { id: 'artifact-presentation' })
  .content.replace(/^import .+ from '[^']+'\n/gm, '')
  .replace('export default', 'return')

/** 执行实际组件 setup，使用受控预览字节检查分组和资源生命周期。 */
function setupCard(
  t,
  artifacts,
  preview = async () => ({ blob: async () => new Blob(['image'], { type: 'image/png' }) })
) {
  const urls = []
  const revoked = []
  const requests = []
  const scope = effectScope()
  const props = reactive({ artifacts, threadId: 'thread-a' })
  const previousWindow = globalThis.window
  globalThis.window = {
    URL: {
      createObjectURL: () => {
        const url = `blob:preview-${urls.length}`
        urls.push(url)
        return url
      },
      revokeObjectURL: (url) => revoked.push(url)
    }
  }
  const component = new Function(
    'deps',
    `const { computed, ref, watch, normalizeArtifacts, threadApi,
    message, ChevronDown, Download, LoaderCircle, Save, FileTypeIcon, WorkspacePathPicker, parseDownloadFilename } = deps;
    ${executable}`
  )({
    computed,
    ref,
    watch,
    normalizeArtifacts,
    threadApi: {
      previewThreadArtifact: (threadId, path) => {
        requests.push([threadId, path])
        return preview(threadId, path)
      }
    }
  })
  const view = scope.run(() => component.setup(props, { expose() {}, emit() {} }))
  t.after(() => {
    scope.stop()
    globalThis.window = previousWindow
  })
  return { props, view, urls, revoked, requests, stop: () => scope.stop() }
}

test('旧路径和带类型交付物统一归一化，同路径更新类型', () => {
  assert.deepEqual(
    normalizeArtifacts([' /a.png ', { path: '/a.png', type: 'image' }, '/b.png', '', null]),
    [
      { path: '/a.png', type: 'image' },
      { path: '/b.png', type: 'file' }
    ]
  )
})

test('图片与文件独立分组和计数，文件折叠不隐藏图片，显式 file 的 PNG 不发起图片请求', async (t) => {
  const { view, requests, urls, revoked, stop } = setupCard(t, [
    { path: '/image.png', type: 'image' },
    '/as-file.png',
    '/a.md',
    '/b.md',
    '/c.md'
  ])
  await setImmediate()
  const [images, files] = view.artifactGroups.value
  assert.equal(images.type, 'image')
  assert.equal(images.visibleItems.length, 1)
  assert.equal(files.items.length, 4)
  assert.equal(files.visibleItems.length, 3)
  view.expanded.value = true
  assert.equal(view.artifactGroups.value[1].visibleItems.length, 4)
  assert.deepEqual(requests, [['thread-a', '/image.png']])
  assert.equal(view.imagePreviews.value['/image.png'].status, 'ready')
  stop()
  assert.deepEqual(revoked, urls)
})

test('线程切换后旧请求不能发布图片，已发布 Blob URL 在切换时释放', async (t) => {
  let resolveOld
  const { props, view, urls, revoked } = setupCard(
    t,
    [{ path: '/a.png', type: 'image' }],
    (threadId) =>
      threadId === 'thread-a'
        ? new Promise((resolve) => {
            resolveOld = resolve
          })
        : Promise.resolve({ blob: async () => new Blob(['new'], { type: 'image/png' }) })
  )
  props.threadId = 'thread-b'
  await nextTick()
  await setImmediate()
  const currentUrl = view.imagePreviews.value['/a.png'].url
  resolveOld({ blob: async () => new Blob(['old'], { type: 'image/png' }) })
  await setImmediate()
  assert.equal(view.imagePreviews.value['/a.png'].url, currentUrl)
  assert.equal(urls.length, 1)
  props.artifacts = []
  await nextTick()
  assert.deepEqual(revoked, [currentUrl])
  assert.deepEqual(view.artifactGroups.value, [])
})

test('预览被拒绝或返回非图片时保留图片交付物及错误状态', async (t) => {
  const { view, urls } = setupCard(
    t,
    [
      { path: '/denied.png', type: 'image' },
      { path: '/large.png', type: 'image' }
    ],
    async (_thread, path) => {
      if (path === '/denied.png') throw new Error('403')
      return new Response(JSON.stringify({ supported: false }), {
        headers: { 'Content-Type': 'application/json' }
      })
    }
  )
  await setImmediate()
  assert.equal(view.artifactGroups.value[0].items.length, 2)
  assert.equal(view.imagePreviews.value['/denied.png'].status, 'error')
  assert.equal(view.imagePreviews.value['/large.png'].status, 'error')
  assert.deepEqual(urls, [])
})
