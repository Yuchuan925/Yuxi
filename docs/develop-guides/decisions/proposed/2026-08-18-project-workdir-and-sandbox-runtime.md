# Project Workdir 与独立 Sandbox Runtime

状态：proposed
类型：simplification
Owner：docker/sandbox_provisioner/app.py

文件授权与实时读取由 `backend/package/yuxi/services/viewer_filesystem_service.py`、
`backend/package/yuxi/services/thread_files_service.py` 和对应 repository 拥有；Conversation 与
Sandbox 的工作目录绑定由 PostgreSQL Conversation 记录、Run 装配服务和 provisioner 共同拥有。
本记录只裁决这些 Owner 之间的边界。

已经生效的 4R-A 身份与挂载基础由
[Project Workdir 与 Sandbox Runtime 基础](../implemented/2026-08-18-project-workdir-runtime-foundation.md)
记录；本提案拥有正在验证的 4R-B 与尚未实现的 4R-C 至阶段 6。

## 问题

当前 Sandbox 把 `/home/gem/user-data/workspace` 作为用户级共享目录，把
`/home/gem/user-data/uploads` 与 `/home/gem/user-data/outputs` 作为每个 Run 的临时工作副本。
Stage 4 又以 PostgreSQL output revision、MinIO 对象、每 Run Sandbox、父子 checkpoint/projection
和三方合并模拟文件共享。该实现已经提交到开发分支，但尚未部署。

当前已实现事实记录在
[API / Worker 文件存储解耦](../implemented/2026-08-17-api-worker-file-storage-decoupling.md)。
本提案只取代其中尚未部署的 Stage 4 outputs 文件模型；Stage 1 至 Stage 3 的权限、runtime 目录和
附件对象安全边界继续有效，直到各自 consumer 在后续切换中被显式替换。

这种发布模型不能满足新的首要产品约束：Agent 写入后，其他已授权 Agent 和 Viewer 必须读取同一份
实时 POSIX 文件系统，不能等待 Run 终态 publish。它还把 `uploads`、`outputs` 和用户 workspace
误建模成三套存储协议，并为父子 Agent 引入本不需要的文件复制、合并和瞬时 404。

目标是把持久共享文件与隔离运行时分开：一个顶层 Conversation 及其子 Conversation/Agent 使用同一
Sandbox runtime；不同顶层 Conversation 使用独立 runtime，但可以挂载同一个 Project Workdir。
当前尚无 Project 产品实体时，只有顶层 Conversation 创建默认 Workdir；`SubagentThread` 关系中的
子 Conversation 必须继承根 Conversation 的 Workdir 与 runtime scope，不能自行分配。未来只需让
多个顶层 Conversation 指向同一 Workdir，不能再迁移文件协议。

## 提案

### 文件作用域

- `Project Workdir` 是实时文件事实源，使用持久 RWX POSIX 存储。Sandbox 内路径固定为
  `/home/gem/projects/project-<opaque-id>`，并作为默认工作目录。
- `uploads/`、`outputs/` 和其他子目录只是 Project Workdir 内的使用约定。Agent 可以覆盖上传文件；
  Prompt 建议把交付物写入 `outputs/`，后端不赋予该目录特殊发布或只读语义。
- `User Data` 是当前用户跨 Project 的私有可写目录，挂载到 `/home/gem/user-data`。第一阶段保留
  已有 `/home/gem/user-data/workspace` 内容和个人 Skill 路径，避免把目录整理混入存储 Owner 切换。
- `Skills` 挂载到 `/home/gem/skills`，只包含当前用户可访问的 Skill 版本并保持只读。父子 Agent
  看到同一目录集合，但各自的 System Prompt 和工具注册只加载已选择的 Skill；选择不是授权边界。
  投影重建必须先取得 uid 级 PostgreSQL advisory lock，再读取最新授权快照，并在共享卷的 uid 级
  文件锁内替换目录；缺失或无效的授权上下文必须阻止 Run，不能被解释为空集合并清理其他 Run 的投影。
- AgentPanel Viewer 只展示当前 Project Workdir，不展示 User Data、Skills 或 Sandbox 系统目录。

### Sandbox 与并发

- PostgreSQL `ProjectWorkdir` 记录拥有持久文件身份、所属 uid、存储键和物化状态。顶层 Conversation
  保存 `workdir_id`，没有 Project 时创建独立默认 Workdir；子 Conversation 通过
  `SubagentThread.parent_conversation_id` 解析根 Conversation 并复制同一个外键。新建子线程和继续
  既有子线程都不能创建新 Workdir。未来 Project 可以让多个顶层 Conversation 使用同一外键。
