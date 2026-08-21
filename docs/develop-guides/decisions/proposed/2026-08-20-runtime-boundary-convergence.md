# 用户文件与 AgentRun 执行边界收敛

状态：proposed
类型：architecture
Owner：backend/package/yuxi/repositories/agent_run_repository.py

## 问题

当前文件与 Run 生命周期的关键规则分散在多个调用点，部分路径仍采用“先检查、后按路径名操作”或由上层拼装状态，因而无法在不可信 Sandbox 可并发修改文件、多个 PostgreSQL 事务可并发转换 Run、以及滚动升级保留旧 Kubernetes Pod 的真实条件下闭合安全与正确性。

已确认的失败面包括：新用户的 Skill projection 在首次 Sandbox 创建时尚未物化；取消执行树与终态转换以相反顺序锁定 root/descendants；worker 只检查 `runtime_scope_id` 非空，不能拒绝错误的跨线程或跨 Run scope；Kubernetes inventory 的新标签 selector 看不到升级前创建的旧 Pod。个人 Skill 位于用户自己的 UserWorkspace，本次不把同一用户进程内极低概率的路径替换竞态作为独立攻击边界。

这些不是要求所有代码都防御任意宿主机攻击。信任模型是：Sandbox 可合法并发写入自己的 UserWorkspace 和个人 Skill 源目录；API、worker 和 provisioner 必须在真正产生副作用的 Owner 处保证用户隔离、Run 归属和升级期资源枚举完整。提案不把 decision record 变成运行时事实源，也不通过继续增加独立的调用点检查来维持这些规则。

## 提案

### 1. Workspace 文件边界由聚焦决策拥有

UserWorkspace、Workdir、Viewer、Attachment、artifact、preview 与 Mention 文件边界由[收敛 Workspace 路径与文件访问 Owner](../implemented/2026-08-21-workspace-owner-convergence.md)独立拥有。本记录不再定义对应类位置、文件原语、Thread 兼容接口或缓存取舍，只保留 Run storage、execution tree、runtime scope、Kubernetes inventory 与 Skill 状态边界。

### 2. 在真实执行边界集中准备 Run 存储

Workdir 继续由现有派发边界在 ARQ 发布前物化，不在 worker 重复创建。chat/resume 共用的 Sandbox bootstrap 只在调用 provisioner 前调用 Skill Owner 物化 uid projection root；Subagent 复用同一执行路径。

不通过扩展每个 `enqueue_agent_run` 调用点的参数来实现该规则，也不让 provisioner 创建业务目录。前者容易遗漏 direct run、resume、recovery 或 subagent producer；后者会把派生存储的 Owner 移到部署控制面。Provisioner 继续只验证和挂载，并分别报告 UserWorkspace、Skill projection、Workdir 的失败名称。CI 的冷启动用例必须从无 uid projection、无 Workdir 的真实 HTTP 请求进入 worker，并在失败时采集 provisioner 日志。

### 3. 在 repository 收敛 execution-tree 转换和 Run 形状

新增一个窄的 repository execution-tree cancel transition：先锁 root，再按确定顺序锁定 descendants，在同一事务内完成允许的状态变化并返回需要发送取消信号的 Run ID。service 只在提交后发布 Redis 信号。终态、lease 与输出路径继续使用现有 repository 方法；本提案不引入新的通用状态机。

Run 约束分三层闭合：

- 同一行、非终态约束由 PostgreSQL `CHECK ... NOT VALID` 保护新写入，同时允许既有历史终态数据保留。Chat 的 creator/relation 为空且 scope 等于 conversation thread；Resume 的 creator 非空、relation 为空且 scope 等于 conversation thread；Subagent 的 creator/relation 非空。
- Subagent service 在创建时校验 creator、relation、uid、父子 Conversation 与 runtime scope 的跨行一致性；不再在 repository 复制同一行规则。
- worker 只回读并验证数据库约束无法表达的跨行关系：Subagent 必须继承 creator scope，relation 的 parent/child Conversation 必须分别对应 creator/run。

