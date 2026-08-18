# 依赖供应链审计门禁

状态：implemented
类型：process
Owner：.github/workflows/dependency-audit.yml

## 问题

Python 与 Node.js 的锁定依赖没有漏洞和许可证审计 gate。新增或更新直接依赖时，传递闭包中的已知漏洞、强 copyleft 或未知许可证可以在合并前保持不可见。

## 决策

新增依赖审计 workflow，并以 shipping 锁文件和安装环境为事实来源。Python 漏洞直接运行 `uv audit`，Node.js 漏洞直接运行 `pnpm audit`；backend 暂时受 PyTorch 版本约束的 advisory 使用工具原生 `--ignore` 明确列出。Python 许可证使用 `pip-licenses` 输出 backend 与 yuxi-cli 的生产传递依赖报告，供 Review 发现变化，不自动判断法律兼容性。Dependabot 定期更新 Python、Node.js、Dockerfile、Compose 与 GitHub Actions 依赖。

容器镜像扫描不进入本次首个阻断 gate。当前生产镜像由多个本地 Dockerfile、Compose 基础镜像和外部 sandbox 镜像组成，必须先确定构建产物、扫描时点和基础镜像例外 Owner；本次仅通过 Dependabot 跟踪 Docker 基础镜像更新。

## 替代方案

- 立即要求所有锁文件零漏洞：backend 当前受 PyTorch/torchvision 版本约束，会让新增 gate 从第一天起不可用。
- 让审计步骤 `continue-on-error` 或使用 `|| true`：只产生日志，不形成合并拒绝后果。
- 只扫描直接依赖声明：无法覆盖本问题关注的传递依赖。
- 自动维护包、版本与许可证允许清单：复制锁文件和包 metadata，形成需要人工同步的第二事实源。

`backend/package/uv.lock` 不是 API、worker、镜像或发布流程的安装输入，shipping 依赖由 `backend/uv.lock` 拥有。本变更删除该重复锁文件，避免出现未被执行入口消费的第二份依赖闭包。初始探针提到的 `igraph` 与 `pymupdf` 只存在于这份陈旧锁文件，当前依赖声明和 shipping 锁不包含二者。

## 后果

- 漏洞数据库与包许可证元数据会变化，网络故障也会导致 gate 失败；失败必须保留工具输出，不静默降级。
- 许可证报告不构成法律意见，也不自动阻断；Review 需要结合分发方式与上游许可证文本判断。
- Python 锁文件升级会改变运行依赖；本次只升级审计直接发现且已有兼容修复版本的包，并运行现有测试范围。Node.js 只调整三个已知高危项。

## 验证

运行 `make audit-dependencies`、`make audit-licenses`、`python3 scripts/verify_engineering_contracts.py`、`python3 -m unittest scripts.test_verify_engineering_contracts`、backend unit、web lint/unit/build 与 `git diff --check`。文档依赖审计通过；文档构建因本地 `docs/node_modules` 权限状态损坏，隔离容器重新安装又受网络超时影响，未形成通过证据。
