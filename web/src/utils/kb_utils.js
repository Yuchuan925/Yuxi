import { Database, DatabaseZap } from 'lucide-vue-next'

export const getKbTypeLabel = (type) => {
  const labels = {
    milvus: 'CommonRAG',
    dify: 'Dify'
  }
  return labels[type] || type
}

export const getKbTypeIcon = (type) => {
  const icons = {
    milvus: DatabaseZap,
    dify: Database
  }
  return icons[type] || Database
}

export const getKbTypeColor = (type) => {
  const colors = {
    milvus: 'blue',
    dify: 'gold'
  }
  return colors[type] || 'blue'
}
