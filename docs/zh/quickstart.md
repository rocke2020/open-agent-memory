# 保存会话、等待索引并召回

[中文文档](README.md) · [English](../quickstart.md)

## 概述

本示例通过公开 HTTP API 保存一条会话，等待学习记忆和关联索引就绪，再召回带引用的上下文。OAM 提供证据，你的 Agent 负责生成答案。

首次使用请先完成[第一条有用的记忆](start-here.md)，其中包含服务部署、创建用户、核对来源和 Agent 接入步骤。已有服务和用户密钥时，可直接运行本页示例。

## 1. 准备连接

按照 [Docker Compose 指南](../../deploy/README_CN.md)启动 OAM，并确保生成模型、向量模型和工作进程可用。你需要 Bash、`curl`、`jq` 和一个已有 OAM 用户的专用密钥。管理员可通过 `POST /v1/users` 创建用户，并安全地交付首次返回的用户密钥。Memory Inspector 也连接到已有用户。在 `api_key` 模式下，个人内容路由需要用户密钥。见[管理 API](api.md#2-管理)。

在 Bash 中设置连接参数。输入密钥时终端不会回显，密钥也不会写入命令历史。

```bash
export OAM_BASE_URL='http://127.0.0.1:17333'
export OAM_USER_ID='your-user-id'
read -r -s -p 'OAM user API key: ' OAM_USER_API_KEY
printf '\n'
export OAM_USER_API_KEY
```

## 2. 运行完整示例

将以下代码粘贴到同一个 Bash 会话中。它向指定用户的集合写入一条演示偏好，并调用部署所配置的模型来索引。每次运行都会发起一次新的保存请求。

```bash
(
  set -euo pipefail
  : "${OAM_BASE_URL:?Set OAM_BASE_URL}"
  : "${OAM_USER_ID:?Set OAM_USER_ID}"
  : "${OAM_USER_API_KEY:?Set OAM_USER_API_KEY}"
  base_url=${OAM_BASE_URL%/}
  user_path=$(jq -rn --arg user "$OAM_USER_ID" '$user | @uri')
  event_time=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  request_key="quickstart-$(date +%s)-$$-$RANDOM"

  # Retain: accepted evidence and its durable receipt commit before model work.
  body=$(jq -n --arg user "$OAM_USER_ID" --arg time "$event_time" '{
    user_id: $user, input_type: "interaction", messages: [{
      role: "user", event_time: $time, learning_eligible: true,
      content: "I prefer vegetarian meals and travelling by train. Please keep my travel plans concise."
    }]
  }')
  receipt=$(curl --fail --silent --show-error --connect-timeout 5 --max-time 30 \
    "$base_url/v1/retain" \
    -H "Authorization: Bearer $OAM_USER_API_KEY" \
    -H 'Content-Type: application/json' -H "Idempotency-Key: $request_key" \
    --data "$body") || exit 1
  operation_id=$(printf '%s' "$receipt" | jq -er '.operation_id') || exit 1
  printf 'Accepted operation: %s\n' "$operation_id"
  operation_url="$base_url/v1/users/$user_path/operations/$operation_id"

  # Readiness: acceptance alone does not mean indexing has finished.
  deadline=$((SECONDS + 180))
  ready=false
  while (( SECONDS < deadline )); do
    operation=$(curl --fail --silent --show-error --connect-timeout 5 --max-time 15 \
      "$operation_url" -H "Authorization: Bearer $OAM_USER_API_KEY") || exit 1
    status=$(printf '%s' "$operation" | jq -er '.status') || exit 1
    case "$status" in
      failed|partial|cancelled)
        printf '%s' "$operation" | jq '{operation_id, status, error}' >&2
        exit 1 ;;
    esac
    if printf '%s' "$operation" | jq -e \
      '.status == "succeeded" and .readiness.memory_ready and .readiness.links_ready' >/dev/null; then
      ready=true
      break
    fi
    sleep 2
  done
  if [[ "$ready" != true ]]; then
    printf 'Still waiting. Inspect the existing operation: %s\n' "$operation_url" >&2
    exit 1
  fi
  printf '%s' "$operation" | jq '{operation_id, status, readiness}'

  # Recall: a separate request retrieves the preference with source citations.
  query=$(jq -n --arg user "$OAM_USER_ID" '{
    user_id: $user, query: "What are my travel and food preferences?", recall_level: "fast"
  }')
  recalled=$(curl --fail --silent --show-error --connect-timeout 5 --max-time 30 \
    "$base_url/v1/recall" \
    -H "Authorization: Bearer $OAM_USER_API_KEY" -H 'Content-Type: application/json' \
    --data "$query") || exit 1
  printf '%s' "$recalled" | jq -e \
    '(.context | length) > 0 and (.items | length) > 0 and any(.items[]; (.citations | length) > 0)' >/dev/null
  printf '%s' "$recalled" | jq '{context, context_tokens, items, diagnostics}'
)
```

## 3. 检查结果

就绪的操作返回 `status: "succeeded"`、`readiness.memory_ready: true` 和 `readiness.links_ready: true`。返回的 `context` 应有证据支持素食、乘火车出行和行程简洁三项偏好。检查每项 `citations` 中的来源标识、版本、位置和引用原文。集合中已有的相关记忆也可能返回。

这些字段表示第一阶段已就绪。延迟执行的 L2 洞察和用户画像各有独立的处理流程。示例显式选用 Fast。省略模式时，服务使用 `search.default_recall_level`，默认 Fast。召回无需调用生成模型。索引和查询向量使用你配置的端点，确切的 token 消耗、延迟和模型质量取决于部署。

轮询持续三分钟，最后一次 HTTP 请求另有明确的超时。如果轮询结束时操作尚未就绪，请查看或继续轮询脚本输出的操作 URL。已接收的任务会继续运行。保存请求结果不明确时，用相同的请求体和 `Idempotency-Key` 查询或重试接收结果。重新运行整个脚本会发起一条新请求。发生模型或工作进程故障时，先用[操作 API](api.md#3-保存与召回)检查失败详情，再决定是否重试。
