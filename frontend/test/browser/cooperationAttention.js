// 在独立浏览器打开开发前端后运行：playwright-cli run-code --filename=frontend/test/browser/cooperationAttention.js
// 使用确定性 HTTP fixture 验证真实 /agent 页面；所有 API 请求均被拦截，不修改服务端数据。
// prettier-ignore
async (page) => {
  const check = (value, message) => { if (!value) throw new Error(message) }
  const origin = page.url().split('/').slice(0, 3).join('/')
  const agent = { agent_id: 'default-chatbot', name: '智能助手', can_run: true, configurable_items: {}, config_json: { context: {} } }
  let failed = false, second = false, finished = false, historyDelay = 700
  const submissions = []
  const pageErrors = []
  const onPageError = error => pageErrors.push(error.message)
  page.on('pageerror', onPageError)
  const summary = () => ({ sessions: [
    { session_id: 'attention-root', path: '/root', turn_status: 'completed' },
    { session_id: 'attention-child', path: '/root/child', name: '资料整理', turn_status: finished ? 'completed' : 'waiting', waiting_for: 'answer' },
    ...(second ? [{ session_id: 'attention-approval', path: '/root/review', name: '文件整理与发布前检查', turn_status: 'waiting', waiting_for: 'approval' }] : [])
  ] })
  const turn = (id) => ({
    id: `turn-${id}`, status: finished || id === 'attention-root' ? 'completed' : 'requires_action',
    yuxi: { current_run_id: `run-${id}`, output: [], runs: [], waitpoint: {
      id: `wait-${id}`, run_id: `run-${id}`, kind: id === 'attention-approval' ? 'approval' : 'answer',
      questions: [{ question_id: 'task-kind', question: '你希望优先处理哪类任务？', options: [{ label: '资料检索', value: '资料检索' }], allow_other: true }],
      calls: [{ call_id: 'call-publish', name: 'write_file', args: { path: '/report.md', content: '测试报告' }, allowed_decisions: ['approve', 'reject'] }]
    } }
  })
  const session = (id) => ({
    id, agent: { id: agent.agent_id }, status: turn(id).status,
    yuxi: { title: id === 'attention-root' ? '协作任务待办验证' : '资料整理', project_id: 'attention-project', current_turn: turn(id), tool_approval_mode: 'ask' }
  })
  await page.route('**/api/**', async route => {
    const request = route.request(), path = request.url().split('?')[0].replace(/^https?:\/\/[^/]+/, '')
    const reply = body => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    if (path.endsWith('/cooperation')) {
      if (failed) return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'fixture offline' }) })
      return reply(summary())
    }
    const match = path.match(/\/sessions\/(attention-[^/]+)/)
    if (match) {
      const id = match[1]
      if (path.endsWith('/events') && request.method() === 'POST') {
        submissions.push({ id, body: request.postDataJSON() })
        finished = true
        return reply({ turn_id: `turn-${id}`, run_id: `run-${id}`, status: 'completed' })
      }
      if (path.includes('/turns/')) return reply(turn(id))
      if (path.endsWith('/items')) {
        if (id === 'attention-child' && historyDelay) await page.waitForTimeout(historyDelay)
        return reply({ data: [], has_more: false, last_id: null, yuxi: { runs: [] } })
      }
      if (path.endsWith('/state')) return reply({ agent_state: {} })
      if (path.endsWith('/queue')) return reply({ inputs: [], status: 'idle' })
      if (path.endsWith('/attachments')) return reply({ attachments: [] })
      return reply(session(id))
    }
    if (path === '/api/v1/agents/sessions') return reply({ data: [session('attention-root')], has_more: false, last_id: null })
    if (path === '/api/agent') return reply({ agents: [agent] })
    if (path === '/api/agent/default-chatbot') return reply(agent)
    if (path.endsWith('/users/me')) return reply({ id: 1, username: '测试用户', role: 'user' })
    if (path === '/api/projects') return reply([{ id: 'attention-project', name: '交互验证', workdir_path: '/test', threads: [] }])
    if (path.includes('/filesystem/tree')) return reply({ tree: [] })
    return reply({ data: [], databases: [], models: {}, first_run: false, enabled: false })
  })
  await page.addInitScript(() => localStorage.setItem('user_token', 'attention-browser-fixture'))
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto(`${origin}/agent/attention-root`)
  const pill = page.locator('.attention-pill')
  await pill.waitFor()
  check((await pill.textContent()).includes('资料整理 待回答'), '主会话没有发现子任务的回答请求')
  await pill.click()
  const dialog = page.locator('.panel-session-chat:visible [role="dialog"]')
  await dialog.waitFor()
  await page.waitForFunction(() => document.activeElement?.getAttribute('role') === 'dialog')
  check(await page.getByRole('tab', { name: '资料整理，待回答', exact: true }).count() === 1, '标签缺少待办徽标')
  await pill.click()
  check(await page.getByRole('tab', { name: '资料整理，待回答', exact: true }).count() === 1, '重复打开产生了重复标签')
  await page.getByRole('button', { name: '关闭 资料整理', exact: true }).click()
  check(await pill.isVisible(), '关闭子标签错误地清除了待办')
  await pill.click()
  await dialog.waitFor()
  await page.waitForFunction(() => document.activeElement?.getAttribute('role') === 'dialog')
  await page.getByRole('button', { name: '关闭 资料整理', exact: true }).click()
  const lateHistory = page.waitForResponse(response => response.url().includes('/sessions/attention-child/items'))
  await pill.click()
  await page.getByRole('tab', { name: '文件', exact: true }).click()
  await lateHistory
  await page.waitForFunction(async () => {
    const { useSessionRuntimeStore } = await import('/src/modules/session/model/sessionRuntime.js')
    return Boolean(useSessionRuntimeStore().getThreadState('attention-child')?.pendingInterrupt)
  })
  check(await page.evaluate(() => document.activeElement?.getAttribute('role') !== 'dialog'), '晚到的人工卡片抢走了其他标签的焦点')
  await pill.click()
  await dialog.waitFor()
  await page.waitForFunction(() => document.activeElement?.getAttribute('role') === 'dialog')
  historyDelay = 0
  await page.waitForFunction(() => [...document.querySelectorAll('.panel-session-chat [role="dialog"]')].some(el => el.offsetParent && getComputedStyle(el).opacity === '1'))
  await page.locator('.agent-view').screenshot({ path: '/tmp/yuxi-cooperation-attention-light.png', animations: 'disabled' })
  second = true
  await page.waitForFunction(() => document.querySelector('.attention-pill')?.textContent.includes('2 个协作任务'))
  await pill.focus()
  await page.keyboard.press('Enter')
  await page.waitForFunction(() => document.activeElement?.classList.contains('attention-item'))
  await page.keyboard.press('Tab')
  check(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')?.includes('待审批')), '键盘未进入下一条待办')
  await page.keyboard.press('Escape')
  await page.waitForFunction(() => document.activeElement?.classList.contains('attention-pill') && document.activeElement.getAttribute('aria-expanded') === 'false')
  await page.evaluate(async () => {
    const { useThemeStore } = await import('/src/shared/model/theme.js')
    useThemeStore().setTheme(true)
  })
  await pill.click()
  await page.getByRole('button', { name: '资料整理 待回答，去回答', exact: true }).waitFor()
  check(await page.evaluate(() => {
    const popup = [...document.querySelectorAll('.ant-popover-inner')].find(el => el.offsetParent)
    return document.documentElement.classList.contains('dark') && getComputedStyle(popup).backgroundColor !== 'rgb(255, 255, 255)'
  }), '暗色浮层仍使用浅色控件主题')
  await page.waitForFunction(() => document.activeElement?.classList.contains('attention-item'))
  await page.locator('.agent-view').screenshot({ path: '/tmp/yuxi-cooperation-attention-dark.png', animations: 'disabled' })
  await page.keyboard.press('Escape')
  await page.evaluate(async () => {
    const { useThemeStore } = await import('/src/shared/model/theme.js')
    useThemeStore().setTheme(false)
  })
  await pill.click()
  await page.getByRole('button', { name: '文件整理与发布前检查 待审批，查看审批', exact: true }).click()
  const approval = page.locator('.panel-session-chat:visible [role="dialog"]')
  await approval.getByRole('button', { name: '允许', exact: true }).waitFor()
  check(await page.getByRole('tab', { name: '文件整理与发布前检查，待审批', exact: true }).count() === 1, '审批没有打开正确标签')
  for (const width of [1024, 768, 375]) {
    await page.setViewportSize({ width, height: 900 })
    await page.getByRole('button', { name: '隐藏侧边栏', exact: true }).click()
    await pill.click()
    const item = page.getByRole('button', { name: '资料整理 待回答，去回答', exact: true })
    await item.waitFor()
    await page.waitForFunction(() => {
      const item = [...document.querySelectorAll('.attention-item')].find(el => el.offsetParent)
      const box = item?.getBoundingClientRect()
      return box && box.x >= 0 && box.right <= innerWidth + 1
    })
    await item.click()
    await dialog.waitFor()
  }
  await page.locator('.agent-view').screenshot({ path: '/tmp/yuxi-cooperation-attention-mobile.png', animations: 'disabled' })
  await page.setViewportSize({ width: 1440, height: 1000 })
  failed = true
  await page.getByText('协作状态更新失败，显示最近已知待办', { exact: true }).waitFor()
  check(await pill.isVisible(), '状态读取失败清除了最近已知待办')
  failed = false
  await page.locator('.attention-error').getByRole('button', { name: '重试', exact: true }).click()
  await page.locator('.attention-error').waitFor({ state: 'detached' })
  second = false
  await page.waitForFunction(() => document.querySelector('.attention-pill')?.textContent.includes('资料整理 待回答'))
  await dialog.locator('textarea').fill('资料检索')
  await dialog.getByRole('button', { name: '提交', exact: true }).click()
  await pill.waitFor({ state: 'detached' })
  check(submissions.length === 1 && submissions[0].id === 'attention-child', '回答提交给了其他会话')
  const event = submissions[0].body.events[0]
  check(event.turn_id === 'turn-attention-child' && event.waitpoint_id === 'wait-attention-child', '回答不属于目标等待点')
  check(await page.getByRole('tab', { name: '资料整理，待回答', exact: true }).count() === 0, '终态仍显示旧待办徽标')
  page.off('pageerror', onPageError)
  check(!pageErrors.length, `页面异常：${pageErrors.join('; ')}`)
  return { single: true, multiple: true, approval: true, slowRestoreFocus: true, hiddenDoesNotStealFocus: true, keyboard: true, retainedOnClose: true, retry: true, terminalRemoval: true, responseOwner: true, widths: [1440, 1024, 768, 375], screenshots: ['light', 'dark', 'mobile'], evidence: 'deterministic browser HTTP fixture' }
}
