# 创建智能体时统一附带 Skill 与 MCP

状态：implemented
类型：feature
Owner：backend/yuxi/modules/agents/services/definitions.py

## 问题

用户创建智能体时需要同时附带操作指南和 MCP。创建后分步导入会留下部分完成的智能体、额外 MCP 或无法判断的重试状态。创建表单只提供基本信息，用户必须再次进入配置完成绑定。

## 决策

一次创建请求同时提交 Agent、可选专属 Skill ZIP、已启用 MCP 的选择及超级管理员导入的远程 MCP 配置。失败时数据库与 Skill 内容不留下部分创建结果。现有 JSON 创建请求契约保留，Skill、MCP 的内容校验、权限和运行加载继续由原 Owner 执行。

非目标是创建向导、普通用户自建 MCP、私有 MCP 授权、自动创建空 Skill、改变 MCP 运行连接、替换系统提示词或引入额外持久状态机。图标上传沿用独立图片资源入口。

### 实现方案

JSON `POST /api/agent` 接受可选远程 MCP 配置列表；附带 ZIP 使用 multipart `POST /api/agent/with-skill`，表单的 agent 字段为同一创建模型的 JSON，file 字段为必选 ZIP。路由只校验 HTTP 输入、限量读取上传及装配响应。definitions 用例刷新操作者权限，准备 MCP 行、校验 Agent 配置、创建 Agent 行；附带 ZIP 时复用绑定内容提交，在同一次 PostgreSQL 事务中发布全部引用。没有 ZIP 时由创建用例提交。提交失败回滚数据库，内容 Owner 清理未引用的新包。

Agent 创建路径统一 flush，由创建用例或内容 Owner 提交；commit 参数与现有 update/delete 的接口一致。MCP 创建支持 flush 模式，其默认提交行为继续服务已有 MCP 管理入口。MCP 输入复用 RemoteMCPConfig；只允许超级管理员创建共享的远程 MCP，已有启用配置通过 Agent 的 mcps 选择。重复 MCP 标识明确拒绝，禁止覆盖旧配置；新导入项自动加入本次 Agent 的显式 MCP 选择，创建人沿用 MCP 管理页的用户名口径。

前端 AgentEditModal 复用单页基本表单，增加可选 ZIP 与已启用 MCP 多选。超级管理员展开清单输入，复用现有 mcpServers parser。只发送一次创建请求，等待期间锁定提交与图标上传；校验失败保留表单与文件。每次打开或关闭使旧编辑响应失效，避免覆盖新的创建草稿。网络中断或未知服务端结果提示取消并刷新列表核对，禁止在本次表单上重复 POST，取消后才能重新打开。

创建的业务校验错误统一返回含 code、message 的 HTTP detail 对象，由通用 API 层显示具体原因；Schema 校验继续使用当前脱敏规则。Modal 的 open 由关闭函数控制，创建和图标上传期间遮罩、Escape 与取消均不能越过等待保护。手机界面纵向排列名称与后端，表单在视口高度内滚动。

## 替代方案

- 前端依次创建 MCP、Agent、Skill：每一步都有独立提交，需要重试、补偿与不确定结果处理，维护负担超出创建操作。
- 全部改成 multipart：破坏现有 JSON 调用者，且没有 ZIP 的请求也承担文件协议。
- 创建时只提供已配置 MCP：实现更窄，无法同步原 PR 的 MCP 清单导入能力。

## 后果

PostgreSQL 原子提交无法保证 HTTP 响应送达；界面提示未知结果并禁止本次表单盲目重试，不引入幂等键或额外状态机。超级管理员导入的是现有系统 MCP 资源，其他 Agent 可选择，凭据不显示给普通用户。ZIP 限 10 MiB，展开预算沿用当前包边界。已有 JSON 创建的 slug 自动去重语义保留。图标上传仍是独立资源，不属于创建事务。

内容提交失败时，内容 Owner 删除本次新包，并回收已经为空的 Skill 目录；已有当前内容与历史内容不会被删除。无法回收时记录错误，保留原始创建失败。

绑定详情使用现有 content_path 读取持久包，复用内容目录校验。前端删除没有调用者的 createSkillFile 包装；后端文件创建协议及现有调用者保留。这两项机械清理不改变绑定、草稿或外部协议。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 普通创建、ZIP 与 MCP 一次创建均可读回完整结果 | HTTP / PostgreSQL / 文件 | definitions / Skill content | test_agent_create_resources.py：17 项真实 HTTP / PG，包括目录字节回读 | 无效 ZIP、缺少依赖不留下 Agent 或新 MCP | Passed |
| 新 MCP 与 Agent、Skill 共同提交 | 事务 / 内容目录 | definitions / repositories / content | 同文件最终 commit 故障注入，独立连接与一级 Skill 目录、二级内容包快照回读 | 提交前全部行必须已 flush；依赖校验与最终提交失败均不留空 Skill 目录或部分结果 | Passed |
| MCP 权限与连接限制沿用现有规则 | 后端授权 / 输入契约 | identity / RemoteMCPConfig | 同文件真实普通用户选择、超级管理员导入；连同原权限文件共 59 项 HTTP 回归 | user/admin 导入、禁用 MCP、已有/内置标识、stdio 皆拒绝 | Passed |
| 创建表单一次请求且失败保留输入 | Vue / HTTP / DOM | AgentEditModal / agent_api | 前端 lint、462 项 unit、build；真实浏览器创建后回读，浅色桌面、深色 390px 截图；浅/深色 MCP 加载失败提示与重试 | ZIP 超限、清单错误、遮罩/Escape/重复提交；延迟编辑响应不能覆盖等待、未知结果或422后的创建草稿；坏 ZIP 保留输入且无 Agent；恢复未定义颜色变量时错误色断言失败，重试保留草稿 | Passed |
| 新创建结果沿用专属预加载与 MCP 装配 | worker / 模型协议 / sandbox | Agent preparation / extensions runtime | test_agent_bound_skill_e2e.py：真实 worker / replay / sandbox 共 3 项，回读运行清单与投影文件 | 缺少 MCP 工具时独立模型协议 oracle 拒绝；根文本必须进入模型输入 | Passed |

本地命令在 Compose api/frontend 中执行，HTTP 与 E2E 使用测试账号环境变量，replay 服务已就绪：

```bash
uv run --no-sync --no-dev pytest test/integration/api/test_agent_create_resources.py test/integration/api/test_agent_config_resource_authorization.py test/integration/api/test_agent_bound_skill.py test/integration/api/test_agent_bound_skill_versions.py test/integration/api/test_shared_skill_edit_router.py test/integration/api/test_permission_convergence.py -q
uv run --no-sync --no-dev pytest test/e2e/test_agent_bound_skill_e2e.py -q
uv run --no-sync --no-dev pytest test/unit -m 'not slow' -q
pnpm run lint:check
pnpm run test:unit
pnpm run build
```

后端 unit 为 2548 passed / 55 skipped，55 项 Compose、Nginx、service boundary 与 worktree 配置检查因 API 容器未挂载仓库根目录而跳过；真实 HTTP 与 3 项 E2E 无跳过。创建 HTTP 文件接入 system-tests 的阻断步骤，既有专属 Skill E2E 步骤执行全部三个场景。浏览器重新读取 Agent、Skill 目录与启用 MCP，确认结果；截图见 [桌面创建表单](../../../assets/agent-create-resources.png) 与 [手机创建表单](../../../assets/agent-create-resources-mobile.png)。真实商业模型及外部第三方 MCP 连通性为 Not run，E2E 使用独立 replay 与真实本地 FastMCP 服务。
