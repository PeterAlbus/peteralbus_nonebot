"""Shared request contract and authenticated in-process NoneBot endpoint."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

INTERNAL_DIGEST_PATH = "/internal/ops/daily-digest"


class DailyDigestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(pattern=r"^[0-9]+$", max_length=30)
    send: StrictBool = False
    force: StrictBool = False


def register_daily_digest_endpoint(
    app: FastAPI,
    token_provider: Callable[[], str],
    runner: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    @app.post(INTERNAL_DIGEST_PATH, include_in_schema=False)
    async def trigger_daily_digest(
        values: DailyDigestRequest,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        token = token_provider()
        scheme, _, supplied = authorization.partition(" ")
        if len(token) < 32:
            raise HTTPException(503, "日报手动入口未配置运维 Token")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            supplied.strip().encode(), token.encode()
        ):
            raise HTTPException(401, "valid bearer token required")
        return await runner(**values.model_dump())
