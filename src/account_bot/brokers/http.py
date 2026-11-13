"""HTTP policy boundary that makes broker adapters read-only by construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx


class ReadOnlyPolicyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BrokerHttpError(RuntimeError):
    status_code: int
    request_id: str | None = None

    def __str__(self) -> str:
        return f"broker HTTP request failed ({self.status_code})"


@dataclass(frozen=True, slots=True)
class RequestRule:
    method: str
    path: str
    api_ids: frozenset[str] = frozenset()


class ReadOnlyHttpClient:
    """Only calls exact rules supplied at construction.

    Request/response payloads are deliberately never logged here because both
    may contain tokens or account identifiers.
    """

    def __init__(
        self,
        base_url: str,
        rules: set[RequestRule],
        *,
        timeout_seconds: float = 12.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("broker base URL must be an HTTPS origin")
        self._base_url = base_url.rstrip("/")
        self._rules = frozenset(rules)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    def _authorize(self, method: str, path: str, headers: dict[str, str]) -> None:
        if not path.startswith("/") or "//" in path or "?" in path or "#" in path:
            raise ReadOnlyPolicyError("request path is not canonical")
        normalized_method = method.upper()
        api_id = headers.get("api-id", "")
        for rule in self._rules:
            if rule.method.upper() != normalized_method or rule.path != path:
                continue
            if rule.api_ids and api_id not in rule.api_ids:
                continue
            return
        raise ReadOnlyPolicyError("request rejected by read-only allowlist")

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], httpx.Headers]:
        safe_headers = dict(headers or {})
        self._authorize(method, path, safe_headers)
        response = await self._client.request(
            method.upper(),
            f"{self._base_url}{path}",
            headers=safe_headers,
            params=params,
            json=json,
            data=data,
        )
        if response.status_code >= 400:
            raise BrokerHttpError(
                status_code=response.status_code,
                request_id=response.headers.get("x-request-id"),
            )
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise BrokerHttpError(response.status_code) from exc
        if not isinstance(payload, dict):
            raise BrokerHttpError(response.status_code)
        return payload, response.headers

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
