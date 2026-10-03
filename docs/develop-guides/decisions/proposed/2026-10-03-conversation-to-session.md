# Conversation 统一为 Session 并保留 Thread 执行身份

状态：proposed
类型：architecture
Owner：backend/yuxi/modules/agents/models/threads.py

## 问题

面向贡献者与维护者，本提案明确业务会话、执行线程与消息展示组的命名边界。完成标准是前后端、数据库和当前契约使用一致的 Session 术语，同时维持 Thread、Turn、Run、权限、队列与最终输出归属。多 Thread 会话层级、调度策略调整和运行框架替换不属于命名统一的默认范围。

`Conversation` 保存整数主键、唯一 `thread_id`、用户与 APP 归属、Agent、Project、会话默认配置及生命周期。Input、Receipt、Turn 和 Run 使用 `conversation_thread_id` 标识执行线程；Message 与 Run 使用整数 `conversation_id` 关联业务行。公开事件已经使用字符串 `session_id = thread_id`，普通公开入口使用 `/threads`。前端部分 `conversations` 保存按 Run 分组的消息，而侧栏使用同名术语表达长期会话。

Schema 初始化入口只接受空库或精确当前版本；已有版本不会自动升级。重命名数据库表和列需要明确新库部署或旧数据迁移的取舍。

## 提案

### 实现方案

候选方案保持业务 Session 与 Thread 一对一。Session 拥有长期交互的业务记录；Thread 保持运行身份、checkpoint、FIFO、安全边界、SSE 订阅和子线程语义。创建 Thread 的现有用例继续创建一条业务 Session，输入事务提交后再发布 ARQ，Turn 的最终输出继续通过 `result_run_id` 读取。

| 当前含义 | 候选名称 | 事实 Owner |
| --- | --- | --- |
| 业务会话实体、仓储与表 | `Session`、`SessionRepository`、`sessions` | agents models/repositories |
| 整数业务行关联 | `session_record_id`，父子关联使用相同后缀 | Message、AgentRun、SubagentThread、workspace bindings |
| 字符串执行线程关联 | `thread_id` | Input、Receipt、Turn、Run 与复合约束 |
| 已有事件会话身份 | 保留字符串 `session_id = thread_id` | events、公开事件 adapter 与前端 stream handler |
| 会话界面与侧栏 | session 模块及 Session 组件命名 | frontend modules、pages 与导航 |
| 单次运行或连续执行展示组 | 按实际粒度使用 `runGroups`、`messageGroups` | messageProcessor、连续执行分组与 workspace |

数据库连接继续使用 `db`、`db_session` 和 `AsyncSession`；业务实体变量使用 `agent_session` 等明确名称，避免覆盖数据库事务变量。数据库主键类型、Thread ID 值与 checkpoint key 保持现有定义。

管理员 `/dashboard/conversations` 与统计、定时任务、子智能体返回值中的 conversation 字段需要与前端同步改名。Public Run 快照当前暴露 `conversation_thread_id`，改为 `thread_id` 属于公开契约变化。是否提供过渡期需要依据实际外部消费者确认，不默认引入双字段或双路由。

`outputs/conversation_history` 是真实摘要文件目录。若改为 `session_history`，需要同步摘要写入、产物拒绝规则与机制文档；旧 checkpoint 中保存的摘要文件路径和已有目录需要在旧数据迁移方案中单独处理。冻结的 archived 决策与历史发布说明保留历史词汇；当前源码、契约和机制说明统一新术语。

### 已确认与待确认边界

- 已确认数据库：使用独立新库，不实现旧库迁移；禁止修改运行中原槽位。
- 已确认关系：沿用一对一，只统一业务名称。
- 已确认身份：采用 `session_record_id` 区分整数主键，原 `conversation_thread_id` 改为 `thread_id`；前端展示组按实际语义命名。
- 待确认外部兼容：公开 Run 字段、管理员路由和相关集成是否允许一次切换。

## 替代方案

机械替换所有 conversation 为 session：变更方式简单，但整数 `session_id` 与已有字符串事件 `session_id` 会发生语义碰撞，消息展示组也会错误地被称为多个业务 Session。

新增 Session → 多 Thread 层级：需要重新定义作用域、Agent 配置、Project/workdir、队列、子线程、权限及删除归属，改变验收结果，应作为独立架构需求。

只修改前端词汇并保留数据库名称：避免 Schema 切换，但无法满足数据库与后端统一的目标。

引入 Agents SDK Session 作为另一套历史存储：需要重定义与 PostgreSQL Message、LangGraph checkpoint、摘要和恢复的关系，增加重复事实源；当前命名目标没有这一 consumer。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| 业务实体统一 Session，Thread 身份不变 | 整数外键冒充事件身份，或遗漏旧导入 | ORM、repository、公开序列化与前端模块 | 定向符号搜索、相关 unit、前端 lint/unit/build | 旧 conversation 字段或错误 ID 类型重新进入当前契约 | Inspected |
| Schema 与外键、索引、复合约束一致 | 新代码误接受旧版数据库，或新表缺失约束 | migrations、postgres schema 与 ORM | schema unit 与真实 PostgreSQL integration | 旧版本或未版本化旧表被作为新库接受 | Not run |
| Thread/Turn/Run 与输入队列语义保持 | SSE 身份不匹配、跨 Run 结果、FIFO 或恢复退化 | inputs、scheduler、events、subagents | 真实 HTTP integration、deterministic lifecycle E2E，并回读终态与产物 | 跨 APP、跨 Thread、跨 Turn 输入或结果归属错误 | Not run |
| 当前 UI 与调用契约一致 | dashboard、定时任务、搜索或子线程使用旧字段 | frontend APIs、workspace、dashboard 与 schedules | 前端 unit/build、真实浏览器与相关 HTTP integration | 恢复旧字段消费者后正确失败 | Not run |
| 摘要文件路径和禁止公开规则一致 | 改名绕过产物拒绝规则或旧引用不可读 | sandbox paths、summary 与 artifacts | summary/artifact unit、真实文件链路 E2E | 摘要目录文件被公开或摘要路径不存在 | Not run |

现有实现已通过源码核对；候选方案尚未实现，产品测试未执行。数据库验证以全新独立槽位为边界，文件路径随新环境统一。

## 风险

表/列/约束重命名需要增加 business Schema 版本，使旧运行进程明确拒绝新库。独立验证环境需要独立 Compose project、端口和持久数据槽位。若保留旧数据，除关系数据库外还要验证 checkpoint、JSON 快照、摘要路径和 workspace 产物，不能只依据 DDL 成功宣告迁移完成。

Session 生命周期状态当前包括 active/archived/subagent 等，而运行状态来自 Turn/Run。OpenAI Agents API 的 idle/in_progress/requires_action/failed 描述执行状态，直接替换业务生命周期会改变列表、授权及归档语义。更清晰的状态投影、会话级配置快照和按 Turn 展示可以另行提出，不默认并入改名。

参考 [Agents API Session 与 Turn](https://developers.openai.com/api/docs/guides/agents-api/sessions)、[AgentSession 类型](https://developers.openai.com/api/reference/python/resources/beta/subresources/agents/subresources/sessions/methods/create) 与 [Agents SDK 多轮状态策略](https://developers.openai.com/api/docs/guides/agents/running-agents)。Agents API Session 聚合配置、对话和长期工作，SDK Session 提供由应用存储的历史连续性；本提案借鉴职责划分，保留 Yuxi 的 PostgreSQL 与 LangGraph 事实 Owner。
