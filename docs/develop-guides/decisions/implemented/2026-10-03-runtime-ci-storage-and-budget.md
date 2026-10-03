# Runtime CI 存储身份、ORM 装配与逐 job 构建预算

状态：implemented
类型：bug-fix
Owner：.github/workflows/system-tests.yml

## 问题

Runtime CI 使用全新 bind mount，Docker 创建的目录归 root 所有，UID 1000 的 API 与 worker 无法创建共享 Skill 源目录。冷构建预算检查只匹配任意一个 job，另一 job 缩短或删除预算仍可通过。

独立执行 Durable Task integration selector 时，部分 ORM 关系指向尚未导入的类，查询无法配置 mapper。完整测试收集时其他文件的导入会掩盖该缺陷。

## 决策

### 实现方案

两个 Runtime job 在构建镜像后、启动服务前，使用一次性的 root API 容器把三个测试存储根目录设置为 UID/GID 1000、模式 0700。API、worker 和 schema-init 保持原有运行身份与权限。准备只作用于 CI 的隔离目录，无递归迁移或生产目录修改。

`scripts/test_release_workflows.py` 按顶层 job 分别检查至少 60 分钟的预算；负向测试逐个缩短或删除预算。Runtime workflow 以 readiness、HTTP、数据库与 worker 链路形成运行后果。

`backend/test/integration/conftest.py` 在 session fixture 中调用现有 `bootstrap.models.load_models`，为独立测试进程显式注册全部 ORM；fixture 不连接服务、不建表，实际 Schema 与连接仍由原 fixture 和迁移入口拥有。单个文件和完整 integration 使用相同的模型装配。

## 替代方案

- CI 全程使用 root：会跳过 shipping 身份的真实文件权限语义，不采用。
- 恢复运行时 chmod 或把目录设为所有人可写：把部署准备带入业务边界，扩大访问范围，不采用。
- 仅延长一个 job：无法拒绝其他 job 的预算回退，不采用。
- 在 Durable Task 测试中补一个 `AgentEnv` import：只覆盖当前首个错误关系，仍依赖导入顺序；选择现有完整模型装配入口。

## 后果

目录准备命令仅供隔离 CI 环境使用，会修改指定根目录的 Owner 与模式。它不提供已有部署的数据迁移，也不修改生产入口。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| UID 1000 可写三个 CI 存储根目录 | 目录归 root，启动失败 | Runtime workflow 的准备步骤 | 同一 Compose 镜像以 UID 1000 写文件并回读；Runtime CI | 未准备目录时同一写入因 PermissionError 失败 | Passed |
| 每个 Runtime job 预算至少 60 分钟 | 一个正确 job 掩盖另一个短预算 | release workflow 单测 | `python3 -m unittest scripts.test_release_workflows` | 逐个缩短或删除预算 | Passed |
| Durable Task selector 独立执行可配置 ORM | 缺少关系目标模型 | integration session fixture、bootstrap models | 独立进程执行 `pytest test/integration/services/test_durable_task_repository.py` | 未装配时16项因 AgentEnv mapper 缺失失败 | Passed |
| MCP PR 的真实运行链路通过 | startup gate 掩盖业务回归 | Runtime workflow、MCP service/runtime | 真实 HTTP/PostgreSQL MCP integration；完整 Runtime CI 在 PR 中记录 | 非法配置不落库，stdio 无文件副作用 | Passed |

- 同一 Compose API 镜像以 UID 1000 在未准备的空目录创建 `shared` 因 PermissionError 失败；执行 workflow 的准备命令后，三根目录 Owner/模式均为 1000/0700，独立文件写入与内容回读通过。
- 隔离 Compose 的 API/worker readiness 通过；真实 MCP HTTP、Schema 和 stdio E2E 回归 31 passed。复用现有镜像，GitHub 冷构建与完整 Runtime CI 在 PR 中另记实际结果。
- release workflow 6 项、工程契约和信任 64 项通过；前端 lint、432 项 unit、生产 build 与文档 build 通过。

- 同一独立 `uv run --no-sync --no-dev pytest test/integration/services/test_durable_task_repository.py -q --tb=short` 进程：缺少模型装配时16 failed/1 passed，加入完整模型装配后17 passed；原有测试回读 PostgreSQL 的 claim、lease 与终态，不以 mapper 配置完成替代行为证据。

- 独立 Durable Task worker 链路：停止隔离 API/worker 后，`test_prepare_task_with_failed_initial_arq_publication` 1 passed；重新启动真实 worker 后，`test_shipping_worker_startup_recovers_pending_publication` 与 `test_shipping_worker_failure_runs_domain_hook` 2 passed，回读 Task 与领域终态。
