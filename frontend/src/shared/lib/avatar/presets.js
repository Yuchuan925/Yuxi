/** 每套色卡包含协调的多色主体、强调色、纸色与墨色，并配有夜间版本。 */
function palette(label, description, light, dark) {
  return { label, description, light, dark }
}
export const presets = {
  default: palette(
    '官方原色',
    '保留原作的鲜明与轻快',
    { body: ['76a7ff', '75c675', 'ffaa64', 'ff4d6f', 'af61f2'], paper: 'ffffff', ink: '292d38' },
    { body: ['97baff', '9bd699', 'ffc28f', 'ff91a5', 'c396f4'], paper: '222633', ink: '252936' }
  ),
  studio: palette(
    '设计工作室',
    '钴蓝 · 珊瑚 · 黄油黄 · 薄荷',
    { body: ['6489c7', 'e69482', 'e4c66f', '89b4a2'], paper: 'f5f3ee', ink: '333846' },
    { body: ['8fafe2', 'f0b09e', 'eed78e', 'a2cbb8'], paper: '242832', ink: '29313e' }
  ),
  heritage: palette(
    '复古印刷',
    '陶红 · 芥黄 · 湖绿 · 烟紫',
    { body: ['bf796b', 'c5a55b', '709b95', 'a18da9'], paper: 'f4eee3', ink: '433a36' },
    { body: ['d89a87', 'dcc080', '98bbb0', 'c0aac5'], paper: '302c2b', ink: '403832' }
  ),
  soft: palette(
    '柔光粉彩',
    '雾蓝 · 丁香 · 杏桃 · 鼠尾草',
    { body: ['a5bbd5', 'bba9cf', 'e5b89a', 'a6bca2'], paper: 'f6f4f7', ink: '454451' },
    { body: ['b5c8e3', 'cbb9de', 'ecc7ac', 'bfd0b5'], paper: '2b2935', ink: '3a3546' }
  )
}

/** 把视觉色卡映射到每种风格的原生颜色通道。 */
export function paletteOptions(style, preset, dark) {
  if (preset === 'default' && !dark) return {}
  const { body, paper, ink } = presets[preset][dark ? 'dark' : 'light']
  switch (style) {
    case 'glyphs':
      return { glyphColor: body, ...(dark ? { paperColor: [paper] } : {}) }
    case 'clay':
      return { backgroundColor: [paper], bodyColor: body, accentColor: body, inkColor: [ink] }
    case 'shape-grid':
      return { backgroundColor: dark ? [paper] : body, ...(dark ? { shapeColor: body } : {}) }
    case 'gaze':
      return { bodyColor: body, inkColor: [ink], glintColor: ['ffffff'] }
  }
}
