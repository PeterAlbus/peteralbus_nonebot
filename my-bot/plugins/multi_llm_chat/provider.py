from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from openai import AsyncOpenAI

from .models import AssistantTurn, FunctionCall, ToolCall
from .raw_request_store import (
    append_raw_error,
    append_raw_request,
    append_raw_response,
)

UPSTREAM_BLOCKED_CONTENT = (
    "The request was rejected because it was considered high risk"
)
UPSTREAM_BLOCKED_NOTICE = "本次请求被上游屏蔽。"
UPSTREAM_BLOCK_MAX_RETRIES = 3


class ProviderConfigurationError(RuntimeError):
    pass


class LLMProvider:
    def __init__(
        self,
        config: Any,
        routes_path: Path,
        raw_request_log_dir: Path,
        logger: Any,
    ) -> None:
        self._config = config
        self._routes_path = routes_path
        self._raw_request_log_dir = raw_request_log_dir
        self._logger = logger
        self._routes = self._load_routes()
        self._client: AsyncOpenAI | None = None
        self._client_params: tuple[str, str, int] | None = None

    def _load_routes(self) -> dict[str, Any]:
        data = json.loads(self._routes_path.read_text(encoding="utf-8"))
        if not isinstance(data.get("providers"), dict) or not isinstance(
            data.get("models"), dict
        ):
            raise ProviderConfigurationError("模型路由缺少 providers 或 models")
        return data

    def _resolve_model(self) -> tuple[str, dict[str, Any], dict[str, Any]]:
        model = self._config.llm_chat_model
        model_config = self._routes["models"].get(model)
        if not isinstance(model_config, dict):
            raise ProviderConfigurationError(f"模型未在路由中定义: {model}")
        provider_name = str(model_config.get("provider", ""))
        provider_config = self._routes["providers"].get(provider_name)
        if not isinstance(provider_config, dict):
            raise ProviderConfigurationError(
                f"模型提供商未在路由中定义: {provider_name}"
            )
        if not provider_config.get("supports_tools", False):
            raise ProviderConfigurationError(
                f"当前提供商不支持工具调用: {provider_name}"
            )
        return model, provider_config, model_config

    def _client_for(self, provider_config: dict[str, Any]) -> AsyncOpenAI:
        api_key_field = str(provider_config.get("api_key_field", ""))
        api_key = str(getattr(self._config, api_key_field, "") or "").strip()
        base_url = _normalize_base_url(str(provider_config.get("base_url", "")))
        timeout = int(self._config.llm_chat_request_timeout)
        if not api_key:
            raise ProviderConfigurationError(f"模型 API 密钥未配置: {api_key_field}")
        if not base_url:
            raise ProviderConfigurationError("模型 base_url 未配置")
        params = (api_key, base_url, timeout)
        if self._client is None or self._client_params != params:
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )
            self._client_params = params
        return self._client

    def image_understanding_enabled(self) -> bool:
        _, _, model_config = self._resolve_model()
        configured = self._config.llm_chat_image_understanding
        if configured is not None:
            return bool(configured)
        return bool(model_config.get("supports_image_understanding", False))

    async def complete(
        self,
        messages: Sequence[dict[str, Any]],
        request_type: str,
        turn_id: str,
        step: int,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        response_format: dict[str, Any] | None = None,
        allow_builtin_tools: bool = False,
    ) -> AssistantTurn:
        model, provider_config, model_config = self._resolve_model()
        client = self._client_for(provider_config)
        request_id = uuid4().hex
        total_started_at = perf_counter()
        create_kwargs = self._build_request(
            model=model,
            provider_config=provider_config,
            model_config=model_config,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            allow_builtin_tools=allow_builtin_tools,
        )
        common_metadata = {
            "turn_id": turn_id,
            "step": step,
            "model": model,
        }
        max_attempts = UPSTREAM_BLOCK_MAX_RETRIES + 1
        for attempt in range(1, max_attempts + 1):
            turn, upstream_blocked = await self._complete_attempt(
                client=client,
                create_kwargs=create_kwargs,
                request_id=request_id,
                request_type=request_type,
                common_metadata=common_metadata,
                message_count=len(messages),
                attempt=attempt,
                max_attempts=max_attempts,
                total_started_at=total_started_at,
            )
            if not upstream_blocked or attempt == max_attempts:
                return turn
            self._logger.warning(  # noqa: PLE1205 - Loguru uses brace formatting.
                "大模型请求被上游屏蔽，准备重试: request_id={}, turn_id={}, "
                "step={}, request_type={}, model={}, attempt={}, max_attempts={}",
                request_id,
                turn_id,
                step,
                request_type,
                model,
                attempt,
                max_attempts,
            )
        raise RuntimeError("大模型请求重试状态异常")

    async def _complete_attempt(
        self,
        client: AsyncOpenAI,
        create_kwargs: dict[str, Any],
        request_id: str,
        request_type: str,
        common_metadata: dict[str, Any],
        message_count: int,
        attempt: int,
        max_attempts: int,
        total_started_at: float,
    ) -> tuple[AssistantTurn, bool]:
        attempt_started_at = perf_counter()
        attempt_metadata = {
            **common_metadata,
            "attempt": attempt,
            "max_attempts": max_attempts,
        }
        await append_request_async(
            directory=self._raw_request_log_dir,
            request_id=request_id,
            request_type=request_type,
            request_body=create_kwargs,
            metadata=attempt_metadata,
        )
        self._logger.info(  # noqa: PLE1205 - Loguru uses brace formatting.
            "开始大模型请求: request_id={}, turn_id={}, step={}, request_type={}, "
            "model={}, attempt={}, max_attempts={}, message_count={}",
            request_id,
            common_metadata["turn_id"],
            common_metadata["step"],
            request_type,
            common_metadata["model"],
            attempt,
            max_attempts,
            message_count,
        )
        raw_response = None
        try:
            raw_response = await client.chat.completions.with_raw_response.create(
                **create_kwargs
            )
            result = raw_response.parse()
        except Exception as error:
            attempt_elapsed_ms = round((perf_counter() - attempt_started_at) * 1000)
            total_elapsed_ms = round((perf_counter() - total_started_at) * 1000)
            status_code = getattr(error, "status_code", None) or getattr(
                raw_response, "status_code", None
            )
            provider_request_id = str(
                getattr(error, "request_id", "")
                or getattr(raw_response, "request_id", "")
                or ""
            )
            await append_error_async(
                directory=self._raw_request_log_dir,
                request_id=request_id,
                request_type=request_type,
                error=_error_record(error, raw_response),
                metadata={
                    **attempt_metadata,
                    "attempt_elapsed_ms": attempt_elapsed_ms,
                    "total_elapsed_ms": total_elapsed_ms,
                    "status_code": status_code,
                    "provider_request_id": provider_request_id,
                    "response_headers": _diagnostic_response_headers(raw_response),
                },
            )
            self._logger.error(  # noqa: PLE1205 - Loguru uses brace formatting.
                "大模型请求失败: request_id={}, turn_id={}, step={}, "
                "request_type={}, model={}, attempt={}, max_attempts={}, "
                "attempt_elapsed_ms={}, total_elapsed_ms={}, error_type={}, "
                "status_code={}, provider_request_id={}, error_message={}",
                request_id,
                common_metadata["turn_id"],
                common_metadata["step"],
                request_type,
                common_metadata["model"],
                attempt,
                max_attempts,
                attempt_elapsed_ms,
                total_elapsed_ms,
                type(error).__name__,
                status_code,
                provider_request_id,
                str(error),
            )
            raise

        choice = result.choices[0]
        message = choice.message
        raw_content = message.content or ""
        upstream_blocked = raw_content == UPSTREAM_BLOCKED_CONTENT
        retry_scheduled = upstream_blocked and attempt < max_attempts
        if upstream_blocked:
            content = "" if retry_scheduled else UPSTREAM_BLOCKED_NOTICE
        else:
            content = raw_content.strip()
        tool_calls = [
            ToolCall(
                id=call.id,
                function=FunctionCall(
                    name=call.function.name,
                    arguments=call.function.arguments,
                ),
            )
            for call in (message.tool_calls or [])
            if getattr(call, "type", "function") == "function"
        ]
        usage = result.usage.model_dump() if result.usage else {}
        attempt_elapsed_ms = round((perf_counter() - attempt_started_at) * 1000)
        total_elapsed_ms = round((perf_counter() - total_started_at) * 1000)
        provider_request_id = str(
            getattr(raw_response, "request_id", "")
            or getattr(result, "_request_id", "")
            or ""
        )
        status_code = int(getattr(raw_response, "status_code", 0) or 0)
        retries_taken = int(getattr(raw_response, "retries_taken", 0) or 0)
        finish_reason = str(choice.finish_reason or "")
        await append_response_async(
            directory=self._raw_request_log_dir,
            request_id=request_id,
            request_type=request_type,
            response_body=_raw_response_body(raw_response),
            metadata={
                **attempt_metadata,
                "attempt_elapsed_ms": attempt_elapsed_ms,
                "total_elapsed_ms": total_elapsed_ms,
                "status_code": status_code,
                "provider_request_id": provider_request_id,
                "sdk_retries_taken": retries_taken,
                "response_headers": _diagnostic_response_headers(raw_response),
            },
            handling={
                "upstream_blocked": upstream_blocked,
                "retry_scheduled": retry_scheduled,
                "outbound_content": content or None,
            },
        )
        self._logger.info(  # noqa: PLE1205 - Loguru uses brace formatting.
            "完成大模型请求: request_id={}, turn_id={}, step={}, "
            "request_type={}, model={}, attempt={}, max_attempts={}, "
            "provider_request_id={}, status_code={}, sdk_retries_taken={}, "
            "attempt_elapsed_ms={}, total_elapsed_ms={}, finish_reason={}, "
            "response_chars={}, outbound_chars={}, tool_call_count={}, "
            "input_tokens={}, output_tokens={}, total_tokens={}, "
            "upstream_blocked={}, retry_scheduled={}",
            request_id,
            common_metadata["turn_id"],
            common_metadata["step"],
            request_type,
            common_metadata["model"],
            attempt,
            max_attempts,
            provider_request_id,
            status_code,
            retries_taken,
            attempt_elapsed_ms,
            total_elapsed_ms,
            finish_reason,
            len(raw_content),
            len(content),
            len(tool_calls),
            _usage_value(usage, "input_tokens", "prompt_tokens"),
            _usage_value(usage, "output_tokens", "completion_tokens"),
            _usage_total(usage),
            upstream_blocked,
            retry_scheduled,
        )
        return (
            AssistantTurn(
                content=content,
                tool_calls=tool_calls,
                reasoning_content=str(getattr(message, "reasoning_content", "") or ""),
                finish_reason=finish_reason,
                usage=usage,
            ),
            upstream_blocked,
        )

    def _build_request(
        self,
        model: str,
        provider_config: dict[str, Any],
        model_config: dict[str, Any],
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
        tool_choice: Any | None,
        response_format: dict[str, Any] | None,
        allow_builtin_tools: bool,
    ) -> dict[str, Any]:
        create_kwargs: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": self._config.llm_chat_temperature,
            "stream": False,
        }
        max_tokens_field = str(provider_config.get("max_tokens_field", "max_tokens"))
        create_kwargs[max_tokens_field] = self._config.llm_chat_max_tokens
        for source in (
            provider_config.get("create_params", {}),
            model_config.get("create_params", {}),
        ):
            if isinstance(source, dict):
                create_kwargs.update(json.loads(json.dumps(source, ensure_ascii=False)))

        configured_tools = create_kwargs.pop("tools", [])
        builtin_tools = configured_tools if allow_builtin_tools else []
        if not allow_builtin_tools:
            create_kwargs.pop("tool_choice", None)
        merged_tools = [*builtin_tools, *(tools or [])]
        if merged_tools:
            has_strict_function = any(
                tool.get("type") == "function"
                and bool(tool.get("function", {}).get("strict"))
                for tool in merged_tools
                if isinstance(tool, dict)
            )
            if has_strict_function and not provider_config.get(
                "supports_strict_tools", False
            ):
                raise ProviderConfigurationError("当前提供商不支持严格工具参数模式")
            create_kwargs["tools"] = merged_tools
            create_kwargs["tool_choice"] = tool_choice or create_kwargs.get(
                "tool_choice", "auto"
            )
        if response_format is not None:
            create_kwargs["response_format"] = response_format

        provider_name = str(model_config.get("provider", ""))
        if provider_name == "deepseek":
            thinking = create_kwargs.pop("thinking", None)
            if thinking is not None:
                extra_body = create_kwargs.setdefault("extra_body", {})
                extra_body.setdefault("thinking", thinking)
            if _thinking_enabled(create_kwargs):
                for field in (
                    "temperature",
                    "top_p",
                    "presence_penalty",
                    "frequency_penalty",
                ):
                    create_kwargs.pop(field, None)
        return create_kwargs


