import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, ref } from 'vue'

const source = readFileSync(
  new URL('../../src/modules/agents/ui/AgentManagePanel.vue', import.meta.url), 'utf8'
)
const expression = source.match(/const filteredAgents = computed\(\(\) => \{([\s\S]*?)\n\}\)/)[1]

test('管理列表按创建时间正序，搜索与重命名保持顺序且不修改来源', () => {
  const managedAgents = ref([
    { id: 4, name: 'Y 问答', agent_id: 'same-time', is_builtin: true, created_at: '2026-10-02T03:00:00Z' },
    { id: 1, name: 'A 内置', agent_id: 'builtin', is_builtin: true, created_at: '2026-10-01T00:00:00Z' },
    { id: 3, name: 'Z 问答', agent_id: 'newer', created_at: '2026-10-02T03:00:00Z' },
    { id: 9, name: 'B 问答', agent_id: 'older', created_at: '2026-10-02T10:00:00+08:00' }
  ])
  const searchQuery = ref('')
  const filtered = computed(new Function('managedAgents', 'searchQuery', expression).bind(null, managedAgents, searchQuery))
  assert.deepEqual(filtered.value.map(agent => agent.id), [1, 9, 3, 4])
  searchQuery.value = '问答'
  assert.deepEqual(filtered.value.map(agent => agent.id), [9, 3, 4])
  managedAgents.value[2].name = 'A 问答'
  assert.deepEqual(filtered.value.map(agent => agent.id), [9, 3, 4])
  assert.deepEqual(managedAgents.value.map(agent => agent.id), [4, 1, 3, 9])
  searchQuery.value = '没有匹配'
  assert.deepEqual(filtered.value, [])
})
