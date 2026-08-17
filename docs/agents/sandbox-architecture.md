# 沙盒配置与运维

本文说明如何为 Yuxi 配置 `sandbox-provisioner`、选择 Docker 或 Kubernetes 承载、注入受控运行环境并验证实例。身份派生、虚拟路径、挂载权限、网络隔离、回收与恢复语义见[沙盒机制详解](../mechanisms/sandbox.md)；本页不重复内部实现。

## 选择承载方式

应用层固定使用 `SANDBOX_PROVIDER=provisioner`。provisioner 进程读取 `PROVISIONER_BACKEND` 选择承载方式；Compose 用户应设置宿主环境的 `SANDBOX_PROVISIONER_BACKEND`，Compose 再把它映射为容器内变量。不要在 `.env` 中把两个名称当作同一入口混用。

| backend | 用途 | 是否提供真实隔离 |
| --- | --- | --- |
| `docker` | 默认开发、单机部署；按需创建本机容器 | 是 |
| `kubernetes` | 由目标集群创建 Pod 与 NodePort Service | 是，取决于集群安全配置 |
| `memory` | unit 或占位测试，只保存 ID 到 URL 映射 | 否 |

生产或开发运行不要使用 `memory`。切换 backend 只改变动态沙盒的承载位置，API 和 worker 仍通过同一个 provisioner 认证代理访问沙盒。

## 应用层配置

API 与 worker 使用下面的变量连接 provisioner；实际默认值和 Compose 注入以 `docker-compose.yml` 为准：

| 变量 | 约束 |
| --- | --- |
| `SANDBOX_PROVIDER` | 当前必须为 `provisioner` |
| `SANDBOX_PROVISIONER_URL` | API/worker 可达的 provisioner 地址 |
| `SANDBOX_PROVISIONER_TOKEN` | 管理与代理接口 Bearer token，至少 32 个随机字符 |
| `SANDBOX_VIRTUAL_PATH_PREFIX` | 用户数据虚拟根，通常为 `/home/gem/user-data` |
| `SANDBOX_EXEC_TIMEOUT_SECONDS` | 单次命令执行超时 |
| `SANDBOX_MAX_OUTPUT_BYTES` | 单次命令返回给调用方的最大字节数 |

`SANDBOX_PROVISIONER_TOKEN` 只能提供给 API、worker 和 provisioner。不要把它写进 `sandbox.env`、Agent 用户环境、Skill、日志或文档示例。

## Provisioner 通用配置

Compose 用宿主变量生成 provisioner 容器变量；直接部署 provisioner 时则设置右侧容器变量：

| Compose/.env 输入 | provisioner 容器变量 | 作用 |
| --- | --- | --- |
| `SANDBOX_PROVISIONER_BACKEND` | `PROVISIONER_BACKEND` | `docker`、`kubernetes` 或仅测试使用的 `memory` |
| `SANDBOX_PROVISIONER_URL` | `PROVISIONER_PUBLIC_URL` | 写入每个响应的认证代理 URL；必须从 API/worker 可达 |
| `SANDBOX_IMAGE` | `SANDBOX_IMAGE` | 动态沙盒使用的镜像 |
| `SANDBOX_CONTAINER_PORT` | `SANDBOX_CONTAINER_PORT` | 镜像内 agent-sandbox HTTP 端口 |
| `SANDBOX_HEALTH_TIMEOUT_SECONDS` | `SANDBOX_HEALTH_TIMEOUT_SECONDS` | 实例创建后的健康检查总等待时间 |
| `SANDBOX_IDLE_TIMEOUT_SECONDS` | `SANDBOX_IDLE_TIMEOUT_SECONDS` | 无活动实例的回收阈值 |
| `SANDBOX_IDLE_CHECK_INTERVAL_SECONDS` | `SANDBOX_IDLE_CHECK_INTERVAL_SECONDS` | idle reaper 扫描间隔 |
| `SANDBOX_EXEC_TIMEOUT_SECONDS` | `SANDBOX_EXEC_TIMEOUT_SECONDS` | provisioner 计算安全回收下限时使用的命令超时 |

