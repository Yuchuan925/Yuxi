# 用户自建 Agent 定时任务

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/scheduled_agent_service.py

## 问题

用户可以手动提交 AgentRun，但无法把一个固定的 Agent 与提示词配置为按时执行的长期任务。定时触发若只依赖 API 进程内存，重启、并发 worker 或多实例部署会导致任务丢失、重复或越权执行。

## 决策

提供用户级定时 Agent 任务资源。用户只能管理自己创建的任务；任务保存 Agent slug、提示词、cron 表达式、IANA 时区、启用状态和下一次触发时间。调度器在 PostgreSQL 事务中锁定到期任务、为本次触发生成唯一 request_id，并复用统一 `submit_run_command` 提交 AgentRun；提交事务成功后才发布 ARQ。每次触发写入独立执行记录，执行事实以 AgentRun 的最终状态为准。

调度扫描由 worker 负责，使用 PostgreSQL 行锁和条件更新保证多 worker 只领取一次；worker 启动和周期扫描都会处理已到期任务。删除任务只停止未来触发，不删除已有 AgentRun。暂停任务不再触发，并保留最近执行状态供用户查询。

## 替代方案

- API 进程内 `asyncio` 定时器：不具备跨进程恢复能力，重启会丢失任务，拒绝采用。
- 直接在 Redis/ARQ 中保存 cron：Redis 不是业务事实 Owner，无法安全完成权限查询、恢复和去重，拒绝采用。
- 复用通用 Tasker 执行 Agent：Tasker 是 API 进程内知识/评估任务机制，不拥有 AgentRun 的线程、消息、lease 和结果语义，拒绝采用。

## 后果

任务定义和触发记录成为 PostgreSQL 业务事实，worker 重启不会丢失已到期意图；代价是新增 business schema 版本和 croniter 依赖。任务删除会级联删除触发记录，不影响已经产生的 AgentRun。

## 验证

- `backend/test/unit/services/test_scheduled_agent_service.py` 覆盖时区换算、非法 cron、非法时区和 Agent 可见性负向案例。
- `backend/test/unit/services/test_storage_migration.py` 覆盖 business schema 升级与当前版本跳过 DDL。
- `pnpm run lint:check` 和 `pnpm run build` 已执行；真实 PostgreSQL worker/E2E 尚未执行。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 用户可创建、读取、更新、启停和删除自己的定时 Agent 任务 | 参数未校验、越权读取或修改他人任务 | scheduled-agent router/service/repository | API integration 测试，回读 PostgreSQL 行 | 另一用户读取、修改、删除同一任务必须返回 404 | Not run |
| 只有可见 Agent 才能绑定到定时任务 | 隐藏/已删除 Agent 被定时任务持有，触发时越权 | AgentRepository 与 scheduled-agent service | service unit + API integration | 恢复 Agent 可见性查询为任意 slug 时测试必须失败 | Not run |
| cron 与时区经过边界校验，下一次触发可确定计算 | 非法表达式、未知时区或 DST 造成 worker 崩溃/忙循环 | scheduled-agent service | unit 测试固定时钟和 cron 计算 | 非法 cron/时区请求返回 422，不能写入数据库 | Not run |
| 到期任务在多 worker 扫描中最多生成一次本次 request | 重复 AgentRun 或重复模型副作用 | scheduled task repository + scheduler worker | PostgreSQL integration 并发测试 | 去掉行锁后并发测试必须出现冲突并失败 | Not run |
| PostgreSQL 提交后才投递 AgentRun，重启可恢复待处理任务 | ARQ 先于事实提交，或 worker 重启漏掉到期任务 | scheduler service + run_submission_service + worker startup | integration/E2E，回读 Task/AgentRun/Message | 模拟发布失败后扫描可重试且不重复创建 request | Not run |
| 前端提供最小任务管理入口并展示最近执行结果 | API 接通但用户无法创建/停用/查看失败 | web API/components/views | lint、unit、build、真实页面 | API 失败时 UI 不显示成功状态 | Not run |

## 风险

- 调度执行依赖 worker 正常运行；readiness 必须继续把 worker 健康作为流量前置条件。
- AgentRun 仍可能因模型、工具或审批而失败/中断；定时任务只记录触发与结果，不假装成功。
- 采用 cron 的本地时区需要明确 DST 行为；本期只接受 IANA 时区并以 `croniter` 的下一次时间为准。
- 本期不提供通知渠道、重试策略、任务共享、并发重叠策略或历史结果删除 API。
