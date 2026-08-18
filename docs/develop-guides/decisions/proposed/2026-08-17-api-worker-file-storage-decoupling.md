# API / Worker 文件存储解耦

状态：proposed
类型：architecture
Owner：docker-compose.yml

测试清理语义由 `backend/test/live_api_cleanup.py` 拥有；决策记录只描述两个 Owner 的边界，不替代运行时或测试实现。

## 问题

默认 Compose 让 API、worker 与 sandbox-provisioner 共享宿主机 `saves`，业务服务和 provisioner 还把宿主机路径当作文件可见性协议。API 与 worker 因而不能在不同宿主机独立运行和扩容，sandbox 文件发布也缺少可持久回读的版本与失败事实。

当前系统已经把 Request、Run、Message 和 checkpoint 的事实收敛到 PostgreSQL，并把正式聊天附件字节保存到 MinIO；剩余 workspace、outputs、Skills、viewer 和 sandbox bind mount 仍依赖共享 POSIX 路径。旧 `feat/filestore-decouple` 分支没有持久化 base revision 与 publish 结果，不能作为当前实现直接重放。

## 提案

按无需迁移到需要迁移的顺序逐步删除共享文件系统协议：

1. 先删除没有应用 runtime consumer 的 API/worker `/app/models` 与 Docker socket 挂载；Docker sandbox-provisioner 保留 Docker socket。集成测试的沙盒清理由直连 Docker daemon 改为调用 provisioner 的鉴权管理 API。该阶段不修改或迁移 `saves`。
2. 以 `YUXI_RUNTIME_DIR` 为进程本地运行目录，API 与 worker 使用不同的容器本地路径；日志和已确认可丢弃的 Office 预览缓存写入该目录，不把它们纳入用户文件抽象。`GET /api/system/logs` 明确只读取 API 进程日志，worker 日志继续由容器日志和 worker 自身运行目录观察；历史日志与预览缓存不迁移。
3. 复用现有 MinIO 附件对象，通过受信任 sandbox 文件 API hydrate uploads，并从 Docker/Kind sandbox 中移除 uploads 的 bind/PVC 子目录挂载。worker 在模型执行前按 Conversation 当前附件集合清空并重建 sandbox uploads，Agent 文件后端对该目录保持只读；MinIO 读取、sandbox 清理或写入任一步失败都阻止执行，不能继续使用上一次残留。API/Viewer 的本地附件物化暂保留，不在本阶段删除共享 `saves`。
4. 当 outputs 作为第二种文件语义进入时，先抽取最小 scoped FileStore 边界，共用 scope/path/object/snapshot 解析与 sandbox hydrate，再实现 outputs/artifacts 的 revision/publish；之后迁移只读版本化 Skills，最后迁移跨线程可写 workspace。该边界不把 MinIO 伪装成 POSIX，uploads、outputs、Skills 和 workspace 仍保留各自 Owner 与写入/冲突语义。
5. PostgreSQL 拥有文件 scope/path、revision、删除/重命名与 publish/unknown 事实；MinIO/S3 拥有对象字节；sandbox 只保存可重建 POSIX 工作副本。完成旧数据核对后删除 provisioner host-path 推导和共享 `saves`。

每个阶段是独立 Review 单元，必须拥有当前 consumer、风险相称的 oracle 和能恢复目标缺陷的负向案例。阶段证据完成后先由独立 Reviewer 审查，再等待用户明确 Review；未经确认不进入下一阶段。

## 后果