API/worker 连接地址与 `PROVISIONER_PUBLIC_URL` 通常来自同一个 `SANDBOX_PROVISIONER_URL`，但混合部署时必须确认该地址既能由 API/worker 请求 create/touch，也能访问返回的 `/api/sandboxes/<id>/proxy`。idle timeout 若小于命令超时加 30 秒，运行时会提高到该下限。

## Docker 后端配置

Docker backend 要求 provisioner 能访问宿主机 Docker daemon，并能解析线程数据在宿主机上的真实路径：

| 变量 | 作用 |
| --- | --- |
| `DOCKER_NETWORK_PREFIX` | 每个沙盒独立 bridge 网络的名称前缀 |
| `DOCKER_SANDBOX_PREFIX` | 动态容器名称前缀 |
| `DOCKER_THREADS_HOST_PATH` | `saves/threads` 在宿主机上的绝对路径；未设置时尝试从 provisioner 挂载推导 |

Compose 部署需要把 Docker socket 和 `saves` 对应目录挂入 provisioner。每个沙盒只加入自身网络，provisioner 同时加入该网络并提供认证代理；不要把动态沙盒接入承载 PostgreSQL、Redis、MinIO 等服务的应用网络，也不要把沙盒端口发布到宿主机。

## Kubernetes 后端配置

Kubernetes backend 使用 kubeconfig 或 Pod 内服务账号创建沙盒 Pod 和 NodePort Service：

| Compose/.env 输入 | provisioner 容器变量 | 作用 |
| --- | --- | --- |
| `SANDBOX_K8S_NAMESPACE` | `K8S_NAMESPACE` | 沙盒 Pod 与 Service 所在 namespace |
| `KUBECONFIG_PATH` | `KUBECONFIG_PATH` | provisioner 容器内 kubeconfig 路径；集群内运行时可留空 |
| `SANDBOX_NODE_HOST` | `NODE_HOST` | provisioner 能访问 NodePort 的节点地址 |
| `THREAD_PVC` | `THREAD_PVC` | workspace、uploads、outputs 与 Skills 线程投影使用的共享 PVC |
| `SKILLS_PVC` | `SKILLS_PVC` | 当前实现读取但未进入 Pod 挂载，属于预留字段 |

当前返回给 API/worker 的仍是 provisioner 代理 URL；`NODE_HOST` 只需从 provisioner 可达。Pod 禁用 ServiceAccount token 自动挂载，除非未来由明确威胁模型和实现变更调整。PVC 必须支持 provisioner 选择的访问模式和 `subPath` 目录结构。

## Docker Compose 开发配置

默认开发拓扑由 Compose 启动 API、worker 和 provisioner，再由 provisioner 动态创建短生命周期沙盒；仓库没有“直接在 API 容器执行用户命令”的本地模式。通常以 `.env.template` 与 Compose 的默认字段为起点，仅生成独立的强随机 provisioner token：

这里还需要把 Compose 里的环境变量分两层看。`api` 和 `worker` 关注的是应用层变量，例如 `SANDBOX_PROVIDER`、`SANDBOX_PROVISIONER_URL`、`SANDBOX_PROVISIONER_TOKEN`、`SANDBOX_VIRTUAL_PATH_PREFIX`、`SANDBOX_EXEC_TIMEOUT_SECONDS`、`SANDBOX_MAX_OUTPUT_BYTES`。`sandbox-provisioner` 自己则有另一组变量，负责决定具体如何创建沙盒实例。两层不要混看，否则很容易误以为改了 API 环境变量就能切换底层承载方式。

## 五、Docker 本机后端是如何工作的

当 `SANDBOX_PROVISIONER_BACKEND=docker` 时，`sandbox-provisioner` 会进入 `LocalContainerProvisionerBackend`。它会检查 Docker 是否可用，解析自身容器里 `/app/saves` 这个挂载点在宿主机上的真实路径，并据此推导出线程数据目录。随后它为每组文件线程与 skills 线程准备一个稳定的 `sandbox_id`，把容器命名为类似 `yuxi-sandbox-<id>` 的形式，并在 Docker 网络中启动真正的沙盒镜像。

