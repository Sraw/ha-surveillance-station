"""Frigate review messages waiting for Surveillance Station: one per review.

A review's later message replaces its earlier one wherever that waits (it says
everything the earlier one did), so a backlog after an outage is one message
per review, not a minute-old queue of updates. A review waits in at most one
of three queues:

- pending: fresh messages, in the order their reviews first came;
- replay: what failed because SS was unreachable, going through again once it
  answers, after anything fresh;
- deferred: what waits for SS to answer again, kept across restarts (for a
  day). The oldest is tried again every minute, first in line (the canary);
  once one goes through the rest are replayed, and they wait again if SS
  fails meanwhile.

The message being handled (current) is apart: a newer message of its review
may wait behind it. Each queue holds at most FRIGATE_QUEUE_MAX reviews.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
import itertools
import time
from typing import Any

from .const import FRIGATE_DEFERRED_MAX_AGE, FRIGATE_QUEUE_MAX

# A waiting message as stored (the format of 0.18): Frigate's message, with
# these beside its "type" and "after".
_SEEN_NEW = "_seen_new"
_MAYBE_MADE = "_maybe_made"
_FAILED_AT = "_failed_at"
_RECEIVED_AT = "_received_at"


@dataclass
class Queued:
    """A review's message, and what its earlier ones told."""

    message: dict[str, Any]
    # When it came (epoch): a replay of an old one is no news. None: just now
    # (a message handled directly).
    received_at: float | None = None
    seen_new: bool = False  # it replaced its review's "new"
    maybe_made: bool = False  # a try of it failed: its bookmark may exist all the same
    failed_at: float | None = None  # when it first failed (epoch), for giving up after a day

    def follows(self, earlier: Queued, news: bool) -> None:
        """It replaces earlier, a message of its review that waited (news: that one had an
        object of interest), and keeps what that one told."""
        if earlier.message.get("type") == "new" or earlier.seen_new:
            self.seen_new = True
        if earlier.maybe_made:
            self.maybe_made = True
        # News since the earlier one came (it waited for SS): as old as that,
        # or a replay would seem fresh.
        if earlier.received_at and news and self.received_at is not None:
            self.received_at = min(self.received_at, earlier.received_at)

    def stored(self) -> dict[str, Any]:
        flags: dict[str, Any] = {_SEEN_NEW: self.seen_new or None, _MAYBE_MADE: self.maybe_made or None}
        flags |= {_FAILED_AT: self.failed_at, _RECEIVED_AT: self.received_at}
        return {**{k: v for k, v in flags.items() if v is not None}, **self.message}

    @classmethod
    def from_stored(cls, stored: dict[str, Any], now: float) -> Queued:
        """Kept by a delayed save, it may have been bookmarked after it (HA died before the
        next), so it is looked for first. Kept undated (before 0.18): dated now."""
        received_at = stored.get(_RECEIVED_AT)
        return cls(
            message={k: v for k, v in stored.items() if not k.startswith("_")},
            received_at=None if received_at is None else float(received_at),
            seen_new=bool(stored.get(_SEEN_NEW)),
            maybe_made=True,
            failed_at=float(stored.get(_FAILED_AT) or now),
        )


def compact(message: dict[str, Any]) -> dict[str, Any]:
    """What handle() reads of a message: what is kept, possibly on disk."""
    after = message["after"]
    data = after.get("data") if isinstance(after.get("data"), dict) else {}
    return {
        "type": message.get("type"),
        "after": {
            **{k: after.get(k) for k in ("id", "camera", "start_time", "end_time", "severity")},
            "data": {k: data.get(k) for k in ("objects", "zones", "thumb_time")},
        },
    }


