import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import test from 'node:test'
import { compileScript, parse } from 'vue/compiler-sfc'
import { ref } from 'vue'

test('专属 Skill 加载失败可重试，首次导入不传修订且冲突保留当前绑定', async () => {
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
  const busyStates = []
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
    agentApi: {
      getAgentBoundSkill: async () => {
        if (failLoad) throw new Error('读取失败')
        return { skill: { slug: 'bound' }, revision: 'before', can_manage: true }
      },
      uploadAgentBoundSkill: async (...args) => {
        uploaded = args
        throw new Error('已存在专属 Skill，请通过内容编辑入口修改')
      }
    }
  }
  const component = new Function(...Object.keys(deps), executable)(...Object.values(deps))
  const panel = component.setup({ agentSlug: 'current-agent' }, {
    expose() {},
    emit: (event, route) => {
      if (event === 'busy') busyStates.push(route)
      else {
        assert.equal(event, 'navigate')
        navigations.push(route)
      }
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
  assert.deepEqual(uploaded, ['current-agent', file])
  assert.equal(panel.binding.value.revision, 'before')
  assert.equal(panel.saving.value, false)
  assert.equal(errors[0], '已存在专属 Skill，请通过内容编辑入口修改')
  panel.openDetail()
  assert.deepEqual(navigations, [
    { name: 'ExtensionSkillDetail', params: { slug: 'bound' }, query: { agent: 'current-agent' } }
  ])
  let rejectUpload
  deps.agentApi.uploadAgentBoundSkill = () => new Promise((_, reject) => { rejectUpload = reject })
  const pending = panel.upload(file)
  panel.openDetail()
  assert.equal(panel.saving.value, true)
  assert.equal(busyStates.at(-1), true)
  await panel.upload(file)
  assert.equal(busyStates.at(-1), true, '重复上传不解除父级忙碌状态')
  assert.equal(navigations.length, 1, '导入等待期间不能跳转文件页')
  rejectUpload(new Error('导入失败'))
  await pending
  assert.equal(busyStates.at(-1), false)
  panel.loading.value = true
  panel.openDetail()
  assert.equal(navigations.length, 1, '重新读取期间也不能跳转')
  panel.loading.value = false
  panel.openDetail()
  assert.equal(navigations.length, 2)
})