这个沙盒镜像默认来自 `SANDBOX_IMAGE`，容器内部监听的端口默认是 `8080`。provisioner 会为每个动态沙盒创建独立的 Docker bridge 网络，只把 provisioner 和该沙盒接入其中；沙盒之间不能互访，也不能访问承载 PostgreSQL、Redis、Neo4j、MinIO 等服务的 `app-network`。沙盒端口不发布到宿主机，provisioner 通过对应的独立网络访问真实容器，再以需要 Bearer token 的代理地址向 API/worker 提供文件和命令接口。API/worker 不直接持有沙盒容器地址。

这个拓扑把沙箱按“其中代码可能被完全控制”处理。`SANDBOX_PROVISIONER_TOKEN` 只配置给 API、worker 和 provisioner，绝不能写进 `sandbox.env` 或用户级 Agent 环境变量，否则沙箱会重新获得 provisioner 管理权限。

Docker 后端在启动沙盒时，会挂载用户级 workspace、文件线程级 outputs 和 skills 线程可见的只读 skills。`/home/gem/user-data/uploads` 不再从宿主机挂载，而是位于沙盒自身的临时 `/home/gem` 中；worker 在每次执行前按 Conversation 当前附件集合从 MinIO 清空并重建该目录。这样附件读取不需要 provisioner 与 worker 共享 uploads 路径，sandbox 重建后也能从持久对象恢复。容器的 `/home/gem` 使用 `tmpfs`，满足沙盒镜像启动时的可写要求；当前仍需持久化的 workspace 与 outputs 继续单独挂载。

为了避免长期空闲的沙盒一直占资源，provisioner 还带了一个 idle reaper。它会记录每个沙盒最近一次被 touch 的时间，超过 `SANDBOX_IDLE_TIMEOUT_SECONDS` 之后自动删除。当前默认空闲超时是 120 秒，但如果这个值小于命令执行超时，系统会自动把它提高到“命令超时 + 30 秒”，以免执行中的任务被误回收。

对应到 `docker-compose.yml` 和 `docker-compose.prod.yml`，当前 `sandbox-provisioner` 实际会读取的 Docker 后端相关变量主要是这些：

- 通用变量：`PROVISIONER_BACKEND`、`SANDBOX_IMAGE`、`SANDBOX_CONTAINER_PORT`、`SANDBOX_HEALTH_TIMEOUT_SECONDS`、`SANDBOX_IDLE_TIMEOUT_SECONDS`、`SANDBOX_IDLE_CHECK_INTERVAL_SECONDS`、`SANDBOX_EXEC_TIMEOUT_SECONDS`、`MEMORY_SANDBOX_URL_TEMPLATE`
- Docker 后端变量：`DOCKER_NETWORK_PREFIX`、`DOCKER_THREADS_HOST_PATH`、`DOCKER_SANDBOX_PREFIX`
- 容器代理变量：`HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`

`DOCKER_NETWORK_PREFIX` 用于生成每个沙盒的独立网络名称。`DOCKER_THREADS_HOST_PATH` 也是 Docker 后端专用；如果不显式传入，provisioner 会尝试根据自身容器挂载反推出宿主机路径。

## 六、Kubernetes 后端是如何工作的

当 `SANDBOX_PROVISIONER_BACKEND=kubernetes` 时，`sandbox-provisioner` 会改用 Kubernetes Python 客户端。它会先加载 kubeconfig 或集群内配置，然后在指定的 namespace 中创建一个沙盒 Pod，再创建一个同名的 NodePort Service，把这个 Service 的 `nodePort` 暴露给 Yuxi 后端使用。

Kubernetes 后端下，沙盒还是同一套镜像，还是暴露同样的 HTTP API，但存储方式和暴露方式变了。它不会依赖宿主机 Docker bind mount，而是要求有一个可写的 PVC。当前实现里真正使用的是 `THREAD_PVC`，Pod 会把这块共享存储挂到 `/mnt/shared-data`，再用 `subPath` 把 `threads/shared/<uid>/workspace`、`threads/<file_thread_id>/user-data/outputs` 和 `threads/<skills_thread_id>/skills` 分别挂到对应虚拟目录。uploads 留在 Pod 的 `emptyDir` home 中，同样由 worker 从 MinIO hydrate；子智能体使用父对话作为 `file_thread_id` 读取相同附件事实，同时保持自己的 skills scope。