历史数据审计显示，现有 Chat 都使用自己的 conversation thread scope；绝大多数 Resume 有 creator 且使用自己的 thread scope；存在少量旧 Resume 缺 creator，以及一批已终态、缺 creator/relation 的旧 Subagent。因而不能直接添加已验证的严格约束，也不能把 Resume 错误地要求为 `created_by_run_id IS NULL`。跨行一致性不使用数据库 trigger：它仍由创建 repository/service 和 worker 边界共同拥有。

### 4. 保留升级期 Kubernetes inventory

Kubernetes inventory 用稳定的 `app=yuxi-sandbox` 枚举候选 Pod，再在代码内要求合法 `sandbox-id`；不能用新增的 `managed-by` 标签排除旧版本 Pod。删除仍保留 resource generation/precondition。Quiesce proof 只有在新旧标签 Pod 都被枚举并终止后才能签发。

### 5. 范围与交付顺序

本提案按可独立验证的三个变更集实现：Run 存储冷启动；execution-tree/runtime scope；Kubernetes legacy inventory。每个变更集先恢复对应缺陷作为负向测试，再修改 Owner。全部完成后才把本记录改写并移动到 `implemented/`。

本提案明确不包含前端轮询、旧环境变量清理、Conversation 创建失败补偿、完整 Run 状态机重写，以及真实 Kubernetes 集群演练。Workspace 与 mention 归属上述聚焦决策。CI 对 `docker/**` 的路径触发和失败时 provisioner 日志属于本提案证据链，不作为无关清理后移。

### 6. Skill 状态与事务只保留单一 Owner

`SkillRepository` 只执行查询、写入和 `flush()`；事务提交属于 Skill Service。文件副作用 Service 接收 `operator` 并完成最终授权，Router 只负责 HTTP 参数与错误映射。共享 Skill 安装统一使用一个拒绝 symlink 的 staging snapshot primitive。

个人 Skill 直接扫描 UserWorkspace，不再维护 Redis metadata cache、刷新 API、`scanned_at` 或 `from_cache`。运行时只保留 `_effective_skill_slugs` 一个依赖闭包；解析必须显式接收已认证的 `db` 与 `user`。uid 级共享投影已经拥有授权边界，规范路径 `/home/gem/skills` 统一由 Sandbox backend 读取并由只读 mount 拒绝写入，不再维护额外 `/skills` backend、slug 过滤或 `_runtime_skill_sources`。`install_skill` 只持久安装并通过正式 Service 更新显式 Agent Skill 白名单，不修改当前 Runtime；`skills=None` 的全部模式无需写入，工具返回个人 `SKILL.md` 路径供当前 Agent 直接读取。

## 替代方案