- 阶段 1 完成后，API/worker 的运行权限和宿主机耦合减少；sandbox-provisioner 继续作为 Docker daemon 的唯一访问边界。
- 集成测试清理仍会在专用测试环境中删除 provisioner 当前管理的全部沙盒，但不再要求被测 API 容器拥有 Docker socket。
- 阶段 2 完成后，API 与 worker 不再向共享 `saves/logs` 追加同一日志文件，Office 预览缓存也不再写入用户数据目录；容器重建后这些可重建数据允许丢失。
- 管理端日志接口不再隐式聚合 worker 日志；它返回 `scope: api`，前端明确标注“API 进程日志”。worker 的历史日志留存由部署环境的容器日志策略负责。
- 阶段 3 完成后，Agent 执行读取附件不再依赖 provisioner 宿主机 uploads 路径或 Kubernetes 共享 PVC 子目录；sandbox 重建后由 worker 从 MinIO 重新 hydrate。Agent 通用文件后端仍拒绝写 uploads，受信任替换接口只接受该虚拟根目录下的路径，并在部分失败后再次清空以 fail-closed。
- 阶段 4 完成后，outputs 由 PostgreSQL revision/current pointer 与 MinIO 不可变对象共同拥有；每个 Run 使用独立 sandbox instance，开始前从指定版本完整重建，结束时先记录 staging 并逐文件上传，随后与输出 Message/Run 终态在同一事务内条件发布。Viewer 与 artifact 读取当前版本，删除发布只复用对象的新 manifest；冲突、失败与确认不明 revision 不会成为当前版本。
- 父 Run 调用子智能体前只创建不推进 current 的私有 checkpoint，子 Run 据此重建自己的 sandbox。子 Run 终态保留完整 checkpoint 供父 Run 合并，同时只把相对父 checkpoint 的子改动投影为公开 revision；父 Run 通过 `task/status/await` 消费完成结果时串行合并并重建自己的运行副本，仍由父 Run 最终事务发布其完整结果。被拒绝的 continuation/busy 启动会回收尚未引用的 checkpoint 与对象。
- uploads/outputs 共用的 scoped FileStore 只负责虚拟 scope/path、对象描述符、流式 hydrate 与取消边界，不提供通用 POSIX API，也不接管 workspace 或 Skills 的 Owner。workspace、Skills、历史本地文件和最终共享 `saves` 删除仍留待后续阶段。
- 每一阶段都可能单独停止、回滚或调整顺序；只有对应证据和用户 Review 通过后才开始下一阶段。

## 替代方案

