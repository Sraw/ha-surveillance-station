"""Frigate's HTTP API: the snapshots of a review's objects.

Reached the way Home Assistant reaches it (typically Frigate's internal,
unauthenticated port 5000). Only ids Frigate itself handed out go into a
path, and only after checking they look like one.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import aiohttp

from .const import FRIGATE_API_TIMEOUT

# A review or tracked-object id ("1790406867.462609-6jc58g").
FRIGATE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class FrigateAPIError(Exception):
    """Frigate didn't answer, or not as expected (text: path and status only)."""


class FrigateAPI:
    def __init__(self, session: aiohttp.ClientSession, url: str) -> None:
        self.url = url.rstrip("/")
        self._session = session

    async def json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        body, _ = await self._get(path, params)
        try:
            return json.loads(body)
        except ValueError:
            raise FrigateAPIError(f"{path}: not JSON") from None

    async def image(self, path: str, params: dict[str, Any] | None = None) -> tuple[bytes, str]:
        body, content_type = await self._get(path, params)
        if not content_type.startswith("image/"):
            raise FrigateAPIError(f"{path}: not an image ({content_type})")
        return body, content_type

    async def _get(self, path: str, params: dict[str, Any] | None) -> tuple[bytes, str]:
        try:
            async with asyncio.timeout(FRIGATE_API_TIMEOUT):
                async with self._session.get(f"{self.url}{path}", params=params) as resp:
                    body = await resp.read()
                    if resp.status != 200:
                        raise FrigateAPIError(f"{path}: HTTP {resp.status}")
                    return body, resp.content_type or ""
        except (aiohttp.ClientError, TimeoutError) as err:
            raise FrigateAPIError(f"{path}: {type(err).__name__}") from None
