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
2. 分离日志与无状态本地缓存，不把它们纳入用户文件抽象。
3. 复用现有 MinIO 附件对象，通过受信任 sandbox 文件 API hydrate uploads，先证明不依赖宿主机路径的真实链路。
4. 依次迁移 outputs/artifacts、只读版本化 Skills、最后迁移跨线程可写 workspace。
5. PostgreSQL 拥有文件 scope/path、revision、删除/重命名与 publish/unknown 事实；MinIO/S3 拥有对象字节；sandbox 只保存可重建 POSIX 工作副本。完成旧数据核对后删除 provisioner host-path 推导和共享 `saves`。

每个阶段是独立 Review 单元，必须拥有当前 consumer、风险相称的 oracle 和能恢复目标缺陷的负向案例。阶段证据完成后先由独立 Reviewer 审查，再等待用户明确 Review；未经确认不进入下一阶段。

## 后果

- 阶段 1 完成后，API/worker 的运行权限和宿主机耦合减少；sandbox-provisioner 继续作为 Docker daemon 的唯一访问边界。
- 集成测试清理仍会在专用测试环境中删除 provisioner 当前管理的全部沙盒，但不再要求被测 API 容器拥有 Docker socket。
- 共享 `saves`、sandbox host-path 推导和用户文件协议保持原状；本阶段不会改善它们的跨宿主机部署能力，后续阶段必须继续处理。
- 每一阶段都可能单独停止、回滚或调整顺序；只有对应证据和用户 Review 通过后才开始下一阶段。

## 替代方案

- 继续共享 bind volume 或把 RWX PVC 作为默认协议：拒绝。它保留跨进程共享文件系统语义，不能满足跨宿主机解耦；PVC 只能作为可选部署优化。
- 在 sandbox 中挂载 s3fs/ossfs：拒绝。对象存储不提供 Agent shell 所依赖的完整 POSIX rename、锁和 partial-write 语义，并会扩大凭据边界。
- 整体重放 `feat/filestore-decouple`：拒绝。该分支与当前配置、附件、文件搜索和 RunAttempt 实现大面积冲突，且同步协议没有持久版本事实。
- 一次迁移所有 `saves` 数据后再切换：拒绝。协议、实现、迁移和故障恢复无法独立验证，失败面过大。
- 先删除无 consumer 挂载，再以已有对象链路验证传输，最后按数据复杂度迁移：采用。它先净删除无用权限和部署耦合，不制造数据迁移风险，并为后续阶段提供可复用的真实边界。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| API/worker 不再拥有无应用 consumer 的 `/app/models` 或 Docker socket 挂载，provisioner 仍保留 Docker socket | 只从一个 Compose 文件删除、误删 provisioner socket、测试清理仍绕过 provisioner | `docker-compose.yml`、`docker-compose.prod.yml`、`backend/test/integration/conftest.py` | `pytest test/unit/config/test_docker_compose_service_boundaries.py test/unit/test_live_api_cleanup.py`；`docker compose config`；真实服务 readiness | 恢复 API models mount、worker socket 或 `--unix-socket` 清理，guard 必须报告；provisioner 非法响应必须使清理失败 | Passed |
| 阶段 1 不修改或迁移任何 `saves` 数据和运行时文件协议 | 顺手删除 saves mount 或引入未被消费的 FileStore | Compose diff、storage/runtime import | `git diff -- docker-compose.yml docker-compose.prod.yml`；`rg` 负向搜索 | 出现 saves mount 删除、FileStore/runtime 新依赖时 Review 拒绝 | Inspected |
| 最终默认生产拓扑中 API/worker 无共享可变数据卷，provisioner 不交换宿主机 saves 路径 | 仍可通过隐式 host path 工作，测试只覆盖单容器 | Compose、provisioner contract | 无共享卷 Docker assembled-path E2E；Docker 与 Kind/Kubernetes smoke | 恢复共享 saves mount 或 host-path 推导时静态 gate/E2E 失败 | Not run |
| 附件、outputs、Skills、workspace 在 sandbox 重建后保持一致 | 只验证 HTTP 200 或日志，没有回读 DB/对象/文件 | 对应 service/repository、文件 revision | PostgreSQL、MinIO、真实 HTTP、worker、sandbox E2E 回读 | 删除 hydrate/publish、相邻线程猜测、回退本地 Path 时失败 | Not run |
| 并发或失联 publish 不静默覆盖新版本 | Redis 锁失效后覆盖、对象成功但 DB 未确认 | 文件 revision/publish transaction | PostgreSQL 并发 integration 与故障注入 E2E | 同 base revision 并发发布、确认前崩溃 | Not run |
| LITE 与显式 SQLite 的本地语义不被分布式存储失败静默替换 | 对象存储初始化失败后悄悄使用 Local，或 LITE 导入知识重依赖 | runtime discovery、storage contract | LITE import/startup unit、Local contract、MinIO integration | S3 失败静默切 Local、LITE 导入知识运行时 | Not run |
| 旧数据迁移可重试且单对象失败不会伪装整批成功 | 部分导入丢文件、重复运行覆盖新版本 | migration command、PostgreSQL/object metadata | 隔离旧 saves fixture，逐对象回读 hash/size | 单对象失败、重复运行、并发冲突 | Not run |

旧能力不存在：阶段 1 完成后，两个 Compose 文件的 API/worker volumes 中不存在 `/app/models` 与 `/var/run/docker.sock`；最终阶段完成后不存在共享 `saves` 与 provisioner host-path 推导。

重新引入条件：只有出现经源码与运行证据确认、无法通过现有 provider/网络边界满足的真实 consumer，并新增对应决策、最小权限设计和负向测试时，才可重新引入宿主机挂载；便利性或旧部署习惯不是充分理由。

## 风险

- 当前 `/app/models` 与 API/worker Docker socket 没有应用源码 consumer；原集成测试存在直连 Docker daemon 的测试专用清理 consumer，本阶段将其迁到 provisioner 管理 API。仓库外私有部署脚本仍可能依赖这些挂载，因此必须通过 Compose 真实重建、readiness 和确定性 Agent E2E 校准。
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