- 继续共享 bind volume 或把 RWX PVC 作为默认协议：拒绝。它保留跨进程共享文件系统语义，不能满足跨宿主机解耦；PVC 只能作为可选部署优化。
- 在 sandbox 中挂载 s3fs/ossfs：拒绝。对象存储不提供 Agent shell 所依赖的完整 POSIX rename、锁和 partial-write 语义，并会扩大凭据边界。
- 一次性把 MinIO 包装成完整 thread 文件系统：拒绝。workspace 实际属于用户跨线程共享作用域，outputs 还需要 revision/publish 事实；用同一层目录 API 抹平它们会隐藏冲突与部分失败。第二个 consumer 出现时抽取最小 scoped FileStore，不在只有附件读取时预造完整文件系统。
- 整体重放 `feat/filestore-decouple`：拒绝。该分支与当前配置、附件、文件搜索和 RunAttempt 实现大面积冲突，且同步协议没有持久版本事实。
- 一次迁移所有 `saves` 数据后再切换：拒绝。协议、实现、迁移和故障恢复无法独立验证，失败面过大。
- 先删除无 consumer 挂载，再以已有对象链路验证传输，最后按数据复杂度迁移：采用。它先净删除无用权限和部署耦合，不制造数据迁移风险，并为后续阶段提供可复用的真实边界。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| API/worker 不再拥有无应用 consumer 的 `/app/models` 或 Docker socket 挂载，provisioner 仍保留 Docker socket | 只从一个 Compose 文件删除、误删 provisioner socket、测试清理仍绕过 provisioner | `docker-compose.yml`、`docker-compose.prod.yml`、`backend/test/integration/conftest.py` | `pytest test/unit/config/test_docker_compose_service_boundaries.py test/unit/test_live_api_cleanup.py`；`docker compose config`；真实服务 readiness | 恢复 API models mount、worker socket 或 `--unix-socket` 清理，guard 必须报告；provisioner 非法响应必须使清理失败 | Passed |
| 阶段 1 不修改或迁移任何 `saves` 数据和运行时文件协议 | 顺手删除 saves mount 或引入未被消费的 FileStore | Compose diff、storage/runtime import | `git diff -- docker-compose.yml docker-compose.prod.yml`；`rg` 负向搜索 | 出现 saves mount 删除、FileStore/runtime 新依赖时 Review 拒绝 | Inspected |
| API/worker 日志与 Office 预览缓存使用彼此独立且位于 `saves` 外的运行目录 | 两个服务仍写同一文件、默认目录回落到 `saves`、预览缓存仍污染用户数据 | `yuxi.config.get_runtime_dir`、两个 Compose service environment、`logging_config.py`、`workspace_service.py` | config/workspace unit；Compose 边界 guard；真实容器分别写入标记并从 API 日志接口回读 | 把任一 `YUXI_RUNTIME_DIR` 恢复为 `/app/saves` 子目录，或恢复 `.office_preview_cache` 时失败 | Passed |
| 日志接口明确且只返回 API 进程日志 | worker 或旧共享日志标记被接口返回、响应路径回退到 `saves` | `GET /api/system/logs`、`logging_config.py` | 真实 HTTP integration；返回 `scope: api`，API 标记存在，worker sibling 与旧共享日志标记不存在 | 恢复 `saves/logs`、扫描 worker runtime 或旧共享日志时失败 | Passed |
| 管理端明确标注 API 进程日志 | UI 继续把当前 API 文件误称为全系统日志 | `DebugComponent.vue` | Web lint/unit/build 与真实登录页面截图 | 移除界面标注时 Review 拒绝 | Not run |
| worker 从 MinIO 当前附件集合直接重建 sandbox uploads，且 sandbox 不挂载共享 uploads | 仍先写 host uploads、旧附件残留、部分 hydrate 后继续执行、Docker 或 Kind 仍挂载 uploads | `attachment_service.py`、`ProvisionerSandboxBackend`、`docker/sandbox_provisioner/app.py` | service/backend unit；真实 MinIO、HTTP、worker、sandbox 文件回读与 sandbox 重建 E2E | 恢复 `materialize_attachment_records` worker 调用、恢复 uploads bind/PVC、让清理/写入失败后继续时失败 | Passed |
| outputs 从当前 revision 重建并在 Run 终态事务内发布，Viewer/artifact 不依赖本地文件 | staging 上传成功却没有可审计事实、旧版本覆盖新版本、Viewer 仍读取 host Path、sandbox 重建丢产物 | `thread_output_revisions`、Conversation 当前指针、`thread_output_service.py`、Viewer/artifact service | repository/service unit；真实 PostgreSQL/MinIO/HTTP；worker/sandbox E2E | 同 base 对同一路径写入不同内容时只能一个成功，不同路径或未变路径必须三方合并；对象校验失败、实际传输超限、sandbox 中途消失、恢复本地 outputs mount 时失败 | Passed |
| 最终默认生产拓扑中 API/worker 无共享可变数据卷，provisioner 不交换宿主机 saves 路径 | 仍可通过隐式 host path 工作，测试只覆盖单容器 | Compose、provisioner contract | 无共享卷 Docker assembled-path E2E；Docker 与 Kind/Kubernetes smoke | 恢复共享 saves mount 或 host-path 推导时静态 gate/E2E 失败 | Not run |
| 附件、outputs、Skills、workspace 在 sandbox 重建后保持一致 | 只验证 HTTP 200 或日志，没有回读 DB/对象/文件 | 对应 service/repository、文件 revision | PostgreSQL、MinIO、真实 HTTP、worker、sandbox E2E 回读 | 删除 hydrate/publish、相邻线程猜测、回退本地 Path 时失败 | Not run |
| 并发或失联 publish 不静默覆盖新版本 | Redis 锁失效后覆盖、对象成功但 DB 未确认 | 文件 revision/publish transaction | PostgreSQL 并发 integration 与故障注入 E2E | 同 base revision 并发发布、确认前崩溃 | Not run |
| LITE 与显式 SQLite 的本地语义不被分布式存储失败静默替换 | 对象存储初始化失败后悄悄使用 Local，或 LITE 导入知识重依赖 | runtime discovery、storage contract | LITE import/startup unit、Local contract、MinIO integration | S3 失败静默切 Local、LITE 导入知识运行时 | Not run |
| 旧数据迁移可重试且单对象失败不会伪装整批成功 | 部分导入丢文件、重复运行覆盖新版本 | migration command、PostgreSQL/object metadata | 隔离旧 saves fixture，逐对象回读 hash/size | 单对象失败、重复运行、并发冲突 | Not run |

