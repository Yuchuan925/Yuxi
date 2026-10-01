import path from 'node:path'

/** 按实际目标路径检查前端分层，覆盖别名、相对路径和动态导入。 */
export const frontendBoundaries = {
  meta: {
    type: 'problem',
    schema: [],
    messages: {
      upward: '{{source}} 不能依赖 {{target}}；把装配保留在 app/pages，把通用能力保留在 shared。',
      obsolete: '旧入口 {{target}} 已移除，请引用所属领域或 shared。'
    }
  },
  create(context) {
    const filename = context.filename.replaceAll('\\', '/')
    const root = filename.lastIndexOf('/src/')
    if (root < 0) return {}
    const source = filename.slice(root + 5)
    /** 拒绝跨越前端职责边界的导入。 */
    function check(node, value) {
      if (typeof value !== 'string') return
      let target
      if (value.startsWith('@/')) target = path.posix.normalize(value.slice(2))
      else if (value.startsWith('/src/')) target = path.posix.normalize(value.slice(5))
      else if (value.startsWith('.')) target = path.posix.normalize(path.posix.join(path.posix.dirname(source), value))
      else return
      if (/^(components|composables|stores|utils|views|layouts|router)(\/|$)/.test(target)) {
        context.report({ node, messageId: 'obsolete', data: { target } })
        return
      }
      const upward = (source.startsWith('modules/') && /^(app|pages)(\/|$)/.test(target)) ||
        (source.startsWith('shared/') && /^(app|pages|modules|apis)(\/|$)/.test(target)) ||
        (source.startsWith('apis/') && /^(app|pages)(\/|$)/.test(target))
      if (upward) context.report({ node, messageId: 'upward', data: { source, target } })
    }
    return {
      ImportDeclaration: (node) => check(node.source, node.source.value),
      ExportNamedDeclaration: (node) => node.source && check(node.source, node.source.value),
      ExportAllDeclaration: (node) => check(node.source, node.source.value),
      ImportExpression: (node) => {
        const source = node.source
        const value = source.type === 'TemplateLiteral' && source.expressions.length === 0
          ? source.quasis[0].value.cooked
          : source.value
        check(source, value)
      }
    }
  }
}
