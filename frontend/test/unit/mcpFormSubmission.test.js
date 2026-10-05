import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, reactive, ref, watch } from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'
import { parseMcpManifest } from '../../src/modules/extensions/model/mcpManifest.js'

const source = readFileSync(
  new URL('../../src/modules/extensions/ui/McpFormModal.vue', import.meta.url), 'utf8'
)
const { descriptor } = parse(source)
const executable = compileScript(descriptor, { id: 'mcp-submit-test' }).content
  .replace(/^import .* from '[^']+'\n/gm, '')
  .replace('export default', 'return')

for (const editMode of [false, true]) {
  for (const headersText of ['false', '0', 'null', '[]']) {
    test(`${editMode ? '编辑' : '创建'}表单拒绝显式非法 headers ${headersText}`, async () => {
      const errors = []
      const writes = []
      const events = []
      const deps = {
        computed, reactive, ref, watch, parseMcpManifest,
        CollapseTransition: {}, ChevronRight: {}, Plug: {},
        message: { error: (value) => errors.push(value) },
        mcpApi: {
          createMcpServer: (data) => writes.push(data),
          updateMcpServer: (slug, data) => writes.push({ slug, ...data })
        }
      }
      const component = new Function(...Object.keys(deps), executable)(...Object.values(deps))
      const form = component.setup({ open: true, editMode, editData: null }, {
        expose() {}, emit: (...args) => events.push(args)
      })
      Object.assign(form.form, {
        slug: 'example', name: 'Example', url: 'https://example.com/mcp', headersText
      })
      await form.handleFormSubmit()
      assert.match(errors[0], /headers 必须是字符串键值对象/)
      assert.deepEqual(writes, [])
      assert.deepEqual(events, [])
      assert.equal(form.formLoading.value, false)
    })
  }
}
