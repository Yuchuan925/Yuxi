# Skills 兼容路径与 Kubernetes 存储收敛

状态：proposed
类型：simplification
Owner：docker/sandbox_provisioner/app.py

本提案只拥有实时 Workdir 主链路完成后的 Stage 5 与 Stage 6。已生效的 Project Workdir、runtime、
Viewer、artifact、迁移和旧 revision 删除由
[实时 Project Workdir 与独立 Sandbox Runtime](../implemented/2026-08-18-live-project-workdir-and-runtime.md)
记录。

## 问题

当前个人 Skill 的来源仍保留在 User Data 兼容目录，用户级 Skill 投影和 User Data 仍依赖 Compose
共享 `saves`。Docker 已能证明实时 Project 语义，但 Kubernetes 只有 Pod spec 级证据；没有真实 RWX
集群 smoke 时，不能宣称跨节点 Sandbox 同时读写一个 Workdir。API/worker 也尚未完全退出最终共享
`saves`，这会继续限制独立扩缩容。

## 提案

### Stage 5：Skills 兼容路径收敛

- 盘点个人 Skill 当前源目录、DB version/content hash、用户授权投影和安装/更新/删除 consumer。
- 在独立迁移中把个人 Skill 源移动到明确的用户级持久 Owner；投影继续只包含当前 uid 授权全集，保持
  `/home/gem/skills` 只读，Agent 选择仍只影响 Prompt/工具。
- 迁移必须先构建不可变可信快照、校验 hash/version 与权限，再切换源引用；失败时不删除旧源。
- 全量切换并通过真实授权 mutation/并发负控后，删除 `/home/gem/user-data/workspace/.skills` 等个人
  Skill 兼容读取、fallback、fixture 和文档，不改变 Project Viewer 范围。

### Stage 6：Kubernetes 与最终共享卷收敛

- 选择支持 `ReadWriteMany` 的目标 CSI/PVC，把 `projects/<workdir_id>`、`threads/shared/<uid>` 与
  `skill-projections/<uid>` 作为明确 subPath；不同 runtime Pod 可以同时挂载同一 Project。
- 在真实 Kind/目标 Kubernetes 环境执行双 Pod producer-consumer、并发覆盖、Pod 重建、generation
  删除保护、只读 Skill 和跨 uid 拒绝 smoke。RWO、单节点 hostPath 或只检查 Pod spec 不能算通过。
- 将 API/worker 的剩余共享 `saves` consumer 逐一迁移到 PostgreSQL、MinIO、专用服务或只读配置；
  provisioner 是用户 POSIX 卷的唯一挂载 Owner，API/worker 不接收宿主机路径。
- 删除 Compose/API/worker/provisioner 中最终 host-path 推导和共享 `saves` mount；保留日志、Office
  cache、模型/runtime 等各自已经裁决的独立生命周期，不建立通用 FileStore。

## 替代方案

- Stage 5 与 Stage 6 合并：拒绝。Skill 数据迁移和 Kubernetes RWX 是不同故障域与回滚单元。
- 继续永久保留个人 Skill 兼容路径：拒绝。它让授权投影有两个可写来源，增加并发和 symlink 边界。
- 用 RWO PVC、hostPath 或 MinIO FUSE 宣称跨节点实时共享：拒绝。它不能证明多个 Sandbox Pod 观察
  同一 POSIX 文件系统。
- 为所有文件引入通用 FileStore：拒绝。Project/User/Skills 已有不同授权和生命周期 Owner，统一接口
  不应掩盖这些边界。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 个人 Skill 只有一个持久源，投影仍按 uid 授权且只读 | 兼容目录仍可回退、撤权后复活、选择改变文件授权 | Skills repository/service、projection lock | 真实 PostgreSQL/文件 migration integration、Docker assembled path | 中断不删旧源；切换后旧路径不可读；撤权与并发 refresh 不复活 | Not run |
| 两个 Kubernetes Sandbox Pod 实时共享同一 Project 且 runtime 隔离 | 测试复用同 Pod/节点、依赖轮询同步或 RWO | K8s provisioner、RWX PVC/subPath | 真实集群双 Pod producer-consumer 与重建 smoke | A 写后 B 直接读；`/tmp`/env 不共享；旧 generation 不删新 Pod | Not run |
| API/worker 不挂载用户 POSIX 卷或共享 `saves` | host path 仍进入业务进程、独立扩缩容假成立 | Compose/K8s contract、各数据 Owner | Compose config gate、Pod mount 回读、全量测试 | 恢复任一用户卷 mount 或 host-path consumer 时失败 | Not run |
| 现有实时 Workdir 契约在收敛后不回退 | Stage 5/6 重新引入 revision、hydrate 或双写 | Workdir/file bridge/runtime lifecycle | 既有 4R-B/C E2E 与静态负向 gate | 任一旧 revision/FileStore consumer 恢复时失败 | Not run |

旧能力不存在：Stage 5 完成后个人 Skill 不再从 User Data 兼容目录读取或回退；Stage 6 完成后 API/worker
不再挂载用户 POSIX 卷或最终共享 `saves`，Kubernetes 不再允许以 hostPath/RWO 代替 RWX 契约。

重新引入条件：只有新的授权与生命周期要求明确需要个人 Skill 双源，或部署目标明确放弃跨节点实时共享，
并提供迁移、回滚、并发和真实目标环境证据时，才可重新引入兼容路径或非 RWX 部署模式；它们不得被
默认配置或静默 fallback 恢复。

## 风险

- 个人 Skill 源迁移涉及用户可写历史目录，必须继续使用 no-follow/beneath 快照，不能普通 `copytree`。
- 不同 Kubernetes CSI 对 subPath、RWX、inode 可见性和删除时序的实现不同，spec unit 不能替代目标环境。
- 删除共享 `saves` 可能暴露尚未登记的日志、缓存或模型 consumer；每个 consumer 必须先找到自己的
  语义 Owner，不能整体搬到新的“通用文件服务”。
