import assert from 'node:assert/strict'

import { describeCron, describeCronFields } from '../cronSchedule.js'

assert.equal(describeCron('0 9 * * *'), '每天 09:00')
assert.equal(describeCron('0 9 * * 1-5'), '每周指定日期 09:00（周一 至 周五）')
assert.equal(describeCron('*/30 * * * *'), '每 30 分钟')
assert.equal(describeCron('0 * * * *'), '每小时整点')
assert.equal(describeCron('not a cron'), '请输入完整的 5 段表达式')
assert.deepEqual(
  describeCronFields('0 9 * * *').map(({ token, meaning }) => ({ token, meaning })),
  [
    { token: '0', meaning: '00 分' },
    { token: '9', meaning: '09 点' },
    { token: '*', meaning: '每天' },
    { token: '*', meaning: '每月' },
    { token: '*', meaning: '每周' }
  ]
)

console.log('cronSchedule: all assertions passed')
