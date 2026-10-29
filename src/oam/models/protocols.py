"""Wire representations for configured generation protocols."""

from __future__ import annotations

from typing import Any

from oam.models.bindings import ResolvedModelBinding


def generation_request(
    binding: ResolvedModelBinding,
    system: str,
    user: str,
    max_tokens: int,
    image_url: str | None = None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    headers = {"content-type": "application/json"}
    if binding.protocol == "anthropic_messages":
        if binding._api_key is not None:
            headers["x-api-key"] = binding._api_key
        headers["anthropic-version"] = "2023-06-01"
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        if image_url:
            prefix, data = image_url.split(",", 1)
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": prefix[5:].split(";", 1)[0],
                        "data": data,
                    },
                }
            )
        body: dict[str, Any] = {
            "model": binding.model,
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
        }
        if binding.reasoning_mode == "effort":
            if binding.reasoning_effort == "none":
                raise ValueError("anthropic_messages does not support effort none")
            body["output_config"] = {"effort": binding.reasoning_effort}
        return "/v1/messages", headers, body
    if binding._api_key is not None:
        headers["authorization"] = f"Bearer {binding._api_key}"
    if binding.protocol == "openai_responses":
        parts: list[dict[str, Any]] = [{"type": "input_text", "text": user}]
        if image_url:
            parts.append({"type": "input_image", "image_url": image_url})
        body = {
            "model": binding.model,
            "instructions": system,
            "input": [{"role": "user", "content": parts}],
            "text": {"format": {"type": "json_object"}},
            "store": False,
        }
        if binding.reasoning_mode == "effort":
            body["reasoning"] = {"effort": binding.reasoning_effort}
        return "/responses", headers, body
    if binding.protocol != "openai_chat":
        raise ValueError("unsupported generation protocol")
    chat_content: str | list[dict[str, Any]] = user
    if image_url:
        chat_content = [
            {"type": "text", "text": user},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
    body = {
        "model": binding.model,
        "messages": ([{"role": "system", "content": system}] if system else [])
        + [{"role": "user", "content": chat_content}],
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        **binding.chat_parameters(),
    }
    return "/chat/completions", headers, body


def _messages_usage(raw: object) -> object:
    usage = raw
    if isinstance(usage, dict):
        usage = dict(usage)
        usage["uncached_input_tokens"] = usage.get("input_tokens")
        usage["cached_input_tokens"] = usage.get("cache_read_input_tokens")
        cache_fields = ("cache_read_input_tokens", "cache_creation_input_tokens")
        for field in ("input_tokens", "output_tokens", *cache_fields):
            value = usage.get(field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("invalid Messages usage count")
        if any(field in usage and usage[field] is None for field in cache_fields):
            usage["input_tokens"] = None
        elif usage.get("input_tokens") is not None:
            usage["input_tokens"] += sum(usage.get(field, 0) for field in cache_fields)
        details = usage.get("output_tokens_details")
        if isinstance(details, dict) and "thinking_tokens" in details:
            value = details["thinking_tokens"]
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("invalid Messages reasoning count")
            usage["output_tokens_details"] = {**details, "reasoning_tokens": value}
    return usage


def normalize_generation(binding: ResolvedModelBinding, body: dict[str, Any]) -> dict[str, Any]:
    normalized = body
    try:
        if binding.protocol == "anthropic_messages":
            normalized = {**body, "usage": _messages_usage(body.get("usage"))}
        return _normalize_generation(binding, normalized)
    except (TypeError, AttributeError, ValueError):
        return {"choices": [], "usage": normalized.get("usage")}


def _normalize_generation(binding: ResolvedModelBinding, body: dict[str, Any]) -> dict[str, Any]:
    if binding.protocol == "openai_chat":
        choices = body.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            return body
        if choices[0].get("finish_reason") != "stop":
            return body
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            return body
        chat_content = message["content"].lstrip()
        if not chat_content.startswith("<think>"):
            return body
        end = chat_content.find("</think>", len("<think>"))
        if end < 0 or "<think>" in chat_content[len("<think>") : end]:
            raise ValueError("malformed reasoning prefix")
        answer = chat_content[end + len("</think>") :]
        if not answer.strip():
            raise ValueError("reasoning-only content")
        message = {**message, "content": answer}
        return {**body, "choices": [{**choices[0], "message": message}]}
    usage = body.get("usage")
    if binding.protocol == "openai_responses":
        status = body.get("status")
        details = body.get("incomplete_details") or {}
        reason = (
            "stop"
            if status == "completed"
            else "length"
            if status == "incomplete" and details.get("reason") == "max_output_tokens"
            else "error"
        )
        content: list[str] = []
        refused = False
        reasoning = any(
            isinstance(item, dict) and item.get("type") == "reasoning"
            for item in body.get("output", [])
        )
        for item in body.get("output", []):
            if item.get("type") not in {"reasoning", "message"}:
                raise ValueError("unexpected response output")
            if item.get("type") == "message":
                if (
                    item.get("role") != "assistant"
                    or item.get("status", "completed") != "completed"
                ):
                    raise ValueError("response message is not final assistant output")
                for part in item.get("content", []):
                    if part.get("type") not in {"output_text", "refusal"}:
                        raise ValueError("unexpected response content")
                    if part.get("type") == "output_text":
                        content.append(part.get("text", ""))
                    if part.get("type") == "refusal":
                        refused = True
    else:
        reasoning = any(
            isinstance(part, dict) and part.get("type") in {"thinking", "redacted_thinking"}
            for part in body.get("content", [])
        )
        if any(
            not isinstance(part, dict)
            or part.get("type") not in {"text", "thinking", "redacted_thinking"}
            for part in body.get("content", [])
        ):
            raise ValueError("unexpected messages content")
        stop = body.get("stop_reason")
        reason = "stop" if stop == "end_turn" else "length" if stop == "max_tokens" else "error"
        content = [
            part.get("text", "") for part in body.get("content", []) if part.get("type") == "text"
        ]
        refused = stop == "refusal"
    return {
        "choices": [
            {
                "finish_reason": reason,
                "message": {
                    "content": "".join(content),
                    "refusal": refused,
                    "reasoning_content": "present" if reasoning else None,
                },
            }
        ],
        "usage": usage,
    }
