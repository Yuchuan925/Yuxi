import assert from 'node:assert/strict'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { after, before, test } from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'

let server
let QueryResultChunk

before(async () => {
  server = await createServer({
    root: path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..'),
    server: { middlewareMode: true }
  })
  ;({ default: QueryResultChunk } = await server.ssrLoadModule(
    '/src/modules/knowledge/ui/QueryResultChunk.vue'
  ))
})

after(async () => server?.close())

/** 渲染实际检索片段，验证评分与不可信文本的用户可见结果。 */
function render(chunk) {
  return renderToString(createSSRApp({ render: () => h(QueryResultChunk, { chunk, index: 0 }) }))
}

for (const [scoreType, label, score] of [
  ['bm25', '关键词相关度', 12.3],
  ['cosine', '向量相似度', 0],
  ['hybrid', '混合检索分数', 0.8],
  ['fusion', '融合排名分数', 0.01]
]) {
  test(`${label} 展示原始分数，零分仍可见`, async () => {
    const html = await render({ score_type: scoreType, score, content: '正文' })
    assert.match(html, new RegExp(`${label}: ${score.toFixed(4)}`))
    assert.doesNotMatch(html, /%/)
  })
}

test('高亮和完整正文中的 HTML 保持文字，只有命中段生成 mark', async () => {
  const html = await render({
    score_type: 'bm25',
    score: 4,
    content: '<script>alert(1)</script>完整正文',
    highlights: [
      [
        { text: '<img src=x onerror=alert(1)>', matched: false },
        { text: 'Milvus', matched: true }
      ]
    ]
  })
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/)
  assert.match(html, /<mark[^>]*>Milvus<\/mark>/)
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;完整正文/)
  assert.match(html, /查看完整片段/)
  assert.doesNotMatch(html, /<script|<img/)
})

test('关闭高亮后完整正文直接展示', async () => {
  const html = await render({ content: '完整正文', score: 0.2, score_type: 'cosine' })
  assert.match(html, /完整正文/)
  assert.doesNotMatch(html, /<mark|<details/)
})
