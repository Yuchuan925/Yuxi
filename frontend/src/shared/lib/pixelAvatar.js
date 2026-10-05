const DICEBEAR_GLYPHS_AVATAR_BASE_URL = 'https://api.dicebear.com/10.x/glyphs/svg'

export const AVATAR_BACKGROUND_TOKENS = [
  { background: 'linear-gradient(135deg, var(--main-600), var(--color-info-500))', color: '#fff' },
  {
    background: 'linear-gradient(135deg, var(--chart-palette-5), var(--chart-palette-9))',
    color: '#fff'
  },
  {
    background: 'linear-gradient(135deg, var(--chart-palette-7), var(--chart-palette-3))',
    color: '#fff'
  },
  {
    background: 'linear-gradient(135deg, var(--color-accent-500), var(--chart-palette-6))',
    color: '#fff'
  },
  {
    background: 'linear-gradient(135deg, var(--chart-palette-4), var(--color-error-500))',
    color: '#fff'
  }
]

const normalizeSeed = (id) => {
  if (id === null || id === undefined || String(id).trim() === '') {
    throw new Error('generatePixelAvatar requires an id')
  }
  return String(id).trim()
}

export const generatePixelAvatar = (id) => {
  const seed = normalizeSeed(id)
  return `${DICEBEAR_GLYPHS_AVATAR_BASE_URL}?seed=${encodeURIComponent(seed)}`
}

export const getAvatarInitials = (name, kind = 'user') => {
  const fallback = kind === 'agent' ? '智能' : '用户'
  const normalizedName = String(name || '').trim()
  if (!normalizedName) return fallback
  return Array.from(normalizedName).slice(0, 2).join('')
}

export const getAvatarColorIndex = (seed) => {
  const normalizedSeed = String(seed || '').trim()
  const value = normalizedSeed || 'avatar'
  let hash = 0
  for (const char of value) {
    hash = (hash * 31 + char.codePointAt(0)) >>> 0
  }
  return hash % AVATAR_BACKGROUND_TOKENS.length
}

export const getAvatarFallbackStyle = (seed) => AVATAR_BACKGROUND_TOKENS[getAvatarColorIndex(seed)]

/** 根据 Agent 身份生成本地图形头像，不依赖外部服务。 */
export const generateAgentAvatar = (id) => {
  const seed = normalizeSeed(id)
  const palette = [
    ['#E8E5F6', '#645596'],
    ['#DBEEF0', '#317E88'],
    ['#F7E4DE', '#AC6350'],
    ['#E2EAE1', '#5D7B59'],
    ['#F2E7CF', '#96722E']
  ]
  const [background, foreground] = palette[getAvatarColorIndex(seed)]
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" rx="9" fill="${background}"/><g stroke="${foreground}" stroke-width="2.5" stroke-linecap="round" fill="none"><path d="M20 9v4"/><rect x="10" y="13" width="20" height="17" rx="5"/><path d="M6 20v5m28-5v5m-17 0h6"/></g><g fill="${foreground}"><circle cx="16" cy="20" r="1.7"/><circle cx="24" cy="20" r="1.7"/></g></svg>`
  return `data:image/svg+xml,${encodeURIComponent(svg)}`
}
