# 移除消息反馈能力

状态：implemented
类型：simplification
Owner：backend/yuxi/modules/agents/services/messages.py

## 问题

消息点赞和点踩原先会保存到数据库、同步 Langfuse，并在 Dashboard 展示统计和详情。该功能不再提供，保留可写、可读入口会继续承诺无效的产品行为。

## 决策

聊天消息保留点赞和点踩按钮，点击后各自提示“反馈功能开发中”，不发请求或保存状态。前端删除反馈 API、点踩原因弹窗和 Dashboard 反馈指标/列表。

后端删除消息反馈路由与 service、MessageFeedback ORM、ConversationStats 中无调用方的 user_feedback 字段、消息 History 反馈字段、Langfuse 用户评分 helper，以及 Dashboard 反馈查询、统计和响应字段。ORM fresh schema 因此不再创建 `message_feedbacks` 表。没有旧库数据迁移；已有部署中的相关表或列可继续留存，但应用不再访问。

### 实现方案

聊天入口由 `RefsComponent.vue` 的两个 handler 提供提示，不调用 `agent_api`。Dashboard 的反馈 UI/API 与 system service/repository 统计一起删除。Public Agent capabilities 移除两条消息反馈路由，agent service、Message/ConversationStats 映射及 Langfuse 评分入口移除对应写入、读取和序列化。旧功能测试及其 workflow 选择器一并删除，其他 Dashboard 与 Langfuse 行为保持原有 Owner。

## 替代方案

- 只隐藏或禁用按钮：仍会留下可直接调用的后端写入、读取与统计 API。
- 只删除 Dashboard：仍会保留消息反馈存储、History 数据和 Langfuse 副作用。
- 为已有部署加入删除迁移：会扩大本次变更的数据操作范围；应用不再读取旧表后没有必要。

## 后果

- 按钮仍显示点赞、点踩图标，点击均反馈功能开发中。
- History、Dashboard 和 Agent analytics 不再返回反馈字段。
- 新创建的 schema 不包含反馈表和 ConversationStats 反馈列。旧数据库中的遗留表/列不再由应用映射或访问。
- 依赖旧 HTTP endpoint 或私有响应字段的外部客户端需要停止使用。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 两个按钮保留并只显示开发中提示 | handler 发请求或写入状态 | `web/src/components/RefsComponent.vue` | 阅读两个 handler 与模板；`rg -n 'submitMessageFeedback|getMessageFeedback|messages/.*/feedback' web/src backend/yuxi` | 恢复任一调用或状态后命中旧 API 引用搜索 | Inspected：handler 只调用提示；旧 API 搜索无结果。Not run：页面点击验证，当前无登录态且 API 容器未启动，访问 `/agent` 被重定向到 `/login` |
| 后端不再有消息反馈读写、ORM 或 Langfuse 评分能力 | 路由、模型、History、score helper 残留 | `backend/yuxi/modules/agents/services/messages.py` | `rg -n 'MessageFeedback|message_feedbacks|submit_user_feedback_score|user_feedback' backend/yuxi` | 恢复映射或入口后命中旧能力搜索 | Inspected：搜索无结果 |
| Dashboard 不再提供反馈端点、统计或界面 | endpoint、card、modal 或 analytics 字段残留 | `backend/yuxi/modules/system/dashboard.py`、`web/src/views/DashboardView.vue` | `rg -n 'feedback_stats|total_feedbacks|satisfaction_rate|agent_satisfaction_rates|/feedbacks|FeedbackModalComponent' backend/yuxi web/src` | 恢复任一字段或组件后命中旧能力搜索 | Inspected：实现源码无结果；测试保留缺失字段断言 |
| 代码格式与前端构建可用 | 删除产生语法、格式或组件构建错误 | 受影响 Python 与 Web 源码 | `git diff --check`；`python3 -m compileall -q backend/yuxi backend/test scripts`；`docker compose exec web pnpm run lint:check`；`docker compose exec web pnpm run build` | 恢复非法语法或 lint/build 破坏后命令失败 | Passed：上述格式检查、Python 编译检查、前端 lint 和构建 |

旧能力不存在：应用不再提供消息反馈路由、保存、读取、Langfuse 同步或 Dashboard 展示；旧部署数据库可能保留未使用的数据表和列。

重新引入条件：产品重新确认消息评分的用户价值、数据保留策略及 Dashboard 责任人后，建立新决策并明确完整读写 Owner 和验证证据。
