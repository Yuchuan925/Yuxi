# Runtime CI 存储身份与逐 job 构建预算

状态：implemented
类型：bug-fix
Owner：.github/workflows/system-tests.yml

## 问题

Runtime CI 使用全新 bind mount，Docker 创建的目录归 root 所有，UID 1000 的 API 与 worker 无法创建共享 Skill 源目录。冷构建预算检查只匹配任意一个 job，另一 job 缩短或删除预算仍可通过。

## 决策

### 实现方案

两个 Runtime job 在构建镜像后、启动服务前，使用一次性的 root API 容器把三个测试存储根目录设置为 UID/GID 1000、模式 0700。API、worker 和 schema-init 保持原有运行身份与权限。准备只作用于 CI 的隔离目录，无递归迁移或生产目录修改。

`scripts/test_release_workflows.py` 按顶层 job 分别检查至少 60 分钟的预算；负向测试逐个缩短或删除预算。Runtime workflow 以 readiness、HTTP、数据库与 worker 链路形成运行后果。

## 替代方案

- CI 全程使用 root：会跳过 shipping 身份的真实文件权限语义，不采用。
- 恢复运行时 chmod 或把目录设为所有人可写：把部署准备带入业务边界，扩大访问范围，不采用。
- 仅延长一个 job：无法拒绝其他 job 的预算回退，不采用。

## 后果

目录准备命令仅供隔离 CI 环境使用，会修改指定根目录的 Owner 与模式。它不提供已有部署的数据迁移，也不修改生产入口。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| UID 1000 可写三个 CI 存储根目录 | 目录归 root，启动失败 | Runtime workflow 的准备步骤 | 同一 Compose 镜像以 UID 1000 写文件并回读；Runtime CI | 未准备目录时同一写入因 PermissionError 失败 | Passed |
| 每个 Runtime job 预算至少 60 分钟 | 一个正确 job 掩盖另一个短预算 | release workflow 单测 | `python3 -m unittest scripts.test_release_workflows` | 逐个缩短或删除预算 | Passed |
| MCP PR 的真实运行链路通过 | startup gate 掩盖业务回归 | Runtime workflow、MCP service/runtime | 真实 HTTP/PostgreSQL MCP integration；完整 Runtime CI 在 PR 中记录 | 非法配置不落库，stdio 无文件副作用 | Passed |

- 同一 Compose API 镜像以 UID 1000 在未准备的空目录创建 `shared` 因 PermissionError 失败；执行 workflow 的准备命令后，三根目录 Owner/模式均为 1000/0700，独立文件写入与内容回读通过。
- 隔离 Compose 的 API/worker readiness 通过；真实 MCP HTTP、Schema 和 stdio E2E 回归 31 passed。复用现有镜像，GitHub 冷构建与完整 Runtime CI 在 PR 中另记实际结果。
- release workflow 6 项、工程契约和信任 64 项通过；前端 lint、432 项 unit、生产 build 与文档 build 通过。