- Sandbox runtime 以稳定 `runtime_scope_id = 根 Conversation thread_id` 为隔离边界，并可复用。
  每个根/子 `AgentRun` 都持久保存同一 runtime scope，现有 Run lease/heartbeat 和终态拥有活跃事实；
  根 Run 进入终态时，在同一个 PostgreSQL 事务中把仍活跃的全部后代收敛为 cancelled，再发取消信号。
  不等待子进程自行优雅退出；execution tree 的终态负责强制关闭其完整进程边界。不同根 Conversation
  即使共享 Workdir，也不共享进程、`/tmp`、安装依赖或环境变量。
- provisioner 对同一 `runtime_scope_id` 原子 get-or-create；并发 worker 只能得到同一健康 runtime
  generation，不能各自创建容器。重试和 resume 必须重新验证 generation，不能依赖 worker 本地缓存。
- 多个 Sandbox 可以同时读写同一 Workdir。系统不做应用层锁、revision 合并或冲突修复；同路径
  并发写遵循底层 POSIX 文件系统语义。
- 根 Run 终态事务设置 durable `runtime_cleanup_pending` fence；新 Run 派发、retry claim 和 SSE `end`
  都必须等待 cleanup 成功。cleanup 在 uid/runtime advisory lock 与 Run 行锁下销毁完整 execution runtime，
  失败由周期 reconciler 重试；retry 清理成功后重新投递原 pending Run。单个子 Run 终态不清理共享
  runtime。Workdir 是独立持久挂载，任何 runtime 终态或清理失败都不删除其中字节；重建只需重新挂载
  Workdir、User Data 和 Skills，不做文件 hydrate。

### 实时读取、artifact 与终态

- Viewer、文件搜索、预览、下载、上传、删除和 artifact 都通过受信任文件边界读取实时挂载，不能
  把 MinIO manifest 或 `ThreadOutputRevision` 作为当前文件源。API/worker 不直接挂载用户卷。
- 文件 API 提供写后读一致的请求语义；AgentPanel 通过文件事件或约一秒轮询刷新。正在并发覆盖的
  文件允许呈现任一真实 POSIX 时刻，不承诺读取尚未 flush 的部分写入。
- `present_artifacts` 接受 Project Workdir、User Data 和当前用户已授权 Skills 三类根中的任意普通
  文件；`outputs/` 不再是校验条件。Skill 是否被当前 Agent 选中只影响 Prompt/工具装配，不影响
  用户级文件授权。路径必须在授权根内，拒绝 `..`、symlink 逃逸、目录、设备、socket、FIFO 与
  Sandbox 系统路径。
- completed、interrupted、failed 和 cancelled 都保留已经写入 Workdir 的字节。文件可见性不再依赖
  Run 终态事务；MinIO checkpoint 若后续保留，只用于异步恢复或审计，失败不能隐藏实时文件。
- 并发运行时的 checkpoint 只表示某个时间点观察到的 Workdir，不能归因成单个 Run 的完整产物，
  也不承诺跨文件原子快照。

### 历史物化与切换

- `ProjectWorkdir.materialization_status` 区分 `pending`、`importing`、`prepared`、`ready` 和 `error`。
  一次全局 `materialization_epoch` 拥有 source inventory/fingerprint 和独立 staging 根；只有 active epoch 的 `ready`
  Workdir 可以被实时 Viewer 或 Sandbox 新链路读取；不能把未迁移目录当成空成功。
- 4R-B 使用停机维护切换，不支持旧、新版本滚动混跑：部署方必须先停止旧 API/worker producer，
  等待或终结全部非终态 Run，再启动新 composition。新版本在最终 legacy inventory 采集前写入全局
  fence，并让上传、删除、Run、Viewer 与 artifact 在 activation 前统一 fail-closed。物化服务持有
  全局 advisory lock 与 Workdir 行锁，在该 epoch 的 staging 根导入当前 Conversation 附件、已发布
  output revision 或旧 thread uploads/outputs，并记录来源 fingerprint。
- 重试遇到同路径同 hash 视为幂等；同路径不同内容、缺失对象、部分复制或确认不明都把状态保留为
  `error/importing`，不得推进 epoch。不同内容保护只适用于未知目录或已经激活的实时数据；尚未成为
  shipping 文件源的旧 epoch staging 可以整体丢弃并由新 epoch 重建。
