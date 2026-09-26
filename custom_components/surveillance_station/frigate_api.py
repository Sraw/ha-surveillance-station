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

# A review or tracked-object id ("1790406867.462609-6jc58g"); never "." or ".."
# (a URL would take those as a path step).
FRIGATE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# Frigate's answers are small (JSON, a snapshot): a bigger one is something else.
MAX_BODY_BYTES = 8 * 1024 * 1024


def _message(body: bytes) -> str:
    """Frigate's own explanation of an error ("Semantic search is not enabled"), if it gave one."""
    try:
        message = json.loads(body).get("message")
    except (ValueError, AttributeError):
        return ""
    return f" ({message[:120]})" if isinstance(message, str) and message else ""


class FrigateAPIError(Exception):
    """Frigate didn't answer, or not as expected (text: path and status only; status 200: an answer
    that isn't what was asked for)."""

    def __init__(self, text: str, status: int | None = None) -> None:
        super().__init__(text)
        self.status = status


class FrigateAPI:
    def __init__(self, session: aiohttp.ClientSession, url: str) -> None:
        self.url = url.rstrip("/")
        self._session = session

    async def json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        body, _ = await self._get(path, params)
        try:
            return json.loads(body)
        except ValueError:
            raise FrigateAPIError(f"{path}: not JSON", 200) from None

    async def image(self, path: str, params: dict[str, Any] | None = None) -> tuple[bytes, str]:
        """A JPEG or WebP, and its type: by its bytes, not by what the answer claims."""
        body, _ = await self._get(path, params)
        if body[:3] == b"\xff\xd8\xff":
            return body, "image/jpeg"
        if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
            return body, "image/webp"
        raise FrigateAPIError(f"{path}: not a JPEG or WebP", 200)

    async def _get(self, path: str, params: dict[str, Any] | None) -> tuple[bytes, str]:
        try:
            async with asyncio.timeout(FRIGATE_API_TIMEOUT):
                async with self._session.get(f"{self.url}{path}", params=params) as resp:
                    if (getattr(resp, "content_length", None) or 0) > MAX_BODY_BYTES:
                        raise FrigateAPIError(f"{path}: answer too big", resp.status)
                    chunks, size = [], 0
                    async for chunk in resp.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > MAX_BODY_BYTES:
                            raise FrigateAPIError(f"{path}: answer too big", resp.status)
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    if resp.status != 200:
                        raise FrigateAPIError(f"{path}: HTTP {resp.status}{_message(body)}", resp.status)
                    return body, resp.content_type or ""
        except (aiohttp.ClientError, TimeoutError) as err:
            raise FrigateAPIError(f"{path}: {type(err).__name__}") from None
