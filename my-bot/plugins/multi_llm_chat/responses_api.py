"""Translate the agent's messages to the Ark Responses protocol."""

from __future__ import annotations

from typing import Any

from .models import AssistantTurn, FunctionCall, ToolCall


def build_responses_request(request: dict[str, Any]) -> dict[str, Any]:
    request = dict(request)
    request["input"] = _input_items(request.pop("messages"))
    for field in ("max_tokens", "max_completion_tokens"):
        if field in request:
            request["max_output_tokens"] = request.pop(field)
    request.setdefault("store", False)
    if "tools" in request:
        request["tools"] = [
            {"type": "function", **tool["function"]}
            if tool.get("type") == "function" and "function" in tool
            else tool
            for tool in request["tools"]
        ]
    choice = request.get("tool_choice")
    if (
        isinstance(choice, dict)
        and choice.get("type") == "function"
        and "function" in choice
    ):
        request["tool_choice"] = {
            "type": "function",
            "name": choice["function"]["name"],
        }
    if "response_format" in request:
        output_format = request.pop("response_format")
        if output_format.get("type") == "json_schema":
            output_format = {"type": "json_schema", **output_format["json_schema"]}
        request["text"] = {**request.get("text", {}), "format": output_format}
    return request


def _input_items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "assistant" and message.get("_responses_output"):
            items.extend(message["_responses_output"])
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": message.get("content") or "",
                }
            )
            continue
        content = message.get("content")
        if content is not None:
            if isinstance(content, list):
                content = [_content_part(part, role) for part in content]
            items.append({"role": role, "content": content})
        for call in message.get("tool_calls", []):
            items.append(
                {
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                }
            )
    return items


def _content_part(part: dict[str, Any], role: str) -> dict[str, Any]:
    if part.get("type") == "text":
        return {
            "type": "output_text" if role == "assistant" else "input_text",
            "text": part["text"],
        }
    if part.get("type") == "image_url":
        image = part["image_url"]
        if isinstance(image, str):
            return {"type": "input_image", "image_url": image}
        return {
            "type": "input_image",
            "image_url": image["url"],
            **{key: value for key, value in image.items() if key != "url"},
        }
    raise ValueError(f"Responses API 不支持的消息内容类型: {part.get('type')}")


def parse_responses_result(result: Any) -> AssistantTurn:
    # Use the full dump so Ark-specific reasoning fields survive SDK parsing.
    data = result.model_dump(exclude_none=True)
    output = data.get("output") or []
    text: list[str] = []
    calls: list[ToolCall] = []
    for item in output:
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text.append(part.get("text", ""))
                elif part.get("type") == "refusal":
                    text.append(part.get("refusal", ""))
        elif item.get("type") == "function_call":
            calls.append(
                ToolCall(
                    id=item["call_id"],
                    function=FunctionCall(
                        name=item["name"], arguments=item["arguments"]
                    ),
                )
            )
    status = data.get("status") or "missing"
    finish_reason = "tool_calls" if calls else "stop"
    if status != "completed":
        reason = (data.get("incomplete_details") or {}).get("reason", "")
        finish_reason = (
            "length" if reason == "max_output_tokens" else (reason or status)
        )
    return AssistantTurn(
        content="".join(text),
        tool_calls=calls,
        finish_reason=finish_reason,
        usage=data.get("usage") or {},
        response_output=output,
    )
