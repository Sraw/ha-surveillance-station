"""The bookmark list, fetched once for everyone and indexed once per fetch.

SS hands out every bookmark in one list, and the event list pages through it
30 at a time, filtered by camera and kind. With tens of thousands of
bookmarks, splitting every name into kinds and scanning for the cursor on
each page would stall HA's event loop; the index does that work once, off
the loop, and a page is then a bisect per list.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable
import heapq
from itertools import islice
import time
from typing import Any

from synology_ss_playback import Bookmark, SSError, SurveillanceStationClient

from homeassistant.core import HomeAssistant

from .const import BOOKMARK_CACHE_SECONDS, BOOKMARK_ERROR_SECONDS, KIND_CHIPS_MAX
from .shared import SharedJobs

# Patchable in tests (time.monotonic itself is the event loop's clock).
_monotonic = time.monotonic


def name_kinds(name: str) -> list[str]:
    """What a bookmark's name says was seen: "Person, Car" is Person and Car."""
    return [k.strip() for k in name.split(",") if k.strip()]


def wanted_kinds(kinds: Iterable[str] | None) -> set[str]:
    """The kinds asked for, as the index compares them: " Person " is person."""
    return {k.strip().casefold() for k in kinds or [] if k.strip()}


class _Run:
    """One camera's bookmarks of one set of kinds, newest first."""

    __slots__ = ("items", "keys")

    def __init__(self) -> None:
        self.items: list[Bookmark] = []
        self.keys: list[tuple[int, int]] = []  # (-start, -id): ascending, for bisect

    def after(self, cursor: tuple[int, int] | None) -> int:
        """Where the bookmarks older than cursor (start, id) begin."""
        return 0 if cursor is None else bisect_right(self.keys, (-cursor[0], -cursor[1]))


class BookmarkIndex:
    """One fetched list of bookmarks (newest first, as SS lists them), indexed
    by camera and by the kinds each is named for."""

    def __init__(self, bookmarks: list[Bookmark]) -> None:
        self.all = bookmarks
        self._runs: dict[int, dict[frozenset[str], _Run]] = {}
        # camera -> kind as written -> times named, and where it was first
        # named in the list (the spelling shown for a kind is its most common
        # one, the newest of those on a tie).
        self._named: dict[int, Counter[str]] = {}
        self._first: dict[int, dict[str, int]] = {}
        for pos, b in enumerate(sorted(bookmarks, key=lambda b: (b.start, b.id), reverse=True)):
            kinds = name_kinds(b.name)
            run = self._runs.setdefault(b.camera_id, {}).setdefault(frozenset(k.casefold() for k in kinds), _Run())
            run.items.append(b)
            run.keys.append((-b.start, -b.id))
            self._named.setdefault(b.camera_id, Counter()).update(kinds)
            first = self._first.setdefault(b.camera_id, {})
            for k in kinds:
                first.setdefault(k, pos)

    def _selected(self, cameras: list[int] | None, wanted: set[str]) -> list[_Run]:
        ids = self._runs if cameras is None else set(cameras) & self._runs.keys()
        return [
            run for cid in ids for kinds, run in self._runs[cid].items() if not wanted or wanted & kinds
        ]

    def page(
        self, cameras: list[int] | None, wanted: set[str], before: tuple[int, int] | None, limit: int
    ) -> tuple[list[Bookmark], int, bool]:
        """Up to limit bookmarks of these cameras (None: all) and kinds (none: all),
        newest first, older than the cursor (start, id); how many there are in
        all (cursor aside); and whether more follow this page."""
        runs = self._selected(cameras, wanted)
        starts = [run.after(before) for run in runs]
        rest = sum(len(run.items) - i for run, i in zip(runs, starts, strict=True))
        # Lazily from each run's cursor: a page costs its own length, however deep.
        older = (map(run.items.__getitem__, range(i, len(run.items))) for run, i in zip(runs, starts, strict=True))
        page = list(islice(heapq.merge(*older, key=lambda b: (-b.start, -b.id)), limit))
        return page, sum(len(run.items) for run in runs), rest > len(page)

    def kind_counts(self, cameras: list[int] | None) -> list[list[Any]]:
        """The kinds to filter the event list by: those of more than one bookmark
        (a hand-made bookmark's own name is no kind), most common first."""
        ids = self._runs if cameras is None else set(cameras) & self._runs.keys()
        counts: Counter[str] = Counter()
        spellings: dict[str, dict[str, tuple[int, int]]] = {}  # "car": {"Car": (times, -where first), "car": ...}
        for cid in ids:
            for spelling, n in self._named[cid].items():
                key = spelling.casefold()
                counts[key] += n
                had, first = spellings.setdefault(key, {}).get(spelling, (0, -len(self.all)))
                spellings[key][spelling] = (had + n, max(first, -self._first[cid][spelling]))
        common = sorted((kc for kc in counts.items() if kc[1] > 1), key=lambda kc: (-kc[1], kc[0]))
        # Each kind as it is mostly written.
        return [[max(spellings[k].items(), key=lambda s: s[1])[0], n] for k, n in common[:KIND_CHIPS_MAX]]


class BookmarkCache:
    """Every entry's bookmark list, at most BOOKMARK_CACHE_SECONDS old.

    One fetch per entry at a time, whose result (or error) every request
    waiting meanwhile shares: a NAS that hangs costs one timeout, not one
    per request queued behind it.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        # entry_id -> (fetched at, every bookmark newest first (indexed), or the error).
        self._lists: dict[str, tuple[float, BookmarkIndex | SSError]] = {}
        self._jobs: SharedJobs[str, BookmarkIndex] = SharedJobs(hass, "bookmarks")

    async def index(self, entry_id: str, client: SurveillanceStationClient) -> BookmarkIndex:
        if (hit := self._lists.get(entry_id)) is not None:
            at, found = hit
            if isinstance(found, SSError):
                if _monotonic() - at < BOOKMARK_ERROR_SECONDS:
                    raise found.with_traceback(None)
            elif _monotonic() - at < BOOKMARK_CACHE_SECONDS:
                return found
        return await self._jobs.run(entry_id, lambda: self._load(entry_id, client), "surveillance_station bookmarks")

    async def _load(self, entry_id: str, client: SurveillanceStationClient) -> BookmarkIndex:
        try:
            cameras = await client.cameras()
            found = await client.list_bookmarks([c.id for c in cameras])
            # Tens of thousands of bookmarks take a while: not on the loop.
            index = await self.hass.async_add_executor_job(BookmarkIndex, found)
        except SSError as err:
            self._lists[entry_id] = (_monotonic(), err)
            raise
        self._lists[entry_id] = (_monotonic(), index)
        return index

    def forget(self, entry_id: str) -> None:
        self._lists.pop(entry_id, None)

    def drop_entry(self, entry_id: str) -> None:
        self._lists.pop(entry_id, None)
        self._jobs.drop(lambda key: key == entry_id)
