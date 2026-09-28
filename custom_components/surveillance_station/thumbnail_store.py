"""Event thumbnails kept on disk, so a restart doesn't make them all again.

One JPEG per camera and moment under HA's cache directory
(``.cache/surveillance_station/thumbnails/<entry>/<camera>/<ts>.jpg``, which
HA backups leave out), capped by total size, the least recently used removed
first. The index lives on the event loop; file work runs in the executor. A
file that went missing underneath is just a miss.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time

from homeassistant.core import HomeAssistant

from .shared import ByteLRU

_LOGGER = logging.getLogger(__name__)

# Entry ids are ULIDs; anything else never becomes a path.
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

type ThumbKey = tuple[str, int, int]  # entry_id, camera_id, ts

WARN_INTERVAL = 3600  # seconds between warnings that the directory can't be written


class ThumbnailStore:
    """put() and the removals it causes are never awaited by the caller, so a
    thumbnail job cancelled (nobody waits for it any more) can't leave a file
    the index doesn't know about. What a write still in flight is for, or
    evicted before it landed, is reconciled when it lands."""

    def __init__(self, hass: HomeAssistant, root: str, max_bytes: int) -> None:
        self.hass = hass
        self.root = root
        self._index: ByteLRU[ThumbKey, int] = ByteLRU(max_bytes, lambda size: size)  # -> size, least recently used first
        self._writing: dict[ThumbKey, asyncio.Future[None]] = {}
        self._jobs: set[asyncio.Future[None]] = set()  # every file job still running
        self._dropped: set[str] = set()  # removed entries: nothing more is written for them
        self._warned_at = -WARN_INTERVAL

    def __len__(self) -> int:
        return len(self._index)

    @property
    def bytes(self) -> int:
        return self._index.bytes

    def _job(self, fn, *args) -> asyncio.Future[None]:
        fut = self.hass.async_add_executor_job(fn, *args)
        self._jobs.add(fut)
        fut.add_done_callback(self._jobs.discard)
        return fut

    async def settle(self) -> None:
        """Wait for the file work started so far (and what it starts)."""
        while self._jobs:
            await asyncio.wait(list(self._jobs))

    def _path(self, key: ThumbKey) -> str | None:
        entry_id, camera_id, ts = key
        if not _SAFE_ID.fullmatch(entry_id):
            return None
        return os.path.join(self.root, entry_id, str(int(camera_id)), f"{int(ts)}.jpg")

    async def load(self) -> None:
        """Index what's on disk (oldest use first) and trim it to the cap."""
        found = await self.hass.async_add_executor_job(self._scan)
        evicted = []
        for key, size, _ in sorted(found, key=lambda f: f[2]):
            if key not in self._index:
                evicted += self._index.put(key, size)
        self._remove(evicted)

    def _scan(self) -> list[tuple[ThumbKey, int, float]]:
        found: list[tuple[ThumbKey, int, float]] = []
        try:
            entries = os.listdir(self.root)
        except FileNotFoundError:
            return found
        except OSError as err:
            _LOGGER.warning("Can't read the thumbnail cache %s: %s", self.root, err)
            return found
        for entry_id in entries:
            if not _SAFE_ID.fullmatch(entry_id):
                continue
            entry_dir = os.path.join(self.root, entry_id)
            try:
                cameras = os.listdir(entry_dir)
            except OSError:
                continue
            for camera in cameras:
                if not (camera.isascii() and camera.isdigit()):
                    continue
                try:
                    files = list(os.scandir(os.path.join(entry_dir, camera)))
                except OSError:
                    continue
                for f in files:
                    stem, ext = os.path.splitext(f.name)
                    try:
                        if ext == ".tmp":  # a write that never finished
                            os.unlink(f.path)
                        elif ext == ".jpg" and stem.isascii() and stem.isdigit():
                            st = f.stat()
                            found.append(((entry_id, int(camera), int(stem)), st.st_size, st.st_mtime))
                    except OSError:
                        continue
        return found

    async def get(self, key: ThumbKey) -> bytes | None:
        # Still being written: the caller has it in memory anyway.
        if key not in self._index or key in self._writing or (path := self._path(key)) is None:
            return None
        self._index.get(key)  # now the most recently used
        data = await self.hass.async_add_executor_job(_read, path)
        if data is None and key not in self._writing:
            self._index.pop(key)
        return data

    def put(self, key: ThumbKey, data: bytes) -> None:
        """Keep a JPEG; the write goes on in the background."""
        if key[0] in self._dropped or key in self._writing or (path := self._path(key)) is None:
            return
        evicted = self._index.put(key, len(data))
        fut = self._job(_write, path, data)
        self._writing[key] = fut
        fut.add_done_callback(lambda f: self._written(key, path, f))
        self._remove(evicted)

    def _written(self, key: ThumbKey, path: str, fut: asyncio.Future[None]) -> None:
        del self._writing[key]
        if (err := None if fut.cancelled() else fut.exception()) is not None:
            self._index.pop(key)
            if time.monotonic() - self._warned_at > WARN_INTERVAL:
                self._warned_at = time.monotonic()
                _LOGGER.warning("Can't keep thumbnails in %s: %s", self.root, err)
        elif key not in self._index or key[0] in self._dropped:
            # Evicted or dropped while it was being written.
            self._job(_unlink, [path])

    async def drop_entry(self, entry_id: str) -> None:
        """Delete a removed entry's thumbnails (after any write still going on)."""
        self._dropped.add(entry_id)
        self._index.pop_where(lambda key: key[0] == entry_id)
        await self.settle()
        if _SAFE_ID.fullmatch(entry_id):
            await self.hass.async_add_executor_job(
                lambda: shutil.rmtree(os.path.join(self.root, entry_id), ignore_errors=True)
            )

    def _remove(self, evicted: list[tuple[ThumbKey, int]]) -> None:
        """Delete the files of what the index let go."""
        # One being written is removed once it lands (_written).
        paths = [path for key, _ in evicted if key not in self._writing and (path := self._path(key)) is not None]
        if paths:
            self._job(_unlink, paths)


def _read(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    try:
        os.utime(path)  # last used: the eviction order after a restart
    except OSError:
        pass
    return data


def _write(path: str, data: bytes) -> None:
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        # Left 0600, as mkstemp makes it: these are camera frames.
        os.replace(tmp, path)  # never a half-written JPEG under the real name
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _unlink(paths: list[str]) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass
