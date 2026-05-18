import { h } from 'vue'
import { Database, DatabaseZap } from 'lucide-vue-next'

const ICON_BASE = 'https://registry.npmmirror.com/@lobehub/icons-static-svg/latest/files/icons'

const createBrandIcon = (url) => {
  const Icon = ({ size = 20 }) => h('img', { src: url, style: { width: size + 'px', height: size + 'px' } })
  Icon.inheritAttrs = false
  return Icon
}

export const brandIcons = {
  dify: createBrandIcon(`${ICON_BASE}/dify-color.svg`),
  notion: createBrandIcon(`${ICON_BASE}/notion.svg`)
}

export const getKbTypeLabel = (type) => {
  const labels = {
    milvus: 'CommonRAG',
    dify: 'Dify',
    notion: 'Notion'
  }
  return labels[type] || type
}

export const getKbTypeIcon = (type) => {
  const icons = {
    milvus: DatabaseZap,
    dify: brandIcons.dify,
    notion: brandIcons.notion
  }
  return icons[type] || Database
}

export const getKbTypeColor = (type) => {
  const colors = {
    milvus: 'blue',
    dify: 'gold',
    notion: 'purple'
  }
  return colors[type] || 'blue'
}
