# 使用命令行工具

`@xerrors/yuxi` 是 Yuxi 的 npm 命令行客户端，要求 Node.js 22 或更高版本。它通过 Public v1 查看智能体、进行纯文本终端对话和查询知识库，并保存多个远程实例。完成本页后，可以在终端连续对话并读取知识库检索结果。

## 安装

从仓库根目录构建并安装本地包：

```bash
cd packages/yuxi-cli
npm ci
npm test
npm pack
npm install --global ./xerrors-yuxi-0.1.0.tgz
yuxi --version
```

`yuxi --version` 输出包版本。调试源码时，使用 `npm run build` 后的 `node dist/cli.js` 代替 `yuxi`；修改 TypeScript 后需要重新构建。

维护者发布到 npm 后，也可以使用 `npm install --global @xerrors/yuxi` 安装，或使用 `npx @xerrors/yuxi --help` 临时运行。包的构建与发布规则见[贡献指南](../develop-guides/contributing.md#候选版本与正式发布)。

## 连接实例

保存实例入口地址，再选择当前实例：

```bash
yuxi remote add production https://example.com
yuxi remote use production
yuxi remote list
yuxi remote ping
```

`remote list` 中的 `*` 表示当前实例；`remote ping` 返回服务健康状态和版本。健康检查只证明 API 进程可达，对话还需要服务端 worker 和模型配置就绪。

配置保存在 `~/.yuxi/config.json`，API Key 以明文保存，POSIX 文件权限为 `0600`。实例地址会去掉末尾 `/api` 并在请求时派生 API 路径；不要填写具体接口地址。修改已有 remote 的 URL 会清除该 remote 的登录凭据。

命令默认使用 `remote use` 选择的实例。`--remote <name>`（简写 `-r`）只覆盖本次命令的选择；`remote ping <name>` 用位置参数指定实例。JSON 配置不读取 Python CLI 的 `config.toml`，需要重新添加实例并登录。

## 登录和退出

浏览器登录使用当前 remote，终端会打印授权码和授权地址：

```bash
yuxi login
```

浏览器中登录 Yuxi 并确认授权后，CLI 保存 API Key。无法自动打开浏览器时，使用 `yuxi login --no-open`，手动打开终端输出的地址。

也可以导入已有 API Key：

```bash
yuxi login --api-key "$YUXI_API_KEY"
yuxi whoami
yuxi status
```

使用环境变量可以避免把密钥值直接写进 Shell 历史，参数仍会出现在本机进程参数中。生产环境使用 HTTPS，保护本地配置文件，不将密钥、配置或授权码放进仓库与公开日志。API Key 的服务端权限见[API Key 接入](../advanced/api-key-integration.md)。

退出当前 remote：

```bash
yuxi logout
```

通过浏览器登录创建的 Key 会在退出时撤销；导入的 Key 没有保存服务端 ID，只清除本地凭据。需要保留浏览器创建的 Key 时使用 `yuxi logout --local-only`。

## 在终端对话

先确认可用智能体，再启动 Chat：

```bash
yuxi agent list
yuxi agent show default-chatbot
yuxi chat --agent default-chatbot
```

Agent 目录和详情展示 Public v1 的公开字段：ID、名称、描述；模型、系统提示词和工具配置属于管理界面。`chat` 默认使用 `default-chatbot`，启动时打印新 Thread ID。在 `> ` 后输入文本，回复逐段显示并保留换行；回复结束后出现新的提示符。

```text
> 请分两行回复，第一行写甲，第二行写乙。
甲
乙
> /exit
```

`/exit` 或 `/quit` 退出终端交互，Thread 历史保留在服务端。失败、取消或事件流在终态前断开时，CLI 返回非零退出码。审批和回答等待点需要在 Yuxi 网页继续；CLI 不提供附件、图片、工具审批或等待点恢复命令。

## 查看 Thread

使用 Chat 打印的 Thread ID 读取状态、历史或提交后续消息：

```bash
yuxi thread list --agent default-chatbot
yuxi thread show <thread-id>
yuxi thread history <thread-id>
yuxi thread send <thread-id> "继续解释第二点"
yuxi thread watch <thread-id>
```

`thread send` 输出持久 Input 回执，接收成功不代表运行完成。`thread watch` 持续输出逐条事件 JSON，在标准错误流中显示恢复 cursor；使用 `--cursor <cursor>` 从该位置订阅，按 `Ctrl+C` 停止观察。Thread、Turn 和 Input 的语义见[智能体 Public API](../advanced/agents-public-api.md)。

查询命令可以加 `--json`，供脚本读取原始响应：

```bash
yuxi agent list --json
yuxi thread show <thread-id> --json
```

## 查询知识库

登录用户通过 Public 工具接口读取自己有权限访问的知识库：

```bash
yuxi kb list
yuxi kb files --kb-id <kb-id>
yuxi kb files --kb-id <kb-id> --query handbook
yuxi kb query --kb-id <kb-id> "如何申请年假？"
```

先用 `files` 找到文件 ID，再打开解析内容或搜索匹配窗口：

```bash
yuxi kb open --kb-id <kb-id> --file-id <file-id>
yuxi kb find --kb-id <kb-id> --file-id <file-id> --pattern "年假"
```

`kb files --query` 只匹配文件名；`kb query` 返回检索结果；`kb open` 返回内容窗口；`kb find` 使用 Public v1 文件查找接口。命令只读，不创建、上传、解析或索引文件。需要脚本处理原始响应时，给这些命令加 `--json`。

## 第一阶段的边界

npm CLI 第一阶段不包含以下能力：

- Agent eval、Langfuse Dataset experiment；
- 知识库上传、解析、索引和管理；
- 旧的本地 HTML Chat；
- MCP、Skill、模型和系统配置管理；
- 附件、图片、工具审批和等待点恢复。

这些能力仍由 Web 界面或各自的后端接口拥有。不要把未列出的管理接口当作 CLI 已支持能力。

## 排查

如果命令找不到，确认全局安装的包和当前构建目录不是两个版本：

```bash
which yuxi
yuxi --version
cd packages/yuxi-cli
npm run typecheck
npm test
npm run build
```

如果 `login` 报服务版本或 discovery 错误，先运行 `yuxi remote ping`，再确认实例提供 Public v1 和 CLI auth 能力。`chat` 返回非零退出时，保留 Thread ID，并用 `thread show`、`thread history` 和服务端日志检查持久状态；不要只依据终端最后一行判断 Turn 结果。
