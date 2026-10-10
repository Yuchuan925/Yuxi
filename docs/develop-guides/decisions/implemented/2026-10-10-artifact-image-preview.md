# 交付物按展示类型分组

状态：implemented
类型：feature
Owner：backend/yuxi/modules/extensions/tools/builtin/present_artifacts.py

## 问题

智能体展示截图时，对话只提供文件卡片，用户无法直接看到图片内容。图片与文件共享交付物身份，但需要不同的预览布局。

## 决策

### 实现方案

`present_artifacts` 接受 `type: file | image`，默认 `file`，一次调用的路径共享指定类型。工具沿用已有普通文件与用户范围校验，登记 `{path, type}`。Agent state reducer 按路径保序去重，同一路径采用最近登记的类型；已有 checkpoint 的路径字符串按 `file` 读取。

前端从成功的工具调用恢复路径与类型，失败的 ToolMessage 明确标记 error，将图片和文件放入两个展示组。图片宽度 300px，小屏受容器宽度限制，预览高度限制在 100–200px，以 object-fit: cover 保持比例并裁切填满预览框，同一行卡片底部对齐；图片读取复用带鉴权、有预览预算的 artifact API，加载失败保留文件操作。两组共用打开预览、下载与保存能力，侧栏继续作为全部交付物的文件索引。内置图片生成 Skill 的交付步骤指定 image；Agent Demo 在读取 state 时兼容对象与旧路径，保留原下载列表。

## 替代方案

- 按扩展名自动选择展示：无法让智能体显式把图片作为普通文件交付。
- 单独增加图片资源及工具：重复交付物身份、路径边界和文件操作。
- 仅从调用参数恢复类型，state 只留路径：state 与消息投影表达不同事实，后续 consumer 无法读取展示类型。

## 验证

| 验收主张 | 语义 Owner | 直接证据与负向案例 | 结果 |
|---|---|---|---|
| 指定 image 登记类型，省略保持 file | 工具 schema 与 state reducer | 后端 unit 验证非法类型、不存在文件、旧路径与重复登记，以及 checkpoint 中断恢复 | Passed |
| 类型在 PostgreSQL 和 HTTP 快照中保留 | checkpoint state 读取用例 | 真实 PostgreSQL / HTTP 验证旧路径与新对象，其他用户读取被拒绝 | Passed |
| 图片与文件独立分组，失败调用不显示交付物 | 消息投影与交付物组件 | 前端 unit 验证混合类型、显式 file 的 PNG、失败调用、折叠、请求失效和 Blob URL 释放 | Passed |
| 图片宽度 300px、保持比例、小屏不溢出 | 交付物组件 | 浏览器挂载实际 Vue 组件，使用固定图片与 404 路由 fixture；DOM 回读为 300×180（原图 600×360），375px 页面宽度为 375；检查浅色、深色、长名称与错误截图 | Passed |
| 预览高度在 100–200px 内，按比例裁切填满且同一行底部对齐 | 交付物组件 CSS | 浏览器以 1:2、1:1、2:1、3:1、5:1、10:1、100:1 的 SVG 响应回读 DOM 尺寸和行底部；高度依次为 200、200、150、100、100、100、100px，fit 为 cover，桌面每行底部相同；检查浅色、深色和 375px 截图。恢复 50–250px 与 contain 样式时因 1:2 图片高度 250px 和 fit 不符而失败 | Passed |
| Demo 下载兼容带类型对象和旧路径 | Demo state consumer | 浏览器回读列表、实际下载字节及带身份的编码路径 | Passed |

执行命令：

```bash
# 标准命令受现有容器 yuxi.egg-info 写权限阻塞；以下使用已安装依赖。
docker compose exec -T api uv run --no-sync --group test pytest test/unit -m 'not slow' -q
docker compose exec -T api uv run --no-sync --group test pytest test/integration/api/test_checkpoint_state_view.py -q
docker compose exec -T frontend pnpm run lint:check
docker compose exec -T frontend pnpm run test:unit
docker compose exec -T frontend pnpm run build
python3 scripts/verify_engineering_contracts.py
python3 -m unittest scripts.test_verify_engineering_contracts
cd docs && pnpm run build
cd packages/agent-demo && npm run lint && npm test && npm run build
cd packages/agent-demo && npm run test:browser -- --grep '历史回读、流式正文'
git diff --check
```

后端 unit 为 2628 passed、55 skipped；容器未挂载完整仓库导致部分静态配置检查跳过，根目录工程契约与其 64 项单元测试另外通过。前端 unit 为 526 passed，checkpoint HTTP integration 为 2 passed，Demo unit 为 13 passed，目标浏览器测试为 1 passed。浏览器组件检查沿用实际 Vite 模块和 artifact API 客户端，以受控预览响应验证 DOM 与交互；真实模型生成图片和完整 worker 图片交付未执行。

`docker compose exec -T api uv run --no-sync --group test pytest test/integration/api/test_debug_message_projection.py test/integration/api/test_chat_router.py test/integration/api/test_checkpoint_state_view.py -q` 的集成复核在 fixture 准备阶段出现 24 errors：PostgreSQL 进程异常退出并进入恢复，未到达业务断言。表中的 HTTP Passed 来自上述成功验证；当前运行环境未完成这组复核。

## 后果

已有 checkpoint 的路径列表由 reducer 和前端归一化入口继续读取，新登记保存路径和展示类型。新增公开对象格式要求当前 consumer 读取 path；仓库外直接消费字符串列表的集成需要同步适配。图片预览请求沿用已有授权和读取预算；组件切换或销毁时失效请求不发布结果，已创建的 Blob URL 被释放。图片内容无法预览时显示错误入口并保留共享文件操作。
