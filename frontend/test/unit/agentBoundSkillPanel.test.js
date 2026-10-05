import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import test from 'node:test'
import { compileScript, parse } from 'vue/compiler-sfc'
import { ref } from 'vue'

test('专属 Skill 加载失败可重试，上传传递整包修订且冲突保留当前绑定', async () => {
  const source = await fs.readFile(
    new URL('../../src/modules/agents/ui/AgentBoundSkillPanel.vue', import.meta.url),
    'utf8'
  )
  const { descriptor } = parse(source)
  const executable = compileScript(descriptor, { id: 'bound-skill-panel' })
    .content.replace(/^import[\s\S]*?from '[^']+'\n/gm, '')
    .replace('export default', 'return')
  let failLoad = true
  let uploaded
  const errors = []
  const navigations = []
  const deps = {
    ref,
    MarkdownPreview: {},
    skillApi: {
      getSkillFile: async (slug, path) => {
        assert.deepEqual([slug, path], ['bound', 'SKILL.md'])
        return { data: { content: '# Read only guide' } }
      }
    },
    onMounted() {},
    message: { error: (value) => errors.push(value), success() {} },
    Modal: { confirm() {} },
    agentApi: {
      getAgentBoundSkill: async () => {
        if (failLoad) throw new Error('读取失败')
        return { skill: { slug: 'bound' }, revision: 'before', can_manage: true }
      },
      uploadAgentBoundSkill: async (...args) => {
        uploaded = args
        throw new Error('Skill 包已修改，请重新加载后上传')
      }
    }
  }
  const component = new Function(...Object.keys(deps), executable)(...Object.values(deps))
  const panel = component.setup({ agentSlug: 'current-agent' }, {
    expose() {},
    emit: (event, route) => {
      assert.equal(event, 'navigate')
      navigations.push(route)
    }
  })
  await panel.load()
  assert.equal(panel.loading.value, false)
  assert.equal(panel.error.value, '读取失败')
  failLoad = false
  await panel.load()
  assert.equal(panel.error.value, '')
  assert.equal(panel.content.value, '# Read only guide')
  assert.match(descriptor.template.content, /<MarkdownPreview :content="content" compact/)
  assert.equal(panel.binding.value.revision, 'before')
  const file = { name: 'skill.zip' }
  await panel.upload(file)
  assert.deepEqual(uploaded, ['current-agent', file, 'before'])
  assert.equal(panel.binding.value.revision, 'before')
  assert.equal(panel.saving.value, false)
  assert.equal(errors[0], 'Skill 包已修改，请重新加载后上传')
  panel.openDetail()
  assert.deepEqual(navigations, [
    { name: 'ExtensionSkillDetail', params: { slug: 'bound' }, query: { agent: 'current-agent' } }
  ])
})
