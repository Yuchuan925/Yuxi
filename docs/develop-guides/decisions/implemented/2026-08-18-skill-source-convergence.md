# Skill 持久源与只读投影收敛

状态：implemented
类型：simplification
Owner：backend/package/yuxi/agents/skills/service.py

Skill 持久目录配置由 `yuxi.config` 拥有；Prompt 与激活路径由
`agents/middlewares/skills.py` 拥有；Sandbox 只读挂载仍由 provisioner 拥有。

## 问题

个人 Skill 曾把 User Data 的 `workspace/agents/skills` 同时当作用户可写文件与运行时 Skill 来源，
共享/内置 Skill、用户投影和安装草稿也都隐式依赖广域 `SAVE_DIR`。这使 Skill 拥有两个 Agent 可达
路径，把 User Workspace 生命周期混入授权投影，并阻碍 API/worker 在后续阶段退出共享 `saves`。

## 决策

- `YUXI_SKILL_DATA_DIR` 是 Skill 持久源根；共享与内置来源位于 `shared/<slug>`，个人来源位于
  `personal/<safe-uid>/<slug>`。个人 Skill 继续允许同 uid 覆盖同名共享版本，但不进入共享 Skill 表。
- `YUXI_SKILL_PROJECTION_DIR/<safe-uid>` 是当前 uid 授权全集的物化投影。Sandbox 只读挂载为
  `/home/gem/skills`；Agent 选择只影响 Prompt 与工具激活。
- Skill 安装草稿属于进程可丢弃状态，使用 `YUXI_RUNTIME_DIR/skill_import_drafts`，不再进入持久卷。
- 一次性 `storage-migrator` 在 PostgreSQL advisory lock 下迁移已识别的旧共享和个人来源。迁移使用
  fd-relative `O_NOFOLLOW` 快照、校验 `SKILL.md` slug，并在目标冲突时拒绝切换。共享旧源在 DB/source
  切换成功后删除；个人旧目录只在 shipping runtime 尚未启动的迁移窗口删除。新代码不读取或激活
  User Workspace 兼容路径。
- PostgreSQL `skills.dir_path` 仅保存相对 Skill 数据根的共享来源路径，例如 `shared/<slug>`；
  个人来源由 uid 目录拥有，不伪装成共享数据库记录。

## 替代方案

- 永久保留 User Workspace 兼容路径：拒绝。它让个人 Skill 同时存在可写源与只读投影两个运行入口。
- 把个人 Skill 迁入 Project Workdir：拒绝。Project 可能由多个 Conversation 共用，而个人 Skill 授权
  与生命周期属于 uid。
- 把所有 Skill 内容迁入 PostgreSQL 或 MinIO：拒绝。Sandbox 需要真实目录与可执行文件，只读 POSIX
  投影已是更直接的运行边界。
- 为个人 Skill 增加共享表记录：拒绝。全局唯一 slug 会破坏不同用户独立使用同名个人版本的语义。

## 后果

- Agent 只有 `/home/gem/skills/<slug>` 一个 Skill 路径；Project Viewer 与 User Data 不展示 Skill 源。
- Skill source 与 projection 可以在 Compose/Kubernetes 中使用独立语义挂载，不从 Project/User POSIX
  卷派生；worker 对 User Data 的独立只读用途是加载用户 Agent 上下文。
- 历史来源损坏、包含链接/特殊路径或与新 Owner 内容冲突时，启动 fail-closed 并保留旧数据；不会静默
  选择任一版本。个人旧目录的物理删除必须等 Stage 6 停机迁移先清理全部旧 runtime。
- Kubernetes 与共享卷边界由
  [显式存储域与 Kubernetes PVC 收敛](2026-08-19-explicit-storage-domains-and-kubernetes-pvc.md)拥有。

## 验证

- backend non-slow unit：`1384 passed, 34 skipped`；宿主 Compose 配置 contract：`39 passed`。
- 真实 PostgreSQL/MinIO/HTTP 与 Docker 集成（迁移、作用域、撤权、投影和实时 Project 挂载）：
  `18 passed`。其中共享旧源、未登记共享/个人旧目录、个人旧源迁移完成标记和撤权投影均有负向案例；远程
  Skill 一次性 Sandbox 不创建持久 User Data 或 Skill projection UID 目录。
- 真实主 Agent 个人 Skill 与父子 Agent 共享 execution runtime E2E：`2 passed`。
- Web lint、`43 passed` unit 与生产 build 通过；docs build、Ruff check/format、工程 contract
  `48 passed` 和 `git diff --check` 通过。

旧能力不存在：生产代码不再定义、注入、激活或回退读取
`/home/gem/user-data/workspace/agents/skills`，个人 Skill 不再以 User Workspace 为持久源，Skill 安装草稿
不再写入持久 `SAVE_DIR`；停机迁移完成后旧目录也不再保留。

重新引入条件：只有新的产品授权明确要求 Project/User 文件直接成为可激活 Skill，并提供 source
ownership、撤权、TOCTOU、并发更新和迁移证据时，才可重新引入第二个 Skill 路径；不得以兼容 fallback
或 Prompt 文案恢复。