- 只有全量 Workdir 都为 `prepared`、source fingerprint 仍与 fence 下的最终 inventory 一致且逐文件
  核对通过，activation 才把 epoch 与所有 Workdir 一次标记为 active/ready。activation 是不可回退
  commit point：此前失败时新版本保持 fail-closed，由运维修复来源后重试完整新 epoch；若要继续旧版本，
  必须在启动新 composition 前取消本次维护，不能在同一数据库中让旧、新 producer 混跑。activation
  后只向前完成 rollout，不能恢复旧写入或按 Conversation 混跑。
- activation 事务同时把 Conversation 附件 metadata、历史 `present_artifacts` ToolCall 路径改写为
  Project Workdir 路径；LangGraph state 对外展示时也归一化历史 uploads/outputs artifact。切换后不能
  通过旧 `/home/gem/user-data/uploads|outputs` 路径 fallback。
- 全局激活后不再回退 revision/host path。4R-C 才删除旧物化 Owner；阶段 6 只负责新架构的 K8s
  部署收敛和删除最终共享 `saves`，不再重试依赖旧 revision/object metadata 的条目。

### 部署边界

- Docker Compose 使用 provisioner 可访问的持久 POSIX 根，每个 Sandbox 只挂载授权的 Project、
  User 和 Skill 子目录，不交换宿主机绝对路径给 API 或 worker。
- Kubernetes 必须使用支持 `ReadWriteMany` 的 CSI/PVC。推荐存储键为 `projects/<workdir_id>`、
  `users/<uid>` 和只读 Skill projection；多个 Sandbox Pod 可同时挂载同一 Project subPath。
- 如果部署环境没有 RWX POSIX 存储，只能运行单节点或单挂载者模式，不能宣称支持跨节点实时共享。

### 实施阶段

1. **4R-A：不切换文件主链路的 Workdir 与身份基础。** 增加 `ProjectWorkdir`、顶层/子 Conversation
   绑定、`runtime_scope_id`、授权路径策略、物化状态与 provisioner 原子 get-or-create/mount contract。
   同时把 `/home/gem/skills` 收敛为当前用户授权全集的只读投影，从 Sandbox identity、wire contract
   和挂载身份中删除 `skills_thread_id` 与 `file_thread_id`；Sandbox 只由 uid、runtime thread、instance
   和可选 Workdir 约束。Agent 选择只决定 Prompt 与工具激活。shipping Viewer、上传、旧附件/output
   文件 scope、每 Run instance 与 output 发布链路保持现状；本阶段不宣称实时 Project 文件行为已生效。
2. **4R-B：全量物化后原子切换实时主链路。** 停止旧 API/worker 并排空全部文件 producer 后，以新的
   materialization epoch 把所有 legacy Conversation 导入隔离 staging；全量 `prepared`、source
   fingerprint 和 hash/size 核对是 activation gate。activation 前失败让新版本保持 fail-closed 并重试
   完整新 epoch；activation 后只向前完成 rollout。随后在同一 Review 单元切换 runtime identity、
   Project/User mounts、默认 cwd、上传、
   Viewer、搜索、预览、下载、删除和 Project/User Data artifact。切换同时从 shipping composition
   断开 output hydrate/stage/publish、父子 checkpoint/projection/merge、outputs 前缀限制和瞬时 404
   workaround；任一旧 consumer 仍被调用都使本阶段失败。
3. **4R-C：删除已无 consumer 的旧表面。** 在 4R-B 已证明只有实时链路后，删除 output revision
   schema/repository/service/export、checkpoint/merge 死代码、旧配置/fixture/文档和迁移完成后不再需要
   的 MinIO 对象。保留仍被安全文件 API 或明确备份 consumer 使用的流式、hash/size 与 symlink
   primitives，不能保留双写或 fallback。
4. **5：Skills 兼容路径收敛。** 4R-A 已建立用户授权全集的统一只读投影，4R-B 已把该授权范围纳入
   artifact；本阶段只在独立迁移 Review 后删除个人 Skill 旧兼容路径。
5. **6：K8s 与最终共享卷收敛。** 删除 host-path 推导和 API/worker/provisioner 最终共享 `saves`，
   完成真实 RWX Kubernetes smoke。本阶段不重试或依赖已经由 4R-C 删除的旧 revision/object Owner；
   如果 4R-B 有任何未 ready 条目，本阶段不能开始。

每个阶段必须完成风险相称的 unit、真实 PostgreSQL/HTTP integration、Docker sandbox assembled path 和
必要 E2E，经过全新上下文 Reviewer 与用户 Review 后才能提交并进入下一阶段。

## 替代方案

- **保留 Stage 4 output revision/publish。** 拒绝。它只能提供终态版本，无法让 Viewer 与并发 Agent
  读取当前 Sandbox 字节，并继续保留父子复制与合并复杂度。
