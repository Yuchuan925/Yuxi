# Agent 输入队列与调度

本页解释 Public Thread 中的持久输入、优先队列、steer、控制输入和失败恢复。接口字段与示例见 [Agents Public API](../advanced/agents-public-api.md)。

## 范围与事实

队列按用户、APP、Agent 和 Thread 隔离。Thread 保存长期对话和独立的 `queue_paused`；Input 保存接收顺序、输入类型、消息成员、冻结的执行配置和消费归属；Receipt 保存事件接收、消费归属与幂等事实；Turn 保存一轮工作的状态和当前 Run；Run 保存执行段、owner、lease 和结果。PostgreSQL 拥有这些业务事实，Redis 负责 ARQ 投递、短期事件和取消加速。

```text
Thread A：Turn U / Run U1 运行中；队列：[Input (S1,S2)][Input F1][Input F2]
          (S1,S2) 是唯一 pending steer 批次，F1/F2 是 follow-up FIFO
Thread B：独立领取自己的优先队头
```

接收事务依次校验完整作用域、锁定 Thread、重读 Receipt、验证 Turn 状态，并保存 Receipt、Input 与原始 Message。队列按数据库接收序号排序，不使用浏览器时间。相同幂等键和意图返回同一回执与批次，并反映已固定的消费归属；同键改变事件类型、模式或内容返回 `409`。HTTP request ID 不参与生命周期。

## follow-up 与 steer

`follow_up` 每次提交形成独立的持久 Input，彼此按接收顺序 FIFO。`steer` 位于所有 follow-up 前面，每个 Thread 只有一个 pending steer Input；多次提交追加有序消息到该批次，各自保留 Receipt 和幂等键。领取事务封闭批次，后到 steer 创建下一批，不修改已消费输入。合并依据是否已领取，不额外等待聚合时间。

两类 pending Input 都不绑定 Turn。线程没有活跃 Turn 且队列未暂停时，调度器优先领取 steer，否则领取最早 follow-up，在同一事务创建 Turn 和首个 pending Run，并固定 Input、Receipt 与 Message 的消费归属。steer 在途期间旧 Turn 已结束时，仍以优先输入接收并调度。

普通消息的 `mode` 表达优先级和切入时机，发送方不指定目标 Turn，HTTP/SSE 接收回执不返回有效模式。未指定 mode 时，在 Thread 锁内按运行中 steer、空闲 follow-up 选择；幂等重试先读取原 Receipt，不重新入队。等待用户回答、审批或取消清理时拒绝普通消息；协作等待时默认为 follow-up，只进入 FIFO 队列。

每个新 Input 在接收时复制会话的完整可配置 Context，随消息原子应用单次模型与审批覆盖，领取时不重新读取 Agent 定义或系统默认值。会话更新只影响之后接收的输入，运行身份与授权在执行边界重新取得。steer 不指定模型或审批配置；空闲领取时使用批次创建时冻结的会话配置，同 Turn 安全接管时继承当前 Run 的配置。

当前模型调用和并行工具批次完成后，工具结果及 PostgreSQL checkpoint 先保存；`SteerMiddleware` 在下一次模型调用前，或无工具轮次的模型调用结束后，触发安全接管。旧 Run `yielded`，同一 Turn 创建下一 Run。首次模型调用前的让位允许 yielded Run 没有 AI 输出；真正 completed 的 Run 仍要求自身输出。没有 steer 的普通工具循环保持同一 Run。正在执行的外部工具不因 steer 被强制停止或撤销。

## 等待与控制

人工问题或审批使当前 Run `interrupted`、Turn `waiting`，等待点保存绑定的 Run、问题或工具调用 ID。等待期间已有 follow-up 保留，新普通消息被拒绝。客户端提交带 `turn_id`、`waitpoint_id` 和完整结构化回答或审批的恢复事件后，等待点只消费一次，并在同一 Turn 创建下一 Run。旧等待点不能恢复已经切换或取消的工作。

排队 Input 可用 `cancel_input` 取消，消息保留取消事实。若模型前已因该批次让位，但输出事务锁定 Thread 时发现批次已取消，原 Run 保持 running，由同一 owner 从保存的 checkpoint 继续，不重放原始输入或已完成的工具；模型后已完成的轮次不额外调用模型。取消 Turn 先设置 `cancelling` 并暂停队列，所有未消费的 steer 和 follow-up 保留；worker 或等待清理 owner 收敛当前 Run 和 checkpoint 后，Turn 才到 `cancelled`。取消一个合并 Input 会取消整个未消费批次。`continue` 只在当前 Turn 已结束时解除暂停并领取优先队头，不复活取消的 Turn。未指定目标的取消在 owning transaction 中固定当前 Turn，重复取消返回原目标；已切换 Run 时可用 `expected_run_id` 拒绝陈旧操作。

