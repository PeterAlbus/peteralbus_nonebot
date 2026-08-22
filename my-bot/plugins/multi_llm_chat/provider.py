import json
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, Optional, Sequence, Tuple
from uuid import uuid4

from openai import AsyncOpenAI

from .models import AssistantTurn, FunctionCall, ToolCall
from .raw_request_store import append_raw_request


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
        self._client: Optional[AsyncOpenAI] = None
        self._client_params: Optional[Tuple[str, str, int]] = None

    def _load_routes(self) -> Dict[str, Any]:
        data = json.loads(self._routes_path.read_text(encoding="utf-8"))
        if not isinstance(data.get("providers"), dict) or not isinstance(
            data.get("models"), dict
        ):
            raise ProviderConfigurationError("模型路由缺少 providers 或 models")
        return data

    def _resolve_model(self) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
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

    def _client_for(self, provider_config: Dict[str, Any]) -> AsyncOpenAI:
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
        messages: Sequence[Dict[str, Any]],
        request_type: str,
        turn_id: str,
        step: int,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        response_format: Optional[Dict[str, Any]] = None,
        allow_builtin_tools: bool = False,
    ) -> AssistantTurn:
        model, provider_config, model_config = self._resolve_model()
        client = self._client_for(provider_config)
        request_id = uuid4().hex
        started_at = perf_counter()
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
        await append_request_async(
            directory=self._raw_request_log_dir,
            request_id=request_id,
            request_type=request_type,
            request_body=create_kwargs,
            metadata={"turn_id": turn_id, "step": step},
        )
        self._logger.info(
            "开始大模型请求: request_id={}, turn_id={}, step={}, "
            "request_type={}, model={}, message_count={}",
            request_id,
            turn_id,
            step,
            request_type,
            model,
            len(messages),
        )
        try:
            result = await client.chat.completions.create(**create_kwargs)
        except Exception as error:
            self._logger.error(
                "大模型请求失败: request_id={}, turn_id={}, step={}, "
                "request_type={}, model={}, elapsed_ms={}, error_type={}",
                request_id,
                turn_id,
                step,
                request_type,
                model,
                round((perf_counter() - started_at) * 1000),
                type(error).__name__,
            )
            raise
        choice = result.choices[0]
        message = choice.message
        content = (message.content or "").strip()
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
        self._logger.info(
            "完成大模型请求: request_id={}, turn_id={}, step={}, "
            "request_type={}, model={}, elapsed_ms={}, response_chars={}, "
            "tool_call_count={}",
            request_id,
            turn_id,
            step,
            request_type,
            model,
            round((perf_counter() - started_at) * 1000),
            len(content),
            len(tool_calls),
        )
        return AssistantTurn(
            content=content,
            tool_calls=tool_calls,
            reasoning_content=str(getattr(message, "reasoning_content", "") or ""),
            finish_reason=str(choice.finish_reason or ""),
            usage=usage,
        )

    def _build_request(
        self,
        model: str,
        provider_config: Dict[str, Any],
        model_config: Dict[str, Any],
        messages: Sequence[Dict[str, Any]],
        tools: Optional[Sequence[Dict[str, Any]]],
        tool_choice: Optional[Any],
        response_format: Optional[Dict[str, Any]],
        allow_builtin_tools: bool,
    ) -> Dict[str, Any]:
        create_kwargs: Dict[str, Any] = {
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


def _normalize_base_url(url: str) -> str:
    normalized = url.strip().removesuffix("/chat/completions")
    return normalized.rstrip("/")


def _thinking_enabled(create_kwargs: Dict[str, Any]) -> bool:
    extra_body = create_kwargs.get("extra_body")
    if not isinstance(extra_body, dict):
        return False
    thinking = extra_body.get("thinking")
    return (
        isinstance(thinking, dict)
        and str(thinking.get("type", "")).lower() == "enabled"
    )
