/** 统一旧路径与带展示类型的交付物，按路径保序并采用最近类型。 */
export function normalizeArtifacts(artifacts = []) {
  const byPath = new Map()
  for (const item of artifacts) {
    const path = typeof item === 'string' ? item.trim() : item?.path?.trim()
    if (!path) continue
    byPath.set(path, { path, type: item?.type === 'image' ? 'image' : 'file' })
  }
  return [...byPath.values()]
}
