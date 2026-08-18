# API / Worker 文件存储解耦

状态：implemented
类型：architecture
Owner：docker-compose.yml

日志与缓存路径由 `yuxi.config` 和 `logging_config.py` 拥有；附件与 outputs 的运行时文件语义分别由
`attachment_service.py`、`thread_output_service.py`、对应 repository 和 provisioner 拥有。本记录保存
当前开发分支已经实现的边界，不替代这些代码事实。

该决定的阶段 4 尚未部署，并计划由
[Project Workdir 与独立 Sandbox Runtime](../proposed/2026-08-18-project-workdir-and-sandbox-runtime.md)
取代。在替代实现通过验证前，本记录仍描述当前分支行为；新提案不构成当前运行时事实。

## 问题

默认 Compose 曾让 API、worker 与 sandbox-provisioner 共享宿主机 `saves`，并让业务服务通过宿主机
路径、Docker socket 和隐式共享日志相互依赖。API 与 worker 因而不能独立收紧权限或部署，附件和
outputs 在 Sandbox 重建、父子 Agent 与并发 Run 中也缺少明确的恢复与发布事实。

## 决策

- API 与 worker 不再挂载 `/app/models` 或 Docker socket；只有 Docker sandbox-provisioner 持有
  Docker daemon 权限。测试清理通过 provisioner 的鉴权管理 API 完成。
- API 与 worker 使用独立 `YUXI_RUNTIME_DIR`。日志和 Office 预览缓存位于各自容器本地运行目录，
  不写入共享 `saves`；管理端日志接口只读取 API 进程日志。
- Conversation 附件字节以 MinIO 对象为正式来源。worker 在 Agent 执行前校验固定对象作用域并通过
  受信任 Sandbox 文件 API 重建 uploads；Docker/Kubernetes 不挂载 uploads。旧本地附件只通过
  不跟随 symlink 的兼容读取进入该链路，失败时阻止执行。
- outputs 由 PostgreSQL `ThreadOutputRevision` 和 Conversation current pointer 记录发布事实，MinIO
  保存不可变对象。每个 Run 使用独立 Sandbox instance，从 base revision 重建 outputs，并在 Run
  终态事务中发布；Viewer、artifact 和删除读取已发布 revision。
- 父 Run 通过私有 checkpoint、子完整 checkpoint 与公开 delta projection 和串行三方合并交换
  outputs；冲突、取消、重试、Sandbox 消失和对象确认不明都 fail-closed，不静默推进 current。
- uploads/outputs 共用的 scoped file primitives 负责授权路径、限长流式传输、hash/size、连接释放、
  symlink 防护和取消边界，但不提供通用 POSIX 文件系统。
- 用户级 `/home/gem/user-data/workspace` 和线程 Skills 投影仍通过共享 `saves`/PVC 挂入 Sandbox；
  API/worker 与 provisioner 的最终共享 `saves` 删除尚未完成。

## 替代方案

- 保留 API/worker Docker socket、models 和共享日志目录：拒绝。没有应用 consumer，权限与部署耦合
  大于收益。
- 附件继续依赖 host uploads：拒绝。MinIO 已拥有正式字节，Sandbox 重建不应要求同一宿主机。
- 把 MinIO 通过 s3fs/ossfs 挂成 POSIX：拒绝。对象存储不拥有 shell 所需的 rename、partial write
  和锁语义，并会扩大凭据边界。
- 整体重放旧 `feat/filestore-decouple`：拒绝。旧实现没有当前 RunAttempt、revision 与确认不明事实，
  且与附件、Viewer 和调度实现冲突。
- 共享 Project RWX POSIX 文件系统：在本决定实施时未采用；实时协作需求确认后由 2026-08-18 新提案
  重新裁决。该提案必须先删除本记录 Stage 4 的双重事实源，不能并行叠加。

## 后果

- 阶段 1 至阶段 3 已减少 API/worker 权限和宿主机耦合，日志/缓存与附件主链路可以独立部署和重建。
- API 日志不是 worker 日志聚合；历史日志留存由容器平台负责。Office 缓存和本地 runtime 可在重建时
  丢失。
- 当前 outputs 只有终态 revision 可被 Viewer 读取，流式 artifact 路径可能在 revision 发布前短暂
  返回 404；这是当前 Stage 4 模型的直接后果。
- 每 Run Sandbox 与父子文件复制、projection 和 merge 增加了状态、对象和故障面。当前实现有对应
  安全边界和证据，但尚未部署，因此可以在形成兼容承诺前被新实时 Workdir 模型删除。
- 真实 Kubernetes RWX、最终旧数据迁移、Skills 与共享 `saves` 删除仍未完成。

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