需要特别说明的是，代码里虽然读取了 `SKILLS_PVC` 这个环境变量，但当前 Pod 规格实际没有使用单独的 skills PVC，而是统一从 `THREAD_PVC` 中切 `threads/<thread_id>/skills` 这个子路径。因此，如果看到环境变量里同时出现 `SKILLS_PVC` 和 `THREAD_PVC`，应当以 `THREAD_PVC` 的真实挂载语义为准，`SKILLS_PVC` 目前更像一个预留字段。

Kubernetes 后端还需要一个 `NODE_HOST`。这是因为当前实现使用的是 NodePort Service，而不是 Ingress，也不是 ClusterIP。provisioner 创建完 Service 后会通过 `http://<NODE_HOST>:<nodePort>` 访问目标沙箱，但返回给 Yuxi 后端的仍是 provisioner 认证代理地址。所以 `NODE_HOST` 必须从 provisioner 可达，不需要直接暴露给 API/worker。

当前 Compose 中与 Kubernetes 后端对应的变量主要是：

- `K8S_NAMESPACE`
- `KUBECONFIG_PATH`
- `NODE_HOST`
- `THREAD_PVC`
- `SKILLS_PVC`

其中真正决定运行时挂载的是 `THREAD_PVC`。`SKILLS_PVC` 目前只保留为代码层读取字段，并没有进入实际 Pod 挂载。

## 七、如果要使用“远程 K8s”，应该怎么接

这里最容易误解的一点是，所谓“选择远程 K8s”，并不是在 Yuxi 页面里点一个开关，然后系统自动发现一个集群。当前实现没有内建集群选择器，也没有多集群管理界面。它的工作方式很直接：我们把 `sandbox-provisioner` 配置成 `kubernetes` 后端，并让它能拿到目标集群的 kubeconfig 或者运行在集群内即可。对 provisioner 来说，只要 Kubernetes 客户端能连上 API Server，这个集群就是它要操作的“远程 K8s”。

如果 Yuxi 部署在 Docker Compose 里，而 Kubernetes 集群在另一台机器或云厂商托管环境中，那么最常见的做法是把本地 kubeconfig 文件挂载进 `sandbox-provisioner` 容器，然后设置 `KUBECONFIG_PATH`。同时把 `SANDBOX_NODE_HOST` 改成一个从 `api` 容器也能访问的节点公网 IP、负载均衡域名，或者已经做过反向代理的地址。

一个典型的 Compose 覆盖配置会长这样：

```yaml
services:
  sandbox-provisioner:
    environment:
      - PROVISIONER_BACKEND=kubernetes
      - K8S_NAMESPACE=yuxi-know
      - KUBECONFIG_PATH=/root/.kube/config
      - THREAD_PVC=yuxi-thread
      - SKILLS_PVC=yuxi-skills
      - NODE_HOST=203.0.113.10
    volumes:
      - ~/.kube/config:/root/.kube/config:ro
```

这段配置表达的意思不是“把整个应用迁到 K8s”，而是“仍然用 Compose 跑 Yuxi 主服务，但沙盒实例改为由远程 Kubernetes 集群承载”。这是当前代码最自然的混合部署方式。

如果 `sandbox-provisioner` 本身就运行在 Kubernetes 集群内部，那么通常不需要显式提供 `KUBECONFIG_PATH`。它会优先尝试 `incluster_config`，也就是使用 Pod 的服务账号权限直接访问 Kubernetes API。此时更需要关注的是 namespace、PVC 和 NodePort 的可达性，而不是 kubeconfig 文件本身。

## 八、当前项目的沙盒文件系统是如何设计的

从模型和工具调用的视角看，Yuxi 主要向 Agent 暴露两类路径：`/home/gem/user-data` 和 `/home/gem/skills`。其中 `user-data` 是可写的用户工作区，`skills` 是只读的技能目录。知识库不再映射为沙盒文件系统路径，模型应通过知识库工具检索和打开文档。

