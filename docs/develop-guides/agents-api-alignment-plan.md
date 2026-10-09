# Agents Public API 对齐计划

本计划供 API、运行时与客户端维护者安排对齐工作。目标是比较 OpenAI Agents API 与 Yuxi 的机制，采用更容易正确使用、失败时可恢复且维护成本合理的设计；同等条件优先采用官方契约。完整比较、选择理由和行为验收由[机制对齐方案](decisions/implemented/2026-10-09-agents-api-mechanism-alignment.md)拥有。

范围包含公开协议、必要的配置和查询机制，以及 Web、CLI、Agent Demo 的同步消费。直接在 main 修改，不建立分支或工作树，不迁移历史数据、不保留旧协议兼容层、不处理部署平台兼容。内部 Thread、Input、Turn、Run 继续拥有持久状态与执行。三端直接按新 API 调整字段和状态处理，禁止把响应重新包装成旧 Thread 结构来维持原解析。官方 SDK 自动结束调用或收集结果不属于前置假设。

官方契约核对日期：2026-10-09。第 1–3 步已完成，实际边界和证据见[公开资源决定](decisions/implemented/2026-10-09-agents-resource-contract.md)、[配置及取消决定](decisions/implemented/2026-10-09-agents-session-lifecycle.md)与[附件提交决定](decisions/implemented/2026-10-09-agents-draft-attachments.md)。第 4 步已完成，证据见[查询恢复决定](decisions/implemented/2026-10-09-agents-query-recovery.md)。第 5 步完成三端整体验收与独立审查，证据见下方交付验证。

## 调整清单与实施顺序

按实现依赖将 P0 收敛为以下五步。机制与取舍由方案解释，这里只维护交付顺序、完成边界和验证。五步完成后，后续增强按 P1/P2 安排；每步包含相关后端、三端调用方、测试和文档，不把客户端适配与验证全部推迟到最后。

| 完成 | 顺序 | 要完成的工作 | 完成边界与验证 |
| --- | --- | --- | --- |
| [x] | 1 | **统一公开接口契约。** 统一 Session、Turn、Items 和回执的结构、ID、状态、模型字段及扩展边界；明确创建、POST 更新、消息提交、附件引用与分页契约，落实基础资源响应和 HTTP/SSE 同一核心投影。 | 基础资源真实 HTTP、worker/SSE、Web 页面与 CLI/Demo 实际消费通过；三端删除还原旧 Thread 的包装与双形状 fallback。配置生效、draft 文件引用/提交和完整分页查询仍由后续步骤验收，实际证据见公开资源决定。 |
| [x] | 2 | **完成消息接收与执行生命周期。** 落实会话配置快照、更新生效时机和 Input 配置冻结；闭合幂等接收、提交后派发、默认 steer、显式 FIFO 与等待点行为，同时修复取消 checkpoint 清理。 | 真实数据库、HTTP、worker 与 checkpoint 通过；完整 Context 和有效模型固定，更新与单次覆盖作用于正确输入，权限实时校验，取消保留完成结果并可重试，证据见配置及取消决定。三端继续直接读取同一 API 字段；接收响应丢失与完整断线定位由第 4 步验收。 |
| [x] | 3 | **完成图片和附件提交链路。** 上传为 draft，发送时关联 Input 并写入 Workdir，删除独立 confirm；OCR 预解析和图片辅助处理迁入私有 API，保留 Agent 解析工具，三端同步调整上传与发送。 | 验证首条消息带附件、发送前隔离、发送后共享、文件就绪后派发、失败恢复和幂等重试；确认取消排队不自动删文件，模型上下文不提前纳入后续排队附件，私有接口授权及图片 MIME 正确。真实 PG/HTTP/worker、Web 与 Demo 浏览器及三端 gate 已通过，外部模型探针 2 skipped，证据见附件提交决定。 |
| [x] | 4 | **完成历史查询与断线恢复。** 补齐 Turn 列表及 Session/Turn/Items 的有界分页，消除全量截页和逐项完整快照查询；闭合 SSE 重连、回执定位与持久结果读取。 | 真实 PostgreSQL 验证查询范围、稳定游标与 APP 隔离；断开提交响应或 SSE 后，三端仍能定位同一次提交和目标结果，不能把断流或相邻 Turn 完成当成本次成功。 |
| [x] | 5 | **完成整体验收与接入文档。** 串联三端的创建、配置、发送、附件、等待、取消、恢复和结果读取；完成 OpenAPI 与公开示例的最终核对，清理遗漏旧入口和旧解析。 | 执行真实 HTTP/worker/SSE、浏览器与 CLI/Demo 链路、独立 Review 和文档 gate；前四步各自通过后，再以跨步骤场景验收整次交付，不假定官方 SDK 会结束调用并取得结果。 |

