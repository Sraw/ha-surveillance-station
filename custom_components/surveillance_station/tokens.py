"""What stands in for HA's auth where a browser can't send it: signed image
URLs (an <img> has no auth header) and single-use live-stream tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import NamedTuple

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, LIVE_TOKEN_TTL_SECONDS, LIVE_TOKENS_MAX, THUMBNAIL_URL, THUMBNAIL_URL_TTL_HOURS


class UrlSigner:
    """Image URLs carrying an HMAC of our own (see sign_thumbnail)."""

    def __init__(self, hass: HomeAssistant) -> None:
        # Kept across restarts (load) so the URLs, and the browser's cached
        # copies, stay good.
        self._key = secrets.token_bytes(32)
        self._store: Store[dict[str, str]] = Store(hass, 1, f"{DOMAIN}.thumbnail_key", private=True)

    async def load(self) -> None:
        """The signing key made on first use."""
        data = await self._store.async_load()
        try:
            key = bytes.fromhex(data["key"])  # type: ignore[index]
        except (TypeError, KeyError, ValueError):
            key = b""
        if len(key) == 32:
            self._key = key
        else:  # none yet, or not one of ours
            await self._store.async_save({"key": self._key.hex()})

    def sign_thumbnail(self, entry_id: str, camera_id: int, ts: int, large: bool = False) -> str:
        """A URL for the frame of camera_id at ts, valid for one to two days.

        An HMAC of our own rather than HA's async_sign_path: HA answers an
        expired or foreign signature (after every HA restart, as its signing
        key lives in memory) with 401, and counts each 401 as a failed login,
        so a long-open card would get its device IP-banned. A bad signature
        here is a plain 404. The expiry is the end of the (UTC) day plus a
        day, so the URL (and the browser's cached copy) is the same all day,
        across list refreshes and HA restarts. large: LARGE_IMAGE_WIDTH wide
        (for a notification) rather than THUMBNAIL_WIDTH.
        """
        return self.sign_path(f"{THUMBNAIL_URL}/{entry_id}/{camera_id}/{ts}{'-large' if large else ''}.jpg")

    def sign_path(self, path: str) -> str:
        """Any image URL of ours, signed as sign_thumbnail's are (checked by check)."""
        exp = (int(time.time()) // 86400 + 1) * 86400 + THUMBNAIL_URL_TTL_HOURS * 3600
        return f"{path}?exp={exp}&sig={self._sig(path, exp)}"

    def check(self, path: str, exp: str | None, sig: str | None) -> bool:
        # isascii: str.isdigit() accepts "²", and compare_digest rejects non-ASCII str.
        # At most 12 digits: int() refuses a string of over 4300 (a 500, not a 404).
        if not exp or not sig or len(exp) > 12 or not (exp.isascii() and exp.isdigit()) or not sig.isascii():
            return False
        if int(exp) < time.time():
            return False
        return hmac.compare_digest(sig, self._sig(path, int(exp)))

    def _sig(self, path: str, exp: int) -> str:
        return hmac.new(self._key, f"{path}\n{exp}".encode(), hashlib.sha256).hexdigest()[:32]


class _LiveToken(NamedTuple):
    entry_id: str
    camera_id: int
    at: float | None  # recordings from then; None: real time
    expires: float


class LiveTokens:
    """Single-use tokens for a camera's stream, handed out over the WebSocket."""

    def __init__(self) -> None:
        self._tokens: dict[str, _LiveToken] = {}  # unused ones, oldest first

    def __len__(self) -> int:
        return len(self._tokens)

    def create(self, entry_id: str, camera_id: int, at: float | None = None) -> str:
        """A token for the stream of camera_id: real time, or recordings from ``at``."""
        now = time.time()
        for token in [t for t, v in self._tokens.items() if v.expires < now]:
            del self._tokens[token]
        while len(self._tokens) >= LIVE_TOKENS_MAX:
            del self._tokens[next(iter(self._tokens))]  # the oldest
        token = secrets.token_urlsafe(32)
        self._tokens[token] = _LiveToken(entry_id, camera_id, at, now + LIVE_TOKEN_TTL_SECONDS)
        return token

    def take(self, token: str) -> tuple[str, int, float | None] | None:
        """(entry_id, camera_id, at) of an unused, unexpired token; it is used up."""
        found = self._tokens.pop(token, None)
        if found is None or found.expires < time.time():
            return None
        return found.entry_id, found.camera_id, found.at

    def drop_entry(self, entry_id: str) -> None:
        for token in [t for t, v in self._tokens.items() if v.entry_id == entry_id]:
            del self._tokens[token]
