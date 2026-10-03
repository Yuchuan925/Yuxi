// 以普通用户登录后运行：playwright-cli run-code --filename=frontend/test/browser/privateAgentPermissions.js
// prettier-ignore
async (page) => {
  const check = (value, message) => { if (!value) throw new Error(message) }
  const token = await page.evaluate(() => localStorage.getItem('user_token'))
  const headers = { Authorization: `Bearer ${token}` }
  const origin = await page.evaluate(() => location.origin)
  const api = async path => (await page.request.get(`${origin}${path}`, { headers })).json()
  const user = await api('/api/auth/me')
  check(user.role === 'user', '本测试需要真实普通用户身份')
  const backends = await api('/api/agent/backends')
  check(backends.backends.some(item => item.backend_id === 'ChatbotAgent' && item.can_create), '普通用户必须保留主 Agent 建设能力')
  check(backends.backends.filter(item => item.backend_id === 'SubAgentBackend').every(item => !item.can_create), '不能建设 SubAgent')
  await page.goto(`${origin}/agent-manage`)
  await page.getByRole('button', {name:'新增智能体',exact:true}).click()
  const modal = page.getByRole('dialog')
  check(await modal.getByText('发布为共享智能体').count() === 0, '普通用户不能发布')
  check(await modal.getByText('共享设置', {exact:true}).count() === 0, '私有创建不能配置共享范围')
  const name = `私有权限验证 ${Date.now()}`
  await modal.getByRole('textbox', {name:'智能体名称',exact:true}).fill(name)
  const received = page.waitForResponse(response => response.url().split('?')[0] === `${origin}/api/agent` && response.request().method() === 'POST')
  await modal.getByRole('button', {name:'创 建',exact:true}).click()
  const response = await received
  check(response.ok(), '创建请求必须成功')
  const agent = (await response.json()).agent
  try {
    const persisted = (await api(`/api/agent/${agent.slug}`)).agent
    check(persisted.visibility === 'private' && persisted.can_manage && persisted.can_run, '回读必须是自己的可管理私有 Agent')
    check(!persisted.can_publish && !persisted.can_share && !persisted.can_transfer, '普通用户不能获得共享写能力')
    await modal.waitFor({state:'hidden'})
    const card = page.locator('.info-card').filter({hasText:name})
    await card.waitFor()
    check(await card.getByText('私有', {exact:true}).count() === 1, '页面必须明确标记私有状态')
    await card.click()
    await modal.waitFor()
    check(await modal.getByText('发布为共享智能体').count() === 0, '编辑时仍不能发布')
    check(await modal.getByText('最大执行步数', {exact:true}).count() === 0, '私有所有权不能解锁高级字段')
    await page.setViewportSize({width:1440,height:1000})
    await page.waitForTimeout(500)
    await page.screenshot({path:'/tmp/yuxi-private-agent-light.png',fullPage:true})
    await page.evaluate(() => document.documentElement.classList.add('dark'))
    await page.screenshot({path:'/tmp/yuxi-private-agent-dark.png',fullPage:true})
    await page.setViewportSize({width:390,height:844})
    await page.screenshot({path:'/tmp/yuxi-private-agent-mobile.png',fullPage:true})
  } finally {
    const deleted = await page.request.delete(`${origin}/api/agent/${agent.slug}`, {headers})
    check(deleted.ok(), '浏览器测试必须清理自己的定义')
  }
}
