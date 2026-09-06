# Skills：把可复用方法按需装入一次运行

Skill 将说明、脚本和依赖组织成可复用能力。Yuxi 在运行前根据来源、用户权限、Agent 配置、部署模式和依赖闭包确定有效 Skill，并在模型真正需要时开放对应工具与 MCP。本页只解释对外架构关系；安装、共享范围、配置语义、文件安全和加载失败由[Skills 管理说明](../agents/skills-management.md)负责。

## 架构关系

```text
Built-in / Shared / Personal
             │
             ▼
Permission + Agent selection + LITE + dependency closure
             │
             ├─ Preload: 首轮完整说明与依赖
             └─ Progressive: 读取 SKILL.md 后激活
                                │
                                ▼
                         Local tools / MCP
```

内置、共享和个人 Skill 先经过权限与 Agent 选择筛选。预加载能力从首轮进入上下文；普通 Skill 在模型读取根说明后才开放对应依赖。共享文件以只读投影进入 Sandbox，个人文件留在用户工作区。完整规则以 owning 页面为准。

## 源码定位与验证

- [Skills 管理说明](../agents/skills-management.md)
- [Skill runtime](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/agents/skills/runtime.py)
- [Skills middleware](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/agents/middlewares/skills.py)