- **多个 Conversation 复用同一个 Sandbox runtime。** 拒绝。用户只要求共享文件；共享进程、依赖、
  `/tmp` 和环境变量会扩大取消、清理和凭据泄漏边界。
- **每个 Sandbox 使用本地副本，依靠 MinIO 高频同步。** 拒绝。同步间隔无论多短都不是同一文件系统，
  rename、partial write 和并发覆盖仍会产生双重事实源。
- **把 MinIO 通过 s3fs/ossfs 挂成 POSIX。** 拒绝。对象存储不拥有 shell 需要的完整 POSIX 语义，
  还会把对象凭据带入不可信运行时。
- **继续把所有内容放在 `/home/gem/user-data`。** 拒绝。它混淆 Project 与 User 生命周期，也让
  AgentPanel 无法只展示当前协作范围。
- **Project Workdir + 独立 Sandbox runtime。** 采用。它让共享边界与产品协作范围一致，运行环境仍按
  Conversation 隔离，并可直接使用 Docker volume 与 Kubernetes RWX PVC。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 顶层 Conversation 有稳定 Workdir，子 Conversation 继承根绑定，未来多个顶层可共享 | 子线程自行创建 Workdir、continuation 漂移，或仍从路径隐式推导 | ProjectWorkdir/Conversation/SubagentThread repository、path policy | PostgreSQL migration/repository integration；新建子线程、继续既有子线程并重建服务后回读 | 子线程不得生成新 ID；两个独立 Workdir 不串读；显式共享同一 ID 时指向同一目录 | Passed |
| 同 Project 的两个独立 Sandbox 实时看到同一文件，但运行环境不共享 | 测试实际复用同一容器，或仍依靠 publish/hydrate | provisioner、Sandbox backend | 真实 Docker 双 Sandbox producer-consumer；K8s spec unit | A 写文件后 B 直接读到；A 写 `/tmp` 或环境变化时 B 不可见 | Passed |
| 父子 Agent 使用同一 runtime scope，且根终态原子关闭完整 execution tree | 子 Agent 创建独立副本、后代数据库状态残留，cleanup 失败后提前发 end，或 retry 复用旧进程 | AgentRun runtime scope/lease、subagent 装配、provisioner、lifecycle service | parent-write → child-read → parent-read E2E；并发 get-or-create/cleanup integration | 根终态必须在同事务取消后代；cleanup fence 未清除时不得派发下一 Run、claim retry 或合成 SSE end；周期重试后使用新 generation | Passed |
| 全部旧文件与持久路径引用在全局切换前以同一 epoch 完成可重试物化 | 部分 Workdir 先 ready、空目录/附件路径遗漏、历史 artifact 仍指向旧根，或重试覆盖实时文件 | ProjectWorkdir/materialization epoch repository、maintenance fence、activation gate | 真实 PostgreSQL/MinIO/POSIX integration；全量 source fingerprint、metadata/ToolCall 与 staging 回读 | activation 中断→旧链路变化→新 epoch 必须包含新字节；目录数/深度有界；active 后 attachment/history 只返回 Project 路径 | Passed |
| Viewer 只展示实时 Project Workdir，并在约一秒内反映修改 | 继续读取 output revision、展示 User Data，或文件存在却瞬时 404 | viewer/thread-files service、AgentPanel | 真实 HTTP + Web polling unit；Sandbox 写入后不经过 Run 终态直接预览 | 禁止调用 publish 后再读；User Data/Skills 不出现在树中；终态事件不得早于 runtime 清理 | Passed |
| uploads/outputs 是可写目录约定，不是不同存储协议 | uploads 仍只读或 outputs 仍要求终态发布 | attachment/file services、Prompt | 上传 → Agent 覆盖 → Viewer 回读 E2E | 移除 MinIO hydrate 后仍可读；恢复 outputs prefix/revision guard 时失败 | Passed |
| 4R-B artifact 可指向 Project、User Data 与用户授权 Skills 的任意普通文件 | 仍只接受 outputs，或可读取未授权/容器系统文件 | artifact service、path policy | HTTP integration + Sandbox assembled-path integration + unit | 三类授权根普通文件通过；未授权 Skill、symlink、设备和 `/proc` 拒绝 | Passed |
| 所有 Run 终态都保留已写文件，且不以 checkpoint 决定可见性 | failed/cancelled 清目录，或 checkpoint 失败使文件消失 | run worker、mount lifecycle | 各终态 lifecycle unit + 真实 runtime 重建 producer-consumer | 每种终态必须只清 runtime、不清 Workdir；旧 runtime 删除后新 runtime 回读同一文件 | Passed |
| Skills 文件按用户授权统一只读可见，Prompt/工具只装配选择项 | 未授权 Skill 被挂载、Skill 选择改变 Sandbox identity，或并发旧快照复活已撤权 Skill | Skills service、agent context builder、provisioner、artifact service | 用户授权投影 unit + 真实 PostgreSQL advisory-lock 撤权 integration + 多进程同步负测 + Docker assembled path | 撤权/disable 在提交前锁定 uid 并先移除旧投影，artifact 每次按 DB 复核；等待锁期间撤权后不得复活；personal symlink/特殊文件 fail-closed；未选中 Skill 不进 Prompt/工具 | Passed |
| Kubernetes 只通过 RWX PVC/subPath 共享 Workdir，API/worker 不挂载用户卷 | 单节点 host path 假绿、RWO 跨节点不可用 | K8s provisioner、Compose contract | pod spec unit、Kind/真实 K8s 双 Pod smoke | 恢复 API/worker mount、缺 RWX 或两个 Pod 不能互读时失败 | Not run |
| 4R-B shipping composition 只使用实时 Workdir | 新实时链路旁保留双写/fallback，或先删除迁移 consumer | output revision services、subagent middleware、Web preview、materialization gate | assembled-path call oracle；全量测试与 docs build | 恢复 hydrate/stage/publish、projection/merge、“发布中 404”或 ready 后 legacy fallback 时失败 | Passed |
| 4R-C 后旧文件表面不存在 | schema、repository、service、export、fixture 或配置残留形成第二事实源 | 旧 output/attachment schema 与服务 | `rg` consumer/export/config/schema 负向搜索；全量测试/docs build | 任一 shipping 或维护入口仍可调用旧文件协议时失败 | Not run |

