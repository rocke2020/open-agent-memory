# 使用 Docker Compose 运行 OAM

[中文文档](../docs/zh/README.md) · [English](README.md)

## 概述

OAM 的 API、Memory Inspector、工作进程和运维命令使用同一个非 root 镜像，PostgreSQL/pgvector 提供持久化存储。镜像内置锁定的 Python 依赖、默认配置与数据库迁移。默认拓扑只在环回地址对外提供 API。多副本拓扑使用一个本地反向代理、两个 API 和两个工作进程，共用同一个数据库，供模拟本地应用恢复。物理多机和数据库高可用需要单独验证。

首次安装可跟随[第一条有用的记忆](../docs/zh/start-here.md)完成部署、创建用户密钥、带引用召回及新 Agent 会话验证。本页说明部署与运维细节。

## 1. 准备配置

在仓库根目录拉取 v0.2.1 多架构镜像，复制环境变量模板。发布与验证状态见[版本记录（英文）](../docs/releases/0.2.1.md)。

```bash
docker pull ghcr.io/rocke2020/open-agent-memory:0.2.1
cp deploy/.env.example deploy/.env
```

示例通过 `OAM_IMAGE` 选择镜像。若改用源码构建，可运行 `docker build -t oam:0.2.1-candidate .`，并在 `deploy/.env` 中设置 `OAM_IMAGE=oam:0.2.1-candidate`。

编辑 `deploy/.env`，分别设置独立的随机十六进制数据库密码、随机的管理员 API 密钥，以及生成和向量端点各自的端点地址、访问凭据和模型名称。可逐个用 `openssl rand -hex 24` 生成密钥。十六进制密码无需在示例 DSN 中额外转义。该文件由 Git 忽略，请妥善保管。管理员密钥负责用户管理。个人记忆使用独立的用户授权。运行期的 API、队列和内容各使用单独的非超级用户数据库凭据。

`deploy/oam.yaml` 选择 API 密钥认证和独立工作进程，省略的行为设置从镜像中的 `configs/oam.yaml` 继承。若需其他运维配置，可更改只读挂载，确保各进程角色读到同一份文件。Mac 宿主机上的模型端点应通过容器可访问的地址提供服务，例如 `host.docker.internal`，并允许来自 Docker 网络的请求。

## 2. 初始化和启动

在仓库根目录运行以下命令。

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --wait postgres
docker compose --env-file deploy/.env -f deploy/compose.yaml run migrate
docker compose --env-file deploy/.env -f deploy/compose.yaml exec -T postgres psql -U postgres -d oam -f /bootstrap/runtime-roles.sql
docker compose --env-file deploy/.env -f deploy/compose.yaml run api preflight --json
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --wait api worker
```

迁移使用数据库运维权限并初始化管理员凭据。角色脚本仅在相应登录不存在时创建三个运行期登录，并授予迁移已建立的角色。重复执行脚本会沿用现有密码。轮换密码时需同步更新所有副本。预检会发起真实的服务商请求，用于验证模型能力。命名的 PostgreSQL 卷在应用重启后保留。一次性命令容器也保留，供检查结果。

打开 `http://127.0.0.1:17333/ui/`。使用管理员密钥通过 `POST /v1/users` 创建用户，再用首次返回的用户密钥连接 Inspector。参见[管理 API](../docs/zh/api.md#2-管理)和[运维](../docs/zh/operations.md)。运行 [保存、就绪、召回示例](../docs/zh/quickstart.md)验证第一条带引用的记忆。如果端口已占用，请在启动前修改 `OAM_HTTP_PORT`。

### 从 v0.2.0 升级