在宿主机侧，和线程相关的数据主要放在 `saves` 目录下。当前可读的目录结构可以概括为下面这样：

```text
saves/
├── skills/
│   ├── <skill-slug>/
│   └── ...
├── threads/
│   ├── <thread_id>/
│   │   ├── user-data/
│   │   │   ├── uploads/
│   │   │   ├── outputs/
│   │   │   └── ...
│   │   └── skills/
│   │       ├── <skill-slug>/
│   │       └── ...
│   ├── shared/
│   │   └── <uid>/
│   │       └── workspace/
│   └── ...
```

这里要重点理解 `workspace`、`uploads` 和 `outputs` 的区别。workspace 是用户级共享目录，位置是 `saves/threads/shared/<uid>/workspace`；outputs 仍属于文件线程目录，位置是 `saves/threads/<file_thread_id>/user-data/outputs`。宿主机 uploads 暂时保留给 API/Viewer 的 legacy 本地物化，但不再挂入 Agent sandbox。普通 Agent 的 `file_thread_id` 是当前对话，子智能体使用父对话作为 `file_thread_id`，因此 worker 会从父对话附件事实 hydrate，并把产物写回父对话 outputs。

运行时 provisioner 只把 workspace、outputs 和 skills 映射到沙盒；uploads 是沙盒内可重建的工作副本，Agent 通用文件后端对它保持只读。容器内虚拟路径仍稳定为 `/home/gem/user-data/uploads`，但它的字节 Owner 是 MinIO、绑定事实 Owner 是 Conversation，不应再从同名宿主机目录推断运行时内容。

## 九、路径暴露规则是什么

Yuxi 不会把整个容器文件系统都开放给 Agent 或 viewer。当前 viewer 根目录只会列出几个命名空间入口，而不会直接暴露 `/` 的真实文件树。这样做是为了避免只看文件树就触发沙盒冷启动，也为了让权限边界更稳定。

`/home/gem/user-data` 是主要工作区。它允许模型和工具写入，但推荐语义并不相同。内置 prompt 中已经明确说明，`workspace` 应当放中间文件，`outputs` 应当放最终产物，`uploads` 是用户上传文件的位置。对于普通对话 Agent，文案甚至提示“非必要不要写 workspace，而优先写 outputs”。

`/home/gem/skills` 是共享与内置 Skill 的只读目录。它不是简单地把 `saves/skills` 整个暴露进去，而是按当前运行时最终生效的共享与内置 Skill，将来源同步到 `saves/threads/<skills_thread_id>/skills`，再把线程目录只读挂进沙盒。个人 Skill 不进入这层投影，Agent 直接读取已经挂载的 `/home/gem/user-data/workspace/agents/skills/<slug>`。

知识库访问不属于沙盒文件系统暴露规则。当前 Agent 可见知识库仍由用户权限和 Agent 配置共同决定，但只通过 `query_kb`、`open_kb_document` 等工具访问，不提供沙盒目录投影。

## 十、skills、知识库、附件是怎么和沙盒结合的

skills 的结合方式分成两层。第一层是提示词层，`prepare_agent_runtime_context` 会先根据当前 Agent 配置的 `context.skills` 展开依赖闭包，`SkillsMiddleware` 再把 `_prompt_skills` 注入到系统提示里，并给出每个 Skill 的真实运行入口。第二层是文件系统层：共享与内置 Skill 由 `sync_thread_readable_skills` 同步到当前 `skills_thread_id` 的线程目录，并只读挂载到 `/home/gem/skills`；个人 Skill 直接使用用户工作区中的 `workspace/agents/skills`，不再生成线程副本。