class ReviewQueue:
    """The messages waiting, by review id; news tells whether a message has an object of interest."""

    def __init__(self, news: Callable[[dict[str, Any]], bool]) -> None:
        self._news = news
        self._pending: OrderedDict[str, Queued] = OrderedDict()
        self._replay: OrderedDict[str, Queued] = OrderedDict()
        self._deferred: OrderedDict[str, Queued] = OrderedDict()
        self._current: tuple[str, Queued] | None = None
        self._canary: str | None = None
        self._wake = asyncio.Event()
        self.coalesced = 0  # messages that replaced an earlier one of their review
        self.dropped = 0  # reviews given up on: out of a full deferred queue, or after a day

    @property
    def queued(self) -> int:
        """Fresh or replayed, to be handled next."""
        return len(self._pending) + len(self._replay)

    @property
    def deferred(self) -> int:
        """Waiting for SS to answer again."""
        return len(self._deferred)

    @property
    def replaying(self) -> bool:
        return bool(self._replay)

    def put(self, key: str, item: Queued) -> bool:
        """A message received: its review's latest (True: the queue was full, and its oldest
        review now waits with those SS failed)."""
        earlier = self._pending.get(key) or self._replay.pop(key, None) or self._deferred.pop(key, None)
        if earlier is not None:
            self.coalesced += 1
            item.follows(earlier, self._news(earlier.message))
        self._pending[key] = item  # an existing key keeps its place
        full = len(self._pending) > FRIGATE_QUEUE_MAX
        if full:
            # SS stuck for a long while: the oldest (but not the one being
            # tried to see whether SS is back) waits with those SS failed.
            oldest = next(k for k in itertools.islice(self._pending, 2) if k != self._canary)
            self._keep(oldest, self._pending.pop(oldest))
        self._wake.set()
        return full

    def next(self) -> tuple[str, Queued] | None:
        """The message to handle now, fresh reviews first (None: nothing waits); current until done()."""
        if self._pending:
            key, item = self._pending.popitem(last=False)
        elif self._replay:
            key, item = self._replay.popitem(last=False)
        else:
            return None
        # Kept if cut short (unload, HA stopping): stop() and waiting() keep it for next time.
        self._current = (key, item)
        return key, item

    def done(self, key: str) -> None:
        """The current message handled: made, found, failed or given up on."""
        self._current = None
        if key == self._canary:
            self._canary = None

    async def wait(self) -> None:
        """Until a message is put, released or tried again."""
        self._wake.clear()
        await self._wake.wait()

    def newer(self, key: str) -> Queued | None:
        """A newer message of the review being handled, come meanwhile: taken, to go instead."""
        return self._pending.pop(key, None)

    def retrying(self, key: str, item: Queued) -> None:
        """The review being handled goes on with this message (kept as it, if cut short)."""
        if self._current is not None and self._current[0] == key:
            self._current = (key, item)

    def hold_replay(self) -> bool:
        """SS failed again mid-replay: the rest wait again (no request each, stalling fresh
        reviews behind timeouts). False: nothing was being replayed."""
        if not self._replay:
            return False
        for key, item in self._replay.items():
            self._deferred.setdefault(key, item)
        self._replay.clear()
        return True

    def defer(self, key: str, item: Queued) -> None:
        """The current message failed with SS unreachable: it waits for SS, unless a newer
        message of its review already waits (that one goes instead, knowing the bookmark may exist)."""
        if (newer := self._newer_of(key, (self._pending, self._replay, self._deferred))) is not None:
            newer.maybe_made = True
        else:
            self._keep(key, item)

    def canary(self) -> bool:
        """Try the oldest review waiting for SS again, first in line (False: none waits)."""
        if not self._deferred:
            return False
        key, item = self._deferred.popitem(last=False)
        self._pending.setdefault(key, item)
        self._pending.move_to_end(key, last=False)
        self._canary = key
        self._wake.set()
        return True

    def release(self) -> bool:
        """SS took a bookmark: what waited for it goes again, after anything fresh (False: nothing waited)."""
        if not self._deferred:
            return False
        for key, item in self._deferred.items():
            if key not in self._pending:  # a newer message waiting wins
                self._replay.setdefault(key, item)
        self._deferred.clear()
        self._wake.set()
        return True

    def expire(self) -> int:
        """Give up on the reviews SS didn't take for a day; how many."""
        stale = [
            k for k, item in self._deferred.items() if time.time() - (item.failed_at or 0) > FRIGATE_DEFERRED_MAX_AGE
        ]
        for key in stale:
            del self._deferred[key]
        self.dropped += len(stale)
        return len(stale)

    def waiting(self) -> OrderedDict[str, Queued]:
        """Everything not bookmarked yet, one message per review, as kept for next time (the queues untouched)."""
        waiting = OrderedDict((k, replace(item)) for k, item in self._deferred.items())
        replay = OrderedDict((k, replace(item)) for k, item in self._replay.items())
        pending = OrderedDict((k, replace(item)) for k, item in self._pending.items())
        if self._current is not None:
            key, item = self._current
            if (newer := self._newer_of(key, (pending, replay, waiting))) is not None:
                newer.maybe_made = True
            else:
                replay[key] = replace(item, maybe_made=True)
        for key, item in (*replay.items(), *pending.items()):
            self._keep(key, item, waiting)
        return waiting

    def stop(self) -> None:
        """Unloaded: what isn't handled yet, or was cut short, waits for next time."""
        self._deferred = self.waiting()
        self._replay.clear()
        self._pending.clear()
        self._current = None

    def stored(self) -> list[list[Any]]:
        return [[key, item.stored()] for key, item in self.waiting().items()]

    def load(self, stored: list[Any], now: float) -> None:
        """What waited before a restart (ValueError, TypeError: unreadable)."""
        for key, message in stored:
            if isinstance(message, dict) and isinstance(message.get("after"), dict):
                self._deferred[str(key)] = Queued.from_stored(message, now)

    @staticmethod
    def _newer_of(key: str, queues: tuple[OrderedDict[str, Queued], ...]) -> Queued | None:
        """A newer message of the review in flight, waiting in one of these. Anything of the
        review waiting while one of its messages is handled came later: put() takes an
        earlier one out of every queue."""
        return next((queue[key] for queue in queues if key in queue), None)

    def _keep(self, key: str, item: Queued, into: OrderedDict[str, Queued] | None = None) -> None:
        """Wait for SS: the review's latest message, with what an earlier one waiting there told."""
        into = self._deferred if into is None else into
        earlier = into.pop(key, None)
        into[key] = replace(
            item,
            message=compact(item.message),
            failed_at=(earlier and earlier.failed_at) or item.failed_at or time.time(),
            maybe_made=item.maybe_made or bool(earlier and earlier.maybe_made),
            seen_new=item.seen_new or bool(earlier and earlier.seen_new),
        )
        if len(into) > FRIGATE_QUEUE_MAX:
            into.popitem(last=False)
            if into is self._deferred:
                self.dropped += 1