保留原有 PostgreSQL 卷和私有 `deploy/.env`。在该文件中设置 `OAM_IMAGE=ghcr.io/rocke2020/open-agent-memory:0.2.1`。如果使用自定义 YAML，重启前按[配置升级](../docs/zh/operations.md#升级运维-yaml)调整共享召回设置。

为迁移 0040 安排写维护窗口。迁移从已存储的词法字段回填 BM25 投影，不调用模型。在仓库根目录运行以下命令。迁移失败时先检查报错，再启动升级后的 API 和工作进程。[迁移详情](../docs/zh/operations.md#3-初始化与凭据)

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml pull migrate api worker
docker compose --env-file deploy/.env -f deploy/compose.yaml stop api worker
docker compose --env-file deploy/.env -f deploy/compose.yaml run migrate
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --wait api worker
```

多副本部署的每个命令都添加 `-f deploy/compose.replicas.yaml`，最后的 `up` 命令加上 `gateway`。重启后，用已授权用户召回一条已有索引的来源，并核对其引用。

## 3. 模拟多副本

按前面的步骤初始化数据库，再同时使用两个 Compose 文件。

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml -f deploy/compose.replicas.yaml up -d --wait api worker gateway
```

只有网关在宿主机的环回地址发布端口。API 副本通过网关访问，`X-OAM-Replica` 标识本次选中的上游。示例网关转发原始授权请求头，OAM 继续执行 `api_key` 认证。测试可信身份时，请使用隔离的后端网络和已认证的演示调用方。

滚动替换时至少保留一个就绪的 API 和一个就绪的工作进程。先启动新 API，等待就绪，再重载网关后停止旧 API，最后再次重载。上游设置 `max_fails=0`，避免短暂连接错误导致所有已解析的副本地址被临时移除。建立连接时，符合条件的请求可以尝试其他副本。模拟检查读取可用性。已发送的 POST/PATCH 和已经开始向客户端发送的响应没有透明重放。写入接收结果不明确时，客户端应复用原幂等键。用 `docker compose --env-file deploy/.env -f deploy/compose.yaml -f deploy/compose.replicas.yaml exec gateway nginx -s reload` 重载网关。故障模拟仅停止和启动本次测试的应用进程，保留持久卷。正常 `stop` 会保留容器和数据以供检查。

## 4. 构建并验证发布产物

Dockerfile 通过摘要固定多架构 Python 基础镜像。更新摘要后要重新检查两个架构。Mac 启用 Docker 模拟时可运行以下命令。

```bash
docker buildx build --platform linux/arm64 --load -t oam:0.2.1-arm64 .
docker buildx build --platform linux/amd64 --load -t oam:0.2.1-amd64 .
```

记录每项检查运行于原生架构还是模拟环境。手动候选版本工作流导出 OCI 归档，发布是之后的独立操作。记录实际测试的产物标识，区分本地镜像 ID、OCI 清单摘要和已发布的仓库摘要。[候选版本验证记录（英文）](../docs/release-evidence.md)介绍证据和可选的公开 HTTP 探测。源码可见性变更、发布文件上传、镜像发布和触发发布的推送都在审查后手动执行。

## 5. 可信网关边界

运行 `tests/release/verify_candidate.py` 时，使用单独的演示部署并应用下列显式运维覆盖。常规洞察延迟为 24 小时，与四分钟探测的时间范围不同。覆盖将演示任务的输出预留限制在 300,000 生成 tokens 预算内。保存演示数据前，把合并后的同一运维文件挂载到 API 和工作进程。将 `OAM_RELEASE_BASE_URL`、`OAM_RELEASE_ROOT_KEY` 和 `OAM_RELEASE_OUTPUT` 指向该隔离部署及私有产物目录，然后运行 `uv run python tests/release/verify_candidate.py`。只有演示输入字节和索引代次相同时才复用产物目录。

```yaml
extraction:
  max_output_tokens: 16384
index:
  insight_consolidation_delay_hours: 0.00001
  generation_token_budget: 300000
  insight_seed_policy: source
  insight_max_output_tokens: 8192
  insight_source_batch:
    seed_max: 32
    input_max_tokens: 32768
    target_max: 64
```

内置网关负责转发用户 API 密钥。使用 `auth.mode: trusted` 时，在网关认证调用方，并将 API 监听隔离于仅后端可访问的网络。根据已认证身份重写 `X-OAM-Tenant`、`X-OAM-User` 和 `X-OAM-Role`，丢弃调用方自行提供的断言。非本地可信调用还需要仅由网关添加的管理员 bearer 授权。在个人网关上，转发该授权前拦截 `/v1/users`、`/v1/credentials` 及其子路由和 `/v1/users/{user_id}/grants/{principal_id}`。管理员使用独立的授权路径，调用方只能经由网关访问后端。本地发布模拟用构造的 Basic 认证验证这些控制。外部身份提供商或生产网关需要单独验证。
