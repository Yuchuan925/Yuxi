# 模块审查中的失败语义与检索边界

状态：implemented
类型：bug-fix
Owner：backend/yuxi/modules/knowledge/base.py

## 问题

对 `backend/yuxi/modules` 的审查报告混合了真实功能缺陷、已经由当前入口拦截的陈旧判断和结构性债务。复核确认：知识库导出基类会落入空实现；Dify/Notion 外部检索在远端失败时返回空结果；Milvus/Dify 的检索数量上限只停留在 UI 配置；损坏的共享权限配置会让 Agent/Skill 权限遍历抛异常；MCP 服务层缺少与 HTTP 层对称的内置保护；评估 prompt 使用了字面 `\\n`。这些行为分别会伪装失败、放大下游查询、扩大单条坏数据影响面或产生错误模型输入。

复核也确认：workspace 文件服务的列表过滤是“Project 选目录”的展示投影，读写边界是当前用户自己的 UserWorkspace；Notion 文件方法虽然与可选参数接口不一致，但当前 shipping 入口会先拒绝不支持文档操作的外部连接器；权限解析器拒绝 legacy 配置本身是 fail-closed 设计。因此本次不改变这些语义，也不进行 transport/sandbox 下沉、事务约定统一或死代码大清理。

## 决策

基类 `KnowledgeBase.export_data` 显式抛出 `NotImplementedError`，沿用已有路由的 501 映射，避免把未实现能力伪装为普通 500。

Dify 与 Notion 的远端请求失败向上抛出；保留 Dify 的兼容重试，但两次失败后不返回空检索结果。Dify 的返回结果按受保护的最终数量截断。

在实际执行检索前执行与公开配置一致的运行时上界：Dify 最终结果 100；Milvus 最终结果 100、召回 200、BM25 200、图实体/三元组 100、图结果 200、图节点 50000。主检索路径的非法数值显式失败，超出合法范围的数值收敛到安全上限。可选图检索保留现有异常降级：图参数解析或图服务失败时记录错误并返回空图结果，主检索继续返回。

权限解析遇到共享配置解析产生的 `TypeError` 或 `ValueError` 时记录结构化告警并返回 `NONE`，不让单条坏资源中断 Agent/Skill 列表；`normalize_permission_config` 的严格校验契约保持不变。MCP 删除服务在真正删除前拒绝代码管理的内置 slug，HTTP 层现有保护继续保留。评估上下文使用真实换行连接。

### 实现方案

执行入口仍保持 `router → knowledge manager → concrete knowledge backend`，边界校验在 concrete backend 的 `aquery` 内完成；故障通过异常返回到已有 API 错误映射，PostgreSQL、Redis 和外部对象不新增状态。权限 guard 位于共享 resolver，所有 Agent/Skill repository 复用同一 fail-closed 结果。MCP guard 位于 service owner，不能只依赖 HTTP route。

## 替代方案

- **实现完整知识库导出**：需要定义各 backend 的格式、向量和文件语义，超出本次审查修复；选择明确返回 501。
- **继续把外部失败转换为空列表**：会让“服务不可用”和“没有命中”不可区分，违反显式失败约定，不采用。
- **只修改 UI 的 max 字段**：无法约束 API、Agent 或持久化历史配置，不采用。
- **在每个 Agent/Skill 调用方分别捕获权限异常**：会产生遗漏和不同安全语义，选择在共享 resolver 统一收口。
- **立即重构模块依赖和工作区授权模型**：需要独立架构决策与真实链路验证，本次不纳入。

## 后果

外部连接器故障从空结果变为请求失败，调用方会显示错误而不是继续生成答案；这是有意的可观察行为变化。上界会截断超过 UI 声明范围的调用参数，但保护 Milvus/Dify 资源。Malformed 权限数据会使对应资源对所有非 superadmin 用户不可见，直到配置被修复；这是 fail-closed 后果。完整知识库导出仍未实现，接口明确返回 501。

## 验证

| 验收主张 | 直接证据 | 结果 |
|---|---|---|
| 未实现导出显式失败 | `test_base_export_is_explicitly_unsupported` | Passed |
| Dify 运行时配置完整性 | `test_dify_kb_aquery_rejects_incomplete_persisted_config`：三个必填字段逐项缺失 | Passed |
| Dify/Notion 远端故障不再伪装为空结果 | 两个 connector error regression tests | Passed |
| Dify/Milvus 检索有运行时上界 | Dify 正常/兼容重试最终结果截断；Milvus vector/keyword/hybrid 召回与最终结果；图实体、三元组、PPR 结果及节点边界测试 | Passed |
| 损坏权限配置不授予权限且不抛出 | `test_invalid_resource_permission_config_denies_access`：legacy、空部门成员、非列表部门/用户成员及非字符串访问级别 | Passed |
| 内置 MCP 不能经 service 删除 | `test_delete_builtin_mcp_server_rejects_service_call` | Passed |
| 评估 prompt 使用真实换行 | evaluator prompt regression assertion | Passed |
| 相关 unit 回归 | `docker compose run --rm --no-deps -e PYTEST_ADDOPTS='-p no:cacheprovider' api uv run --no-sync pytest test/unit -m 'not slow' -q` | Passed: 2462 passed, 55 skipped |
| 负向测试可信度 | 在独立测试进程中逐项将七个 Milvus 上限提高至 1000000，并撤销 Dify 返回截断，运行对应边界测试 | Passed: 八种上限/截断变异均因断言失败被检出；恢复 Dify 配置缺失时返回空结果后三项回归测试失败 |
| 文档构建与相对链接 | `cd docs && pnpm run build` | Passed |
| Dify 追加回归 | `docker compose run --rm --no-deps -e PYTEST_ADDOPTS='-p no:cacheprovider' api uv run --no-sync pytest test/unit/plugins/test_dify_kb.py -q` | Passed: 10 passed（包含全集运行后追加的三个缺字段案例） |
| 工程契约 | `python3 scripts/verify_engineering_contracts.py`；`python3 -m unittest scripts.test_verify_engineering_contracts` | Passed: 64 tests |
| 代码风格 | `uv tool run --offline ruff check` 与 `uv tool run --offline ruff format --check`（本次涉及源码和测试） | Passed |

真实 Dify、Notion、Milvus provider、HTTP integration 和 PostgreSQL 场景未在本次工作树中单独运行；unit 测试使用确定性 fake/replay 覆盖失败与边界语义。

权限回归测试在修复异常捕获前有 4 项因 `TypeError` 失败，修复后通过。容器镜像没有 Ruff 可执行文件，风格检查通过本机已缓存的 Ruff 运行。当前长驻 API 挂载另一工作树，本工作树使用 Compose 一次性容器运行 unit；未用另一分支的 HTTP 结果作为本分支证据。
