# v1 Agent 专属 Skill

状态：proposed
类型：feature
Owner：backend/yuxi/modules/extensions/skills/shared.py

本文面向 Agent、Skills 与权限模块维护者，提议为智能体（Agent）提供一份按需创建、随自身权限管理、运行时自动加载的专属 Skill。读者需要了解当前 Skill 文件编辑、授权投影和 Input / Turn / Run 执行链路。本文只提出方案，产品行为尚未实现。

## 问题

Agent 的角色指令由 `system_prompt` 表达，复杂操作流程还需要脚本、参考文件和工具依赖。普通共享 Skill 有独立权限，可被多个 Agent 选用；个人 Skill 随用户自动可用。两种来源都没有表达“这份能力资产属于一个 Agent”的持久关系。

专属内容如果仍作为普通共享 Skill 管理，维护者需要分别修改 Agent 和 Skill 权限，用户也会在普通选择器中看到本不应独立选用的资产。仅增加编辑入口无法保证执行时加载，也无法防止客户端通过空 Skill 配置关闭它。

方案参考 [PR #1064](https://github.com/xerrors/Yuxi/pull/1064) 的绑定、权限派生与编辑器复用思想。调研基线为 `origin/develop/1.0` 的 `07aea9bc0270bae12471c7e321930adebc944438`；该版本已具备远程 MCP 清单、共享 Skill 修订冲突检查和私有 Agent 权限。代码继续调整，实施前必须重新读取相关 Owner；本文中的路径与接口是当前定位和拟议契约。

## 提案

### 范围与产品语义

每个 Agent 至多绑定一个专属 Skill，绑定按需创建。没有专属内容需求的 Agent 保持现有行为。主 Agent 和 SubAgent 采用相同绑定规则，子 Agent 执行时使用自己的绑定。

`system_prompt` 保留，用于角色、目标、行为原则和回答风格。专属 `SKILL.md` 用于操作指南，目录中的脚本和参考文件提供按需读取的资源。存在专属 Skill 时，运行准备自动加载其根文件；大篇幅资料留在参考文件中，避免全部占用模型上下文。两者进入系统消息，不承诺模型天然赋予其中一方更高优先级，内容冲突由 Agent 管理者修正。

本范围包含绑定创建与读取、既有 Agent 的 ZIP 上传、内容与依赖编辑、权限派生、运行时加载和删除生命周期。创建 Agent 弹窗中的 MCP / ZIP 联动导入、跨资源创建恢复属于后续独立提案。版本、草稿回滚、灰度开关和历史数据库升级不在范围内；已存在的安装草稿与文件修订保护继续复用。

### 数据与权限

在 Skill ORM 增加可空 `bound_agent_id`，指向 Agent 的稳定数据库主键。外键和非空绑定的唯一约束证明 Agent 存在且至多有一个绑定；`source_type` 继续表达来源，运行时视图使用 `source_scope = agent_bound`。关联身份不从 slug 命名推断，Skill slug 仍用于文件和现有编辑接口寻址。新库初始化通过当前 schema-init 创建约束并维护 Schema 版本；已有不兼容 Schema 按当前启动规则显式拒绝，不新增旧版本升级路径。

专属 Skill 的读取和管理直接派生自关联 Agent 的当前权限解析，包括 private/shared、内置 Agent 的管理限制、产品用户与 Public 调用身份。运行激活使用 Agent 的实际可运行查询。仅复制 `created_by` 和 `share_config` 再调用普通 Skill 权限函数不足以覆盖这些语义，因此不持久化第二份授权配置；关联 Agent 缺失、身份失效或权限无法解析时拒绝访问。

默认的扩展卡片、普通 Skill 选择器、聊天提及、安装目标和其他 Skill 的依赖候选排除绑定资产。按 slug 访问文件、导出和编辑接口仍执行派生授权；禁止普通安装路径覆盖绑定目录。专属 Skill 可依赖普通 Skill、本地工具和 MCP，依赖范围需覆盖关联 Agent 的实际使用范围；普通 Skill 与其他 Agent 的专属 Skill 均不能引用该绑定资产。

“专属”表示资产归属和自动激活关系，权限仍以调用身份为边界。当前沙盒允许 Agent 读取当前用户获授权的文件，本文不增加同一用户的 Agent 之间的文件保密承诺。跨用户、跨 Public App 和未授权资源仍必须隔离。个人同名 Skill 不得覆盖专属根内容；实现需要显式拒绝冲突或选择无冲突的持久 slug，并以负向用例验证。

### 实现方案

| 模块 / Owner | 拟议职责 |
| --- | --- |
| `modules/extensions/skills` | 持久绑定、授权解析、目录发布与依赖校验；复用 package、draft、edit、projection 的既有边界 |
| `modules/agents` | 提供当前 Agent 身份与权限；service 协调权限变化和删除，repository 只查询或修改持久状态 |
| `modules/identity/permissions` | 维持 Agent 权限、身份刷新与角色上限的唯一判定，不为绑定保存独立授权规则 |
| `api/routers/agents/management.py` 与扩展路由 | 认证和协议适配；上传通过 `api/uploads.py` 转为业务中立输入 |
| Agent runtime 与 `services/preparation.py` | 从已授权 Run 的 Agent 派生绑定，构建唯一执行快照和 manifest |
| `frontend/src/modules/agents` 与 `modules/extensions` | Agent 编辑入口、按需创建或上传；复用 SkillDetailView 的文件和依赖编辑，API 集中在 apis |

拟在既有 Agent 管理路径下提供按 Agent slug 寻址的专属 Skill 子资源：读取绑定、幂等创建最小可编辑包和上传 ZIP。已有绑定时创建返回同一资源；上传替换同一绑定的内容，不另建第二个绑定。整包替换需要新增明确的目录修订契约：读取返回由目录文件路径和字节派生的整包 revision，替换在行锁内重新计算并核对；仅根文件的 expected_revision 不能发现脚本或参考文件的并发编辑。具体 HTTP 方法、请求模型和前端 DTO 在实现时按当前路由定稿，协议参数不接受客户端指定别的 Agent 数据库主键或独立共享权限。

服务在持久化边界锁定 Agent，刷新写入身份并重新检查管理权限；数据库唯一约束承担并发创建的最终拒绝。ZIP 先在现有包边界校验路径、文件类型、大小和唯一根级 SKILL.md；复用安装草稿选择，只发布一个已确认 Skill。数据库记录与正式目录发布使用现有临时目录、替换和失败恢复机制，校验失败保持原绑定及文件。不能以请求成功提示替代数据库和文件回读，也不能把目录发布与 PostgreSQL 提交称为跨介质原子事务。

文件读取、保存、节点增删和依赖修改继续经 `edit.py`，保留 expected_revision、共享行锁、根索引更新和路径校验。绑定 Skill 的普通共享范围、启停、独立删除和独立安装入口由后端拒绝，前端隐藏对应控件。停用操作指南可以改成最小说明，完整资产随 Agent 删除处理。

Agent 发布、共享范围变更、删除和 Skill 编辑必须使用一致锁序，锁后复核实际身份与关联授权。权限变化复用投影失效和刷新机制，使已撤权用户的残留投影不可继续读取；执行授权中间件在现有模型和工具边界重新确认相关权限。正文权限变更与绑定投影更新的具体事务界面需要在实现前对照当前 Owner 定稿，不能只在保存后异步复制共享配置。

删除由 Agent service 协調，维持已有活跃执行限制：在同一数据库事务中移除 Agent 和绑定记录，文件采用现有垃圾目录策略；提交失败恢复文件，提交成功移除授权投影并清理资产。清理失败形成可观察错误，保留可核对位置，不把删除写成已完整清理。Repository 不反向调用 Skill service；不引入新的后台回收框架。

worker 的执行准备从 `run.agent_slug` 及实际授权结果取得绑定身份，不接受用户配置的覆盖。`resolve_runtime_skills_for_context` 在现有共享行锁保护下纳入当前绑定及其依赖，将绑定根自动加入预加载集合；客户端的 skills / preload_skills 空列表不关闭它。根文件不可读时显式失败，未绑定时正常运行。依赖激活复用现有工具门控，预加载不扩大工具或 MCP 权限。

执行 Context、Skill metadata、实际预加载内容和 manifest 摘要来自同一次准备。当前执行已预加载的根文本保持准备时内容，脚本和参考文件仍从实时用户投影读取，不承诺整包不可变。同一 Run 的 worker 重试按现有规则重新准备，资产变化导致 fingerprint 不一致时显式失败，不复原旧文件。后续 Run 包括 steer 接续和等待恢复按当前 preparation 契约重新准备，不额外保证整 Turn 内容版本不变。子 Agent 只自动加载自身绑定，父子 Workdir 与执行 ownership 保持既有边界。

Agent 编辑页新增“专属 Skill”入口，展示未创建、创建中、可读取、可编辑和失败状态；按权限提供创建、上传或查看入口。文件管理复用 SkillDetailView，明确说明权限跟随 Agent，并区分“角色与行为”的 system prompt 与“操作指南与资源”的 Skill。普通列表隐藏属于展示规则，所有直接接口仍以服务端权限为准。

## 替代方案

| 候选 | 收益 | 代价与选择 |
| --- | --- | --- |
| 继续仅使用 system prompt | 适合简短角色说明，维护成本低 | 不能表达文件包、依赖和专属资产；保留为无文件需求的现有选择 |
| 普通共享 Skill 加命名约定 | 复用现有表与编辑器 | 绑定缺乏数据约束，权限与列表易漂移；不选择 |
| 新建独立专属 Skill 存储与编辑器 | 领域表面直观 | 重复包校验、目录编辑、修订、投影与依赖能力；不选择 |
| Skill 增加稳定绑定，权限从 Agent 派生 | 复用当前内容能力，绑定由数据库证明 | 需要在真实查询、写入、投影和执行路径适配来源；推荐 |
| 专属 Skill 替代 system prompt，所有 Agent 自动生成 | 只有一个文字入口 | 改变现有角色配置并制造空资产；不选择 |

## 后果

Agent 管理者在一个资产中维护操作流程与配套文件，共享范围变化直接影响专属内容的授权。Skill 模块增加一种绑定来源，列表、编辑、投影与依赖解析必须共同处理它；实现不能把所有逻辑重新集中成一个大型 service。

专属根文件会进入模型上下文，按需创建和较短根文件控制成本。根文本保存影响后续准备的 Run，当前执行的预加载文本由快照拥有；脚本和参考文件遵循实时投影。整包上传需要新增目录修订核对，单文件编辑继续使用既有修订契约。目录与数据库的失败恢复、权限撤销后的投影失效仍是实际维护成本，提出方案不代表这些风险已通过运行验证。

## 验收标准

以下证据针对后续实现；当前结果均为 `Not run`，原因是本提案仅新增方案文件。实现前按[测试规范](../../testing-guidelines.md)选择最小相关集合，并将权限、文件和 worker 链路接入实际 workflow。新增 guard 必须以可恢复目标缺陷的负向用例证明拒绝原因。

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| 至多一个绑定且重复创建返回同一资源 | 并发创建多个行或孤立目录 | Skill models / repository、绑定用例 | 真实 PostgreSQL 并发 HTTP；回读行、约束和目录 | 两个请求同时创建；强制写重复或悬空关联 | Not run |
| 权限跟随当前 Agent | private、发布、撤权或身份变化仍暴露内容 | Agent 查询与 permissions、绑定用例、projection | 真实 HTTP 与权限撤销 E2E；回读投影和访问结果 | 非所有者访问 private；等待行锁时移出授权部门；不同 App 使用旧资源标识 | Not run |
| 普通入口不能独立使用或覆盖绑定 | 列表过滤代替授权、普通安装改写专属资产 | catalog / shared / draft / 绑定用例 | HTTP 查询列表并直接调用写入；回读绑定和文件 | 猜 slug 修改共享、停用、删除、安装覆盖；普通 Skill 引用绑定 | Not run |
| 上传与编辑保持目录和修订一致性 | ZIP 越界、同名覆盖、旧文件或整包 revision 丢失新内容 | package / draft / edit | HTTP 上传与编辑，回读根文件、依赖、revision | 路径穿越、符号链接、多个根 Skill、个人同名、两个客户端保存旧 revision；仅脚本被修改后旧整包 revision 的 ZIP 替换 | Not run |
| 当前 Agent 绑定自动生效且失败可见 | 空配置关闭绑定、坏根文件静默跳过 | runtime / preparation / middleware | 确定性 worker E2E；核对模型协议请求、manifest 和工具产物 | skills 与 preload_skills 均为空；根文件不可读；依赖工具已撤权 | Not run |
| 父子与后续执行段使用正确快照 | 子加载父绑定、manifest 与实际内容不同 | preparation / subagents / runner | E2E 覆盖主 Agent、子 Agent、steer 和恢复；核对各 Run 的请求及摘要 | 父子具有不同说明；编辑恰逢新执行准备；相邻 Run 内容不同；修改资产后重试同 Run，核对 fingerprint 拒绝 | Not run |
| 删除提交与文件清理可核对 | rollback 丢内容、删除后残留可用投影 | Agent service / repository、Skill 文件发布与 projection | 真实事务故障注入、文件与投影回读 | 提交失败、清理失败、活跃执行时删除、同文件并发读取 | Not run |
| 编辑入口复用现有能力并解释 prompt 分工 | 平行编辑器丢依赖、隐藏按钮伪装权限 | Agent UI / SkillDetailView / apis | frontend lint、unit、build 和真实 DOM；浅暗主题、窄屏 | 只读用户、保存冲突、上传失败、未绑定和键盘跳转 | Not run |

## 风险

- Agent 和 Skill 的角色上限不同，复制共享配置会错误授权；运行、管理与 Public 身份必须分别对照实际 Agent 查询。
- 共享文件修订、Agent 身份刷新与用户投影涉及多个锁，新增绑定不能引入反向锁序。具体锁序与故障测试是实施前必须确认的边界。
- 依赖范围随 Agent 发布而扩大，既有较私有的依赖不能因专属 Skill 自动获得更宽权限。发布或使用时必须由当前依赖 Owner 明确拒绝或按已有 capability-limited 契约处理，不能静默假装完整能力。
- 数据库提交和文件发布无法组成单一事务；进程崩溃时的可观察失败、残留清理和重试规则需要沿用并验证现有机制，普通异常补偿不能替代崩溃验证。
- 当前用户级授权文件空间不提供同一用户不同 Agent 的保密隔离；如果产品要求该隔离，需要单独评估 runtime scope 文件映射，不能在此提案中暗示已经具备。
- 基线会继续调整，实施时以最新模块、路由、权限和 preparation 事实为准；本文在实现验收前保持 proposed。

## 验证

现状源码、Skill ORM、权限解析、文件修订、投影和执行准备已静态核对，结果为 `Inspected`。提案的相对链接、工程契约检查、文档构建和独立语义 Review 在交付 PR 中记录实际命令与结果。后端 unit、真实 HTTP、worker E2E、浏览器与外部模型验证用于后续实现，本提案不以旧 PR 的结果证明 v1 功能通过。
