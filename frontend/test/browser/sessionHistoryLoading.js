// 确定性公开 API replay，使用真实 Vite 页面验证历史窗口、滚动锚点与重试。
// 执行：playwright-cli -s=history-loading run-code --filename=frontend/test/browser/sessionHistoryLoading.js
// prettier-ignore
async (page) => {
  const threadId = '00000000-0000-4000-8000-000000000001'
  const agentId = '00000000-0000-4000-8000-000000000002'
  const thread = {
    id: threadId, object: 'agent.session', status: 'completed', agent: { id: agentId },
    created_at: 1791580000, metadata: {},
    yuxi: { title: '历史消息加载验证', current_turn: null, context: {}, project_id: 'history-project', config: {}, is_pinned: false }
  }
  const agent = { agent_id: agentId, name: '历史验证助手', description: '用于历史阅读验证', config_json: { context: {} }, configurable_items: {}, metadata: {} }
  let longRun = false
  const item = id => ({
    id: String(id), object: 'agent.session.item', type: 'message',
    role: longRun ? 'user' : id % 2 ? 'user' : 'assistant', status: 'completed', turn_id: longRun ? 'turn-long' : `turn-${Math.ceil(id / 2)}`,
    content: [{ type: longRun || id % 2 ? 'input_text' : 'output_text', text: `历史消息 ${String(id).padStart(4, '0')}：这是一条用于验证阅读位置的消息。` }],
    yuxi: { message_id: String(id), run_id: longRun ? 'run-long' : `run-${Math.ceil(id / 2)}`, created_at: new Date(1791580000000 + id * 1000).toISOString() }
  })
  const requests = [], unknown = new Set()
  let failNext = false, delayed = false, release
  const check = (condition, message) => { if (!condition) throw new Error(message) }
  await page.addInitScript(() => localStorage.setItem('user_token', 'history-replay-token'))
  await page.route('**/api/**', async route => {
    const parsed = await page.evaluate(raw => { const url = new URL(raw); return { path: url.pathname, params: Object.fromEntries(url.searchParams) } }, route.request().url())
    const path = parsed.path, url = { searchParams: { get: name => parsed.params[name] || null } }
    let json
    if (path === '/api/auth/me') json = { id: 1, username: '历史验证', role: 'user' }
    else if (path === '/api/agent') json = { agents: [agent] }
    else if (path === `/api/agent/${agentId}`) json = agent
    else if (path.endsWith('/items')) {
      const after = url.searchParams.get('after'), limit = Number(url.searchParams.get('limit'))
      requests.push({ after, limit })
      if (delayed) { delayed = false; await new Promise(resolve => { release = resolve }) }
      if (failNext) { failNext = false; return route.fulfill({ status: 503, json: { detail: '历史读取暂不可用' } }) }
      const newest = after ? Number(after) - 1 : 950
      const data = Array.from({ length: Math.min(limit, newest) }, (_, index) => item(newest - index))
      json = { data, last_id: data.at(-1)?.id || null, has_more: newest > limit,
        yuxi: { runs: [...new Set(data.map(item => item.yuxi.run_id))].map(id => ({ id, turn_id: id.replace('run-', 'turn-'), status: 'completed', artifacts: [] })) } }
    } else if (path === '/api/v1/agents/sessions') json = { data: url.searchParams.get('is_pinned') === 'true' ? [] : [thread], has_more: false }
    else if (path.endsWith(`/${threadId}`) || path.endsWith('/viewed')) json = thread
    else if (path.endsWith('/state')) json = { agent_state: {}, interrupt: null }
    else if (path.endsWith('/queue')) json = { inputs: [], status: 'ready' }
    else if (path.endsWith('/cooperation')) json = { sessions: [], members: [], edges: [] }
    else if (path.endsWith('/attachments')) json = { files: [], attachments: [] }
    else if (path === '/api/projects') json = []
    else if (path.includes('/databases')) json = { databases: [] }
    else if (path.includes('/models')) json = { providers: [], models: [], data: [] }
    else { unknown.add(path); json = { data: [], files: [], skills: [], projects: [], providers: [], config: {}, site_name: '语析', first_run: false } }
    await route.fulfill({ json })
  })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`http://localhost:5173/agent/${threadId}`)
  const chat = page.locator('.agent-view .chat-main')
  await page.getByText('历史消息 0950：', { exact: false }).waitFor()
  await page.waitForFunction(() => document.querySelector('.agent-view .chat-main')?.scrollTop > 600)
  await page.waitForTimeout(300)
  check(requests.length === 3, `首次窗口请求数 ${requests.length}`)
  const bubbleAlignment = await chat.locator('.message-box.human').first().evaluate(el => ({
    right: el.getBoundingClientRect().right, parentRight: el.parentElement.getBoundingClientRect().right,
    width: el.getBoundingClientRect().width, parentWidth: el.parentElement.getBoundingClientRect().width
  }))
  check(bubbleAlignment.width < bubbleAlignment.parentWidth && Math.abs(bubbleAlignment.right - bubbleAlignment.parentRight) < 2, '用户气泡没有保持右对齐')
  check(await page.getByText('历史消息 0651：', { exact: false }).count() === 1, '初次没有读取300条')
  delayed = true
  await chat.evaluate(el => { el.scrollTop = 500 })
  await page.getByRole('button', { name: '正在加载更早消息…', exact: true }).waitFor({ state: 'attached' })
  await chat.evaluate(el => {
    const group = [...el.querySelectorAll('.history-display-item')].find(node => node.getBoundingClientRect().bottom > el.getBoundingClientRect().top)
    group.dataset.historyAnchor = 'true'
  })
  await chat.evaluate(el => { el.scrollTop = 0 })
  const movedTop = await page.locator('[data-history-anchor]').evaluate(el => el.getBoundingClientRect().top)
  await page.screenshot({ path: '/tmp/yuxi-history-loading.png' })
  check(requests.length === 4, '在途追加产生重复请求')
  release()
  await page.getByText('历史消息 0351：', { exact: false }).waitFor()
  await page.getByRole('button', { name: '加载更早消息', exact: true }).waitFor({ state: 'attached' })
  await page.waitForTimeout(200)
  const restoredTop = await page.locator('[data-history-anchor]').evaluate(el => el.getBoundingClientRect().top)
  check(Math.abs(restoredTop - movedTop) < 2, `阅读位置偏移 ${restoredTop - movedTop}px`)
  check(requests.length === 6, `追加请求数 ${requests.length}`)
  await page.screenshot({ path: '/tmp/yuxi-history-loaded.png' })
  failNext = true
  await chat.evaluate(el => { el.scrollTop = 400 })
  await page.getByRole('button', { name: '加载失败，点击重试', exact: true }).waitFor({ state: 'attached' })
  await chat.evaluate(el => { el.scrollTop = 200 })
  await page.waitForTimeout(200)
  check(requests.length === 7, '失败后发生自动重试')
  await chat.evaluate(el => { el.scrollTop = 0 })
  await page.screenshot({ path: '/tmp/yuxi-history-error.png' })
  await page.getByRole('button', { name: '加载失败，点击重试', exact: true }).click()
  await page.getByText('历史消息 0051：', { exact: false }).waitFor()
  await page.getByRole('button', { name: '加载更早消息', exact: true }).waitFor({ state: 'attached' })
  await page.evaluate(() => document.documentElement.classList.add('dark'))
  await page.setViewportSize({ width: 375, height: 812 })
  await chat.evaluate(el => { el.scrollTop = 300 })
  await page.getByText('历史消息 0001：', { exact: false }).waitFor()
  await page.waitForFunction(() => !document.querySelector('.history-load-btn'))
  check(requests.length === 11, `末页没有停止读取: ${requests.length}`)
  await page.locator('.ant-message-notice').waitFor({ state: 'hidden' })
  await page.screenshot({ path: '/tmp/yuxi-history-dark-mobile.png' })
  const completedRequests = [...requests]
  longRun = true
  requests.length = 0
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.evaluate(() => document.documentElement.classList.remove('dark'))
  await page.goto(`http://localhost:5173/agent/${threadId}`)
  await page.getByText('历史消息 0950：', { exact: false }).waitFor()
  await page.waitForFunction(() => document.querySelector('.agent-view .chat-main')?.scrollTop > 600)
  await page.waitForTimeout(300)
  check(await chat.locator('.group-box').count() === 1, '长Run用例没有合并到同一消息组')
  delayed = true
  await chat.evaluate(el => { el.scrollTop = 500 })
  await page.getByRole('button', { name: '正在加载更早消息…', exact: true }).waitFor({ state: 'attached' })
  const longAnchor = await chat.evaluate(el => {
    const item = [...el.querySelectorAll('.history-display-item')].find(node => node.getBoundingClientRect().bottom > el.getBoundingClientRect().top)
    return { key: item.dataset.historyKey, top: item.getBoundingClientRect().top }
  })
  release()
  await page.getByText('历史消息 0351：', { exact: false }).waitFor()
  await page.getByRole('button', { name: '加载更早消息', exact: true }).waitFor({ state: 'attached' })
  await page.waitForTimeout(200)
  const longTop = await chat.evaluate((el, key) => [...el.querySelectorAll('.history-display-item')].find(node => node.dataset.historyKey === key).getBoundingClientRect().top, longAnchor.key)
  check(Math.abs(longTop - longAnchor.top) < 2, `同Run内部追加阅读位置偏移 ${longTop - longAnchor.top}px`)
  check(await chat.locator('.group-box').count() === 1, '追加拆散了长Run')
  return { initialItems: 300, appendedItems: 300, anchorShift: restoredTop - movedTop, sameRunAnchorShift: longTop - longAnchor.top, totalItems: 950, requests: completedRequests, loading: true, retry: true, darkMobile: true, unknown: [...unknown] }
}
