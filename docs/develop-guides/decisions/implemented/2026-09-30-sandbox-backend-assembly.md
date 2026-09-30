# 沙盒后端直接装配与显式运行作用域

状态：implemented
类型：simplification
Owner：backend/yuxi/modules/agents/runtime/sandbox/backend.py

## 问题

CompositeBackend 装配只有一个沙盒默认后端和产物根目录，却经由独立文件、一次性 scope 类和多来源字段解析完成。已准备的 Context 是唯一生产输入，缺失 runtime scope 时回退到 checkpoint thread 会掩盖执行身份准备错误，并使子 Agent 脱离父级沙盒。

## 决策

### 实现方案

`sandbox/backend.py` 提供有类型的 `create_agent_composite_backend()`，直接读取 Context 的 uid、runtime scope 和持久化 Workdir 相对路径。缺失或空白 runtime scope、缺失 Workdir 明确失败；uid 与路径合法性仍由沙盒构造器和路径 Owner 校验。删除 `sandbox/composite.py` 与 `_BackendScope`，调用方直接导入 backend 模块。

两个 Agent 执行实现与主动压缩继续按现有时机创建实例。文件与摘要 middleware 共用构图创建的 backend，`CompositeBackend(routes={}, artifacts_root=<runtime Workdir>/outputs)` 保留产物路径契约。实例构造不创建沙盒，实际文件或命令操作继续通过 provider 惰性取得同一 uid、runtime scope、Workdir 的连接。Context 类型仅用于注解，不引入运行时导入依赖。

## 替代方案

- 保留独立 composite 文件与 scope 类：支持尚不存在的多来源消费者，增加装配的阅读层数。
- 直接使用 ProvisionerSandboxBackend：DeepAgents middleware 只从 CompositeBackend 读取 artifacts_root，产物会改写到根目录。
- 共享一个动态读取 LangGraph runtime 的 backend：需要重新处理可变 client 缓存和构造时固定的产物前缀，超出当前需求。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| runtime scope 必须显式提供 | 子线程被错误当成沙盒 scope | sandbox/backend.py | 装配 unit 与缺失 scope 回归 | 有效 child thread 配合缺失或空白 scope 仍失败 | Passed |
| 父子 Agent 共用 runtime 与 Workdir 产物 | 身份或产物根在迁移中改变 | sandbox/backend.py、sandbox/provider.py | 沙盒 unit、真实文件链路 | 不同 uid 的 sandbox ID 不同；过期虚拟路径不影响产物根 | Passed |
| 装配保持惰性，当前调用方使用新入口 | 导入迁移或提前创建产生回归 | agent_backends、compression、sandbox/backend.py | 相关 unit、工程 gate、docs build | 缺失 Workdir 失败；构造时不调用 provider.get | Passed |

旧能力不存在：独立 composite 模块、一次性 scope 类、多来源 dict/object 搜索和 runtime scope 到 checkpoint thread 的回退均删除。

重新引入条件：出现明确的第二种文件 backend 路由或独立输入来源，并由真实消费者和测试证明需要独立装配边界。

## 后果

所有装配调用方显式提供 runtime_scope_id。仅填充 checkpoint thread 的内部调用在构图时失败，阻止子 Agent 意外取得独立沙盒。生产入口使用准备流程注入的 Context；普通执行、子 Agent 和主动压缩仍采用原有装配时机。

## 执行证据

- 缺失 scope 的回归测试先在旧实现执行，None、空字符串和空白字符串三项均因未抛出 ValueError 失败；合并后通过。
- `docker compose exec -T api uv run --no-sync --group test pytest test/unit/sandbox test/unit/agents/test_summary_graph_config.py test/unit/services/test_context_compression_service.py -q --tb=short -p no:cacheprovider`：206 passed。
- `docker compose exec -T api uv run --no-sync --group test pytest 'test/e2e/test_agent_lifecycle_subagent_boundaries_e2e.py::test_subagent_inherits_write_policy_and_shares_workdir[always_trust]' test/e2e/test_agent_lifecycle_e2e.py::test_attachment_survives_run_runtime_recreation -m e2e -q --tb=short -p no:cacheprovider --timeout=120`：2 passed。使用仓库的确定性模型重放服务，验证真实 API、PostgreSQL、worker 和动态沙盒；回读子 Agent 写入的 Workdir 文件、附件 artifact 与重建后的文件字节。临时重放进程在测试后终止。
- `python3 scripts/verify_engineering_contracts.py` 与 `python3 -m unittest scripts.test_verify_engineering_contracts`：通过，契约测试 64 项。
- 本次受影响 Python 文件执行 `ruff check --no-cache`，backend 与 sandbox unit 文件执行 `ruff format --no-cache --check`：通过。容器默认 Ruff cache 不可写，使用无缓存模式。
- `cd docs && pnpm run build`：首次通过；决策迁至 implemented 后的最终全局构建发现并行知识库改动在 runtime 边界记录中引用尚不存在的 implemented 页面，报 dead link。装配决定的两处相对引用指向当前存在的 implemented 文件；全局 docs build 未通过。
- `docker compose exec -T api uv run --group test pytest test/unit -m 'not slow' -q --tb=short -p no:cacheprovider`：在 worker 启动案例停滞后人工中止，已执行部分为 1882 passed、1 failed、55 skipped。增加逐项时间界限后执行 `docker compose exec -T api uv run --no-sync --group test pytest test/unit -m 'not slow' -q --tb=short -p no:cacheprovider --timeout=20`：2264 passed、1 failed、55 skipped。唯一失败为 `test_awrap_model_call_mounts_knowledge_base_skill_tools`。
- 撤回本次改动的隔离源码副本执行上述 Skill 失败案例及 `test_worker_startup_ensures_builtin_mcp_servers`：同一 Skill 断言失败、worker 启动通过。失败在本次变更前复现，保留为全量 gate 未通过，不修改无关的 Skill fixture。
- 独立 Reviewer 未发现可行动源码问题，并独立执行装配定向集合：8 passed。Review 覆盖全部消费者、导入依赖、身份与 Workdir 边界、惰性创建和 Skill 刷新时机。
- 本次 delta 的空白与文件终止换行检查通过。工作树全局 `git diff --check` 报另一项路由测试文件尾部空行，不属于本次差异。

### 合并提交验证

后端命名迁移与装配简化合并后，在独立 HEAD checkout 上验证，排除其他并行改动：

- `python3 scripts/verify_engineering_contracts.py`、`python3 -m unittest scripts.test_verify_engineering_contracts`：通过，64 项契约单测。
- 容器指定隔离 checkout 的 PYTHONPATH 执行 Agent、sandbox、preparation、compression、summary、subagent task 与 tool error guard 单测：483 passed。
- 同一隔离源码执行全量 `pytest test/unit -m 'not slow' -q --tb=short -p no:cacheprovider --timeout=20`：2305 passed、1 failed。失败仍是已在基线复现的 Skill 工具挂载案例，全量 gate 未通过。
- 使用现有依赖直接执行 `vitepress build /tmp/yuxi-runtime-sandbox-commit/docs`：通过。此结果覆盖合并提交中的文档，不覆盖其他并行知识库调整。
- Ruff 与暂存 diff 的空白检查通过。
