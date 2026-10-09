# 公开 Thread 路径统一为 Session

状态：implemented
类型：refactor
Owner：backend/yuxi/api/routers/public_v1/agents/threads.py

## 问题

公开 Thread 路径与既有 Session 事件名称不一致，对外文档缺少参数、幂等与异步完成条件的说明。

## 决策

公开路径统一为 `/api/v1/agents/sessions`，路径参数使用 `session_id`，创建与详情 object 使用 `agent.session`。Session ID 沿用内部 Thread UUID。Web、CLI、Demo、文件链接和测试同步使用新路径；公开输入模型命名为 SessionCreate 与 SessionEventCreate。

保留原有请求字段、HTTP 状态码、业务响应形状、FIFO、事务和运行时生命周期。补充 OpenAPI 参数、错误、示例与长期 SSE 说明；接入流程由[公开 API 参考](../../../advanced/agents-public-api.md)和[接入教程](../../../intro/agents-api-quickstart.md)维护。

## 替代方案

保留 Thread 路径会继续要求外部调用方理解两套命名。增加旧路径 alias 会扩大维护范围，因此直接迁移当前调用方。

## 后果

调用方必须更新 URL；内部 Thread、Turn、Run 与持久化命名继续表达业务归属。新增 Session Items、字符串输入、模型配置迁移及响应结构对齐属于后续功能，不纳入本次路径迁移。

## 验证

索引版本经过 Python 编译与 OpenAPI 路由核对：旧 Thread 路由不再注册，既有会话子路由使用 session_id，未新增 Session Items 路由；创建请求仍仅接受消息数组及 model_spec。先前完整工作区的 HTTP、worker、浏览器和 unit 证据不作为本次拆分版本的独立运行证明。拆分版本未重新运行真实 HTTP/worker E2E。
