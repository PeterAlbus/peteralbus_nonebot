from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .config_store import ConfigValidationError, EnvConfigStore
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
    app.state.settings = settings
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

    @app.get("/api/v1/nonebot/config", dependencies=[Depends(require_admin)])
    def nonebot_config() -> dict[str, Any]:
        try:
            return app.state.config_store.read()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail="NoneBot .env file not found") from exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail="Unable to read NoneBot config") from exc

    @app.patch("/api/v1/nonebot/config", dependencies=[Depends(require_admin)])
    def update_nonebot_config(
        values: Annotated[dict[str, Any], Body()],
    ) -> dict[str, Any]:
        try:
            result = app.state.config_store.update(values)
        except ConfigValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail="NoneBot .env file not found") from exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail="Unable to update NoneBot config") from exc
        return {**result, "restart_required": True}

    return app
