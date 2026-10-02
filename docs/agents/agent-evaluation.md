# 评估智能体

当前 npm CLI `@xerrors/yuxi` 不提供 Agent eval 或 Langfuse Dataset experiment 命令。CLI 的第一阶段只支持认证、Agent 目录、Public Thread 对话和知识库只读查询；不要使用 `yuxi agent eval`。

知识库检索评估仍见[知识库评估](../intro/evaluation.md)。如果需要构建自定义 Agent 评估器，应直接使用[智能体 Public API](../advanced/agents-public-api.md)创建 Thread、提交 Input、观察事件并回读 Turn 结果；评估脚本需要自行拥有 Dataset、并发、超时、评分和结果存储语义。

Langfuse tracing 是 API/worker 的观测能力，不代表 npm CLI 支持 Dataset experiment。相关配置与服务端观测边界见[Langfuse 集成](../advanced/langfuse-integration.md)。
