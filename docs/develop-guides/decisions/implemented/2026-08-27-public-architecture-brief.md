# 对外架构材料的事实与派生边界

状态：implemented
类型：process
Owner：docs/mechanisms/public-architecture-brief.md

## 问题

Yuxi 的架构事实分布在根架构地图、机制页、Agent 文档、Compose 和源码中。对外材料如果直接从页面截图或模型能力列表拼接，容易把 Request 和 AgentRun、Skill 和 Memory、checkpoint 和文件系统、事件和最终状态混为一谈，也容易把未来方向写成当前能力。

## 决策

Yuxi 的对外架构材料以 `docs/mechanisms/public-architecture-brief.md` 作为总纲，并把知识图谱、Agent Harness、上下文工程、AgentRun、可观测性、Memory、Skills 和子智能体拆成独立机制页。总纲按“项目定位 → 整体架构 → 机制拆解”的总分结构展开，每节只回答一个架构问题。每个箭头都表达数据、控制、观察或权限校验的真实语义。文字稿只组织已有事实，不成为运行时、数据、权限或测试的独立 Owner；详细语义继续由对应机制页和源码拥有。

派生的展示稿、网页或图片可以压缩文字，但必须链接回总纲，并保留会改变理解的事务顺序、权限边界、存储 Owner、失败结局和 LITE 范围。派生产物不构成新的架构事实源。

## 替代方案

- 只做产品截图和功能清单：更容易制作，但无法解释知识、运行、文件和治理如何形成同一条链路。
- 用一张超大总图承载全部细节：信息密度过高，读者无法分辨核心运行顺序和各模块 Owner。
- 让派生展示稿成为新的架构事实源：交付物更独立，但会形成可漂移的第二份契约，增加维护和审查成本。

## 后果

- 更新架构边界、状态、权限或存储语义时，先更新对应事实 Owner，再更新总纲和派生展示稿。
- 派生材料不承诺未经当前测试证明的性能、准确率、可用性或 exactly-once 外部副作用。
- 总纲负责跨机制阅读顺序；运行装配、压缩、权限、失败和恢复等细节仍由对应机制 Owner 完整解释。

## 验证

| 验收主张 | 直接证据 / 命令 | 当前结果 |
|---|---|---|
| 总纲按总—分组织并链接各机制 Owner | 逐节检查 `docs/mechanisms/public-architecture-brief.md`、机制索引和站点导航 | `Inspected` |
| 架构、权限、状态和存储主张能回到当前 Owner | 对照 `ARCHITECTURE.md`、机制页、Compose、源码入口和现有测试 | `Inspected` |
| 文档与决策记录没有空白错误 | `git diff --check` | `Passed` |
| 工程契约和文档构建保持有效 | `python3 scripts/verify_engineering_contracts.py`；`python3 -m unittest scripts.test_verify_engineering_contracts`；`cd docs && pnpm run build` | `Passed — build has existing VitePress/Rolldown plugin warnings only` |
