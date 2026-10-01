import assert from 'node:assert/strict'
import test from 'node:test'

import { createMarkdownRenderer } from '../../src/shared/lib/markdown_preview.js'

test('无代码高亮器时 Markdown 仍保留结构化渲染', () => {
  const renderer = createMarkdownRenderer({ themeName: 'github-light', highlighter: null })
  const html = renderer.render('# Skill\n\n```python\nprint(42)\n```')
  assert.match(html, /<h1>Skill<\/h1>/)
  assert.match(html, /<pre><code/)
})

test('frontmatter 保留多行字段的 YAML 缩进', () => {
  const renderer = createMarkdownRenderer({ themeName: 'github-light', highlighter: null })
  const html = renderer.render(`---
name: minimax-pdf
description:
  Use this skill when visual quality and design identity matter for a PDF.
  CREATE (generate from scratch): "make a PDF", "generate a report".
license: MIT
metadata:
  version: "1.0"
  category: document-generation
---
`)

  assert.match(html, /class="frontmatter-card"/)
  assert.match(html, /minimax-pdf/)
  assert.match(html, /Use this skill when visual quality and design identity matter for a PDF\./)
  assert.match(html, /document-generation/)
})

test('relative document images resolve beside their artifact with the same authorization boundary', async () => {
  const { resolveMarkdownImageUrl } = await import('../../src/shared/lib/markdown_preview.js')
  const base = '/api/v1/agents/threads/thread-1/artifacts/home/gem/user-data/project/parsed/document.md'
  const origin = 'https://yuxi.example'
  assert.equal(
    resolveMarkdownImageUrl('images/chart.png', base, origin),
    '/api/v1/agents/threads/thread-1/artifacts/home/gem/user-data/project/parsed/images/chart.png'
  )
  assert.equal(resolveMarkdownImageUrl('images/chart.png', '', origin), null)
  assert.equal(resolveMarkdownImageUrl('https://outside.example/api/knowledge/databases/a/images/x.png', base, origin), null)
  assert.equal(resolveMarkdownImageUrl('//outside.example/x.png', base, origin), null)
  assert.equal(resolveMarkdownImageUrl('/api/knowledge/databases/kb/images/kb-images/chart.png', '', origin), '/api/knowledge/databases/kb/images/kb-images/chart.png')
})
