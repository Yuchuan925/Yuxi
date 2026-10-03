# 角色、共享资源与私有 Agent 权限收敛

状态：implemented
类型：feature
Owner：backend/yuxi/modules/identity/permissions/resource_permission.py

## 问题

共享资源管理权限与角色上限不一致，主 Agent 缺少独立私有状态，账号管理和长生命周期执行需要使用当前身份与授权闭合隔离。

## 决策

角色限制共享资源建设与管理，资源授权限制可使用与管理的对象。主 Agent 默认私有，所有者可以管理；管理员可以将自己的私有主 Agent 单向发布为共享。系统管理员治理其他人的私有定义但不能运行、转移或发布。共享范围采用同类型子集规则，指定管理者必须为有效管理员。账号角色只升不降，至少保留一个有效系统管理员。APP 终端用户只能使用共享 Agent；执行边界重新读取调用者授权。

按新版本直接修改契约，不保留旧权限行为、不迁移历史授权数据。个人 Skill、会话和文件隔离沿用原有 Owner，不增加策略引擎、自定义角色或审批机制。

### 实现方案

Identity permission 解析角色上限、同类型范围与私有可见性，资源写入服务校验指定管理者；User repository 与 PostgreSQL 触发器共同保证锁定后的成员授权、只升不降与系统管理员存续。Agent repository 保存明确的 visibility 和内置标记，发布在定义行锁内完成校验与状态提交，专用系统接口转移共享资源所有权。

HTTP 依赖限定系统维护入口，directory 与 worker 使用实际调用者的可运行查询。运行授权中间件在模型前后和工具边界刷新身份与资源，子 Agent 启动服务在子执行持久化前重新授权；账号或 Agent 失权形成失败结果，依赖失权通过通用事件和工具结果提示。前端使用返回的 capability flags 选择入口，并保留字段角色限制。

## 替代方案

保留根据授权列表推断私有状态会混淆治理和正常运行；继续允许普通用户管理共享定义会突破角色上限。二者均不采用。

## 后果

持久状态变更使用新的独立数据库环境，业务 Schema 版本为 13；旧授权与旧数据不受兼容承诺。执行失败沿用既有暂停 FIFO 队列的契约，排队输入保留，显式继续后重新授权。等待取消是按持久 Run 与完整 Thread 归属执行的服务端 checkpoint 维护，不取得用户资源使用权，也不运行模型或工具。

## 验证

验证在独立 Docker Compose 项目和全新 PostgreSQL 数据库执行。HTTP 测试回读定义、账号和授权；并发测试使用真实连接锁验证成员变更、系统管理员存续及恢复与删除的锁序。E2E 通过真实 HTTP 模型协议、worker、PostgreSQL checkpoint、MCP 和文件副作用证明当前权限，模型回复由本地确定性协议服务提供。

- `python3 scripts/verify_engineering_contracts.py`：通过。
- `python3 -m unittest scripts.test_verify_engineering_contracts`：64 项通过。
- `docker compose exec -u 0 api uv run --group test pytest test/unit -m "not slow"`：2454 项通过，55 项跳过。容器不包含部分仓库根配置与脚本，这些宿主机契约测试按既有环境规则跳过。
- `docker compose exec -u 0 api uv run --group test pytest test/integration/api/test_permission_convergence.py test/integration/identity/test_identity_permission_constraints.py -q`：8 项通过；包含其他用户与 APP 私有越权、普通用户共享写入、管理范围、部门成员边界、角色降级、所有权转移与并发管理员删除。
- `docker compose exec -u 0 api uv run --group test pytest test/e2e/test_permission_revocation_e2e.py -q`：9 项通过，验证 Agent、账号、Skill、SubAgent 与 MCP 撤权，FIFO 显式继续、定时启动、等待恢复和失权取消。取消维护图还通过原执行图回读验证任务清空、消息和 Skill 状态保留。
- `pnpm --dir frontend run lint:check`、`pnpm --dir frontend run test:unit`、`pnpm --dir frontend run build`：通过，前端 unit 415 项通过。Playwright 使用真实普通用户完成私有创建、详情回读、编辑和删除，检查共享及高级字段入口限制，并查看浅色、暗色和移动页面。
- `pnpm --dir docs run build`、Python Ruff 和 `git diff --check`：通过。

真实外部模型供应商未纳入本次权限验证；本地协议服务不替代供应商兼容性测试。当前授权在正常执行边界生效，已经提交到外部服务的动作和已经进入上下文的信息无法撤回。
