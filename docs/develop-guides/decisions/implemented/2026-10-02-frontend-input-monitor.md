# 前端 Input 监视器收敛

状态：implemented
类型：simplification
Owner：frontend/src/modules/conversation/model/useAgentInputQueue.js

## 问题

队列同步会为同一 Thread 的每条 pending Input 创建一个独立 1 秒 timer 和 AbortController。多条 follow-up 输入同时排队时，timer、请求和清理路径按 Input 重复增长。

## 决策

每个 Thread 使用一个共享 Input 监视循环，顺序读取当前已登记 Input 的精确状态；保留 `inputMonitors[inputId]` 作为每条 Input 的身份索引和取消/完成收敛入口。任一 Input 的临时错误使用较长下一轮退避，终态、取消、路由离开和清理仍中止整个共享循环。队列快照继续只登记服务端返回的 pending Input，不猜测已消费记录。

## 替代方案

- 每条 Input 独立轮询：改动最小，但连接、timer 和清理随队列长度线性复制，不选择。
- 仅依赖队列快照：当前快照只返回 pending Input，不能证明已消费 Input 的 Run/Turn 归属，不选择。
- 新增后端批量状态接口：长期可进一步降低请求数，但需要公共协议和后端证据；本次先收敛前端生命周期，不擅自扩展 API。

## 后果

同一 Thread 至多有一个 Input 监视 timer/controller；Input 的状态语义和 `getThreadInput` 精确读取保持不变。多条输入仍可能产生多次读取，但不会产生多个独立长期调度器；未来若后端提供批量状态/事件契约，可由同一 Owner 替换读取策略。

## 验证

旧能力不存在：`useAgentInputQueue.js` 不再为每个 Input 创建独立 `AbortController` 和 `setTimeout`；同一 Thread 的登记项共享 `inputQueueMonitor.controller`。

重新引入条件：只有后端明确提供批量 Input 状态或事件契约，且真实 HTTP/worker 证据证明逐条读取仍是瓶颈时，才改变读取协议；不得在前端把 pending 快照推断为 consumed。

- `cd frontend && pnpm exec node --test test/unit/agentInputQueue.test.js test/unit/agentThreadQueueTransition.test.js test/unit/agentRunEvents.test.js`：Passed，15/15。
- `cd frontend && pnpm run lint:check`：Passed。
- `python3 scripts/verify_engineering_contracts.py`：Passed。
