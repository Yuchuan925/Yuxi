import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = readFileSync(
  new URL('../../src/components/common/ActionDropdown.vue', import.meta.url),
  'utf8'
)

test('搜索输入框使用单一键盘处理器并消费 Escape', () => {
  const searchInput = source.match(/<a-input[\s\S]*?\/>/)?.[0]
  assert.ok(searchInput)
  assert.match(searchInput, /@keydown="handleSearchKeydown"/)
  assert.doesNotMatch(searchInput, /@keydown\.[^\s=]+/)
  assert.match(source, /function handleSearchKeydown\(event\)[\s\S]*?event\.stopPropagation\(\)[\s\S]*?event\.key === 'Escape'[\s\S]*?event\.preventDefault\(\)/)
})
