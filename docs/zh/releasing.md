# 准备并发布新版本

[English](../releasing.md) · 简体中文

## 概述

产品发布对 Python 发行包、CLI、服务端镜像和 GitHub 源码标签使用同一版本。产品版本由 `pyproject.toml` 定义，`uv.lock` 记录解析后的发行包。下文以 0.2.1 的发布流程为例，[该版本的发布记录（英文）](../releases/0.2.1.md)说明验证范围并链接到发布产物。准备发布时需验证拟发布源码及其构建产物。发布需要维护者授权发布该确切版本。

接入包使用独立版本号。客户端变更时构建有版本号的新归档，未变更时复用既有归档。GitHub Release 归档可以直接安装，npm 发布是独立渠道。保留既往的产品标签、镜像和发布记录。

## 1. 审阅发布说明和文档

`pyproject.toml` 中的每个新产品版本都需要一份独立的发布记录 `docs/releases/<version>.md`。可参考[首次发布说明（英文）](../releases/0.0.1.md)。**Changes** 和 **Upgrade** 两节说明新增功能、行为变更、问题修复、兼容性或迁移操作，以及已知限制。将两节内容同时用作 GitHub Release 说明。保留历史发布记录原样，产品版本号递增无需改动历史版本引用和独立编号的插件包。

设置目标版本后，将本次需要纳入的新文件暂存到 Git，再基于上一个产品标签生成草稿。例如，从 v0.2.0 准备 0.2.1 审阅稿时，可将扫描结果写入临时文件。

```bash
mkdir -p tmp
python tools/check_release_docs.py --base v0.2.0 --prepare > tmp/release-0.2.1-draft.md
git diff v0.2.0 -- src configs migrations deploy integrations pyproject.toml
```

扫描器会清点 Git 跟踪的每个 Markdown、reStructuredText 和 AsciiDoc 文件，包括翻译、子目录中的集成指南和仓库说明。它列出自上一个产品标签以来发生变更的文件。根据变化后的源码及其调用方，逐份审阅文档的行为描述、示例、命令和配置。更新受影响的文档，并在最后一次修改源码和文档后重新生成草稿。写完发布说明后，为每个待审阅条目填入以下两种结果之一和具体理由，必要时引用相关源码路径。

```markdown
- **updated** `docs/api.md`: Documented the new request field implemented in src/oam/contracts/.
- **no update needed** `deploy/README.md`: Deployment commands and configuration are unchanged.
```

构建或发布前运行检查器。以下示例的上一个产品标签为 `v0.2.0`。

```bash
python tools/check_release_docs.py --base v0.2.0
```

检查器会拒绝缺失的发布说明、遗漏或未完成的文档审阅、没有修改相应文档的 `updated` 声明，以及随 Git 跟踪文件内容变化而过期的审阅。当前发布记录不计入自身的指纹，因此补全发布说明不会使审阅过期。生成草稿前须暂存新增文件，已删除的文档会退出当前清单。**Base** 字段记录上一个产品标签的提交，**Scope** 保存已审阅的 Git 跟踪输入的指纹。缺少 Git 历史或标签会导致检查失败，应先获取完整的提交历史和标签。检查器只读文件，不调用模型或外部服务。

CI 在拉取请求、`main`、产品标签和手动运行中检查产品版本变更。候选版本构建镜像之前也执行该检查。历史版本 `v0.0.1` 之后，在产品标签的确切提交上，自动检查以上一发布版本为基线，要求完成审阅且标签与版本相符。两次发布之间，只要产品版本未变，便跳过发布要求。编码智能体或发布者须负责内容核对。结构检查通过只能证明规定的审阅条目已填写且仍然有效。既往发布无需补写记录，只发布插件时继续使用插件自己的版本号和发布说明。在仓库支持的分支和标签保护设置中，将 **Release documentation / review** 设为必需状态。本地检查无法阻止维护者在流程之外手动发布。

## 2. 验证当前源码与升级

运行仓库检查并测试每个集成包。

```bash
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
npm --prefix integrations/openclaw ci
npm --prefix integrations/openclaw test
npm --prefix integrations/mcp ci
npm --prefix integrations/mcp test
npm --prefix integrations/dsh test
```