第 1 步先稳定后续实现与客户端共用的契约；第 2 步确定输入接收和执行边界；第 3 步在该边界上接入跨存储的附件提交；第 4 步补全查询和恢复；第 5 步验证整条用户链路。详细图片、附件与 OCR 规则见[方案对应章节](decisions/implemented/2026-10-09-agents-api-mechanism-alignment.md#图片附件与-ocr)。每步只在实现与证据闭合后勾选，实施期间保持同一步的生产调用方同步可用。

本轮 P0 总估算仍为约 15–23 人日，已包含图片、附件与 OCR 的 4–6 人日及取消专项的 1–2 人日；重新分组不增加工作范围，已完成核心子集不重复计入。估算需按配置快照范围、跨存储恢复及真实链路结果校准。

后续兼容增强保持 P1：统一公开错误结构与稳定错误码、比较 required_actions 与人工问答/审批、指定版本的官方 SDK 冒烟；本轮具体接口仍需验证各自错误与拒绝行为。Agent 管理、Artifacts、外部工具结果和完整环境配置保持 P2，按真实消费者另行安排。这些后续事项不阻塞上述五步。

## 事实归一与简化

在上述接口对齐基础上继续删除重复事实与编排。实现选择、维护成本和验收由[事实归一决定](decisions/implemented/2026-10-09-agents-owner-simplification.md)拥有。

| 完成 | 顺序 | 实现任务 | 验证 |
| --- | --- | --- | --- |
| [x] | 1 | 附件表拥有文件、作用域、Input/Receipt、准备状态与位置；删除 manifest、多处 JSON 附件副本及文件 advisory lock。 | PG 约束、并发绑定、跨用户/APP 拒绝、消息关系展示。 |
| [x] | 2 | 接收只绑定，统一准备文件，调度只消费 ready；首发与恢复共用编排，回执与重放只读；就绪提交后删除临时来源。 | 失败/崩溃恢复、回执无副作用、Workdir 字节与临时来源删除、就绪前禁止执行。 |
| [x] | 3 | 批量事实直接生成唯一 Session 资源；搜索片段也批量查询，删除旧 Thread 投影与写操作中的弃用响应。 | 详情/列表/搜索/更新一致、有界查询、权限隔离、未读与状态独立。 |
| [x] | 4 | 每个时间点只有完整配置快照；删除同层 model/审批副本与逐层 fallback，steer/resume 继承当前 Run。 | 输入配置冻结、默认值更新时序、缺失快照拒绝、真实 worker 执行。 |
| [x] | 5 | 公开状态直接表示调用方动作，协作等待为执行中；三端按统一字段消费，贯通文件、等待、结果恢复与文档。 | 三端 unit、浏览器、CLI、HTTP/SSE/worker、独立审阅与文档 gate。 |

本轮五步在 main 完成。回归数据：后端 2573 passed / 55 skipped，Web 514 passed；真实 PG 22 passed、HTTP 10 passed、worker 9 passed；CLI 15 passed，Demo unit 13 passed / 浏览器 18 passed。真实三端均验证附件与回执/SSE 故障恢复，Web 另验证人工问答及审批，新 Run 和 result_run_id 正确。完整命令、独立审阅与未验证范围见事实归一决定；真实链路使用临时隔离环境，原共享 PostgreSQL 的恢复故障不计为通过。

## 已完成的核心子集

以下为上一阶段 P0 的原验收范围与完成记录，当前实现和测试证据见[Session 协议决定](decisions/implemented/2026-10-09-agents-session-protocol.md)。机制对齐方案将在此基础上替换部分协议和客户端消费方式。

| 完成 | 优先级 | 调整 | 工作量估计 | 验证方式 |
| --- | --- | --- | --- | --- |
| [x] | P0 | HTTP path 改为 sessions，参数改为 session_id，同步调用方 | 0.5–1 天 | 路由与真实 HTTP；旧路径拒绝 |
| [x] | P0 | Session 响应核心字段对齐，业务快照放入 yuxi | 1–2 天 | 响应模型与数据库回读；越权访问拒绝与 APP 隔离 |
| [x] | P0 | 创建 input 支持字符串和数组，配置字段规范化 | 1–2 天 | 两种输入持久化等价；非法字段拒绝 |
| [x] | P0 | 普通消息、取消与幂等接收契约对齐 | 1–2 天 | 重试不重复写入；冲突与取消 E2E |
| [x] | P0 | SSE 类型、报文与创建流响应文档对齐 | 2–3 天 | 真实 SSE、恢复游标、整轮终态回读 |
| [x] | P0 | Session Items 持久历史及游标分页 | 1–2 天 | 双向分页、跨 Session cursor 拒绝、公开白名单 |
| [x] | P0 | 核心 OpenAPI 响应模型、字段、示例与接入文档 | 1–2 天 | OpenAPI 契约检查、相对链接与 docs build |

该阶段原估算为 8–12 人日，不代表实际耗时。状态仅对应当时验收范围，未验证项见对应决策记录。

## 验证发现的后续问题

并行提问与协作等待的取消清理已在第 2 步收敛：原图状态 API 合入已完成节点的 pending writes，补齐当前 AI 的取消结果，再清除任务；真实 checkpoint、worker 与提交后崩溃重试验证通过，详见[配置及取消决定](decisions/implemented/2026-10-09-agents-session-lifecycle.md)。附件专项已在第 3 步收敛，详见[附件提交决定](decisions/implemented/2026-10-09-agents-draft-attachments.md)；完整查询恢复由第 4 步收敛，整体验收按第 5 步推进。

## 原接口对齐交付验证

以下记录对应事实归一前的验收；当前简化证据由事实归一决定拥有。

2026-10-09 在 main 完成五步，保持真实 PostgreSQL、Redis、MinIO、API、worker 与三端运行链路。核心对齐收益为统一资源、明确结果归属和断线可恢复；不以 SDK 自动结束调用作为成功条件。完整查询、回执和三端恢复由[查询恢复决定](decisions/implemented/2026-10-09-agents-query-recovery.md)拥有，原附件扫描方案由[有界批次决定](decisions/archived/2026-10-09-draft-cleanup-batches.md)记录；当前简化用附件行状态替换对象扫描，最终证据见事实归一决定。

| 验证层级 | 实际结果与可观察事实 |
| --- | --- |
| 后端全量 unit | 2571 passed、55 skipped；结果包含独立审查后的有界清理修复。 |
| 真实 PostgreSQL、HTTP 与 OpenAPI | 查询/分页/回执 17 passed；清理及受影响逻辑 51 passed，首批已提交来源保留、后批过期 draft 被删除，缓存游标丢失可重复扫描。 |
| 真实 worker / SSE | 生命周期、SSE、配置及附件共 21 项；两处旧 running 断言改为公开 in_progress 后，相应 3 项重跑通过。FIFO 回执、Redis 事件到期后的指定 Turn 恢复及相邻 Turn 游标拒绝均回读持久事实。 |
| Web | 最终全量 517 passed；审查修复专项 27 passed；lint/build 通过。真实浏览器上传 draft、消息响应丢失后 1 POST/1 receipt 恢复、SSE 连接失败、刷新后仍展示目标结果。 |
| CLI | 15 passed，typecheck、打包检查通过；真实客户端验证附件、响应丢失与 SSE EOF 恢复、两个 Turn 的结果归属和双页查询。 |
| Demo | unit 13 passed，浏览器 18 passed，lint/build 通过；真实浏览器验证两轮持久结果、回执恢复、SSE 故障、用户与 APP 隔离，附件回读 Input 关联和原件字节。 |
| 工程契约与文档 | engineering contracts、64 项 gate 单测、docs build 和补丁空白检查通过。 |

全新、无开发上下文的 Reviewer 从第一性原理审阅相关完整 diff、源码、需求和证据，结论为值得交付；独立执行创建重试测试 2 passed。发现的完整意图冻结、上传全量清理和 Demo 附件证据问题已修复。实际简化删除旧 offset/pinned 列表、无人使用的 MIME 推断、CLI 旧正文辅助模块及 Web 创建转发层。必要的 Input/Receipt、Turn/Run、明确 result_run_id 和附件就绪边界保留，不引入通用恢复框架。

未验证范围：官方 SDK 完整调用属于 P1；外部视觉/非视觉模型探针 2 skipped，未把确定性模型验收写成外部模型能力通过。三端未确认请求与 SSE 游标只在当前客户端生命周期内保存；重载后依据持久 Session/Input/Turn 恢复，不宣称保存未确认草稿的跨重载恢复。当前 MinIO 清理由上传逐批触发，不承诺无人上传时自动物理删除。
