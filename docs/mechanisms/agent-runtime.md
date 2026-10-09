# Agent 运行时上下文

本页解释 Yuxi 如何从持久化 Agent 配置构建一次运行，以及配置、权限、文件和 LangGraph state 在运行中分别负责什么。Agent 配置和扩展开发见[配置和开发智能体](../agents/agents-config.md)。

## 运行入口

Public Thread 接入把普通消息保存为 Input 与 Message，调度器领取时建立 Turn/Run；审批恢复在同一 Turn 建立下一 Run，协作会话从持久 Input 建立自己的 Thread、Turn、Run，并通过创建者关系关联父执行。worker 只执行已持久化的 Run：

```mermaid
flowchart LR
    Input["已持久化的 Run 与输入 Message"] --> Lease["worker 取得执行权"]
    Lease --> Prepare["准备 Context：配置、身份、工作区提示词与资源"]
    Prepare --> Manifest["从 Context 派生并提交 manifest"]
    Manifest --> Identity["复查线程归属与 Agent 可见性"]
    Identity --> Graph["get_graph 复用同一 Context"]
    Graph --> State["LangGraph state + PostgreSQL checkpoint"]
    State --> Result["消息、事件、文件和产物"]
```

worker 在取得 lease 并校验输入后，合并 Agent 可配置字段、Run 模型与审批模式、运行身份和 Workdir，准备一个 Context。准备期间持续续租，manifest 从准备结果派生并提交；固化失败时执行不开始。chat/resume 流和 BaseAgent 传递同一个 Context，构图只消费已准备的资源与 Skill 内容。当前 Agent 的专属 Skill 自动加入预加载，系统提示词继续生效；执行边界复查权限，保留本 Run 的专属根文本，中途新增绑定在下一 Run 加载。执行流复查 Agent 可见性，后端发生变化时显式失败。

执行流要求明确的 Thread、Turn 和 Run 身份，并检查 Thread 及其 Project 属于当前用户、APP 和 Agent 作用域。缺失身份、归属不一致或资源已归档时显式失败。Thread 创建和用户消息写入由接入用例负责；流中的 init 消息用于展示已经保存的输入。

运行入口读取用户工作区的 `agents/AGENTS.md` 和 `agents/USER.md`，把非空内容追加到系统提示词；文件不存在或不可读不会阻断运行，每个文件最多读取 64 KiB。`prepare_agent_runtime_context` 按当前用户权限过滤工具、知识库、MCP、Skills，并展开 Skill 依赖。准备结果仅属于该 Context 对象，独立执行入口对新 Context 显式准备；`get_graph(context)` 创建模型、工具和中间件。LangGraph state 保存消息、待办、产物、协作消息消费位置和用量，checkpoint 只使用 PostgreSQL。

API/worker 不信任浏览器内存中的完整配置。请求可以提供受限的单次覆盖值，例如模型或工具审批模式；配置快照也不能替代实时授权。

状态查询在 Session 与 Workdir 授权后直接读取 PostgreSQL checkpointer 的根 namespace，返回最近完整快照及同批 pending writes 中的中断，仅在最新 Run 为 interrupted 时展示审批。读取不创建 Context 或模型；业务 pending writes 的合并仍由执行图拥有。HTTP 状态查询返回待办、产物、协作树和用量；Turn SSE 提供执行增量与状态，前端通过 `/api/v1/agents/sessions/{session_id}/cooperation` 轮询完整协作摘要；该入口只读取持久关系，不读取 checkpoint 或结果正文。批量读取全部成员、最新 Turn、执行状态和待处理输入。同一 Thread 在页面与侧栏共享 session runtime 的历史、运行状态、流订阅和恢复请求；最后一个观察视图卸载才释放订阅，视图可见性与浏览器标签可见性共同约束已读和滚动。终态与 resync 请求读取新历史，不复用较早发出的快照。文件由 Workdir/Sandbox 边界持久化，前端文件面板通过文件系统接口读取当前 Workdir。

普通来源调用 `modules/agents/services/inputs.py` 接收用例：作用域校验后保存 Message、Input 与幂等 Receipt，空闲时按优先队头领取并创建 Turn/Run；事务提交后物化 Workdir 并投递 Run。Input 保存来源、优先级、消息成员和接收时冻结的模型/审批配置，消息正文由 Message 拥有，其余 Agent 配置在 worker 准备时读取。调度、引导和控制的完整契约见 [Agent 输入队列与调度](./agent-request-queue.md)。

断线后调用方从 Public Thread、Input、Turn、Run 与 History 查询读取明确的接收、消费和结果归属。排队 Input 尚无 Turn/Run；最终输出只属于 Turn 的 `result_run_id` 所指顶层 Run。HTTP `202`、SSE 中断和 Run `yielded` 都不是整轮成功证明。

## 配置和运行态的区别

