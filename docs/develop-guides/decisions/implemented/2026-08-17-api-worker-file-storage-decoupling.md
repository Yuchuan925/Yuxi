# API / Worker 文件存储解耦

状态：implemented
类型：architecture
Owner：docker-compose.yml

日志与缓存路径由 `yuxi.config` 和 `logging_config.py` 拥有。本记录继续拥有 Stage 1/2 的进程、日志和
缓存解耦边界；Stage 3/4 的附件、outputs、Viewer 与 Sandbox 文件协议已经由
[实时 Project Workdir 与独立 Sandbox Runtime](2026-08-18-live-project-workdir-and-runtime.md)
取代，不能把下述历史验证中的 MinIO/revision 模型视为当前事实。

## 问题

默认 Compose 曾让 API、worker 与 sandbox-provisioner 共享宿主机 `saves`，并让业务服务通过宿主机
路径、Docker socket 和隐式共享日志相互依赖。API 与 worker 因而不能独立收紧权限或部署，附件和
outputs 在 Sandbox 重建、父子 Agent 与并发 Run 中也缺少明确的恢复与发布事实。

## 决策

- API 与 worker 不再挂载 `/app/models` 或 Docker socket；只有 Docker sandbox-provisioner 持有
  Docker daemon 权限。测试清理通过 provisioner 的鉴权管理 API 完成。
- API 与 worker 使用独立 `YUXI_RUNTIME_DIR`。日志和 Office 预览缓存位于各自容器本地运行目录，
  不写入共享 `saves`；管理端日志接口只读取 API 进程日志。
- Conversation 附件与 outputs 当前直接使用实时 Project Workdir；MinIO 正式附件、output revision、
  父子 checkpoint/projection/merge 和 scoped hydrate 属于已被后续决定删除的历史 Stage 3/4 实现。
  未确认的临时附件解析仍可使用用户隔离的 MinIO 前缀。
- 用户级 `/home/gem/user-data/workspace` 和按 uid 汇总的授权 Skills 只读投影仍通过共享 `saves`/PVC
  挂入 Sandbox；Sandbox 的 Skills identity/wire 已由
  [2026-08-18 基础决定](2026-08-18-project-workdir-runtime-foundation.md) 接管。附件与 outputs 的旧文件
  scope 仍由本记录描述，API/worker 与 provisioner 的最终共享 `saves` 删除尚未完成。

## 替代方案

- 保留 API/worker Docker socket、models 和共享日志目录：拒绝。没有应用 consumer，权限与部署耦合
  大于收益。
- 附件继续依赖 host uploads：拒绝。MinIO 已拥有正式字节，Sandbox 重建不应要求同一宿主机。
- 把 MinIO 通过 s3fs/ossfs 挂成 POSIX：拒绝。对象存储不拥有 shell 所需的 rename、partial write
  和锁语义，并会扩大凭据边界。
- 整体重放旧 `feat/filestore-decouple`：拒绝。旧实现没有当前 RunAttempt、revision 与确认不明事实，
  且与附件、Viewer 和调度实现冲突。
- 共享 Project RWX POSIX 文件系统：本决定实施时未采用，随后已由 2026-08-18 实时 Workdir 决定
  取代 Stage 3/4 文件模型并删除双重事实源。

## 后果

- 阶段 1 至阶段 3 已减少 API/worker 权限和宿主机耦合，日志/缓存与附件主链路可以独立部署和重建。
- API 日志不是 worker 日志聚合；历史日志留存由容器平台负责。Office 缓存和本地 runtime 可在重建时
  丢失。
- 实时 Project Workdir 已删除每 Run 文件副本、父子 projection/merge 与发布前 404；这些后果由后续
  owning decision 记录。
- 真实 Kubernetes RWX、Skills 兼容路径和最终共享 `saves` 删除仍未完成。

## 验证

- 阶段 1：Compose/cleanup 相关 37 tests、公开 health integration、确定性 Agent E2E 2 tests、backend
  non-slow 1293 passed/22 skipped；真实容器 mounts 证明 API/worker 无 models/socket，provisioner 保留
  socket。工程 gate、Compose config、docs build 和 diff 检查通过。
- 阶段 2：配置、Compose 与 workspace 61 tests；真实日志/Office HTTP integration 2 tests；backend
  non-slow 1295 passed/26 skipped；确定性 Agent E2E 2 tests；Web lint/unit/build 与工程 gate 通过。
  真实登录管理页面截图未执行，结果为 `Not run`。
- 阶段 3：附件/service/provisioner 151 tests；真实 HTTP、PostgreSQL、MinIO、worker 与 Sandbox
  assembled-path E2E 3 tests；backend non-slow 1312 passed/26 skipped。Docker 动态 Sandbox 无 uploads
  mount；Kind/Kubernetes 真实 smoke 为 `Not run`。
- 阶段 4：backend non-slow 1349 passed/26 skipped；真实 PostgreSQL/MinIO/provisioner integration
  5 tests；Viewer HTTP 36 tests；确定性 Agent E2E 3 tests。覆盖 output revision、冲突/合并、legacy
  首次发布、对象恢复、父子双向 producer-consumer、取消/重试隔离、限长与 symlink 边界。
- 阶段 4 的外部模型 subagent 探针两次没有完成要求的完整工具链，因此记录为失败，不替代确定性证据；
  真实 Kubernetes smoke 为 `Not run`。
- 各阶段提交：`681019f1`、`54f75050`、`6a3ac434`、`8a7f2b11`。均经过独立 Reviewer 与用户
  Review；当前分支未 push、未创建 PR，阶段 4 未部署。
