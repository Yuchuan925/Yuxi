# AgentRun：一次请求怎样获得执行权并留下结果

Yuxi 把用户意图和实际执行分成 `AgentRunRequest` 与 `AgentRun`，让接入、排队、执行和结果可以分别观察。本页只提供对外架构摘要；完整状态、队列策略、取消、租约和恢复语义由[Agent 请求队列](../agents/agent-request-queue.md)负责。

## 架构关系

```text
Message + AgentRunRequest
            │
            ▼
       ready FIFO head
            │
            ▼
         AgentRun ── PostgreSQL commit ── ARQ ── worker lease
            │                                      │
            └──────── Message / state / files ◀────┘
```

| 对象 | 架构职责 |
| --- | --- |
| Request | 保存已接收的用户意图和排队事实 |
| Run | 绑定一次 worker 执行、lease、事件和终态 |
| Redis / SSE | 投递任务并投影实时过程 |
| PostgreSQL | 保存 Request、Run、Message、checkpoint 和最终业务状态 |
| Project Workdir | 保存当前执行产生的文件字节 |

只有当前有效 lease owner 可以提交同一 Run 的输出和终态。Redis 事件不能替代 PostgreSQL 事实，过期 lease 会收敛为可观察的失败。具体事务顺序、状态表和负向边界见 owning 页面。

## 源码定位与验证

- [Agent 请求队列](../agents/agent-request-queue.md)
- [统一请求提交](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/services/run_submission_service.py)
- [Run worker](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/services/run_worker.py)
