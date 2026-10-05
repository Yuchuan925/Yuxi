import assert from 'node:assert/strict'
import test from 'node:test'

import {
  AVATAR_BACKGROUND_TOKENS,
  generatePixelAvatar,
  generateAgentAvatar,
  getAvatarColorIndex,
  getAvatarFallbackStyle,
  getAvatarInitials
} from '../../src/shared/lib/pixelAvatar.js'

const DICEBEAR_GLYPHS_AVATAR_BASE_URL = 'https://api.dicebear.com/10.x/glyphs/svg'

test('pixel avatars are stable and provide localized fallbacks', () => {
  assert.equal(generatePixelAvatar('user-001'), generatePixelAvatar('user-001'))
  assert.notEqual(generatePixelAvatar('user-001'), generatePixelAvatar('user-002'))
  assert.equal(generatePixelAvatar('user-003'), `${DICEBEAR_GLYPHS_AVATAR_BASE_URL}?seed=user-003`)
  assert.equal(
    generatePixelAvatar(' user/中文 '),
    `${DICEBEAR_GLYPHS_AVATAR_BASE_URL}?seed=user%2F%E4%B8%AD%E6%96%87`
  )
  assert.throws(() => generatePixelAvatar(''), /requires an id/)
  assert.throws(() => generatePixelAvatar(null), /requires an id/)

  assert.equal(getAvatarInitials('张三丰', 'user'), '张三')
  assert.equal(getAvatarInitials('Alice', 'user'), 'Al')
  assert.equal(getAvatarInitials('', 'user'), '用户')
  assert.equal(getAvatarInitials('', 'agent'), '智能')

  const first = getAvatarColorIndex('user-001')
  assert.equal(first, getAvatarColorIndex('user-001'))
  assert.notEqual(first, getAvatarColorIndex('user-002'))
  assert.ok(first >= 0 && first < AVATAR_BACKGROUND_TOKENS.length)

  const style = getAvatarFallbackStyle('agent-001')
  assert.equal(typeof style.background, 'string')
  assert.equal(typeof style.color, 'string')
})

test('Agent 图形头像完全本地生成，身份文本不能成为 SVG 内容', () => {
  const avatar = generateAgentAvatar('agent-001')
  assert.match(avatar, /^data:image\/svg\+xml,/)
  assert.equal(avatar, generateAgentAvatar('agent-001'))
  assert.notEqual(avatar, generateAgentAvatar('agent-002'))
  const svg = decodeURIComponent(generateAgentAvatar('<script>alert(1)</script>').split(',')[1])
  assert.match(svg, /<svg /)
  assert.doesNotMatch(svg, /script|alert|href|https:/)
})
