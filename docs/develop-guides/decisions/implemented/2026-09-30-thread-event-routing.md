# 线程事件归属使用明确字段

状态：implemented
类型：simplification
Owner：backend/yuxi/modules/agents/runtime/base.py

## 问题

通用 thread_id 提取器同时扫描顶层、configurable、metadata、stream_event 和 meta，混合 LangGraph 接入与内部 chunk 的契约。内部 chunk 已由执行器写入顶层 thread_id，重复扫描和备用参数掩盖缺失字段，并让嵌套业务数据参与事件路由。

## 决策

### 实现方案

LangGraph 接入层保留消息 metadata 的顶层 thread_id，子图按已有 namespace 路由和确定性子线程标识覆盖 metadata。当前 SDK 子图句柄不提供 metadata/state，接入层使用句柄的图名和调用标识构造路由。执行器使用已明确归属的顶层字段构造 chunk；worker 和缓冲 writer 直接读取 chunk 的 thread_id。通用提取器、转发函数和 writer 的备用线程参数删除，内部缺失字段直接失败，由现有 worker 错误收敛路径处理。

线程归属由执行器的 thread_id 参数和明确的子线程字段拥有，make_chunk 不从 meta 借用线程标识。父子输出隔离、事件分桶、FIFO、Turn/Run 生命周期和公开 API 保持原有职责。

## 替代方案

- 保留通用扫描：消费者继续依赖多个字段的隐含优先级。
- 仅缩小扫描列表：仍混合接入结构与内部契约，保留重复兜底。
- 引入新的事件类或路由器：现有 chunk 顶层字段已满足当前消费者，增加维护表面。
- 完全删除子线程路由：破坏子智能体输出和父线程终态隔离。

## 后果

嵌套业务数据不参与路由。非空 namespace 且未明确归属的模型消息被丢弃；普通根消息使用当前执行线程。已有非消息 stream_event 的投影行为保留，不扩大为整体事件协议重构。内部 chunk 缺失顶层 thread_id 时显式失败，不默认归入父线程。

## 验证

- 负向测试在原实现上复现 13 个失败：四种嵌套位置分别覆盖 runtime、execution、writer，加上子图句柄身份被非协议属性覆盖。修改后这些案例通过；worker 缺失字段的补充案例断言 failed/error/end，并拒绝发布 messages。
- 定向 unit：`docker compose exec -T api uv run --no-sync --group test pytest test/unit/agents/test_thread_event_routing.py test/unit/agents/test_base_tool_event_normalize.py test/unit/services/test_chat_service_langfuse_stream.py test/unit/services/test_base_agent_langfuse_config.py test/unit/services/test_run_worker.py -q`：115 passed。
- 全量 unit：`docker compose exec -T api uv run --no-sync --group test pytest test/unit -m "not slow" -q`：2408 passed、55 skipped。标准 `uv run --group test` 在执行测试前因容器中 root-owned yuxi.egg-info 无法写入失败，使用已安装依赖验证。
- 真实链路：`docker compose exec -T api uv run --no-sync --group test pytest test/e2e/test_agent_lifecycle_subagent_boundaries_e2e.py::test_subagent_inherits_write_policy_and_shares_workdir test/e2e/test_agent_lifecycle_subagent_boundaries_e2e.py::test_child_model_retry_exhaustion_is_reported_to_parent test/e2e/test_agent_lifecycle_e2e.py::test_first_input_and_follow_up_fifo_cross_worker test/e2e/test_agent_lifecycle_e2e.py::test_tool_cycle_without_steer_stays_in_one_run -q`：5 passed，回读父子 Run、Message、文件和 SSE 结果。
- 并发观察：`docker compose exec -T api uv run --no-sync --group test pytest test/e2e/test_agent_lifecycle_subagent_boundaries_e2e.py::test_child_end_is_public_while_parent_waits_for_slow_child -q`：45 秒超时。PostgreSQL 回读快子 Run completed、慢子 Run failed；worker 日志在进流前的 Langfuse observation 持久化报告 DeadlockDetectedError。该并发 SSE 场景仍未通过验证，不能计入通过。
- 工程检查：`python3 -m unittest scripts.test_verify_engineering_contracts`：64 passed；`python3 scripts/verify_engineering_contracts.py`：最终通过。包含工具契约和线程路由改动的完整 unit 最终回归：2411 passed、55 skipped、7 subtests passed。
- 文档：`cd docs && pnpm run build` 通过；本任务核心源码、新测试与执行流测试的 Ruff lint/format 通过，`git diff --check` 通过。worker 测试文件原有无关 E501 与格式问题保留。真实模型探针未执行，确定性 replay 不替代外部 provider 校准。

旧能力不存在：源码和测试中无 extract_thread_id、_metadata_thread_id、通用扫描模块及 chunk_thread_id 转发函数。

重新引入条件：实际支持的上游协议引入新的线程字段时，仅在该协议接入处显式适配，并以真实协议与父子隔离测试证明。
