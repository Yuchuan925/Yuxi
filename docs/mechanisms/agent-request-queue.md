# Agent 输入队列与调度

本页解释 Public Thread 中的持久输入、FIFO、steer、控制输入和失败恢复。接口字段与示例见 [Agents Public API](../advanced/agents-public-api.md)。

## 范围与事实

队列按用户、APP、Agent 和 Thread 隔离。Thread 保存长期对话和独立的 `queue_paused`；Input 保存接收顺序、输入类型、消息成员、冻结的执行配置和消费归属；Receipt 保存事件接收与幂等事实；Turn 保存一轮工作的状态和当前 Run；Run 保存执行段、owner、lease 和结果。PostgreSQL 拥有这些业务事实，Redis 负责 ARQ 投递、短期事件和取消加速。

```text
Thread A：Turn U / Run U1 运行中；follow-up FIFO：[Input F1][Input F2]
          当前 Turn U 的 pending steer：[Message S1][Message S2]
Thread B：独立领取自己的 follow-up Input
```

接收事务依次校验完整作用域、锁定 Thread、重读 Receipt、验证 Turn 状态，并保存 Receipt、Input 与原始 Message。队列按数据库接收序号排序，不使用浏览器时间。相同幂等键和意图返回首次接收事实；同键改变事件类型、目标或内容返回 `409`。HTTP request ID 不参与生命周期。

## follow-up 与 steer

`follow_up` 是独立的持久 Input。线程没有活跃 Turn 且队列未暂停时，调度器领取 FIFO 队头，在同一事务创建 Turn 和首个 pending Run，并固定该 Input 的全部有序消息。排队期间没有预建 Turn；接收时解析的模型和审批配置不会在领取时按新默认值重新解释。

`steer` 必须指定当前活跃 Turn。多次提交追加到同一个尚未领取的 steer Input，保留每次 Receipt 和原始消息顺序。领取事务固定截止接收序号，后到消息进入下一批，不修改已消费输入。steer 继承本轮配置，不在同一批次改变模型或审批模式，也不会被转换成下一 Turn 的 follow-up。

当前模型调用和并行工具批次完成后，工具结果及 PostgreSQL checkpoint 先保存；`SteerMiddleware` 在下一次模型调用前，或无工具轮次的模型调用结束后，触发安全接管。旧 Run `yielded`，同一 Turn 创建下一 Run。没有 steer 的普通工具循环保持同一 Run。正在执行的外部工具不因 steer 被强制停止或撤销。

## 等待与控制

人工问题或审批使当前 Run `interrupted`、Turn `waiting`，等待点保存绑定的 Run、问题或工具调用 ID。等待期间已有 follow-up 保留，新普通消息被拒绝。客户端提交带 `turn_id`、`waitpoint_id` 和完整结构化回答或审批的恢复事件后，等待点只消费一次，并在同一 Turn 创建下一 Run。旧等待点不能恢复已经切换或取消的工作。

排队 Input 可用 `cancel_input` 取消，消息保留取消事实。取消 Turn 先设置 `cancelling`、暂停队列并取消该 Turn 未消费的 steer；worker 或等待清理 owner 收敛当前 Run、执行树和 checkpoint 后，Turn 才到 `cancelled`。后续 follow-up 保留。`continue` 只在当前 Turn 已结束时解除暂停并领取 FIFO 队头，不复活取消的 Turn。重复取消返回原目标；已切换 Run 时可用 `expected_run_id` 拒绝陈旧操作。

## 状态与故障恢复

| 对象 | 状态 | 业务含义 |
| --- | --- | --- |
| Input | `pending`、`consumed`、`cancelled` | 只表达投递，不跟随工作执行重复转态 |
| Turn | `running`、`waiting`、`cancelling`、`completed`、`failed`、`cancelled` | 一轮工作的当前段、等待点和明确结果 |
| Run | `pending`、`running`、`cancel_requested`、`completed`、`failed`、`cancelled`、`interrupted`、`yielded` | 一段执行及其 owner、lease 和结束原因 |

正常输出、Run 结束和 Turn 最终结果在 PostgreSQL 中按明确关联收敛；`output_message_id` 只指向同 Run 的 assistant Message，Turn `result_run_id` 只指向同 Turn 的顶层 Run。Run `yielded` 或 `interrupted` 不是 Turn 完成。失败和取消使后续队列暂停，用户显式继续后才能领取保留的 follow-up。

ARQ 投递只发生在 owning transaction 提交后。持久 `pending` Run 可由恢复扫描补投同一个 Run；已经失败的工作不会自动创建新业务 Run。Worker 取得 Run 时记录唯一 attempt token、heartbeat 和 lease；过期的 `running` 或 `cancel_requested` 会收敛为可观察的 `worker_lease_expired` 失败。该失败只说明执行 ownership 丢失，外部工具副作用仍需核对。

Thread SSE 把输入接收、Input 消费、Run 结束、Turn 结束和短期增量分开通知。断线时按 `Last-Event-ID` 续订，并重读 Input、Turn、Run 和历史的持久事实；Redis 的短期事件不是业务终态。

## 权限、源码与验证

Public 身份在 HTTP 边界变为包含 `uid`、`app_id` 的作用域；接收、查询及副作用用例在 Thread 和相关记录上再次校验。归档 Thread 拒绝活跃 Turn 或待处理 Input，并保留历史。Project 删除在同一事务检查这些条件后归档所属 Thread，不修改 Workdir 字节。

入口位于 [Public Agent router](https://github.com/xerrors/Yuxi/tree/main/backend/server/routers/public_v1/agents)，接收与 FIFO 分别由 [inputs](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/services/agents/inputs.py) 和 [scheduler](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/services/agents/scheduler.py) 拥有；[turns](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/services/agents/turns.py)、[runs](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/services/agents/runs.py) 与 [worker](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/services/run_worker.py) 拥有控制和执行收敛。真实 PostgreSQL/HTTP 与 worker 测试位于 `backend/test/integration`、`backend/test/e2e`；验收需回读 Input、Turn、Run、消息、checkpoint 和产物，不能只以 `202` 或 SSE 结束判定成功。
