# 会话协作

本页说明模型的协作工具及用户的会话操作。Agent 保存可复用配置；Session 保存独立上下文和输入队列。创建关系组成协作树，树内会话共享同一 Project 工作目录与实际沙盒。

## 身份与创建

`create_session(name, description)` 创建当前 Session 的直属子会话并提交初始输入，返回 `session_id`、自动生成的树内路径和输入回执。`name` 只填写 1–64 位字母、数字、下划线或连字符组成的局部名称；同父节点下唯一，创建后固定。父节点由实际调用的 Run 所属 Session 决定，调用者不能指定父节点或完整路径。

根会话 `/root` 传入 `name="aaa"`，系统生成 `/root/aaa`；只有 `/root/aaa` 传入 `name="bbb"`，才会生成 `/root/aaa/bbb`。根传入 `name="aaa/bbb"` 或 `name="/root/aaa/bbb"` 会被拒绝。路径用于在当前树内寻址已有成员；持久身份与授权使用 `session_id`。

新会话继承派发方实际模型、配置和授权约束。`description` 提供工作目标、必要资料和交付要求；会话独立保存历史、checkpoint 和附件上下文。共享目录文件可通过统一文件权限边界读取。角色分工写入描述，配置入口见[配置智能体](./agents-config.md)。

## 工具与用户操作

| 工具 | 行为 |
| --- | --- |
| `create_session` | 创建关联会话并提交首个输入，立即返回身份与回执 |
| `submit_input` | 按 FIFO 提交新工作；等待用户回答或审批时返回拒绝 |
| `send_message` | 持久投递信息，运行中在模型边界读取，空闲时留待下次运行 |
| `list_sessions` | 一次读取整树成员、当前 Turn/Run、队列和等待原因，不包含结果正文 |
| `get_result` | 用 `input_id` 或 `turn_id` 精确读取任务状态、最终输出和错误；两个参数只能选择一个 |
| `wait_inputs` | 等待 1–100 个已提交 `input_id` 全部结束；返回精确结果或带当前结果的超时响应，最长等待 1800 秒 |
| `wait_sessions` | 从 `after_cursor` 等待指定其他成员任一更新，返回新游标或超时结果；空列表与当前会话自身明确拒绝 |
| `cancel_turn` | 按 Session 与 Turn 身份取消指定轮次，保留历史和已有文件 |

`create_session` 和 `submit_input` 返回的 `input_id` 在排队时就已固定；消费后仍映射到同一个 Turn。等待任务交付使用 `wait_inputs`，等待成员消息使用 `wait_sessions`。排队输入取消也会结束任务等待。成员开始下一轮后，`get_result` 仍返回指定任务的结果；会话摘要只表达当前状态，不携带正文。

树内任何成员都可向兄弟、祖先及后代投递信息、追加输入或取消指定 Turn；后端按相同用户、APP 与协作树校验授权。完成、失败、取消和等待用户产生持久更新。协作文本作为模型参考，不替代用户授权或审批。

用户在状态面板选择会话即可进入其普通消息页，直接输入、steer、回答问题或审批。直接干预成员会话会通知父会话。父轮次结束或失败保留后代工作；“停止全部”明确停止整树在途工作和队列消费，“继续整树队列”重新消费待处理输入，已取消轮次保留终态。

## 执行、等待与环境

根与所有后代共用四个执行名额，由 PostgreSQL 树锁原子领取。排队、等待用户以及等待协作更新不占名额；协作等待保存 Input 目标或成员与事件游标，以及截止时间后退出当前 worker 执行，恢复时在同一 Turn 创建下一 Run 并重新领取名额。

消息、回执和终态事实先提交，再投递执行。同一 Turn 内跨 Run 重放的工具调用复用回执，新 Turn 的相同 tool call ID 可提交新工作；消息本身不创建 Turn，不自动唤醒空闲成员。最终输出来自目标 Turn 的 `result_run_id` 所指向 Run，运行失败与 worker 失联都有持久终态通知。

等待恢复按记录隔离失败，坏记录保留错误日志并继续处理其他等待。worker 各恢复阶段独立处理错误；失败阶段不阻断后续派发，完整恢复成功后才续报健康。空闲沙盒回收使用独立循环与专用生命周期锁；释放期间新执行暂缓领取，协作消息仍可提交。

同树文件、已安装软件、后台进程与端口可共享。单个 Run 结束只解除其执行和清理责任；整树没有未结束的执行后，沙盒空闲五分钟释放，工作目录保留。下次使用重建沙盒，不恢复临时环境与后台进程。取消等待已开始的工具结束，已有副作用保留。共享文件并行修改由工作分工协调。

## 源码定位与验证

[协作服务](https://github.com/xerrors/Yuxi/blob/main/backend/yuxi/modules/agents/services/cooperation.py)拥有创建、输入、通知与等待恢复，[协作 repository](https://github.com/xerrors/Yuxi/blob/main/backend/yuxi/modules/agents/repositories/cooperation.py)拥有树范围与并发领取，[leases](https://github.com/xerrors/Yuxi/blob/main/backend/yuxi/modules/agents/services/leases.py)拥有执行续租与空闲释放。持久结构由 Session、CooperationEvent 和 CooperationRuntime 的 Schema 约束。

真实 PostgreSQL 测试覆盖幂等、递归并发、跨树拒绝、等待恢复及取消竞态；[确定性 E2E](https://github.com/xerrors/Yuxi/blob/main/backend/test/e2e/test_session_cooperation_e2e.py)回读公开 Turn、会话树与共享目录文件。取舍见[统一 Session 决策](../develop-guides/decisions/implemented/2026-10-03-session-cooperation.md)。