旧能力不存在：4R-B 完成后，shipping runtime 不再创建、hydrate、合并或发布
`ThreadOutputRevision`，不再按 Run 创建独立文件副本，不再要求 artifact 位于 outputs，也不再把
“文件已在 Sandbox 但 revision 未发布”显示成等待状态。全量 ready 是部署前置，不能形成新旧版本
按 Conversation 混跑；切换后没有 fallback。4R-C 进一步删除已无 consumer 的 schema、repository、
service、export、fixture、配置和文档。

### 4R-B 当前验证记录

- Backend unit：`1431 passed`；终态 cleanup/chat 定向 `47 passed`；工程契约 unittest：`48 passed`。
- 当前树定向 integration 共 `23 passed`：真实 PostgreSQL lease/schema/授权锁、MinIO/POSIX migration、
  Docker provisioner，以及 Skill artifact 撤权前后真实 HTTP。覆盖 migration 中断后新 epoch 重试、双
  Sandbox 实时共享、runtime 重建、跨用户 Workdir 约束与撤权后旧 artifact URL 立即拒绝。
- E2E：启动仓库内确定性 replay server 后，API/worker/SSE、附件 runtime 重建与
  父写→子读写→父读→终态后 HTTP 回读共 `4 passed`。
- Web：unit `43 passed`，lint 与 build 通过；docs build 通过。浏览器未以测试凭据运行
  AgentPanel DOM，因此不把已认证实页写成通过。
- Kubernetes/Kind 未运行，保留到阶段 6；Compose 单机证据不替代 RWX 多节点验证。

重新引入条件：只有产品明确要求不可变历史 artifact、跨文件原子版本或跨不共享 POSIX 的部署，并且
愿意让这些能力成为独立的“冻结/导出”操作时，才可重新引入对象快照。它不能再次成为实时 Viewer、
父子文件共享或普通 artifact 的默认路径。

## 风险

- RWX 文件系统成为生产依赖。NFS/CephFS/EFS 等实现的缓存、一致性、配额和故障语义必须通过目标
  Kubernetes 环境校准，Compose 单机结果不能替代。
- 多个 Sandbox 可同时覆盖同一路径；系统明确不提供 merge 或自动恢复。测试只能证明真实可见性，
  不能把最后写入者的不确定顺序写成稳定 oracle。
- 历史 Message 中的 artifact 默认跟随实时路径，文件之后被覆盖或删除时内容会变化或 404。不可变归档
  属于未来显式“冻结”能力，不在本阶段暗中保留双写。
- `/home/gem/user-data` 当前同时承载用户 workspace 与个人 Skill 文件。第一轮扩大挂载根但保留目录，
  后续整理必须独立迁移，不能在主链路切换时移动用户文件。
- Stage 4 已提交但未部署；删除时仍须覆盖 schema、migration、repository、service、worker、subagent、
  Viewer、Web、测试与正式文档，不能只旁路调用留下无 consumer 维护面。
