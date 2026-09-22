# Build agents on your knowledge

Yuxi（语析）是一个可私有部署的多租户知识智能体平台。它把知识库、知识图谱、Agent 运行、Skills、Memory、子智能体和沙盒工作区放进同一条可追踪链路，让团队可以用自己的资料构建能够检索、执行并交付文件的 Agent。

这组对外材料围绕一个观点展开：**将知识能力作为 Agent Harness 的原生能力。**

## 为什么这样组织

知识问答只解决“模型能查到什么”。真实任务还需要回答五个问题：

1. 资料怎样变成可以引用的证据；
2. 当前用户和 Agent 能使用哪些知识与能力；
3. 一次请求怎样排队、执行、取消和结束；
4. 主 Agent 怎样调度子智能体完成并行工作；
5. 代码、工具和文件怎样在隔离环境中执行并持续保留。

Yuxi 用三个平面连接这些问题：

```text
知识平面：原件 → 解析 → Chunk → 向量 / 全文 → 可选图谱 → 来源证据
运行平面：身份 + Agent 配置 + 权限 → Harness → AgentRun → LangGraph
工作平面：Project Workdir ↔ Sandbox → Artifact / Viewer / Download
```

PostgreSQL 保存业务状态、运行记录、消息、Chunk 正文和 LangGraph checkpoint。Redis 负责 ARQ 投递、运行事件、取消信号和短期缓存。MinIO 保存知识库原件和解析对象。Milvus 保存向量与 BM25 字段。Neo4j 保存可选的实体关系和 Chunk 关联。

## 11 页展示结构

整套展示采用“总—分”结构：前两页说明项目定位和整体架构，后九页依次拆解知识、运行、上下文、观察和执行边界。每个机制只占一页。

### 01｜项目定位

页面只保留：

> Build agents on your knowledge
>
> 将知识库能力作为 Agent Harness 的原生能力

画面表现资料、关系网络、Agent 执行核心和持久工作区连成一条链。

### 02｜整体架构

标题：

> 知识、能力、执行，落在同一个工作区

主图从左到右展示知识系统、Agent Harness 和用户结果。Harness 内只保留六类原生能力：Knowledge、Skills、Memory、MCP/Tools、SubAgents、Workspace/Sandbox；LangGraph 作为执行主轴。

### 03｜知识库与知识图谱

标题：

> 文件成为证据，图谱补充关系

主图上半部分展示原件 → 解析/OCR → Chunk/Embedding → PostgreSQL + Milvus → 来源结果；下半部分展示文本检索和可选图谱检索怎样回到同一份 Chunk 证据。图谱负责关系组织，Chunk 保留原文和来源。

完整机制见[知识库机制](./knowledge-base.md)和[知识图谱机制](./knowledge-graph.md)。

### 04｜Agent Harness

标题：

> 每次运行都按身份装配知识与能力

主图从用户身份、Agent 配置和 Project 进入运行时筛选，再把知识库、Skills、MCP/Tools、Memory、子智能体和 Workspace 交给 Agent Graph。资源选择只能在当前用户可见范围内生效；产生副作用时，执行器继续校验具体目标。

完整机制见[Agent Harness](./agent-harness.md)。

### 05｜上下文工程

标题：

> 按需加载，压缩必须权衡成本

主图先区分 Minimum Context 与按需加载的 Knowledge、Memory、Skills、Files，再展示 Context Budget。L1 裁剪和外置大结果后可以直接继续；仍然过大时才进入 L2 摘要。页面底部用一条轴表达 token 成本与保留原文之间的权衡。

完整机制见[上下文工程](./context-engineering.md)和[上下文压缩](./context-compression.md)。

### 06｜AgentRun

标题：

> 每次执行都有请求、运行、事件和最终状态

主图：Message + AgentRunRequest → FIFO 队头 → AgentRun → PostgreSQL 提交 → ARQ → worker lease → LangGraph → Message/Run 终态。Redis Stream 负责实时过程，PostgreSQL 负责断线后的结果回读。

完整机制见[AgentRun 生命周期](./agent-run.md)。

### 07｜可观测性

标题：

> 实时状态、子智能体活动和调用链同时可见

主图把四类观察结果分开：Redis Stream 与 SSE 投影实时事件，PostgreSQL checkpoint 支持 Agent state 恢复，Child AgentRun 使用自己的事件链，Langfuse 记录模型和工具 trace。PostgreSQL 保存 Request、Run、Message、checkpoint 和终态。

完整机制见[可观测性](./observability.md)和[Langfuse 集成](../advanced/langfuse-integration.md)。

### 08｜Memory

标题：

> 长期记忆由用户开启，写入受当前 Run 约束

读路径从 `enable_memory` 和 `agents/MEMORY.md` 进入主 Agent。模型策略要求用户明确提出记住或纠正后才调用写工具；服务端继续校验用户、thread、request、顶层 Run、worker 和 lease，再以串行、原子的方式更新同一文件。子智能体不加载这套 Memory middleware。

完整机制见[用户 Memory](./memory.md)。

### 09｜Skills

标题：

> 可复用方法按权限、按需要进入一次运行

主图从内置、共享和个人 Skill 进入运行时筛选。预加载 Skill 在首轮提供完整说明和依赖；渐进加载 Skill 在模型读取 `SKILL.md` 后开放对应工具和 MCP。共享 Skill 只读投影到运行环境，个人 Skill 保存在用户工作区。

完整机制见[Skills 运行机制](./skills.md)。

### 10｜子智能体

标题：

> 主 Agent 拆任务，子智能体共享文件、隔离状态

一个运行中的根 AgentRun 可以创建多个子 Run。每个子智能体使用独立 child thread 和 LangGraph checkpoint；父子运行共享 Project Workdir 和 runtime scope，结果回到父 Agent 汇总。当前链路只支持一层子智能体。

完整机制见[子智能体调度](./subagents.md)。

### 11｜Sandbox 与 Workspace

标题：

> 隔离执行，持久交付

Agent 在按用户和执行树创建的临时 Sandbox 中运行。Project Workdir 以读写方式挂载，共享 Skill 以只读方式挂载。Agent 写入的文件直接成为 UserWorkspace 中的持久字节；运行环境回收后，Viewer、Artifact 和下载接口继续读取同一份文件。

完整机制见[沙盒与文件系统](./sandbox.md)。

## 展示约束

- 固定画布 `1280 × 720`，每页就是一个 16:9 展示区域。
- 封面左上角显示 `ICON 语析 Yuxi`；其余页面右上角显示 `ICON Yuxi`。
- 纯白或近白背景，深蓝、青色为主，绿色表示持久结果，橙色只用于执行权、写入和恢复边界。
- 每页一个标题和一张主图；能删掉的解释不进入页面。
- 标题使用投屏可读的大字号；图内标签只保留节点名和必要边界。
- PPT 与机制文档互补：展示稿负责讲清关系，机制页负责保存状态、权限、失败和验证入口。

## 维护入口

- [Agent 运行时上下文](./agent-runtime.md)
- [知识库机制](./knowledge-base.md)
- [知识图谱机制](./knowledge-graph.md)
- [AgentRun 生命周期](./agent-run.md)
- [上下文工程](./context-engineering.md)
- [可观测性](./observability.md)
- [用户 Memory](./memory.md)
- [Skills 运行机制](./skills.md)
- [子智能体调度](./subagents.md)
- [沙盒与文件系统](./sandbox.md)
- [上下文压缩](./context-compression.md)

当前架构总边界以仓库根目录的 [ARCHITECTURE.md](https://github.com/xerrors/Yuxi/blob/main/ARCHITECTURE.md) 为准。