附件上传后，原件和可选 Markdown 解析结果保存在 MinIO，Conversation 保存文件 ID、对象名、请求绑定和虚拟路径。`metadata.attachments` 是服务端保留字段；worker 消费前还会校验固定 bucket、`file_thread_id/file_id` 对象前缀和规范虚拟路径，不会仅因记录位于当前 Conversation 就使用全局 MinIO 凭据读取它。worker 在模型执行前先提交业务数据库事务，再读取实际 `file_thread_id` 的当前附件集合，通过受信任 sandbox 文件 API 清空并逐个重建 `/home/gem/user-data/uploads`。任一对象读取、清理或写入失败都会阻止本次执行；部分写入失败或取消后还会等待已启动写入到达终点并再次清空，避免旧 Run 回写新旧混合内容。LangGraph state 中的 `uploads` 列表继续把稳定虚拟路径告诉模型，Agent 通用文件后端仍拒绝写 uploads。缺少 MinIO 元数据的历史附件可以通过不跟随符号链接的读取从旧本地普通文件回填；任一历史文件缺失或不安全会阻止本次执行，统一迁移留给后续阶段。

知识库不再与沙盒文件系统结合。它不会被复制到每个线程目录，也不会生成虚拟目录；模型通过专门的知识库工具检索，并在需要更完整上下文时用 `open_kb_document` 按 `kb_id` 和 `file_id` 打开文档内容。

## 十一、当前推荐如何使用 Docker 沙盒

如果只是正常开发、调试或单机部署，最简单也是当前默认的方式就是保留 `SANDBOX_PROVIDER=provisioner`，同时把 `SANDBOX_PROVISIONER_BACKEND` 设为 `docker`。这会让整个项目继续由 Docker Compose 管理，而沙盒实例由 provisioner 动态创建。通常不需要手工 `docker run` 沙盒镜像，也不需要在 Compose 文件里静态声明每一个沙盒容器。

最小必要配置通常就是下面这几项：

```env
SANDBOX_PROVIDER=provisioner
SANDBOX_PROVISIONER_URL=http://sandbox-provisioner:8002
SANDBOX_PROVISIONER_TOKEN=<至少 32 个随机字符>
SANDBOX_PROVISIONER_BACKEND=docker
```

启动与初步检查：

```bash
docker compose up -d
curl --fail http://localhost:8002/health
```

健康响应应报告 `backend=docker`。动态沙盒只在首次文件或命令操作时创建；仅启动 Compose 后看不到沙盒容器是正常现象。

## Kubernetes 接入步骤

1. 在目标 namespace 创建或确认 `THREAD_PVC`，预先验证 provisioner 与沙盒 Pod 都能访问预期 `subPath`。
2. 为 provisioner 提供最小权限的 kubeconfig，或让它在集群内使用受限 ServiceAccount；权限仅覆盖目标 namespace 所需的 Pod 与 Service 操作。
3. Compose 混合部署设置 `SANDBOX_PROVISIONER_BACKEND=kubernetes`、`SANDBOX_K8S_NAMESPACE`、PVC、`SANDBOX_NODE_HOST` 与 API/worker 可达的 `SANDBOX_PROVISIONER_URL`，并把 kubeconfig 只读挂入 provisioner。直接部署 provisioner 时使用对应的容器变量；集群内部署通常不设置 `KUBECONFIG_PATH`。
4. 从 provisioner 所在网络验证 Kubernetes API 和 `http://<NODE_HOST>:<nodePort>` 可达。API/worker 无需直接访问 NodePort。
5. 创建测试线程触发真实 shell 与文件读写，再核对 Pod、Service、PVC 文件和 provisioner 代理响应。

当前没有多集群选择 UI、Ingress backend 或自动节点发现。需要这些能力时应作为明确的部署功能实现，不能只通过文档假设存在。

## 沙盒运行环境

动态沙盒的环境由两类来源合并：provisioner 读取的全局 `docker/sandbox_provisioner/sandbox.env`，以及当前用户为 Agent 配置的环境变量；用户级值覆盖同名全局值。它们都会对沙盒内代码可见，应按可被不可信代码读取和外传来处理。

只注入任务真正需要的低权限变量。禁止注入 provisioner token、数据库凭据、对象存储管理凭据、云平台管理员密钥和其他租户秘密。代理变量可以配置，但应限制目标网络并避免让沙盒进入应用内部网络。

远程 Skill 拉取使用不继承全局和用户环境的一次性 sandbox；不要依赖 `sandbox.env` 为 Skill 安装提供凭据。Kubernetes 沙盒同样禁用 ServiceAccount token 自动挂载。

