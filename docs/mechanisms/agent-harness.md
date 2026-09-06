# Agent Harness：每次运行怎样装配知识与能力

Yuxi 的 Agent Harness 把知识检索、Skills、Memory、MCP、子智能体和沙盒文件能力组织在同一条运行链路中。本页只解释这些能力的关系；Context 的详细构建顺序、配置语义、权限和恢复由[Agent 运行时上下文](./agent-runtime.md)负责。

## 入口

普通聊天、审批恢复和子智能体运行都会在服务端重新构建 Context。浏览器只提交请求和受限覆盖值，服务端从当前事实读取 Agent、用户、Conversation、Project 与资源权限。

```text
Agent config
User identity
Conversation → Project → Workdir
Runtime identity: thread / request / run / worker
            │
            ▼
Runtime Context assembler
            │
            ├─ Knowledge
            ├─ Skills
            ├─ MCP / Tools
            ├─ Memory（仅主 Agent）
            ├─ SubAgents
            └─ Workspace / Sandbox
            │
            ▼
LangGraph Agent
```

## 能力关系

| 能力 | 在 Harness 中的作用 | 事实 Owner |
| --- | --- | --- |
| Knowledge | 提供带来源的检索结果和文档窗口 | [知识库](./knowledge-base.md)、[知识图谱](./knowledge-graph.md) |
| Skills | 按选择、权限和激活状态提供方法、工具与 MCP | [Skills 运行机制](./skills.md) |
| Memory | 在开关开启且 `agents/MEMORY.md` 非空时为主 Agent 提供用户长期信息 | [用户 Memory](./memory.md) |
| SubAgents | 让根 Run 分派有独立 checkpoint 的子 Run | [子智能体调度](./subagents.md) |
| Workspace / Sandbox | 提供持久文件边界和隔离执行环境 | [沙盒与文件系统](./sandbox.md) |
| Context / checkpoint | 组合当前运行身份、配置和有效资源，并保存 Graph state | [Agent 运行时上下文](./agent-runtime.md) |

Agent 配置只确定候选能力。服务端根据当前用户重新计算有效范围，产生副作用的 service、repository、filesystem 或 executor 再校验具体目标。Full 与 LITE 的装配差异、配置空值语义、失败和恢复规则均以各机制 Owner 为准。

## 对外一句话

Yuxi 在每次运行前按用户身份重新装配知识、方法、工具、协作 Agent 和工作区，让 Agent 的能力范围与结果归属保持清晰。

## 源码定位与验证

- [Context 定义与资源归一化](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/agents/context.py)
- [AgentRun Manifest](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/services/agent_run_manifest_service.py)
- [主 Agent Graph](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/agents/buildin/chatbot/graph.py)
- [子 Agent Graph](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/agents/buildin/subagent/graph.py)
- [Agent 运行时上下文](./agent-runtime.md)
- [Agent 主链路测试](https://github.com/xerrors/Yuxi/tree/main/backend/test/e2e)