async def append_request_async(**kwargs: Any) -> None:
    import asyncio

    await asyncio.to_thread(append_raw_request, **kwargs)


async def append_response_async(**kwargs: Any) -> None:
    import asyncio

    await asyncio.to_thread(append_raw_response, **kwargs)


async def append_error_async(**kwargs: Any) -> None:
    import asyncio

    await asyncio.to_thread(append_raw_error, **kwargs)


def _raw_response_body(raw_response: Any) -> Any:
    text = str(getattr(raw_response, "text", "") or "")
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _diagnostic_response_headers(raw_response: Any) -> dict[str, str]:
    headers = getattr(raw_response, "headers", {})
    recorded: dict[str, str] = {}
    for name, value in headers.items():
        normalized = str(name).lower()
        if (
            normalized.endswith("request-id")
            or normalized.startswith("x-ratelimit-")
            or normalized in {"retry-after", "x-stainless-retry-count"}
        ):
            recorded[normalized] = str(value)
    return recorded


def _error_record(error: Exception, raw_response: Any = None) -> dict[str, Any]:
    body = getattr(error, "body", None)
    response = getattr(error, "response", None)
    if response is None:
        response = raw_response
    if body is None and response is not None:
        response_text = str(getattr(response, "text", "") or "")
        if response_text:
            try:
                body = json.loads(response_text)
            except json.JSONDecodeError:
                body = response_text
    return {
        "type": type(error).__name__,
        "message": str(error),
        "status_code": getattr(error, "status_code", None),
        "provider_request_id": str(getattr(error, "request_id", "") or ""),
        "body": body,
    }


def _usage_value(usage: dict[str, Any], *field_names: str) -> Any:
    for field_name in field_names:
        if field_name in usage:
            return usage[field_name]
    return None


def _usage_total(usage: dict[str, Any]) -> Any:
    if "total_tokens" in usage:
        return usage["total_tokens"]
    input_tokens = _usage_value(usage, "input_tokens", "prompt_tokens")
    output_tokens = _usage_value(usage, "output_tokens", "completion_tokens")
    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
        return input_tokens + output_tokens
    return None


def _normalize_base_url(url: str) -> str:
    normalized = url.strip().removesuffix("/chat/completions")
    return normalized.rstrip("/")


def _thinking_enabled(create_kwargs: dict[str, Any]) -> bool:
    extra_body = create_kwargs.get("extra_body")
    if not isinstance(extra_body, dict):
        return False
    thinking = extra_body.get("thinking")
    return (
        isinstance(thinking, dict)
        and str(thinking.get("type", "")).lower() == "enabled"
    )
