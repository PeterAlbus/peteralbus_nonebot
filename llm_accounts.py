from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx


class AccountQueryError(Exception):
    def __init__(self, code: str, status_code: int = 502) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def sign_volc_request(
    method: str,
    host: str,
    action: str,
    version: str,
    service: str,
    access_key: str,
    secret_key: str,
    body: bytes = b"",
    now: datetime | None = None,
) -> tuple[str, dict[str, str]]:
    """Sign fixed-host Volcengine OpenAPI requests with HMAC-SHA256."""
    stamp = (
        (now or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .strftime("%Y%m%dT%H%M%SZ")
    )
    query = urlencode({"Action": action, "Version": version})
    digest = hashlib.sha256(body).hexdigest()
    headers = {
        "content-type": "application/json",
        "host": host,
        "x-content-sha256": digest,
        "x-date": stamp,
    }
    names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{key}:{headers[key]}\n" for key in sorted(headers))
    canonical = f"{method}\n/\n{query}\n{canonical_headers}\n{names}\n{digest}"
    scope = f"{stamp[:8]}/cn-beijing/{service}/request"
    to_sign = "\n".join(
        ("HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest())
    )
    key = secret_key.encode()
    for component in (stamp[:8], "cn-beijing", service, "request"):
        key = hmac.new(key, component.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={names}, Signature={signature}"
    )
    return f"https://{host}/?{query}", headers


class ProviderAccounts:
    def __init__(
        self,
        *,
        deepseek_api_key: str = "",
        volc_access_key: str = "",
        volc_secret_key: str = "",
        volc_project_name: str = "default",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.deepseek_api_key = deepseek_api_key
        self.volc_access_key = volc_access_key
        self.volc_secret_key = volc_secret_key
        self.volc_project_name = volc_project_name
        self.transport = transport

    async def _request(
        self, method: str, url: str, headers: dict[str, str], content: bytes = b""
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method, url, headers=headers, content=content
                )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise AccountQueryError("invalid_upstream_response")
            metadata = data.get("ResponseMetadata", {})
            if isinstance(metadata, dict) and metadata.get("Error"):
                raise AccountQueryError("upstream_error")
            return data
        except httpx.TimeoutException as exc:
            raise AccountQueryError("upstream_timeout", 504) from exc
        except (httpx.HTTPError, ValueError) as exc:
            # Upstream payloads and request headers can contain credentials.
            raise AccountQueryError("upstream_error") from exc

    async def _volc(
        self, action: str, service: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self.volc_access_key or not self.volc_secret_key:
            raise AccountQueryError("not_configured", 503)
        encoded = (
            json.dumps(body, separators=(",", ":")).encode()
            if body is not None
            else b""
        )
        method = "POST" if body is not None else "GET"
        host = (
            "ark.cn-beijing.volcengineapi.com"
            if service == "ark"
            else "billing.volcengineapi.com"
        )
        version = "2024-01-01" if service == "ark" else "2022-01-01"
        url, headers = sign_volc_request(
            method,
            host,
            action,
            version,
            service,
            self.volc_access_key,
            self.volc_secret_key,
            encoded,
        )
        data = await self._request(method, url, headers, encoded)
        result = data.get("Result")
        if not isinstance(result, dict):
            raise AccountQueryError("invalid_upstream_response")
        return result

    async def balance(self, provider: str) -> dict[str, Any]:
        try:
            if provider == "deepseek":
                key = self.deepseek_api_key
                if not key:
                    raise AccountQueryError("not_configured", 503)
                data = await self._request(
                    "GET",
                    "https://api.deepseek.com/user/balance",
                    {"Authorization": f"Bearer {key}"},
                )
                entries = data.get("balance_infos")
                if not isinstance(entries, list) or any(
                    not isinstance(x, dict) for x in entries
                ):
                    raise AccountQueryError("invalid_upstream_response")
                result = {
                    "is_available": data.get("is_available"),
                    "balance_infos": [
                        {
                            k: entry.get(k)
                            for k in (
                                "currency",
                                "total_balance",
                                "granted_balance",
                                "topped_up_balance",
                            )
                        }
                        for entry in entries
                    ],
                }
            elif provider == "volc_ark":
                data = await self._volc("QueryBalanceAcct", "billing")
                result = {
                    key: data.get(key)
                    for key in (
                        "AvailableBalance",
                        "CashBalance",
                        "ArrearsBalance",
                        "CreditLimit",
                        "FreezeAmount",
                    )
                }
            else:
                return {
                    "provider": provider,
                    "status": "unsupported",
                    "reason": "no_documented_public_balance_api",
                }
            return {
                "provider": provider,
                "status": "ok",
                "scope": "account",
                "source": "provider_api",
                "balance": result,
            }
        except AccountQueryError as exc:
            return {"provider": provider, "status": exc.code}

    async def balances(self) -> dict[str, Any]:
        items = await asyncio.gather(
            *(self.balance(p) for p in ("volc_ark", "deepseek", "xiaomi_mimo"))
        )
        return {
            "queried_at": datetime.now(timezone.utc).isoformat(),
            "providers": items,
        }

    async def volc_usage(
        self, start: date, end: date, api_key_id: str | None, model: str | None
    ) -> dict[str, Any]:
        if start > end:
            raise ValueError("start_date must not be later than end_date")
        filters = []
        if api_key_id:
            filters.append({"Key": "ApikeyID", "Values": [api_key_id]})
        if model:
            filters.append({"Key": "ModelName", "Values": [model]})
        body = {
            "StartTime": start.isoformat(),
            "EndTime": end.isoformat(),
            "QueryInterval": "Day",
            "ProjectName": self.volc_project_name,
            "Filters": filters,
        }
        result = await self._volc("GetInferenceUsage", "ark", body)
        if not isinstance(result.get("Fields"), list) or not isinstance(
            result.get("Data"), list
        ):
            raise AccountQueryError("invalid_upstream_response")
        return {
            "source": "provider_api",
            "provider": "volc_ark",
            "scope": "api_key" if api_key_id else "account_project",
            "timezone": "Asia/Shanghai",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "filters": filters,
            "project_name": self.volc_project_name,
            "usage": {key: result.get(key) for key in ("Fields", "Data", "DataCount")},
        }
