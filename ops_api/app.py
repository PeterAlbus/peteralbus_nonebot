# FastAPI evaluates route annotations at runtime on Python 3.9.
# ruff: noqa: UP045
from __future__ import annotations

import hmac
import sqlite3
from datetime import date
from typing import Annotated, Any, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from llm_accounts import AccountQueryError
from llm_usage import UsageStore

from .config_store import ConfigValidationError, EnvConfigStore
from .daily_digest import DailyDigestRequest
from .model_routes_store import ModelRoutesStore
from .provider_accounts import provider_accounts_for_settings
from .settings import OpsSettings
from .supervisor import OpsActionError, OpsSupervisor


def create_app(
    settings: OpsSettings,
    supervisor: OpsSupervisor | Any | None = None,
) -> FastAPI:
    app = FastAPI(
        title="NoneBot Host Operations API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.provider_accounts_factory = lambda: provider_accounts_for_settings(settings)
    app.state.settings = settings
    app.state.model_routes_store = ModelRoutesStore(settings.model_routes_file)
    app.state.supervisor = supervisor or OpsSupervisor(settings)
    app.state.config_store = EnvConfigStore(
        settings.nonebot_env_file,
        settings.repo_root / ".env.example",
    )

    @app.middleware("http")
    async def no_store(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def require_admin(authorization: str = Header(default="")) -> None:
        scheme, separator, supplied = authorization.partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not hmac.compare_digest(
                supplied.strip().encode("utf-8"), settings.api_token.encode("utf-8")
            )
        ):
            raise HTTPException(
                status_code=401,
                detail="valid bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.exception_handler(OpsActionError)
    async def action_error_handler(
        _request: Request, exc: OpsActionError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc)},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/system", dependencies=[Depends(require_admin)])
    def system_status() -> dict[str, Any]:
        return app.state.supervisor.get_system_status()

    @app.get("/api/v1/nonebot", dependencies=[Depends(require_admin)])
    def nonebot_status() -> dict[str, Any]:
        return app.state.supervisor.get_nonebot_status()

    @app.get("/api/v1/napcat", dependencies=[Depends(require_admin)])
    def napcat_status() -> dict[str, Any]:
        return app.state.supervisor.get_napcat_status()

    @app.post("/api/v1/napcat/restart", dependencies=[Depends(require_admin)])
    def napcat_restart() -> dict[str, Any]:
        return app.state.supervisor.restart_napcat()

    @app.post("/api/v1/nonebot/restart", dependencies=[Depends(require_admin)])
    def nonebot_restart() -> dict[str, Any]:
        return app.state.supervisor.restart_nonebot()

    @app.post("/api/v1/nonebot/daily-digest", dependencies=[Depends(require_admin)])
    async def trigger_daily_digest(values: DailyDigestRequest) -> dict[str, Any]:
        return await app.state.supervisor.trigger_daily_digest(values.model_dump())

    @app.get("/api/v1/nonebot/config", dependencies=[Depends(require_admin)])
    def nonebot_config() -> dict[str, Any]:
        try:
            return app.state.config_store.read()
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=503, detail="NoneBot .env file not found"
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail="Unable to read NoneBot config"
            ) from exc

    @app.patch("/api/v1/nonebot/config", dependencies=[Depends(require_admin)])
    def update_nonebot_config(
        values: Annotated[dict[str, Any], Body()],
    ) -> dict[str, Any]:
        try:
            result = app.state.config_store.update(values)
        except ConfigValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=503, detail="NoneBot .env file not found"
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail="Unable to update NoneBot config"
            ) from exc
        return {**result, "restart_required": True}

    @app.get(
        "/api/v1/nonebot/model-routes/models", dependencies=[Depends(require_admin)]
    )
    def read_models() -> dict[str, Any]:
        try:
            return app.state.model_routes_store.read_models()
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail="Unable to read model routes; check OPS_MODEL_ROUTES_FILE",
            ) from exc

    @app.put(
        "/api/v1/nonebot/model-routes/models", dependencies=[Depends(require_admin)]
    )
    def replace_models(values: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        try:
            return app.state.model_routes_store.replace_models(values)
        except ConfigValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail="Unable to update model routes; check OPS_MODEL_ROUTES_FILE",
            ) from exc

    @app.get("/api/v1/llm/balances", dependencies=[Depends(require_admin)])
    async def llm_balances() -> dict[str, Any]:
        return await app.state.provider_accounts_factory().balances()

    @app.get(
        "/api/v1/llm/providers/volc_ark/usage", dependencies=[Depends(require_admin)]
    )
    async def volc_usage(
        start_date: date,
        end_date: date,
        api_key_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            return await app.state.provider_accounts_factory().volc_usage(
                start_date, end_date, api_key_id, model
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AccountQueryError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc

    @app.get("/api/v1/llm/usage", dependencies=[Depends(require_admin)])
    def llm_usage(
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        request_type: Optional[str] = None,
    ) -> dict[str, Any]:
        if settings.llm_usage_db_file is None:
            raise HTTPException(
                status_code=503, detail="OPS_LLM_USAGE_DB_FILE is not configured"
            )
        try:
            return UsageStore(settings.llm_usage_db_file).query(
                start_date, end_date, provider, model, request_type
            )
        except (ValueError, OverflowError) as exc:
            raise HTTPException(status_code=422, detail="Invalid date range") from exc
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(
                status_code=503, detail="Unable to read LLM usage database"
            ) from exc

    return app