等待取消通过原图的状态 API 清理 checkpoint：先合入已完成节点的 pending writes 并清除等待任务，再为当前 AI 的未完成调用补齐取消 ToolMessage，最后清除状态改写触发的待办节点。已完成工具结果与业务增量保留；当前 AI 之后的工具结果拥有完成事实，历史重复 call ID 不影响本批次。清理不执行模型或工具，进程在任一持久改写后失联时可幂等重试；失败仍保留 `cancelling` 由恢复扫描处理。

## 状态与故障恢复

| 对象 | 状态 | 业务含义 |
| --- | --- | --- |
| Input | `pending`、`consumed`、`cancelled` | 只表达投递，不跟随工作执行重复转态 |
| Turn | `running`、`waiting`、`cancelling`、`completed`、`failed`、`cancelled` | 一轮工作的当前段、等待点和明确结果 |
| Run | `pending`、`running`、`cancel_requested`、`completed`、`failed`、`cancelled`、`interrupted`、`yielded` | 一段执行及其 owner、lease 和结束原因 |

正常输出、Run 结束和 Turn 最终结果在 PostgreSQL 中按明确关联收敛；`output_message_id` 只指向同 Run 的 assistant Message，Turn `result_run_id` 只指向同 Turn 的顶层 Run。Run `yielded` 或 `interrupted` 不是 Turn 完成。失败和取消使后续队列暂停，用户显式继续后才能领取保留的优先队头。

附件 Input 先持久接收来源，再准备 Workdir 文件并提交就绪事实。队头未就绪时不创建 Turn/Run，也不跳过它；pending steer 未就绪时不能安全接管。恢复扫描补全同一文件，取消只停止执行，仍完成已接收的文件提交；会话归档或 Project 软删除后准备恢复也继续，执行领取仍拒绝归档会话。准备失败与状态通过 Input/queue 的 `attachment_status`、`attachment_error` 回读，详细文件规则见[公开 API](../advanced/agents-public-api.md#图片与文件附件)。

ARQ 投递只发生在 owning transaction 提交后。持久 `pending` Run 可由恢复扫描补投同一个 Run；已经失败的工作不会自动创建新业务 Run。Worker 取得 Run 时记录唯一 attempt token、heartbeat 和 lease；过期的 `running` 或 `cancel_requested` 会收敛为可观察的 `worker_lease_expired` 失败。该失败只说明执行 ownership 丢失，外部工具副作用仍需核对。

Thread SSE 把输入接收、Input 消费、Run 结束、Turn 结束和短期增量分开通知。断线时按 `Last-Event-ID` 续订，并重读 Input、Turn、Run 和历史的持久事实；Redis 的短期事件不是业务终态。

## 权限、源码与验证

Public 身份在 HTTP 边界变为包含 `uid`、`app_id` 的作用域；接收、查询及副作用用例在 Thread 和相关记录上再次校验。归档 Thread 拒绝活跃 Turn 或待处理 Input，并保留历史。Project 删除在同一事务检查这些条件后归档所属 Thread，不修改 Workdir 字节。

入口位于 [Public Agent router](https://github.com/xerrors/Yuxi/tree/main/backend/yuxi/api/routers/public_v1/agents)，接收与优先调度分别由 [inputs](https://github.com/xerrors/Yuxi/blob/main/backend/yuxi/modules/agents/services/inputs.py) 和 [scheduler](https://github.com/xerrors/Yuxi/blob/main/backend/yuxi/modules/agents/services/scheduler.py) 拥有；[turns](https://github.com/xerrors/Yuxi/blob/main/backend/yuxi/modules/agents/services/turns.py)、[runs](https://github.com/xerrors/Yuxi/blob/main/backend/yuxi/modules/agents/services/runs.py) 与 [worker](https://github.com/xerrors/Yuxi/blob/main/backend/yuxi/workers/settings.py) 拥有控制和执行收敛。真实 PostgreSQL/HTTP 与 worker 测试位于 `backend/test/integration`、`backend/test/e2e`；验收需回读 Input、Turn、Run、消息、checkpoint 和产物，不能只以 `202` 或 SSE 结束判定成功。

## 独立委派

协作会话复用持久 Input、Receipt、优先领取、提交后投递和 lease 链路，各自 Turn 拥有 current_run_id、result_run_id 和等待点。普通取消只影响指定 Turn，父轮次完成、失败或取消均保留后代独立工作。新工作通过 submit_input 按 FIFO 接收，队列暂停时只排队；用户显式“停止全部”才取消整树在途 Turn 并阻止各队列消费。“继续整树队列”重新调度保留输入，已取消的 Turn 保持终态。协作等待保存目标、游标和截止时间，在同一 Turn 建立下一 Run，等待中的普通 follow-up 只排队，steer 明确拒绝。具体约束见[会话协作](../agents/session-cooperation.md)。
