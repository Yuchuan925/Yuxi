# 子智能体调度：共享文件，隔离运行状态

Yuxi 允许一个运行中的主 Agent 将任务拆给多个子智能体。每个子智能体形成独立 AgentRun 和 checkpoint thread，父子运行共享同一个 Project Workdir 与 execution runtime。本页只解释对外架构关系；创建条件、工具、权限、状态查询和取消语义由[子智能体配置](../agents/subagents-management.md)负责。

## 架构关系

```text
Parent AgentRun
      │
      ├─ SubagentThread → Child thread A → Child AgentRun A
      ├─ SubagentThread → Child thread B → Child AgentRun B
      └─ SubagentThread → Child thread C → Child AgentRun C

每个 Child：独立 checkpoint
所有 Child：共享 Project Workdir + runtime_scope_id
```

每个 child thread 隔离 LangGraph 状态，`runtime_scope_id` 和 Project Workdir 让同一执行树协作文件。子 Run 使用统一 worker、lease 和终态机制，结果沿持久化父子关系返回创建它的根运行。完整执行与失败边界以 owning 页面为准。

## 源码定位与验证

- [子智能体配置](../agents/subagents-management.md)
- [SubAgent Run service](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/services/subagent_run_service.py)
- [SubAgent Graph](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/agents/buildin/subagent/graph.py)
- [Agent 运行时上下文](./agent-runtime.md)
