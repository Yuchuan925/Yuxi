# 用户文件与 AgentRun 执行边界收敛

状态：proposed
类型：architecture
Owner：backend/package/yuxi/services/workspace_filesystem.py

## 问题

当前文件与 Run 生命周期的关键规则分散在多个调用点，部分路径仍采用“先检查、后按路径名操作”或由上层拼装状态，因而无法在不可信 Sandbox 可并发修改文件、多个 PostgreSQL 事务可并发转换 Run、以及滚动升级保留旧 Kubernetes Pod 的真实条件下闭合安全与正确性。

已确认的失败面包括：Workspace API 在校验后重新按路径名打开；新用户的 Skill projection 在首次 Sandbox 创建时尚未物化；取消执行树与终态转换以相反顺序锁定 root/descendants；worker 只检查 `runtime_scope_id` 非空，不能拒绝错误的跨线程或跨 Run scope；Kubernetes inventory 的新标签 selector 看不到升级前创建的旧 Pod；artifact 同名保存采用先检查再覆盖写入。个人 Skill 位于用户自己的 UserWorkspace，本次不把同一用户进程内极低概率的路径替换竞态作为独立攻击边界。

这些不是要求所有代码都防御任意宿主机攻击。信任模型是：Sandbox 可合法并发写入自己的 UserWorkspace 和个人 Skill 源目录；API、worker 和 provisioner 必须在真正产生副作用的 Owner 处保证用户隔离、Run 归属和升级期资源枚举完整。提案不把 decision record 变成运行时事实源，也不通过继续增加独立的调用点检查来维持这些规则。

## 提案

### 1. 以现有 WorkspaceFilesystem 作为 UserWorkspace 文件操作 Owner

保留并扩展 `WorkspaceFilesystem`，让 Workspace API、Viewer、附件、artifact 和知识库导入只传用户可见虚拟路径；上层不得取得指向不可信 UserWorkspace 条目的宿主机 `Path` 后自行打开、覆盖或删除。该限制不适用于 Owner 内部的目录 fd、可信临时文件、缓存文件，或已停止写入并由迁移流程独占的目录。

`WorkspaceFilesystem` 只补齐当前 consumer 所需的完整原语：列举、受限读取与元数据、创建目录、编辑既有普通文件、删除、上传和下载。实现沿用已有的 root fd、逐层 `O_NOFOLLOW`/`dir_fd` 打开和文件类型校验；创建与上传使用原子 no-clobber。下载先复制到可信临时文件，再交给响应层并负责清理；知识库导入消费受限字节快照；Office 预览缓存按安全快照的内容摘要识别，不再回到原始 Workspace 路径。

创建与编辑保持不同并发语义：创建、上传和 artifact 保存不得覆盖已经成功创建的同名文件；编辑既有文本采用明确的 last-writer-wins，但最终替换不得跟随符号链接或逃逸 root。提案不承诺并发编辑合并。

不创建新的通用 `RootedFilesystem` 类。Workspace 继续使用 fd-relative 原语；个人 Skill 使用标准解析后路径边界校验、拒绝 symlink 的 staging copy 与原子 rename，不再把 fd 和 `/proc/self/fd` 扩散到业务层。

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

### 4. 保留升级期 Kubernetes inventory 和 artifact 的原子语义

Kubernetes inventory 用稳定的 `app=yuxi-sandbox` 枚举候选 Pod，再在代码内要求合法 `sandbox-id`；不能用新增的 `managed-by` 标签排除旧版本 Pod。删除仍保留 resource generation/precondition。Quiesce proof 只有在新旧标签 Pod 都被枚举并终止后才能签发。

Artifact 的后缀命名策略留在 `thread_files_service`，最终落盘调用现有 `upload_authorized_file_from_path(..., overwrite=False)`。发生 `FileExistsError` 时原子重试下一个后缀，避免“先检查、再覆盖”。目前没有第二个需要相同后缀策略的生产 consumer，因此不新增 `create_unique_file` 抽象。

### 5. 范围与交付顺序

本提案按可独立验证的四个变更集实现：文件边界；Run 存储冷启动；execution-tree/runtime scope；Kubernetes legacy inventory 与 artifact no-clobber。每个变更集先恢复对应缺陷作为负向测试，再修改 Owner。全部完成后才把本记录改写并移动到 `implemented/`。

本提案明确不包含 mention 缓存、前端轮询、旧环境变量清理、Conversation 创建失败补偿、完整 Run 状态机重写，以及真实 Kubernetes 集群演练。CI 对 `docker/**` 的路径触发和失败时 provisioner 日志属于本提案证据链，不作为无关清理后移。

