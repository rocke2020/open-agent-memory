# Retain, wait for readiness and recall

## Overview

This example stores one conversation, waits for its learned memory and links to become ready, then retrieves cited context through the public HTTP API. Recall returns evidence for your agent; your agent generates the final answer.

## 1. Prepare your connection

Start OAM with generation and embedding models and a running worker using the [Docker Compose guide](../deploy/README.md). You need Bash, `curl`, `jq`, and an existing OAM user with its user-scoped API key. Create the user through the Memory Inspector or `POST /v1/users` as described in the [administration API](api.md#2-administration). The administrator/root key cannot replace the user's key on personal content routes in `api_key` mode.

Set the connection variables in Bash. Read the user key without echoing it or placing it in shell history:

```bash
export OAM_BASE_URL='http://127.0.0.1:17333'
export OAM_USER_ID='your-user-id'
read -r -s -p 'OAM user API key: ' OAM_USER_API_KEY
printf '\n'
export OAM_USER_API_KEY
```

## 2. Run the complete example

Copy this block into the same Bash session. It writes a synthetic preference to the selected user's collection and uses the deployment's configured models for indexing. It makes no deletion requests. Each execution creates a new logical retain request.

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

## 3. Inspect the result

The ready operation reports `status: "succeeded"`, `readiness.memory_ready: true` and `readiness.links_ready: true`. The returned `context` should contain the vegetarian-food, train-travel and concise-plan preferences. Check each item's `citations` for its source identity, revision, locator and supporting quote. Existing memories in the selected collection may also appear when relevant.

This is phase-one readiness. It does not wait for delayed L2 Insights or generate a User Profile. Fast recall performs no generative model calls; indexing and query embedding use your configured endpoints. Exact token usage, latency and model quality depend on that deployment.

The polling deadline is three minutes, with a bounded final HTTP request. If it expires, inspect or keep polling the printed operation URL; the accepted work continues. Preserve the same request body and `Idempotency-Key` when resolving an ambiguous retain response. Running the entire block again creates a new request rather than resuming the old one. Model or worker failures appear on the operation; use the [operation API](api.md#3-retain-and-recall) to inspect them before retrying.
