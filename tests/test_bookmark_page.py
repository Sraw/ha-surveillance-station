"""The event list's pages, from the bookmark list indexed once per fetch."""

import random
from typing import Any
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator
from synology_ss_playback import Bookmark

from custom_components.surveillance_station import bookmarks
from custom_components.surveillance_station.bookmarks import BookmarkIndex, name_kinds
from custom_components.surveillance_station.const import KIND_CHIPS_MAX
from homeassistant.core import HomeAssistant

from .conftest import T0


def _scan(marks: list[Bookmark], cams, kinds, before, limit) -> tuple[list[Bookmark], int, bool, list[list[Any]]]:
    """The event list as it was worked out before the index: every bookmark, every page."""
    shown = [b for b in marks if cams is None or b.camera_id in cams]
    wanted = {k.strip().casefold() for k in kinds if k.strip()}
    matching = [b for b in shown if not wanted or wanted & {k.casefold() for k in name_kinds(b.name)}]
    rest = [b for b in matching if before is None or (b.start, b.id) < before]
    page = rest[:limit]
    counts: dict[str, int] = {}
    spellings: dict[str, dict[str, int]] = {}
    for b in shown:
        for k in name_kinds(b.name):
            counts[k.casefold()] = counts.get(k.casefold(), 0) + 1
            spellings.setdefault(k.casefold(), {})[k] = spellings.setdefault(k.casefold(), {}).get(k, 0) + 1
    common = sorted((kc for kc in counts.items() if kc[1] > 1), key=lambda kc: (-kc[1], kc[0]))
    chips = [[max(spellings[k].items(), key=lambda s: s[1])[0], n] for k, n in common[:KIND_CHIPS_MAX]]
    return page, len(matching), len(rest) > len(page), chips


def test_pages_match_a_full_scan() -> None:
    rnd = random.Random(7)
    kinds = ["Person", "person", "Car", "CAR", "Animal", "Dog", "Bicycle", "Boat", "Bus", "Bird", "Cat", "Truck", "Van"]
    names = [", ".join(rnd.sample(kinds, rnd.randint(0, 3))) for _ in range(40)] + ["", "My mark", "Car, car", " , "]
    marks = sorted(
        (
            Bookmark(id=i, camera_id=rnd.choice([1, 2, 3]), name=rnd.choice(names), comment="",
                     start=T0 + rnd.randint(0, 200), end=T0 + 300)  # many share a start: ordered by id then
            for i in range(400)
        ),
        key=lambda b: (b.start, b.id),
        reverse=True,
    )
    index = BookmarkIndex(marks)
    for _ in range(300):
        cams = rnd.choice([None, [1], [2, 3], [3, 3], [], [9]])
        wanted = rnd.sample(["person", " Car ", "animal", "dog", "", "nothing"], rnd.randint(0, 3))
        cursor = rnd.choice(marks)
        before = rnd.choice([None, (cursor.start, cursor.id), (T0 + 100, 10_000), (T0 - 1, 0)])
        limit = rnd.choice([1, 5, 30, 100])
        page, total, more, chips = _scan(marks, cams, wanted, before, limit)
        wanted_set = {k.strip().casefold() for k in wanted if k.strip()}
        assert index.page(cams, wanted_set, before, limit) == (page, total, more)
        assert index.kind_counts(cams) == chips
    assert index.all is marks


async def test_kinds_worked_out_once_per_fetch(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    """Scrolling the event list, or filtering it, doesn't go through every bookmark again."""
    ws = await hass_ws_client(hass)
    with patch.object(bookmarks, "name_kinds", wraps=name_kinds) as split:
        for extra in ({}, {"kinds": ["car"]}, {"limit": 1, "before": T0 + 900, "before_id": 2}, {"camera_ids": [6]}):
            await ws.send_json_auto_id({"type": "surveillance_station/bookmark_page", **extra})
            assert (await ws.receive_json())["success"]
    assert split.call_count == 3  # the three bookmarks, once