| 数据 | 来源 | 生命周期 |
| --- | --- | --- |
| `config_json.context` | Agent 管理页面/管理 API | 跨运行保存的配置 |
| `runtime.context` | 配置 + 用户身份 + 运行身份 + 权限快照 | 当前 Run |
| LangGraph state | Graph 执行和中间件 | 当前 checkpoint thread |
| PostgreSQL Input/Receipt/Turn/Run/Message | 接收服务、调度器和 worker 提交 | 业务接收、执行与最终结果 |

`_skill_runtime_snapshot` 中的授权 Skill、依赖和预加载内容在 Context 准备时派生；中间件在运行期间维护 token 等状态。身份与运行标记由 worker 注入，持久 Agent 配置通过 `update_config` 仅装载 configurable 字段。接入和执行使用同一装载规则。运行事件的模型、审批与 Workdir 元数据从准备后的 Context 投影。

普通输入模型依次取显式输入值、Thread 保存值、Agent 配置和系统默认；接收时确定并保存在 Input 的配置快照中，Run 消费该快照。协作会话继承派发方实际模型和能力配置快照，只接收显式描述；模型历史、checkpoint 和附件上下文各自独立。

manifest v2 的配置摘要来自准备后的可配置字段，包含模型覆盖、schema 默认值和工作区提示词，排除用户、线程、worker 等运行身份。Skill 条目的来源、版本与哈希来自首次授权解析；预加载内容另保存实际读取字节的摘要，manifest 生成不再次查询 Skill。完整提示词和 Skill 正文不持久化到 manifest。MCP 工具发现、Memory 与文件动态读取发生在后续执行边界，manifest 不承诺冻结其实际可用性或字节。

## 资源权限

角色上限、共享范围、共享范围修改与治理由[资源权限机制](./resource-permissions.md)解释。模型返回和新工具执行复查最新调用者授权；账号或 Agent 失权形成失败终态，仅依赖失权则返回能力受限提示并继续可执行部分。

