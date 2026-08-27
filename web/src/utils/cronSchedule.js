export const cronFieldGuides = [
  { label: '分钟', range: '0–59', unit: '分钟', description: '每小时中的第几分钟' },
  { label: '小时', range: '0–23', unit: '小时', description: '一天中的第几小时' },
  { label: '日期', range: '1–31', unit: '天', description: '每月的第几天' },
  { label: '月份', range: '1–12', unit: '月', description: '哪几个月执行' },
  { label: '星期', range: '0–6', unit: '周', description: '0 和 7 都代表周日' }
]

const weekdays = ['日', '一', '二', '三', '四', '五', '六']

const isNumber = (value) => /^\d+$/.test(value)

const describeValue = (value, fieldIndex) => {
  if (!isNumber(value)) return value
  const number = Number(value)
  if (fieldIndex === 0) return `${String(number).padStart(2, '0')} 分`
  if (fieldIndex === 1) return `${String(number).padStart(2, '0')} 点`
  if (fieldIndex === 2) return `每月 ${number} 号`
  if (fieldIndex === 3) return `${number} 月`
  if (fieldIndex === 4) return `周${weekdays[number === 7 ? 0 : number] || value}`
  return value
}

const describeToken = (token, fieldIndex) => {
  const field = cronFieldGuides[fieldIndex]
  if (!token) return '未填写'
  if (token === '*') return `每${field.unit}`
  if (/^\*\/\d+$/.test(token)) return `每 ${token.slice(2)} ${field.unit}`
  if (/^\d+-\d+$/.test(token)) {
    const [start, end] = token.split('-')
    return `${describeValue(start, fieldIndex)} 至 ${describeValue(end, fieldIndex)}`
  }
  if (token.includes(',')) {
    return token.split(',').map((part) => describeValue(part, fieldIndex)).join('、')
  }
  return describeValue(token, fieldIndex)
}

const simpleTime = (tokens) => {
  if (!isNumber(tokens[0]) || !isNumber(tokens[1])) return null
  return `${String(Number(tokens[1])).padStart(2, '0')}:${String(Number(tokens[0])).padStart(2, '0')}`
}

export const describeCron = (expression) => {
  const tokens = String(expression || '').trim().split(/\s+/)
  if (tokens.length !== 5 || tokens.some((token) => !token)) return '请输入完整的 5 段表达式'

  const [minute, hour, day, month, weekday] = tokens
  const time = simpleTime(tokens)
  if (time && day === '*' && month === '*' && weekday === '*') return `每天 ${time}`
  if (time && day !== '*' && month === '*' && weekday === '*') return `每月指定日期 ${time}（${describeToken(day, 2)}）`
  if (time && day === '*' && month === '*' && weekday !== '*') return `每周指定日期 ${time}（${describeToken(weekday, 4)}）`
  if (minute === '0' && hour === '*' && day === '*' && month === '*' && weekday === '*') return '每小时整点'
  if (minute.startsWith('*/') && hour === '*' && day === '*' && month === '*' && weekday === '*') return `每 ${minute.slice(2)} 分钟`

  return [minute, hour, day, month, weekday]
    .map((token, index) => `${cronFieldGuides[index].label}：${describeToken(token, index)}`)
    .join('；')
}

export const describeCronFields = (expression) => {
  const tokens = String(expression || '').trim().split(/\s+/)
  return cronFieldGuides.map((field, index) => ({
    ...field,
    token: tokens.length === 5 ? tokens[index] : '',
    meaning: tokens.length === 5 ? describeToken(tokens[index], index) : '等待填写'
  }))
}
