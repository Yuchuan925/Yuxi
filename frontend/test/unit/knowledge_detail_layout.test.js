import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

function readSource(relativePath) {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8')
}

test('知识库详情提供面板插槽并校验深链接 Tab', () => {
  const source = readSource('../../src/pages/KnowledgeBaseInfoView.vue')

  assert.match(source, /<ExtensionDetailLayout/)
  assert.match(source, /<template #breadcrumb>/)
  assert.match(source, /<template #actions>/)
  assert.match(source, /<template #panel-filetable>/)
  assert.match(source, /<template #panel-query>/)
  assert.match(source, /<template #panel-graph>/)
  assert.match(source, /<template #panel-evaluation>/)
  assert.match(source, /availableTabs\.some\(\(tab\) => tab\.key === requestedTab\)/)
  assert.match(
    source,
    /loaded && requestedTab && requestedTab !== activeTab\.value[\s\S]*?section: activeTab\.value/
  )
})

test('只读连接器没有知识库详情入口并拒绝直接详情 URL', () => {
  const listSource = readSource('../../src/modules/knowledge/ui/KnowledgeCatalog.vue')
  const detailSource = readSource('../../src/pages/KnowledgeBaseInfoView.vue')

  assert.match(listSource, /:disabled="kbUtils\.isReadOnlyKnowledgeBase\(knowledgeBase\)"/)
  assert.match(
    listSource,
    /const navigateToKnowledgeBase = \(knowledgeBase\) => \{\s*if \(kbUtils\.isReadOnlyKnowledgeBase\(knowledgeBase\)\) return/
  )
  assert.match(listSource, /<a-menu-item v-if="knowledgeBase\.can_manage" key="edit">/)
  assert.match(
    detailSource,
    /store\.knowledgeBase\?\.kb_id === nextKbId &&[\s\S]*?kbUtils\.isReadOnlyKnowledgeBase\(store\.knowledgeBase\)[\s\S]*?route\.query\.action === 'edit' && canManageKnowledgeBase\.value[\s\S]*?showEditModal\(\)[\s\S]*?router\.replace\(\{ path: '\/extensions', query: \{ tab: 'knowledge' \} \}\)/
  )
  assert.match(
    detailSource,
    /action !== 'edit' \|\| loading \|\| !loaded \|\| !canManageKnowledgeBase\.value/
  )
  assert.match(detailSource, /@after-close="handleEditModalAfterClose"/)
  assert.match(
    detailSource,
    /const handleEditModalAfterClose = \(\) => \{\s*if \(isConnector\.value\) backToKnowledgeBase\(\)/
  )
})

test('检索面板强制挂载以保留上传后的示例问题生成', () => {
  const detailSource = readSource('../../src/pages/KnowledgeBaseInfoView.vue')

  assert.match(detailSource, /key: 'query', label: '检索测试', icon: Search, forceRender: true/)
})
