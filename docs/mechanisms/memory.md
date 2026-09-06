# 用户 Memory：可控的长期上下文

Yuxi Memory 使用用户工作区中的 `agents/MEMORY.md` 保存长期信息。用户决定是否开启，主 Agent 在新 Run 构建时读取。模型策略要求只在用户明确表达记忆或纠正意图时调用写工具；后端授权由当前用户和运行身份校验执行。

## 生命周期

用户工作区初始化时会创建 `agents/MEMORY.md`，已有文件保持原样。`enable_memory` 默认关闭，配置保存在 PostgreSQL。

新的主 Agent Run 构建 Graph 时读取开关。开关开启、文件存在且包含内容时，系统加载有界前缀，并将其标记为用户维护的参考数据。Memory 内容会进入主 Agent 上下文，同时注册三个受限工具：

- `remember_memory`：保存或纠正长期信息；
- `search_thread_messages`：搜索当前用户可见的主 Agent 历史；
- `read_thread_messages`：按需读取历史消息。

子智能体不加载这套 Memory middleware。当前机制也不执行自动画像、运行结束自动写入、后台总结、向量索引或 Project Memory。

## 读路径

```text
enable_memory + agents/MEMORY.md
                │
                ▼
        Main Agent Memory middleware
                │
                ├─ Prompt reference
                ├─ Search history
                └─ Read history
```

文件缺失、为空或开关关闭时，运行继续执行，只是不装配 Memory。历史工具只返回当前用户可见的普通主 Agent 对话。

## 写路径

Memory middleware 指示 Agent 只在用户明确表达“记住”或“纠正”时调用 `remember_memory`；这是模型行为策略，不是后端能够独立证明的授权条件。工具参数不接受任意路径、uid、run 或 worker，写入目标固定为当前用户的 `/agents/MEMORY.md`。

服务在写入前重新校验：

- `enable_memory` 仍然开启；
- uid、thread、request 与顶层 Run 匹配；
- 当前 worker 持有未过期 lease；
- Run 仍处于可写状态；
- 参数和更新后的文件没有超过大小限制。

同一用户的写入通过 PostgreSQL advisory lock 串行化，同时锁定 AgentRun 行。文件使用同目录临时普通文件、no-follow 检查、`fsync`、rename 和目录 `fsync` 原子发布。精确纠正要求旧文本只出现一次，零次或多次都会返回冲突。

## 运行内可见性

Memory middleware 在 Graph 构建时读取文件。当前 Run 中完成的写入不会让已经构造的 middleware 自动重载；下一次 Run 会读取最新内容。

系统提示、当前消息和实时工具证据拥有更高优先级。Memory 用来保存用户希望长期保留的偏好与事实，不承担系统授权或不可覆盖指令。

## 对外一句话

用户决定 Agent 记住什么；每次写入都绑定当前用户和有效 Run。

## 源码定位与验证

- [Memory service](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/services/memory_service.py)
- [Memory middleware](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/agents/middlewares/memory.py)
- [Workspace filesystem](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/workspace/filesystem.py)
- [Conversation repository](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/repositories/conversation_repository.py)
- [Memory 集成测试](https://github.com/xerrors/Yuxi/blob/main/backend/test/integration/services/test_memory_service.py)
- [上下文压缩](./context-compression.md)
