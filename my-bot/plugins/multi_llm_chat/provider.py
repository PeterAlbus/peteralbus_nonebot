from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from openai import APIStatusError, AsyncOpenAI, DefaultAsyncHttpxClient

from llm_accounts import ProviderAccounts
from llm_usage import UsageStore

from .conversation import INTERNAL_EVENT_CONTEXT_MARKER
from .models import AssistantTurn, FunctionCall, ToolCall
from .raw_request_store import (
    append_raw_error,
    append_raw_request,
    append_raw_response,
)
from .responses_api import build_responses_request, parse_responses_result

UPSTREAM_BLOCKED_CONTENT = (
    "The request was rejected because it was considered high risk"
)
UPSTREAM_BLOCKED_NOTICE = "本次请求被上游屏蔽。"
UPSTREAM_BLOCK_MAX_RETRIES = 3
CHAT_REPLY_MAX_CHARS = 4000


class ProviderConfigurationError(RuntimeError):
    pass


class ProviderResponseError(RuntimeError):
    pass


class LLMProvider:
    def __init__(
        self,
        config: Any,
        routes_path: Path,
        raw_request_log_dir: Path,
        logger: Any,
        usage_store: UsageStore,
    ) -> None:
        self._usage_store = usage_store
        self._config = config
        self._routes_path = routes_path
        self._raw_request_log_dir = raw_request_log_dir
        self._logger = logger
        self._routes = self._load_routes()
        self._client: AsyncOpenAI | None = None
        self._client_params: tuple[str, str, int, bool] | None = None

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
        if provider_config.get("api", "chat_completions") not in {
            "chat_completions", "responses"
        }:
            raise ProviderConfigurationError("提供商 api 必须为 chat_completions 或 responses")
        return model, provider_config, model_config

    def _client_for(self, provider_config: dict[str, Any]) -> AsyncOpenAI:
        api_key_field = str(provider_config.get("api_key_field", ""))
        api_key = str(getattr(self._config, api_key_field, "") or "").strip()
        base_url = _normalize_base_url(str(provider_config.get("base_url", "")))
        timeout = int(self._config.llm_chat_request_timeout)
        use_system_proxy = provider_config.get("use_system_proxy", True)
        if not isinstance(use_system_proxy, bool):
            raise ProviderConfigurationError("use_system_proxy 必须是布尔值")
        if not api_key:
            raise ProviderConfigurationError(f"模型 API 密钥未配置: {api_key_field}")
        if not base_url:
            raise ProviderConfigurationError("模型 base_url 未配置")
        params = (api_key, base_url, timeout, use_system_proxy)
        if self._client is None or self._client_params != params:
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                http_client=(
                    None
                    if use_system_proxy
                    else DefaultAsyncHttpxClient(trust_env=False)
                ),
            )
            self._client_params = params
        return self._client

    def current_provider_name(self) -> str:
        return self._resolve_model()[2]["provider"]

    async def get_balance(self, volc_access_key: str, volc_secret_key: str) -> dict[str, Any]:
        model, provider_config, model_config = self._resolve_model()
        provider_name = model_config["provider"]
        api_key_field = str(provider_config.get("api_key_field", ""))
        accounts = ProviderAccounts(
            deepseek_api_key=str(getattr(self._config, api_key_field, "") or "").strip()
            if provider_name == "deepseek" else "",
            volc_access_key=volc_access_key,
            volc_secret_key=volc_secret_key,
        )
        return {"model": model, **await accounts.balance(provider_name)}

    async def read_urls(self, url_list: list[str]) -> dict[str, Any]:
        _, provider_config, model_config = self._resolve_model()
        if model_config["provider"] != "volc_ark":
            raise ProviderConfigurationError("当前提供商未配置火山网页解析")
        # Same endpoint and body as Arkitect's built-in LinkReader. Reuse the
        # loaded route's credentials and proxy settings without the Arkitect runtime.
        client = self._client_for(provider_config).with_options(max_retries=0)
        try:
            result = await client.post(
                "/tools/execute",
                cast_to=dict[str, Any],
                body={
                    "action_name": "LinkReader",
                    "tool_name": "LinkReader",
                    "parameters": {"url_list": url_list},
                },
            )
        except APIStatusError as error:
            body = error.body if isinstance(error.body, dict) else {}
            details = body.get("error", body)
            details = details if isinstance(details, dict) else {}
            if "security strategy" in str(details.get("message", "")).lower():
                reason = "链接被火山网页解析安全策略拦截，未获取正文；不要重复请求该链接"
            else:
                reason = f"火山网页解析请求失败（HTTP {error.status_code}），未获取正文"
            raise ProviderResponseError(reason) from error
        data = result.get("data")
        if result.get("status_code") != 200 or not isinstance(data, dict):
            raise ProviderResponseError("火山网页解析接口返回失败状态")
        records = data.get("ark_web_data_list")
        if not isinstance(records, list) or not records:
            raise ProviderResponseError("火山网页解析接口未返回网页结果")
        if len(records) != len(url_list) or any(not isinstance(page, dict) for page in records):
            raise ProviderResponseError("火山网页解析接口返回无效网页结果")
        # Leave room for titles, URLs and the tool-result envelope. Truncate each
        # page's body rather than cutting the whole serialized result mid-document.
        budget = max(100, self._config.llm_chat_tool_output_max_chars - 2000)
        page_limit = max(1, budget // len(records))
        pages = []
        for index, page in enumerate(records):
            error_code = page.get("error_code")
            error = str(page.get("error_message") or "")
            if error_code not in (None, "", 0, "0") and not error:
                error = f"网页解析失败: {error_code}"
            content = str(page.get("content") or "") if not error else ""
            pages.append({
                "url": str(page.get("url") or url_list[index]),
                "title": str(page.get("title") or "")[:300],
                "success": bool(content) and not error,
                "content": content[:page_limit],
                "truncated": len(content) > page_limit,
                "error": error or ("" if content else "网页解析未返回正文"),
            })
        return {"pages": pages}

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
            "provider": model_config["provider"],
            "api": provider_config.get("api", "chat_completions"),
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
        await asyncio.to_thread(
            self._usage_store.begin, request_id, attempt,
            common_metadata["provider"], common_metadata["model"], request_type,
        )
        raw_response = None
        try:
            endpoint = (
                client.responses
                if common_metadata["api"] == "responses"
                else client.chat.completions
            )
            raw_response = await endpoint.with_raw_response.create(**create_kwargs)
            result = raw_response.parse()
            parsed_turn = (
                parse_responses_result(result)
                if common_metadata["api"] == "responses"
                else _parse_chat_completion(result)
            )
        except Exception as error:
            await asyncio.to_thread(
                self._usage_store.finish, request_id, attempt, {}, "error",
                int(getattr(raw_response, "retries_taken", 0) or 0),
            )
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

        usage = parsed_turn.usage
        await asyncio.to_thread(
            self._usage_store.finish, request_id, attempt, usage, "completed",
            int(getattr(raw_response, "retries_taken", 0) or 0),
        )
        raw_content = parsed_turn.content
        finish_reason = parsed_turn.finish_reason
        tool_calls = parsed_turn.tool_calls
        upstream_blocked = raw_content == UPSTREAM_BLOCKED_CONTENT
        retry_scheduled = upstream_blocked and attempt < max_attempts
        embedded_reasoning_removed = False
        output_rejection = ""
        if upstream_blocked:
            content = "" if retry_scheduled else UPSTREAM_BLOCKED_NOTICE
        else:
            try:
                if common_metadata["api"] == "responses" and finish_reason not in {
                    "stop", "tool_calls"
                }:
                    raise ProviderResponseError(
                        f"模型响应未正常结束: finish_reason={finish_reason}"
                    )
                content, embedded_reasoning_removed = normalize_outbound_content(
                    raw_content
                )
                if request_type.endswith("_chat_agent") and not tool_calls:
                    validate_chat_outbound_content(content, finish_reason)
            except ProviderResponseError as error:
                content = ""
                output_rejection = str(error)
        attempt_elapsed_ms = round((perf_counter() - attempt_started_at) * 1000)
        total_elapsed_ms = round((perf_counter() - total_started_at) * 1000)
        provider_request_id = str(
            getattr(raw_response, "request_id", "")
            or getattr(result, "_request_id", "")
            or ""
        )
        status_code = int(getattr(raw_response, "status_code", 0) or 0)
        retries_taken = int(getattr(raw_response, "retries_taken", 0) or 0)
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
                "embedded_reasoning_removed": embedded_reasoning_removed,
                "output_rejection": output_rejection or None,
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
            "upstream_blocked={}, retry_scheduled={}, "
            "embedded_reasoning_removed={}, output_rejected={}",
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
            embedded_reasoning_removed,
            bool(output_rejection),
        )
        if output_rejection:
            self._logger.error(  # noqa: PLE1205 - Loguru uses brace formatting.
                "拒绝使用不符合输出约束的模型响应: request_id={}, turn_id={}, "
                "step={}, request_type={}, model={}, reason={}",
                request_id,
                common_metadata["turn_id"],
                common_metadata["step"],
                request_type,
                common_metadata["model"],
                output_rejection,
            )
            raise ProviderResponseError(output_rejection)
        return (
            parsed_turn.model_copy(update={"content": content}),
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
        if provider_config.get("api") == "responses":
            return build_responses_request(create_kwargs)
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
    normalized = url.strip().rstrip("/").removesuffix("/chat/completions").removesuffix("/responses")
    return normalized.rstrip("/")


def _parse_chat_completion(result: Any) -> AssistantTurn:
    choice = result.choices[0]
    message = choice.message
    return AssistantTurn(
        content=message.content or "",
        tool_calls=[
            ToolCall(
                id=call.id,
                function=FunctionCall(
                    name=call.function.name,
                    arguments=call.function.arguments,
                ),
            )
            for call in (message.tool_calls or [])
            if getattr(call, "type", "function") == "function"
        ],
        reasoning_content=str(getattr(message, "reasoning_content", "") or ""),
        encrypted_content=str(getattr(message, "encrypted_content", "") or ""),
        finish_reason=str(choice.finish_reason or ""),
        usage=result.usage.model_dump() if result.usage else {},
    )


def _thinking_enabled(create_kwargs: dict[str, Any]) -> bool:
    extra_body = create_kwargs.get("extra_body")
    if not isinstance(extra_body, dict):
        return False
    thinking = extra_body.get("thinking")
    return (
        isinstance(thinking, dict)
        and str(thinking.get("type", "")).lower() == "enabled"
    )


def normalize_outbound_content(raw_content: str) -> tuple[str, bool]:
    content = raw_content.strip()
    embedded_reasoning_removed = False
    if "</think>" in content:
        _, content = content.rsplit("</think>", 1)
        content = content.strip()
        embedded_reasoning_removed = True
    if "<think" in content or "</think>" in content:
        raise ProviderResponseError("模型响应包含无法完整分离的思考内容")
    if INTERNAL_EVENT_CONTEXT_MARKER in content:
        raise ProviderResponseError("模型响应包含内部群聊消息上下文")
    return content, embedded_reasoning_removed


def validate_chat_outbound_content(content: str, finish_reason: str) -> None:
    if finish_reason != "stop":
        reason = finish_reason or "missing"
        raise ProviderResponseError(f"模型响应未正常结束: finish_reason={reason}")
    if len(content) > CHAT_REPLY_MAX_CHARS:
        raise ProviderResponseError(
            f"模型回复超过 {CHAT_REPLY_MAX_CHARS} 字符上限: content_chars={len(content)}"
        )