旧能力不存在：阶段 1 完成后，两个 Compose 文件的 API/worker volumes 中不存在 `/app/models` 与 `/var/run/docker.sock`；最终阶段完成后不存在共享 `saves` 与 provisioner host-path 推导。

重新引入条件：只有出现经源码与运行证据确认、无法通过现有 provider/网络边界满足的真实 consumer，并新增对应决策、最小权限设计和负向测试时，才可重新引入宿主机挂载；便利性或旧部署习惯不是充分理由。

## 风险

- 当前 `/app/models` 与 API/worker Docker socket 没有应用源码 consumer；原集成测试存在直连 Docker daemon 的测试专用清理 consumer，本阶段将其迁到 provisioner 管理 API。仓库外私有部署脚本仍可能依赖这些挂载，因此必须通过 Compose 真实重建、readiness 和确定性 Agent E2E 校准。
- `GET /api/system/logs` 过去因为共享日志文件而偶然看到 worker 输出，并没有稳定的跨进程聚合契约；阶段 2 将它收紧为 API 进程日志。依赖聚合日志的部署应使用 Docker 或平台日志采集，而不是重新共享应用文件。
- 日志、显式 SQLite、知识库本地目录和用户文件的生命周期不同；如果过早统一抽象，会形成新的万能存储层和隐式 fallback。
- workspace 跨线程可写，shell 能产生 rename/delete，必须在后续阶段先确定 revision 与 publish transaction，不能只依赖 Redis 锁或对象列表。
- 旧附件和线程文件仍是持久 consumer；最终删除 `saves` 前必须完成可重试迁移、逐对象核对和失败可观察性。

## 阶段 1 验证记录

- `docker compose run --rm --no-deps -v "$PWD:/workspace:ro" -e YUXI_PROJECT_ROOT=/workspace api uv run --group test pytest test/unit/config/test_docker_compose_checkpointer.py test/unit/config/test_docker_compose_service_boundaries.py test/unit/test_live_api_cleanup.py -q`：37 passed；覆盖恢复 API/worker 挂载与环境变量、恢复 Docker socket 直连、鉴权头、非法 provisioner 响应，以及 DELETE 返回 200 但回读仍残留。
- 开发与生产 Compose `config --quiet`：Passed；生产配置使用临时占位环境变量解析，未启动生产服务。
- 重建 API/worker 后读取容器 mounts：API/worker 均无 `/app/models` 和 `/var/run/docker.sock`，provisioner 保留 socket；三个服务均 healthy，`/api/system/ready` 返回 ready 且无 degraded。
- `pytest test/integration/api/test_system_router_api.py::test_health_endpoint_is_public -q -s`：1 passed；session 清理通过 provisioner API 执行。
- 首次确定性 E2E 因项目规定的 replay 服务未启动而 2 failed（连接拒绝/模型连接错误）；按 CI workflow 启动 replay 后原命令重跑：2 passed。
- `pytest test/unit -m "not slow" -q`：1293 passed，22 skipped；Compose 文件因常驻 API 容器未挂载仓库根目录而跳过，但已由上面的只读根目录 one-off Compose 测试真实覆盖。
- `python3 scripts/verify_engineering_contracts.py` 与 `python3 -m unittest -q scripts.test_verify_engineering_contracts`：Passed，48 tests。
- `cd docs && pnpm run build`：Passed；只有既有 Rolldown、env lexer、chunk 与 esbuild deprecation warnings。
- changed-file Ruff format/check 与 `git diff --check`：Passed。

