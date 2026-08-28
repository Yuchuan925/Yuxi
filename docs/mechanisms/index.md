# 机制详解

本组页面解释 Yuxi 在运行时为什么这样工作，适合已经完成[快速开始](../intro/quick-start.md)的开发者和运维人员。这里重点回答“谁创建、谁保存、谁能访问、失败后怎么办”，不重复配置手册里的完整变量清单。

## 怎么读

- 想做对外技术介绍：先看[Build agents on your knowledge](./public-architecture-brief.md)，再按页序阅读下面的机制。
- 想知道知识怎样进入 Agent：看[知识库](./knowledge-base.md)和[知识图谱](./knowledge-graph.md)。
- 想知道一次运行怎样装配能力和控制上下文成本：看[Agent Harness](./agent-harness.md)、[上下文工程](./context-engineering.md)、[Skills](./skills.md)和[用户 Memory](./memory.md)。
- 想知道长任务怎样执行、观察和协作：看[AgentRun](./agent-run.md)、[可观测性](./observability.md)和[子智能体调度](./subagents.md)。
- 想知道文件和命令在哪里执行：看[沙盒与文件系统](./sandbox.md)。
- 想知道长对话怎样压缩：看[上下文压缩](./context-compression.md)。

每页都按“入口 → 装配或派发 → 执行 Owner → 持久化/文件 → 可观察结果”展开。排查问题时先看“失败、恢复与观察边界”，修改实现时从“源码定位与验证”进入 owning 模块和测试。

## 专题地图

| 专题 | 回答的问题 | 配置/操作入口 |
| --- | --- | --- |
| [Build agents on your knowledge](./public-architecture-brief.md) | 如何用总—分结构介绍当前平台架构？ | [认识 Yuxi](../intro/project-overview.md) |
| [Agent Harness](./agent-harness.md) | 用户身份、配置、知识和能力怎样组成一次运行？ | [配置和开发智能体](../agents/agents-config.md) |
| [上下文工程](./context-engineering.md) | 哪些内容常驻，哪些内容按需加载，压缩怎样权衡成本？ | [中间件](../agents/middleware.md)、[智能体配置](../agents/agents-config.md) |
| [AgentRun](./agent-run.md) | Request、Run、worker、事件和终态怎样协作？ | [Agent 请求队列](../agents/agent-request-queue.md) |
| [可观测性](./observability.md) | 实时事件、Agent state、子 Run 和 Langfuse 怎样分工？ | [Langfuse 集成](../advanced/langfuse-integration.md) |
| [用户 Memory](./memory.md) | 长期信息怎样进入主 Agent，写入怎样授权？ | [智能体配置](../agents/agents-config.md) |
| [Skills](./skills.md) | 内置、共享和个人 Skill 怎样筛选、激活和挂载？ | [Skills 管理](../agents/skills-management.md) |
| [子智能体调度](./subagents.md) | 父子 Run 怎样共享文件并隔离 checkpoint？ | [子智能体](../agents/subagents-management.md) |
| [Agent 运行时上下文](./agent-runtime.md) | 配置、权限、文件和 checkpoint 怎样组成一次运行？ | [配置和开发智能体](../agents/agents-config.md) |
| [沙盒与文件系统](./sandbox.md) | runtime identity、挂载、路径权限和回收怎样协作？ | [沙盒配置与运维](../agents/sandbox-architecture.md) |
| [上下文压缩](./context-compression.md) | L1/L2 何时触发，摘要和历史文件由谁保存？ | [中间件](../agents/middleware.md)、[智能体配置](../agents/agents-config.md) |
| [知识库](./knowledge-base.md) | 文件状态、存储、权限和 Agent 检索怎样连接？ | [知识库教程](../intro/knowledge-base.md)、[文档处理](../advanced/document-processing.md) |
| [知识图谱](./knowledge-graph.md) | 关系检索怎样补充文本检索并回到 Chunk 证据？ | [知识图谱配置](../advanced/knowledge-base-graph.md) |

只有当主题有稳定的 Owner、真实 consumer 和可验证链路时，才新增机制页。未来设计放入 roadmap 或 proposed decision，不画进当前运行图。
