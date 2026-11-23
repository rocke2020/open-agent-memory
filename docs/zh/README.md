# Open Agent Memory 文档

[English](../README.md) · 简体中文

## 概述

Open Agent Memory（OAM）保存会话和文档，从中学习有来源支撑的记忆，并为 Agent 召回带引用的上下文。先完成一次可以核对来源的召回，再根据你的应用或部署方式查阅相应指南。Compose 快速开始使用 v0.2.1 镜像。召回模式和证据限制可查阅 API 与配置参考。

## 1. 快速开始

- [保存并召回第一条记忆](start-here.md)带你完成安装、创建用户、召回证据、核对来源，以及在新 Agent 会话中使用记忆。
- [通过 HTTP 保存与召回](quickstart.md)提供可直接运行的示例，适用于已有服务和用户密钥的情况。
- [Memory Inspector](memory-web-ui.md)介绍如何连接、浏览记忆和来源、管理文档及刷新用户画像。

## 2. 接入 Agent 或应用

- [MCP：Claude Code、Cursor 及兼容客户端](../../integrations/mcp/README_CN.md)
- [OpenClaw 插件](../../integrations/openclaw/README_CN.md)
- [DeepSeek Harness 插件](../../integrations/dsh/README_CN.md)
- [HTTP API 参考](api.md)涵盖保存、召回、画像、资源和管理接口。

## 3. 理解记忆与召回

- [索引与检索](indexing-retrieval.md)介绍原始证据、学习记忆、洞察和召回流程。
- [个人画像与自定义画像](profiles.md)说明已保存画像、自动刷新和临时生成。
- [评测结果](benchmarks.md)说明实测准确率和上下文用量，并提供固定版本的结果和复现细节。

## 4. 部署和运维

- [Docker Compose 部署](../../deploy/README_CN.md)介绍单实例和本地多副本的安装、配置与启动。
- [运维](operations.md)涵盖模型服务、凭据、数据库角色、迁移与失败处理。
- [召回配置](../../configs/README_CN.md)说明默认模式、各模式的预设、共享检索设置和单请求覆盖。

## 5. 开发和发布

- [架构](architecture.md)与[运行时提示词](prompts.md)说明实现细节。
- [验收](acceptance.md)、[基础验证记录（英文）](../foundation-evidence.md)和[候选版本验证记录（英文）](../release-evidence.md)说明验收标准、历史检查和实测范围。
- [贡献指南（英文）](../../CONTRIBUTING.md)、[发布流程](releasing.md)和[版本历史](releases/README.md)涵盖开发和发布。
