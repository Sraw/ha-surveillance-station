"""What the caches share: one job per key, and a least-recently-used map bounded by bytes."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from typing import Any

from synology_ss_playback import SSError

from homeassistant.core import HomeAssistant


class ByteLRU[K, V]:
    """Values by key, least recently used first, the oldest dropped once they
    take more than max_bytes (the newest always stays, however big)."""

    def __init__(self, max_bytes: int, size: Callable[[V], int]) -> None:
        self.max_bytes = max_bytes
        self._size = size
        self._items: OrderedDict[K, V] = OrderedDict()
        self.bytes = 0

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def get(self, key: K) -> V | None:
        """The value, now the most recently used."""
        if (value := self._items.get(key)) is not None:
            self._items.move_to_end(key)
        return value

    def put(self, key: K, value: V) -> list[tuple[K, V]]:
        """Keep value (the most recently used); what that pushed out, oldest first."""
        self.pop(key)
        self._items[key] = value
        self.bytes += self._size(value)
        evicted = []
        while self.bytes > self.max_bytes and len(self._items) > 1:
            old = self._items.popitem(last=False)
            self.bytes -= self._size(old[1])
            evicted.append(old)
        return evicted

    def pop(self, key: K) -> V | None:
        if (value := self._items.pop(key, None)) is not None:
            self.bytes -= self._size(value)
        return value

    def pop_where(self, match: Callable[[K], bool]) -> None:
        for key in [k for k in self._items if match(k)]:
            self.pop(key)


class SharedJobs[K, T]:
    """One job per key, whose result every request for it meanwhile shares.

    The job runs as its own task, so a request that goes away (a player
    aborts on every seek) neither kills the work nor the other requests
    waiting for it. With ``abandon``, a job that everyone waiting has left is
    cancelled if abandon(key, task) says so.
    """

    def __init__(
        self, hass: HomeAssistant, what: str, abandon: Callable[[K, asyncio.Task[T]], bool] | None = None
    ) -> None:
        self._hass = hass
        self._what = what
        self._abandon = abandon
        self._tasks: dict[K, asyncio.Task[T]] = {}
        self._waiters: dict[K, int] = {}

    def __len__(self) -> int:
        return len(self._tasks)

    async def run(self, key: K, job: Callable[[], Coroutine[Any, Any, T]], name: str) -> T:
        """job()'s result: of the one already running for key, if any (and not being cancelled)."""
        task = self._tasks.get(key)
        if task is None or task.cancelling():
            # Not eager: a task that finished inside the call (an error before
            # the first await) would clear its slot before it was set, and the
            # finished task would then answer for that key for good.
            task = self._hass.async_create_background_task(self._own(key, job), name, eager_start=False)
            task.add_done_callback(retrieve_exception)
            self._tasks[key] = task
        self._waiters[key] = self._waiters.get(key, 0) + 1
        try:
            return await join(task, self._what)
        finally:
            if left := self._waiters.pop(key) - 1:
                self._waiters[key] = left
            elif self._abandon is not None and not task.done() and self._abandon(key, task):
                task.cancel()

    async def _own(self, key: K, job: Callable[[], Coroutine[Any, Any, T]]) -> T:
        me = asyncio.current_task()
        try:
            return await job()
        finally:
            if self._tasks.get(key) is me:
                del self._tasks[key]

    def cancel(self, match: Callable[[K], bool]) -> None:
        """Cancel the jobs whose key matches; they end on their own."""
        for key, task in self._tasks.items():
            if match(key):
                task.cancel()

    def drop(self, match: Callable[[K], bool]) -> None:
        """Cancel the jobs whose key matches, and forget them at once."""
        for key in [k for k in self._tasks if match(k)]:
            self._tasks.pop(key).cancel()


async def join[T](task: asyncio.Task[T], what: str) -> T:
    """Wait for a shared job; its cancellation (entry unloaded) is an error, not ours.

    A CancelledError escaping into a WebSocket handler would leave the card's
    call unanswered for good; a view would drop the connection.
    """
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if (me := asyncio.current_task()) is not None and me.cancelling():
            raise  # the request itself was cancelled
        raise SSError(what, "fetch", None, "Surveillance Station entry was unloaded") from None


def retrieve_exception(task: asyncio.Future) -> None:
    # Everyone waiting may have gone away; don't log "never retrieved".
    if not task.cancelled():
        task.exception()
