# 配置系统详解

## 概述

系统采用多层配置架构，模型配置由网页界面管理，应用配置由 Pydantic、PostgreSQL 与 Redis 协同提供。

## 配置层级

```
代码默认值 → 本地 base.toml 兼容默认值 → PostgreSQL 管理员配置
   (低)                                      (高)
```

## 模型配置

由网页统一管理，详见 [模型配置](../intro/model-config.md)。

## 应用配置

配置项定义于 `backend/package/yuxi/config/app.py`。管理员通过系统配置接口修改后，完整配置快照保存到 PostgreSQL `config_options` 表的内部记录 `system_runtime_config`；API 与 worker 启动时均从该记录加载。

### 修改配置

```http
POST /api/system/config
{"key": "default_model", "value": "provider-id:model-id"}
```

管理员更新会先提交 PostgreSQL，再更新当前 API 进程内存并写入 Redis 快照（`yuxi:runtime_config`）。快照包含可运行时同步的公开配置字段，不包含 `_` 开头的内部属性和 `save_dir`；API/worker 进程在启动时从 PostgreSQL 加载并刷新 Redis，之后各自按 5 秒间隔从快照刷新内存值。Redis 不可用时，重启仍可从 PostgreSQL 恢复配置。

`base.toml` 仅保留为本地启动默认值的兼容读取，不再由运行时写入，也不是管理员配置的持久事实。现有数据库为空时不会自动迁移文件内容；数据库中一旦保存管理员配置，其值会在启动时覆盖本地默认值。

`save_dir` 是启动期内部路径配置，不在管理员配置中展示，不从 `base.toml` 覆盖，不写入 PostgreSQL 或 Redis，也不支持通过管理员配置接口修改。sandbox 相关配置仍属于启动期敏感配置，运行中的已初始化组件不承诺完整热更新，修改后需要重启服务保证生效。

### FileStore

`FILESTORE_BACKEND` 默认使用 `s3`，并通过 `FILESTORE_S3_*` 连接 MinIO 或兼容 S3 的对象存储。标准 Docker Compose 会拆分 API 与 worker，因此显式拒绝 `FILESTORE_BACKEND=local`，避免两个进程看到不同文件事实。只有单进程本地测试可以同时设置 `FILESTORE_ALLOW_LOCAL=true`；`FILESTORE_LOCAL_ROOT` 默认是 `saves/filestore`。该开关不适用于 Compose、生产或多实例部署。