## 阶段 2 验证记录

- config、Compose 边界与 workspace unit：61 passed；覆盖显式/默认运行目录、dev/prod API/worker 独立路径、把任一服务恢复到 `/app/saves/runtime` 的负控、Office 缓存命中与源文件失效后重建，并证明旧 `.office_preview_cache` 不产生写入。
- 真实 HTTP integration：管理员日志接口 1 passed，断言日志文件位于 API runtime、返回 `scope: api`、API 标记可见且 worker sibling 标记不可见；真实 DOCX 上传和两次预览 1 passed，返回有效 PDF，只在 API runtime 产生新缓存，旧缓存目录集合及文件快照均不变化。
- 重建 API/worker 后两个容器均 healthy；分别通过真实 logger 写入标记，两个 runtime 文件互斥且共享 `saves/logs` 无新标记。真实 HTTP 回读为 `scope=api`、API 标记存在、worker 标记不存在；容器 restart 后隔离与文件均保持。
- `pytest test/unit -m "not slow" -q`：1295 passed，26 skipped；依赖仓库根目录的 Compose 测试已由只读根目录 one-off 命令单独真实执行。
- 确定性 Agent assembled-path E2E：2 passed；Web lint、36 个 unit 与生产 build 均通过；工程契约 gate 与配套 48 tests、dev/prod Compose config、changed-file Ruff、docs build、`git diff --check` 均通过。
- `DebugComponent.vue` 已标注“API 进程日志”，lint/unit/build 已覆盖编译与回归；真实登录页面的截图验证尚未完成，因此管理端页面验收当前结果仍为 `Not run`，不能写成页面验证通过。
- 2026-08-17 用户 Review 接受容器本地日志可随容器重建丢失、worker 历史日志由部署环境管理的当前语义，并明确要求提交阶段 2、开始阶段 3。真实页面截图仍作为未验证范围保留，不改写为通过。

## 阶段 3 验证记录

- 定向 service/backend/provisioner/chat/conversation unit：151 passed。覆盖 MinIO 原件与 Markdown 逐个下载/写入、无附件时清空、两种真实历史本地 fallback、对象或历史文件缺失时阻止执行、固定 bucket 与 thread/file 对象前缀、创建对话时禁止注入附件保留字段、legacy 符号链接拒绝、producer/consumer 文件名规范化、受信任虚拟路径限制、取消后等待阻塞写完成再清空、失败后再次清空、主 Run 剔除配置伪造文件 scope、子智能体读取授权父对话附件，以及 hydrate 失败后模型流入口未被调用。
- 真实确定性 Agent E2E：3 passed。新增链路通过真实 HTTP 上传、PostgreSQL/Conversation 附件事实、MinIO 对象、worker 执行和 sandbox 文件 API 回读原字节；确认宿主机 uploads 未生成文件，sandbox 删除并重建后同一线程可重新 hydrate，从 Conversation 删除附件后下次执行不再保留旧文件。
- 真实 Docker sandbox mount 回读：动态容器只挂载 workspace、outputs 和只读 skills，不存在 `/home/gem/user-data/uploads` mount。dev/prod Compose 展开配置均不含 sandbox uploads 挂载；prod 首次因本机缺少 `.env.prod` 必填值未展开，后用 `config --no-interpolate` 完成结构检查。
- 最终全量 backend non-slow unit：1312 passed、26 skipped。较早一次全量运行暴露 chat unit 脚手架在默认情况下误调真实 provisioner，导致顺序相关失败与挂起；改为默认 no-op、仅专项测试注入 hydrate 后，单文件通过，之后每轮安全/兼容修复后的全量重跑均通过。
- 工程契约检查与配套 48 tests、changed-file Ruff check/format、`git diff --check`、docs build、health/readiness 均通过。docs build 仅有既有 Rolldown、env lexer、chunk 与 esbuild deprecation warnings。
- 本地未提供可用 Kind/Kubernetes 集群，因此本阶段完成 Kubernetes pod spec/mount 负控 unit，但真实 Kubernetes smoke 仍为 `Not run`。
- 独立 Reviewer 首轮发现 3 个 P1 和 1 个 P2：附件 metadata 可注入跨作用域 MinIO 对象、legacy fallback 跟随符号链接、取消后后台线程可回写 stale 附件，以及整线程附件字节聚合的内存风险。修复后 freshness 复核又校正了真实历史 direct-upload 路径、旧文件名规范化和主 Agent 配置 scope。最终复核结论为所有 finding 均按正确原因关闭，无新 P0–P3 finding，patch 正确且无阻塞。