- Context 准备阶段将 `"all"` 展开为当前执行用户可用的资源列表，将固定列表与可用资源取交集，空列表保持为空；后续构图消费解析后的列表。字段默认值、界面操作与 API 写入规则见[智能体配置](../agents/agents-config.md)。
- MCP 选择控制直接加载的服务器；有效 Skill 激活后仍可按需加载其 MCP 依赖。
- Agent 的知识库选择只能缩小用户已经拥有的读取权限。Context 准备阶段只保存归一化后的知识库 ID；知识工具在每次执行时按当前用户权限与会话选择重新解析，撤权后的资源不能继续读取。权限查询失败通过工具错误边界暴露，具体机制见[知识库与检索](./knowledge-base.md#agent-如何看到知识库)。
- Skill 选择控制 Prompt 和工具激活；共享 Skill 的文件投影按用户授权生成，个人 Skill 位于 UserWorkspace。
资源快照只解决运行时“能看见哪些资源”。产生文件、知识库、MCP 或外部系统副作用的工具还要在执行处校验具体目标和当前身份。

## 文件和 Memory

当前 Project 的 `workdir_path` 决定 Agent 的默认工作目录。同树会话共享 Project Workdir 和根 Session 的真实沙盒；各自拥有 Thread checkpoint、Run lease、heartbeat 和清理责任。父轮次结束保留后代工作；环境由整树空闲回收释放，规则见[会话协作](../agents/session-cooperation.md)。

`agents/MEMORY.md` 只有在用户配置 `enable_memory=true`，且该文件存在并包含非空内容时，才由 Memory middleware 读取并提供受限的记忆工具。未保存配置的用户默认启用 Memory，已保存的关闭选择继续生效。用户可在账户设置中切换开关，或点击“查看 Memory”进入个人空间并打开该文件。它是用户主动维护的参考资料，不是系统指令。Memory 读取和更新有独立的用户、Run、worker 和文件大小校验。

Viewer、附件和 artifact API 通过持久化 Workspace/Workdir 读取文件，不连接 Agent execution runtime。沙盒虚拟路径、Viewer scope、对象 URL 和宿主机路径在各自边界中转换，不能互相替代。

## 用户定时 Agent

用户定时 Agent 由 `scheduled_agent_jobs` 保存 Project、Agent、提示词、审批模式和计划，worker 在 PostgreSQL 行锁下为到期任务创建唯一 occurrence。每次 occurrence 通过统一接收用例创建绑定原 Project 的独立 Thread、首个 Input 和 Turn/Run；触发记录保存输入关联，排队与执行状态分别从 Input、Turn 和 Run 读取。明确的领域错误终结 occurrence，未知瞬时错误在下一轮用确定的幂等键重查已接收输入；单条失败不阻断同批任务。Redis/ARQ 只负责唤醒。

任务 API 只返回当前用户拥有且未删除的任务，并在创建、更新和触发时重新校验 Project 归属与 Agent 可见性。停用或任务软删除只阻止未来触发；账号软删除在同一事务删除任务及 occurrence。任务支持 Run now、5 段 cron 和 IANA 时区，数据库保存 UTC 下一次触发时间；错过多个周期只合并一次，已有非终态执行时记录 skipped。

## 恢复和失败

审批或用户问题中断时，系统把等待点绑定在 Turn 的当前 interrupted Run 和 PostgreSQL checkpoint。结构化回答或审批只消费该等待点一次，在同一 Turn 创建恢复 Run；worker 再为该 Run 准备 Context 并固化 manifest。

新的普通输入按接入时的规则解析模型和审批模式。根会话的其余 Agent 配置与基础工作区提示词在 worker 准备 Context 时读取；关联会话使用创建时配置快照，并在当前 Run 重新准备工作区提示词与授权资源，动态文件与权限仍在各自读取或执行边界生效；输出、事件和消息绑定明确的 `input_id`、`turn_id` 与 `run_id`。

准备期间收到取消时，worker 使用已提交的取消状态完成取消收尾；manifest 失败不能把取消请求留待 lease 超时。manifest 使用 write-once 指纹，已有旧版 manifest 的 Run 重试若与新准备结果不一致会显式失败；历史 manifest 保留原记录。

## 源码和验证入口

- [Context 与资源归一化](https://github.com/xerrors/Yuxi/blob/main/backend/yuxi/modules/agents/runtime/context.py)
- [BaseAgent](https://github.com/xerrors/Yuxi/blob/main/backend/yuxi/modules/agents/runtime/base.py)
- [Chatbot graph](https://github.com/xerrors/Yuxi/blob/main/backend/yuxi/modules/agents/runtime/agent_backends/chatbot/graph.py)
- [会话协作 middleware](https://github.com/xerrors/Yuxi/blob/main/backend/yuxi/modules/agents/runtime/middlewares/cooperation.py)
- [Memory middleware](https://github.com/xerrors/Yuxi/blob/main/backend/yuxi/modules/agents/runtime/middlewares/memory.py)
- [运行时上下文 unit](https://github.com/xerrors/Yuxi/tree/main/backend/test/unit/agents)
- [Agent 主链路 E2E](https://github.com/xerrors/Yuxi/tree/main/backend/test/e2e)

修改配置、权限、模型可见输入、文件作用域或恢复语义时，同时验证对应的 unit、integration 或 E2E，并回读最终 state、消息、文件或协议结果。


## 线程阅读数据

`GET /api/v1/agents/sessions/{session_id}/history` 返回当前作用域可见的 `thread`、`runs` 和 `items`。`thread` 来自持久 Thread/Turn/队列快照；`runs` 是轻量执行段归属，包含 `run_id`、`turn_id`、`run_type`、父 Run 和状态；`items` 保留用户输入、取消的排队消息和已经进入用户输出链路的正文、工具参数及结果。公开 item 身份与 output_index 保存于 Message 元数据，Turn 锁分配递增索引；实时与历史复用同一 serializer。完整 Model/Tool 审计由独立管理员接口读取。

History 读取不改变已读标记。页面加载后以 `POST /api/v1/agents/sessions/{session_id}/viewed` 显式标记已查看；未知、跨 APP 或跨用户的 Thread 返回 404。多个查询遵循数据库事务隔离，运行中变化通过 Thread SSE 与持久快照重读收敛。

接口契约由 `modules/agents/services/messages.py` 装配、`PublicItemRepository` 查询和前端 History consumer 共同拥有；真实 HTTP 测试回读公开 item、Run 归属和 PostgreSQL 已读标记。公开协议取舍见[原生事件与 Agents API 决策](../develop-guides/decisions/implemented/2026-09-30-langgraph-agents-events.md)。

## 原生流与公开协议

BaseAgent 保留 LangGraph v3 ProtocolEvent 的通道、顺序、内容块和 namespace，独立 GraphExecutionResult 返回 checkpoint。同一 Run 因取消让位批次而续图时，执行层在审计与公开投影前将各图段 seq 衔接为 Run 内递增序号，不修改源事件对象、内容和 namespace。Model/Tool 审计消费原生结构；agents service 的唯一 OpenAIEventAdapter 消费相同原生事件，Redis 逐条保存公开事件。批量写入仅优化传输，不合并事件。业务结果通过独立 RunExecutionResult 与 PostgreSQL 收敛，不从公开流反推。

完成、等待、失败和取消由已提交的 Run/Turn 事实通知。原始推理和业务状态使用明确的 yuxi 扩展；官方 item 与 content 事件保持官方结构。详情见 [Agents Public API](../advanced/agents-public-api.md)。

子 Thread 在委派事务中保存实际模型和审批默认值，各 Input 接收时仍冻结本次配置。直接向子 Thread 提交新的 follow-up 会创建独立的用户 Turn，不继承旧 Turn 的 `created_by_run_id`。Thread 委派关系继续证明其用户、APP 和共享 Project 的授权；本轮 Run 的委派身份只用于本轮父子取消与 tracing，不以 Thread 历史委派推断取消范围。附件绑定在输入事务中写入公开用户 item 快照，回执替换与刷新使用同一已授权记录。
