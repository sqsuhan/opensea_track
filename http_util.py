"""Shared HTTP client with exponential backoff for 429 / 5xx."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

log = logging.getLogger(__name__)


class RateLimitedClient:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        max_retries: int = 6,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers=headers or {},
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = await self._client.request(
                    method, url, params=params, json=json, headers=headers
                )
            except httpx.HTTPError as exc:
                last_error = exc
                delay = self._backoff(attempt)
                log.warning("HTTP error %s on %s; retrying in %.1fs", exc, url, delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = min(float(retry_after), self.max_delay)
                else:
                    delay = self._backoff(attempt)
                log.warning(
                    "%s %s -> %s; backing off %.1fs",
                    method,
                    url,
                    response.status_code,
                    delay,
                )
                await asyncio.sleep(delay)
                last_error = RuntimeError(f"HTTP {response.status_code}")
                continue

            return response

        if last_error:
            raise last_error
        raise RuntimeError(f"request failed after {self.max_retries} retries: {url}")

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.request("GET", url, **kwargs)
        response.raise_for_status()
        return response.json()

    async def post_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.request("POST", url, **kwargs)
        response.raise_for_status()
        return response.json()

    def _backoff(self, attempt: int) -> float:
        delay = min(self.max_delay, self.base_delay * (2**attempt))
        return delay + random.uniform(0, delay * 0.25)