## 验证与排障

按下面顺序验证，避免把应用、provisioner、实例和文件路径问题混在一起：

1. 调用 provisioner `/health`，确认 backend、idle timeout 和依赖初始化状态。
2. 触发一个真实线程的 shell 命令与 `outputs` 写入，确认创建或复用的是该线程对应实例。
3. Docker 检查独立网络、挂载和 provisioner 代理；Kubernetes 检查 Pod、NodePort Service、PVC `subPath` 与 `NODE_HOST` 可达性。
4. 分别从沙盒 API 和 viewer 读取同一个虚拟文件，确认虚拟路径解析到同一所属线程；HTTP 状态仅作为接口可达性证据。
5. 等待超过 idle timeout，确认实例被回收、持久文件仍存在，并能在下一次操作重建实例。

常见错误应优先检查：应用层 URL/token 与 provisioner backend 是否混配、Docker host path 是否推导错误、Kubernetes PVC 子目录是否缺失、file thread 与 skills thread 是否取错、以及 provisioner touch 失败后复用的实例是否已经失效。进一步定位使用[沙盒机制详解](../mechanisms/sandbox.md)中的 Owner 和失败边界。

## 配置来源

变量名与注入位置以 `docker-compose.yml`、`docker-compose.prod.yml`、`.env.template` 和 `docker/sandbox_provisioner/app.py` 为准。本页只解释运维语义，不复制镜像标签或全部默认值；修改配置时同步检查这些 Owner 与部署模板。

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `PROVISIONER_BACKEND` | 底层后端类型，`docker` 或 `kubernetes` | `docker` |
| `SANDBOX_IMAGE` | 沙盒容器镜像 | 详见 compose 文件 |
| `SANDBOX_CONTAINER_PORT` | 沙盒容器内部端口 | `8080` |
| `SANDBOX_IDLE_TIMEOUT_SECONDS` | 空闲回收时间 | `120` |
| `SANDBOX_HEALTH_TIMEOUT_SECONDS` | 健康检查超时 | `300` |

**Docker 后端专用：**

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `DOCKER_NETWORK_PREFIX` | 每沙盒独立网络的名称前缀 | `yuxi-know-sandbox` |
| `DOCKER_SANDBOX_PREFIX` | 沙盒容器名前缀 | `yuxi-sandbox` |
| `DOCKER_THREADS_HOST_PATH` | 线程数据宿主机路径 | 自动推断 |

**Kubernetes 后端专用：**

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `K8S_NAMESPACE` | Kubernetes namespace | `yuxi-know` |
| `NODE_HOST` | Kubernetes 节点地址 | `host.docker.internal` |
| `KUBECONFIG_PATH` | kubeconfig 文件路径 | 空（使用 incluster 配置） |
| `THREAD_PVC` | 线程数据持久化卷 | `yuxi-thread` |
| `SKILLS_PVC` | 技能目录持久化卷（预留） | `yuxi-skills` |

### 环境变量传递链

```
宿主机 .env / 系统环境变量
         ↓
    docker-compose.yml
         ↓
    ┌────────────────────────────────┐
    │  api/worker 服务               │  应用层变量 (SANDBOX_*)
    │    SANDBOX_PROVISIONER_URL     │
    │    SANDBOX_PROVISIONER_TOKEN   │
    └────────────┬───────────────────┘
                 ↓  带 Bearer token 的 HTTP 调用
    ┌────────────────────────────────┐
    │  sandbox-provisioner 服务       │  沙盒层变量 (PROVISIONER_BACKEND, DOCKER_*, K8S_*)
    │    PROVISIONER_BACKEND         │
    └────────────┬───────────────────┘
                 ↓  Docker API / K8s API + 认证 HTTP 代理
    ┌────────────────────────────────┐
    │  动态创建的沙盒容器              │
    └────────────────────────────────┘
```

两层变量不要混看。改了 `api/worker` 的 `SANDBOX_PROVISIONER_URL` 只是改了后端找 provisioner 的地址；改了 `sandbox-provisioner` 的 `PROVISIONER_BACKEND` 才是改了 provisioner 本身用什么方式创建沙盒。