### 6. Skill 状态与事务只保留单一 Owner

`SkillRepository` 只执行查询、写入和 `flush()`；事务提交属于 Skill Service。文件副作用 Service 接收 `operator` 并完成最终授权，Router 只负责 HTTP 参数与错误映射。共享 Skill 安装统一使用一个拒绝 symlink 的 staging snapshot primitive。

个人 Skill 直接扫描 UserWorkspace，不再维护 Redis metadata cache、刷新 API、`scanned_at` 或 `from_cache`。运行时只保留 `_effective_skill_slugs` 一个依赖闭包；解析必须显式接收已认证的 `db` 与 `user`。uid 级共享投影已经拥有授权边界，规范路径 `/home/gem/skills` 统一由 Sandbox backend 读取并由只读 mount 拒绝写入，不再维护额外 `/skills` backend、slug 过滤或 `_runtime_skill_sources`。`install_skill` 只持久安装并通过正式 Service 更新显式 Agent Skill 白名单，不修改当前 Runtime；`skills=None` 的全部模式无需写入，工具返回个人 `SKILL.md` 路径供当前 Agent 直接读取。

## 替代方案

- 继续逐个调用点增加 `resolve`、`is_symlink` 或 bootstrap：改动局部，但检查与副作用仍可分离，新增 producer 和 consumer 也容易遗漏，拒绝。
- 立即抽取通用 `RootedFilesystem`：命名统一，但当前已有 `WorkspaceFilesystem`、`open_directory_fd` 和 Skill fd helper；第三层抽象会先增加迁移和审阅表面，只有满足两个生产 consumer 与净删除条件后再考虑。
- 让 provisioner 自动创建 Workspace、Skill projection 和 Workdir：能隐藏冷启动缺目录，却把业务/派生存储所有权交给部署控制面，并可能掩盖错误绑定，拒绝。
- 在所有 enqueue producer 中增加 uid/workdir 参数并准备存储：发布前较早失败，但 direct run、recovery、resume 与 subagent 必须长期同步签名；既有遗漏已证明这一方案脆弱。Workdir 保留发布前物化，Skill projection 放到统一执行 bootstrap。
- 只依靠 worker 校验 Run，不加数据库约束：能阻止错误执行，不能阻止错误非终态状态进入持久层并污染恢复/调度查询；拒绝。同样不采用跨行数据库 trigger，避免把 service/repository 关系验证复制成数据库过程代码。
- 一次性重写全部 Run transition 为新状态机：可获得表面一致性，但超出已确认死锁和 scope 缺陷，迁移风险和认知负担过高；只收敛 execution-tree cancel 与创建/执行边界。
- 只枚举带 `managed-by` 的 Kubernetes Pod：新部署更精确，但滚动升级时会漏掉旧 Pod并产生虚假的 quiesce proof，拒绝。
- Artifact 在写前检查文件是否存在：顺序请求可用，并发请求仍会返回相同路径且后写覆盖先写，拒绝。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 所有 shipping UserWorkspace I/O 都在同一次 fd-relative no-follow 操作中闭合，且上层不重新打开宿主机路径 | Sandbox 在校验与 I/O 之间把中间或最终组件替换为 symlink，造成跨 uid 读写、删除或覆盖 | `backend/package/yuxi/services/workspace_filesystem.py` | Workspace/Viewer/附件/artifact/知识导入相关 unit；真实 HTTP integration；符号搜索确认无 UserWorkspace host-Path consumer | 并发把已校验目录 rename 后换成指向另一 uid/Skill source 的 symlink；操作必须失败且受害文件不变 | Inspected |
| 个人 Skill 只安装到当前用户 UserWorkspace，路径解析不能越过该根 | 非法 slug、`..` 或指向根外的 symlink 读取其他路径 | `backend/package/yuxi/agents/skills/service.py` | Personal Skill service/router unit | 根外 symlink 与路径穿越必须失败；不额外模拟校验后并发替换 | Passed |
| Skill 事务、授权和运行时状态各有单一 Owner | 可变 autocommit、Router 前置授权、Redis cache 和重复 runtime 字段产生多套事实 | Skill Repository、Service 与 Runtime | 相关 repository/service/router/runtime/backend/toolkit unit | 直接调用副作用 Service 仍须鉴权；Runtime 缺少 db/user 不能退化为读取全部 Skill | Passed |
| 新用户首次请求在 Sandbox 创建前已有可见的 UserWorkspace、Skill projection 和 Workdir | 首个 Agent Run 终态 error，第二次才因目录已出现而成功 | `backend/package/yuxi/services/chat_service.py` 与 `docker/sandbox_provisioner/app.py` | 从干净 volume 发起真实 HTTP Agent Run，等待 worker 最终状态和产物；`system-tests.yml` 收集 api/worker/provisioner 日志 | 删除 uid projection 与 Workdir 后只执行一次请求；必须一次成功，不能用重试掩盖 | Inspected |
| execution-tree 的所有级联转换按 root 到 descendants 的一致顺序锁定 | cancel 持 child 等 root，terminal 持 root 等 child，PostgreSQL 中止其中一个事务 | `backend/package/yuxi/repositories/agent_run_repository.py` | 真实 PostgreSQL integration，用独立 session 与 `pg_stat_activity`/`NOWAIT` 证明等待对象和最终状态 | Session A 锁 root，Session B cancel tree；第三个 session 必须仍能 `NOWAIT` 锁 child，证明 B 没有先持 child | Inspected |
| 新的非终态 Run 具有合法同一行形状，Subagent 的跨行 scope/creator/relation 在创建和执行两处均一致 | 错误 scope 的 Run 共享 runtime，或跨用户/跨线程 relation 被 worker 执行 | AgentRun 数据库约束、subagent service 与 `run_worker.py` | 真实 PostgreSQL integration；worker unit 与队列 E2E；重新读取 Run/Conversation/relation | 非法同行写入被 DB 拒绝，绕过创建 service 的跨行错误被 worker 拒绝；历史终态行仍可读取 | Inspected |
| Kubernetes quiesce 枚举新旧标签 Pod，不能在旧 Sandbox 运行时签发空 proof | 滚动升级后 selector 看不到旧 Pod，migrator 在其仍写入时移动数据 | `docker/sandbox_provisioner/app.py` | legacy/new Pod unit；provisioner API integration；真实集群 smoke 若环境可用 | 仅含 `app` 与 `sandbox-id` 的 legacy Pod 必须被列出并删除；删除失败不得签发完成 proof | Inspected |
| 并发同名 artifact 保存返回不同路径且两份内容都保留 | 两个请求都预检不存在，后写覆盖先写并都返回同一 `saved_path` | `backend/package/yuxi/services/thread_files_service.py` 与 `workspace_filesystem.py` | 并发 service unit 与真实 HTTP integration，重新读取两个 artifact 内容 | barrier 同步两个相同 basename 保存；必须生成唯一后缀且不丢内容 | Inspected |
| 实现没有新增无当前 consumer 的文件系统抽象或重复调用点校验 | 修复代码比问题更复杂，Owner 再次分裂 | 上述各 Owner 与工程契约 | `rg` 审计 host Path/resolve；独立 Reviewer 审查完整 diff；`python3 scripts/verify_engineering_contracts.py`；`python3 -m unittest scripts.test_verify_engineering_contracts` | 重新引入 Workspace 上层 `Path.open/read_text/os.rename` 或第二套 suffix/bootstrap 逻辑时 Review/gate 必须拒绝 | Not run |
| 改动面的最低证据完整通过 | unit 掩盖真实 HTTP、PostgreSQL、worker 或文件并发语义 | 测试与 CI workflow | 相关 unit → HTTP/PG integration → Agent Run E2E；`git diff --check`；根级提交前命令 | 恢复每个目标缺陷时，对应高层测试必须在正确原因上失败 | Not run |

## 风险

Workspace fd-relative API 主要面向 shipping Linux 容器；下载与预览的安全快照增加临时磁盘和一次复制，需沿用大小上限与确定清理。个人 Skill 的标准路径校验不承诺抵御同一用户在校验后并发替换路径；这是为降低业务层复杂度接受的剩余风险。last-writer-wins 编辑会覆盖另一个合法并发编辑，但不会越界；若产品需要冲突检测，应另行引入版本号/ETag，而不是误用 create-only no-clobber。

`NOT VALID` 仍会约束后续 insert/update，但历史终态违规行继续存在；任何会更新这些历史行的维护任务都需要显式处理。跨行 scope 仍依赖 subagent service 与 worker 双边验证。真实 Kubernetes smoke 可能因本地环境不可用而保持 Not run，届时不得把 unit 结果写成升级安全已实证。

现有工作区中针对这些问题的未提交代码仅视为探索性原型，其中已发现 Resume creator 约束错误；实现阶段应按本提案逐项保留或重写，不得因已有 diff 较大而默认接受。
