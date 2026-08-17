# 保留 GPL/AGPL 第三方镜像并文档化许可证边界

状态：implemented
类型：process
Owner：docker-compose.yml

## 问题

Compose 拓扑默认引入 Neo4j 社区版（GPL-3.0-only）与 MinIO（AGPL-3.0）镜像，而仓库又通过 `docker/save_docker_images.*` 与 `scripts/init.*` 拉取并导出这些镜像，Yuxi 本体是 MIT。缺少书面边界时，部署者与再分发者无法判断 GPL/AGPL 义务是否触发，商业部署也没有可依赖的合规入口（#873）。同时 `neo4j:5.26` 是浮动 minor 标签，同一引用在不同时间解析到不同补丁版本（撰写本记录时 Docker Hub 已指向 5.26.29，而既有部署实际运行 5.26.28），镜像清单不可复现。

## 决策

保留 Neo4j 社区版与 MinIO 作为独立进程依赖，Yuxi 后端仅通过 bolt 与 S3 API 通信（进程间聚合），MIT 代码不构成衍生作品。把组件许可证对照、再分发义务和商业替代选项写入 [deployment.md](../../../advanced/deployment.md) 的「第三方组件与许可证」章节，README 许可证节指向该章节。所有 Neo4j 镜像引用（两个 Compose 文件与四个拉取/导出脚本）统一锁定精确补丁版本 `neo4j:5.26.28`，即当前部署与数据卷已验证的版本；后续补丁升级必须显式修改该引用。

## 替代方案

- 替换为宽松许可证存储：MinIO 是 Milvus standalone 的必要依赖，替换图数据库属于架构级变更，超出本记录范围。
- 默认使用 Neo4j Enterprise / MinIO 商业订阅：需要商业协议与凭据，不应成为开源默认值，仅作为文档中的商业选项。
- 移除 `save_docker_images` 分发脚本：切断离线部署能力，代价大于收益；改为在文档中明确分发义务。
- 锁定镜像 digest 而非版本 tag：可复现性最强，但可读性差且无法表达补丁升级意图，作为后续可选强化。
- 引入 CI 依赖与许可证审计：由独立事项（#870）承接。

## 后果

部署与再分发者获得单一可引用的合规边界；镜像版本可复现，Neo4j 补丁升级从隐式浮动变为显式提交评审，代价是安全补丁更新需要主动改 tag。GPL/AGPL 组件保持未修改使用；若后续修改这些组件、以其构建衍生镜像或改为进程内集成，本记录的边界不再适用，需重新评估。文档声明为工程侧整理，不替代法务判断。

## 验证

| 验收主张 | 直接证据 | 结果 |
|---|---|---|
| `neo4j:5.26.28` 标签存在且为社区版 5.26.28 | `docker pull neo4j:5.26.28`；镜像 `NEO4J_TARBALL=neo4j-community-5.26.28-unix.tar.gz` | Passed |
| 浮动标签已漂移，锁定引用消解该风险 | `docker pull neo4j:5.26` 解析为 5.26.29，digest 与 5.26.28 不同 | Passed |
| 全部 Neo4j 引用已锁版本且 Compose 可解析 | `grep -rn "neo4j:5.26"`；`docker compose config -q`（dev/prod） | Passed |
| 文档构建与工程契约检查通过 | docs `pnpm build`；`python3 scripts/verify_engineering_contracts.py` | Passed |