## 阶段 4 验证记录

- 完整 backend non-slow unit：1349 passed、26 skipped；定向 repository/scoped store/sandbox/chat/thread-files/worker/tool/subagent 主集合与随后新增的 checkpoint 投影、并行同步、幂等重放和拒绝启动回收负控均通过。覆盖虚拟 scope 越界、legacy outputs 目录 fd 遍历与 symlink 拒绝、损坏 current pointer fail-closed、文件数/字节限制、取消边界、uploads/outputs 整份重放、发布 commit 确认不明、同路径冲突、不同路径三方合并、父子私有 checkpoint/公开投影、并行父同步、递归 thread-files，以及 Message/revision/interrupted 在一次 commit 前完成。
- 真实 PostgreSQL/MinIO/provisioner outputs integration：5 passed。同 base 同路径冲突只有一个事务成功；父长 session 发布会保留并发子文件；动态 sandbox 验证实际传输上限、逐文件 MinIO 发布与 hash/size 恢复；历史宿主 outputs 会在无 current 的首次 Run 回放、发布后再从对象恢复；被拒绝的子启动 checkpoint 会同时删除 PostgreSQL revision 与 MinIO 对象；确定性父私有文件 → 子读取/写入 → 子投影 → 父合并 → `present_artifacts` 文件存在校验通过。真实 Message、空 revision 与 `AgentRun=interrupted` 在同一 PostgreSQL transaction 提交。Viewer HTTP 36 passed，覆盖本地 outputs 不存在时的树、预览、流式下载、artifact、跨用户拒绝和以新 manifest 删除。
- 父/子智能体外部模型 E2E 的一次运行完成父写入、子真实 `read_file`/写入和父真实读取，但模型未按提示调用 `present_artifacts`；第二次模型没有执行完整子任务。因此这两次外部模型结果均记录为失败，不用它们替代上述确定性 producer-consumer integration。
- 真实确定性 Agent assembled-path E2E：3 passed。Run 完成行、输出 Message、request 因果与 Conversation 当前 outputs revision 在 PostgreSQL 中一致；sandbox 释放重建、附件删除后的下次执行及完整文件 scope 重放均通过。动态 Docker 容器 mount 回读只包含用户 workspace 与只读 skills，不存在 uploads 或 outputs mount。
- 修复审查 finding 后的完整 backend non-slow unit 单命令：1330 passed、26 skipped，两个独立进程 import 边界用例本轮也在原命令内通过。此前两次运行曾分别因 1/2 个固定 30 秒 subprocess timeout 未得到单命令全绿，失败用例独立通过；保留该历史环境时序事实，不用本轮结果抹去。
- 工程契约检查与配套 48 tests、changed-file Ruff check/format、`git diff --check`、dev/prod Compose config、docs build、health/readiness 均通过；docs build 只有既有 Rolldown、env lexer、chunk 与 esbuild deprecation warnings。本机没有可用 Kind/Kubernetes 集群，Kubernetes pod spec/mount 负控 unit 已通过，真实 Kubernetes smoke 仍为 `Not run`。