### sandbox.env 的特殊作用

`docker/sandbox_provisioner/sandbox.env` 文件的用途与上述两层变量不同。它通过 volume 挂载到 provisioner 容器内 (`/app/sandbox.env`)，然后由 `LocalContainerProvisionerBackend` 在创建沙盒容器时读取，解析后的键值对会作为**环境变量注入到每个动态创建的沙盒容器**中。

```yaml
# docker-compose.yml 中 sandbox-provisioner 的挂载
sandbox-provisioner:
  volumes:
    - ./docker/sandbox_provisioner/sandbox.env:/app/sandbox.env:ro
```

也就是说，`sandbox.env` 配置的是沙盒容器内部可见的环境变量，而不是 provisioner 本身的配置。当前该文件内容为：

```env
CHECK_YUXI_SANDBOX_ENV_EXISTS=True
```

如果需要给所有沙盒容器注入额外的环境变量（如代理配置、认证信息等），可以添加到 `sandbox.env` 文件中。

远程 Skill 拉取使用专门的一次性 Sandbox，不继承这里的全局环境变量或用户级 Agent 环境变量，避免不可信仓库通过复制文件带出凭据。Kubernetes 创建的 Sandbox 同时会禁用 ServiceAccount token 自动挂载。

### 配置方式汇总

| 配置目标 | 配置位置 | 示例变量 |
|----------|----------|----------|
| 应用层连接 provisioner | `.env` 或 compose 环境 | `SANDBOX_PROVISIONER_URL`, `SANDBOX_PROVISIONER_TOKEN` |
| provisioner 自身行为 | `.env` 或 compose 环境 | `PROVISIONER_BACKEND`, `DOCKER_*` |
| 沙盒容器内部环境 | `sandbox.env` 文件 | 代理、认证等运行时变量 |

## 十四、和旧版文档相比，今天最重要的理解方式

当前项目不应再按“应用直接管理一个长期存在的本地 sandbox 服务”去理解。更准确的认识应该是：Yuxi 只管理线程和上下文；provisioner 负责创建线程对应的沙盒实例；文件系统不是简单地暴露一个容器根目录，而是把可写工作区、只读 skills 等组合成一个受控命名空间（知识库不再映射为沙盒目录，改由 `query_kb`/`open_kb_document` 等工具访问）。

因此，当你在界面上“启用沙盒”或者在文档里“选择 K8s”时，本质上做的不是切换一段业务逻辑，而是在切换 provisioner 的底层实例承载方式。选择 `docker` 时，沙盒由当前部署机上的 Docker daemon 动态创建；选择 `kubernetes` 时，沙盒由目标 K8s 集群动态创建。Yuxi 自己始终只面对一个 provisioner 服务地址。

## 十五、排障时建议先看什么

如果怀疑是 provisioner 级问题，先看 `http://localhost:8002/health`，确认 backend 类型和 idle timeout 是否符合预期。默认 Docker 部署下这里应看到 `backend=docker`。接着看 `docker logs sandbox-provisioner --tail 200`，因为这里能直接看到创建容器、复用旧实例、健康检查失败和 idle reaper 删除的日志。

如果怀疑是 Docker 地址不可达，先确认每个动态沙箱只连接自己的 `yuxi-know-sandbox-<id>` 网络，provisioner 同时连接该网络，而 API/worker 只在 `app-network`。provisioner 日志中的目标地址应是动态容器名，API/worker 拿到的地址应是 `/api/sandboxes/<id>/proxy`；代理请求必须携带 `SANDBOX_PROVISIONER_TOKEN`。如果怀疑是 Kubernetes 地址不可达，重点检查 `NODE_HOST` 和 NodePort 是否从 provisioner 可达。

如果附件在 Viewer 可见但模型读不到，先检查 Conversation 中的对象元数据和 MinIO 对象，再检查 worker hydrate 错误与 sandbox 内 `/home/gem/user-data/uploads`；不要再以宿主机 uploads 是否存在判断 Agent 可见性。workspace、outputs 或 skills 问题仍需分别检查宿主机/PVC 路径、当前 `file_thread_id` / `skills_thread_id` 和实际挂载。
