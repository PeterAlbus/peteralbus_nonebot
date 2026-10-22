from __future__ import annotations

import httpx
from dotenv import dotenv_values

from llm_accounts import ProviderAccounts

from .settings import OpsSettings


def provider_accounts_for_settings(
    settings: OpsSettings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProviderAccounts:
    try:
        key = dotenv_values(settings.nonebot_env_file, interpolate=False).get(
            "LLM_CHAT_DEEPSEEK_API_KEY"
        )
    except (OSError, UnicodeError):
        key = None
    return ProviderAccounts(
        deepseek_api_key=key or "",
        volc_access_key=settings.volc_access_key,
        volc_secret_key=settings.volc_secret_key,
        volc_project_name=settings.volc_project_name,
        transport=transport,
    )