- 继续逐个调用点增加 `resolve`、`is_symlink` 或 bootstrap：改动局部，但检查与副作用仍可分离，新增 producer 和 consumer 也容易遗漏，拒绝。
- 让 provisioner 自动创建 Workspace、Skill projection 和 Workdir：能隐藏冷启动缺目录，却把业务/派生存储所有权交给部署控制面，并可能掩盖错误绑定，拒绝。
- 在所有 enqueue producer 中增加 uid/workdir 参数并准备存储：发布前较早失败，但 direct run、recovery、resume 与 subagent 必须长期同步签名；既有遗漏已证明这一方案脆弱。Workdir 保留发布前物化，Skill projection 放到统一执行 bootstrap。
- 只依靠 worker 校验 Run，不加数据库约束：能阻止错误执行，不能阻止错误非终态状态进入持久层并污染恢复/调度查询；拒绝。同样不采用跨行数据库 trigger，避免把 service/repository 关系验证复制成数据库过程代码。
- 一次性重写全部 Run transition 为新状态机：可获得表面一致性，但超出已确认死锁和 scope 缺陷，迁移风险和认知负担过高；只收敛 execution-tree cancel 与创建/执行边界。
- 只枚举带 `managed-by` 的 Kubernetes Pod：新部署更精确，但滚动升级时会漏掉旧 Pod并产生虚假的 quiesce proof，拒绝。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 个人 Skill 只安装到当前用户 UserWorkspace，路径解析不能越过该根 | 非法 slug、`..` 或指向根外的 symlink 读取其他路径 | `backend/package/yuxi/agents/skills/service.py` | Personal Skill service/router unit | 根外 symlink 与路径穿越必须失败；不额外模拟校验后并发替换 | Passed |
| Skill 事务、授权和运行时状态各有单一 Owner | 可变 autocommit、Router 前置授权、Redis cache 和重复 runtime 字段产生多套事实 | Skill Repository、Service 与 Runtime | 相关 repository/service/router/runtime/backend/toolkit unit | 直接调用副作用 Service 仍须鉴权；Runtime 缺少 db/user 不能退化为读取全部 Skill | Passed |
| 新用户首次请求在 Sandbox 创建前已有可见的 UserWorkspace、Skill projection 和 Workdir | 首个 Agent Run 终态 error，第二次才因目录已出现而成功 | `backend/package/yuxi/services/chat_service.py` 与 `docker/sandbox_provisioner/app.py` | 从干净 volume 发起真实 HTTP Agent Run，等待 worker 最终状态和产物；`system-tests.yml` 收集 api/worker/provisioner 日志 | 删除 uid projection 与 Workdir 后只执行一次请求；必须一次成功，不能用重试掩盖 | Inspected |
| execution-tree 的所有级联转换按 root 到 descendants 的一致顺序锁定 | cancel 持 child 等 root，terminal 持 root 等 child，PostgreSQL 中止其中一个事务 | `backend/package/yuxi/repositories/agent_run_repository.py` | 真实 PostgreSQL integration，用独立 session 与 `pg_stat_activity`/`NOWAIT` 证明等待对象和最终状态 | Session A 锁 root，Session B cancel tree；第三个 session 必须仍能 `NOWAIT` 锁 child，证明 B 没有先持 child | Inspected |
| 新的非终态 Run 具有合法同一行形状，Subagent 的跨行 scope/creator/relation 在创建和执行两处均一致 | 错误 scope 的 Run 共享 runtime，或跨用户/跨线程 relation 被 worker 执行 | AgentRun 数据库约束、subagent service 与 `run_worker.py` | 真实 PostgreSQL integration；worker unit 与队列 E2E；重新读取 Run/Conversation/relation | 非法同行写入被 DB 拒绝，绕过创建 service 的跨行错误被 worker 拒绝；历史终态行仍可读取 | Inspected |
| Kubernetes quiesce 枚举新旧标签 Pod，不能在旧 Sandbox 运行时签发空 proof | 滚动升级后 selector 看不到旧 Pod，migrator 在其仍写入时移动数据 | `docker/sandbox_provisioner/app.py` | legacy/new Pod unit；provisioner API integration；真实集群 smoke 若环境可用 | 仅含 `app` 与 `sandbox-id` 的 legacy Pod 必须被列出并删除；删除失败不得签发完成 proof | Inspected |
| 改动面的最低证据完整通过 | unit 掩盖真实 HTTP、PostgreSQL、worker 或文件并发语义 | 测试与 CI workflow | 相关 unit → HTTP/PG integration → Agent Run E2E；`git diff --check`；根级提交前命令 | 恢复每个目标缺陷时，对应高层测试必须在正确原因上失败 | Not run |

## 风险

个人 Skill 的标准路径校验不承诺抵御同一用户在校验后并发替换路径；这是为降低业务层复杂度接受的剩余风险。

`NOT VALID` 仍会约束后续 insert/update，但历史终态违规行继续存在；任何会更新这些历史行的维护任务都需要显式处理。跨行 scope 仍依赖 subagent service 与 worker 双边验证。真实 Kubernetes smoke 可能因本地环境不可用而保持 Not run，届时不得把 unit 结果写成升级安全已实证。

现有工作区中针对这些问题的未提交代码仅视为探索性原型，其中已发现 Resume creator 约束错误；实现阶段应按本提案逐项保留或重写，不得因已有 diff 较大而默认接受。
