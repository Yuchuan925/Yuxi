# 文档解析产物与资源发布边界

状态：implemented
类型：architecture
Owner：backend/yuxi/infrastructure/document_parsing/parser.py

## 问题

解析引擎在转换过程中上传图片并生成知识库 URL，聊天附件与 OCR 工具无法保存可搬运的完整产物。图片缺少文档级归属，部分引擎吞掉资源失败后仍返回成功文本。

## 决策

### 实现方案

`infrastructure/document_parsing/parser.py` 的 `parse` 接收本地输入、输出目录与已解析的参数，生成 `document.md` 及相对引用的资源，返回 `ParseResult`。输出目录由调用方管理，轻量契约集中在包入口，格式转换由 `parser.py` 拥有，资源与归档处理由 `artifacts.py` 拥有；引擎契约、声明和惰性装配集中在 `engines/` 包入口，引擎统一位于 `engines/`，不访问 MinIO、业务数据库或知识库 URL。资源缺失、非法路径及写入失败明确拒绝。

`modules/documents` 解析系统配置和 MinIO 输入；`parse_to_hosted_markdown` 接收明确的上传位置与 URL 构造能力，上传资源后返回 Markdown。知识库在文档下以一次解析的唯一前缀发布资源与 Markdown，持久化结果后回收旧产物，失败回收本次产物。聊天临时解析保存用户隔离的本地目录，确认时完整复制到 Project Workdir，删除附件时删除整个解析目录；OCR 工具同样写入完整目录。

管理员配置定义分别由 documents、extensions 与 system 拥有，system 统一汇总并管理持久化、脱敏和缓存。无产品调用方的独立 Markdown 转换 HTTP 接口删除。项目为全新体系，不提供旧资源迁移或旧 Python 入口兼容。

## 替代方案

- 保留上传回调：引擎继续混合转换和存储副作用，不能保证本地产物完整。
- 只缩窄知识库回调：减少部分耦合，但聊天仍依赖 MinIO。
- 为每种资源引入发布状态机：增加没有当前消费者的维护表面；采用一次解析目录和现有文件状态即可。
- 保留独立转换接口：仓库仅测试使用，没有产品调用方，删除其长期维护表面。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 解析目录完整且不上传 MinIO | 图片丢失或隐式上传 | document_parsing | parser unit，回读 Markdown 与图片 | 非法 ZIP 路径、缺失资源、迟到线程写入、特殊文件名 | Passed |
| 聊天确认和删除覆盖资源目录 | 孤立资源、跨用户读取 | agents/services/attachments.py | 附件 unit 与真实 HTTP 文件链路 | 他人/APP 解析路径、未提交原文件取消回收 | Passed |
| 知识库资源按文档回收 | 失败残留、误删其他文档 | knowledge/base.py | 真实 worker、MinIO 与 PG 文件记录回读 | 上传取消/失败、真实 PG lease/owner 丢失、其他文档资源隔离 | Passed |
| 配置定义归属模块且统一管理 | 循环导入、配置失效 | modules/system/options.py | options 与 OCR 配置测试 | 无效 OCR 引擎、缓存过期值 | Passed |

旧能力不存在：已搜索旧引擎路径、上传回调及 `/files/markdown` 产品入口，真实 HTTP integration 确认路由返回 404。

重新引入条件：独立转换存在明确消费者、资源生命周期与授权 Owner 时，单独建立契约。

- 相关后端 unit 集合通过；覆盖真实 Office 图片、相对链接身份、取消后 I/O 完成与回收、配置、聊天目录和 OCR 发布失败。
- `docker compose exec api uv run --no-sync pytest test/e2e/test_document_parsing_artifacts_e2e.py -q -p no:cacheprovider`：2 passed。真实 HTTP、worker、PG、MinIO 和 Workspace 回读证明完整目录、权限、删除、owner 丢失后回收及结果替换；确定性 MinerU 协议服务提供 fixture。
- `docker compose exec api uv run --no-sync pytest test/integration/api/test_knowledge_router.py -k 'standalone_markdown or image' -q -p no:cacheprovider`：3 passed，覆盖删除接口与知识库图片访问。
- Web unit 385 passed；lint 与 build 通过。Playwright 在真实 Vite 页面挂载 AgentFilePreview，鉴权访问真实 Thread artifact，相对图片以 blob 显示，DOM 回读尺寸 160×80；截图保存在测试环境的临时目录。
- 独立 Reviewer 复现并检查文件副作用、发布边界、图片引用与阅读顺序；验证从最终文件、对象及数据库记录读取结果。

- `docker compose exec -e PYTHONDONTWRITEBYTECODE=1 api uv run --group test pytest test/unit -m 'not slow' -q --timeout=45 -p no:cacheprovider`：2280 passed、55 skipped。Skill middleware 快照显式提供知识工具依赖，worker terminal-loser unit 显式替代与其终态断言无关的 checkpoint 用量读取；生产行为不变。
- 两引擎的标题、引用式及远端图片链接使用完整路径映射统一改写；并发引擎配置调用返回各自请求的实例。新回归先复现缺失本地图片与错误配置实例，修复后两个相关解析 unit 文件 21 passed。
- `docker compose exec -e PYTHONDONTWRITEBYTECODE=1 api uv run --no-sync --group test pytest test/integration/api/test_knowledge_access_resolution.py test/integration/api/test_knowledge_router.py test/integration/services/test_project_workdir_provisioner.py -q -p no:cacheprovider --timeout=90`：39 passed，包含真实 sandbox 的完整目录复制与图片回读。已删除 HEAD 中没有接口和产品消费者的两项虚拟目录迁移旧测试。
- `python3 scripts/verify_engineering_contracts.py` 通过；`python3 -m unittest scripts.test_verify_engineering_contracts`：64 passed；`cd docs && pnpm run build`、本次 Python 文件 ruff 与 `git diff --check` 通过。

## 后果

取消请求等待当前不可取消的同步 I/O 完成，再回收本次目录或对象；响应时延因此受引擎超时约束。Office 公式转换依赖显式安装的 pylatexenc，转换失败不返回静默替代内容。

其他云端 OCR 与真实 GPU 引擎未执行。聊天临时目录需要跨 API 进程可见，使用现有共享 UserWorkspace，由 filesystem boundary 执行 no-follow 操作。进程崩溃的远端残留不承诺即时清理，最终文档删除按文档前缀回收。