缺少显式隔离的基础设施时，默认 pytest 运行可能跳过数据库或浏览器用例。分别记录实际执行和跳过的检查。0.2.0 的升级验证应将迁移 0038 和 0039 应用到隔离的 v0.1.0 数据库，并确认可继续使用现有索引。按[变更验收标准](acceptance.md#11-v020-变更验收)验证画像策略和自定义生成、共享洞察窗口及召回覆盖参数。

针对 v0.2.1，执行[变更验收](acceptance.md#12-v021-变更验收)，并在已有索引的数据上验证[迁移 0040 升级](operations.md#3-初始化与凭据)。历史版本 v0.1.0 使用[对应标签的验收标准（英文）](https://github.com/rocke2020/open-agent-memory/blob/v0.1.0/docs/acceptance.md#10-v010-changed-surface-checks)和[升级说明（英文）](../releases/0.1.0.md#upgrade)。

发布检查只使用本次任务专用的 PostgreSQL/pgvector 实例和合成用户，避开持久保存的个人和评测数据库。保留历史基准证据，发布冒烟测试与重新执行 LME-500 或 BEAM-10M 是各自独立的工作。[验收标准](acceptance.md)和该版本的发布记录说明实际测量的范围与尚存限制。

## 3. 准备发布产物

从通过验证的源码构建 Python 发行包及两种支持架构的容器镜像。构建新版本时，使用拟发布版本替换示例中的 `0.2.1` 镜像标签。

```bash
uv build
mkdir -p dist/plugins
docker buildx build --platform linux/arm64 --load -t oam:0.2.1-arm64 .
docker buildx build --platform linux/amd64 --load -t oam:0.2.1-amd64 .
```

安装接入包的构建依赖后，从已验证源码打包有变更的客户端。v0.2.1 使用以下三个归档。

```bash
npm pack ./integrations/openclaw --pack-destination dist/plugins
npm pack ./integrations/mcp --pack-destination dist/plugins
npm pack ./integrations/dsh --pack-destination dist/plugins
```

记录源码提交、归档校验和、原生与模拟环境分别执行的检查，以及不可变镜像标识。发布后的注册表摘要须独立于本地镜像 ID 验证。检查已安装的 Python 和 CLI 元数据，以及打包后的配置、迁移和 Inspector 静态资源。通过隔离的候选镜像验证经过授权的 API 输入、就绪状态、带引用的召回、画像和洞察及召回的变更，以及相关集成在原生环境中的运行路径。约束提供商调用规模，将实际用量和失败记录与确定性测试结果分别保存。

显式运行的 `tests/release/verify_candidate.py` 命令使用[部署指南](../../deploy/README_CN.md#5-可信网关边界)中有界的合成覆盖配置。明确配置隔离的端点、根密钥及私有输出目录。公开产物中不得包含凭据、模型原始响应或私有用户内容。

## 4. 发布已验证的产品

提交已验证的源码后，在该确切提交上创建带注释的新产品标签，并以上一产品发布为基线运行文档检查。保留现有标签。根据新标签制作源码归档，并为已审阅的发行包、源码归档和插件归档计算校验和。将产物及脱敏的验证证据附加至 GitHub Release，使用发布记录的 Changes 和 Upgrade 两节作为发布说明。

从同一份源码发布多架构镜像。以下是 0.2.1 的标签示例。发布新版本时，使用目标版本对应的确切版本标签和滚动别名。

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/rocke2020/open-agent-memory:0.2.1 \
  -t ghcr.io/rocke2020/open-agent-memory:v0.2.1 \
  -t ghcr.io/rocke2020/open-agent-memory:0.2 \
  -t ghcr.io/rocke2020/open-agent-memory:0 \
  -t ghcr.io/rocke2020/open-agent-memory:latest --push .
```

确认确切版本标签和滚动别名均解析到已发布、已审阅的镜像清单。未变更的客户端继续下载并验证既有归档。已发布的 npm 版本保持不变。分别报告 GitHub 源码与镜像发布结果，以及任何单独的集成包发布结果。

## 5. 检查公开下载

在两个支持的架构上按摘要匿名拉取发布镜像，再通过隔离部署验证启动、迁移、授权读写、带引用的召回和 Inspector 静态资源。匿名下载 GitHub 上的源码、发行包、插件归档及校验和，并与已审阅的字节内容比较。根据已打标签的代码树验证源码归档，根据集成包各自的已发布版本验证插件归档。明确记录仍未执行的原生环境或恢复检查。公开下载验收需要实际下载，上传成功和本地构建结果分别只覆盖各自的环节。
