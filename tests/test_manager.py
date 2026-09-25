"""VodManager's bounds: the fetch queue, the segment and thumbnail caches."""

import asyncio
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from synology_ss_playback import Segment, SSConnectionError, SSError

from custom_components.surveillance_station import views
from custom_components.surveillance_station.thumbnail_store import ThumbnailStore
from custom_components.surveillance_station.views import DATA_MANAGER, VodManager, VodSession
from homeassistant.core import HomeAssistant

from .conftest import T0


def _seg(i: int) -> Segment:
    return Segment(i, 100 + i, 1, T0 + 10 * i, 10.0, 10_000 * i, 10.0 * i, False, i == 0)


def _session(entry: MockConfigEntry) -> VodSession:
    return VodSession(entry.entry_id, 6, T0, [_seg(i) for i in range(8)], expires=T0 + 3600)


async def test_waiters_share_one_fetch(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    manager = hass.data[DATA_MANAGER]
    session = _session(setup_integration)
    gate = asyncio.Event()

    async def slow(client, seg, ffmpeg):
        await gate.wait()
        return b"i", b"m"

    fetch = AsyncMock(side_effect=slow)
    with patch.object(views, "fetch_segment", fetch):
        a = asyncio.create_task(manager.fetch(session, session.segments[0]))
        b = asyncio.create_task(manager.fetch(session, session.segments[0]))
        await asyncio.sleep(0)
        # One of the two callers gives up (a player aborts on a seek): the
        # download already reached the NAS, so it keeps going for the other.
        a.cancel()
        gate.set()
        assert await b == (b"i", b"m")
    assert fetch.await_count == 1
    assert manager.stats()["fetches_in_flight"] == 0


async def test_abandoned_queued_fetch_is_cancelled(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A fetch still waiting for a slot is dropped once nobody wants it."""
    manager = hass.data[DATA_MANAGER]
    session = _session(setup_integration)
    gate = asyncio.Event()
    started: list[int] = []

    async def slow(client, seg, ffmpeg):
        started.append(seg.index)
        await gate.wait()
        return b"i", b"m"

    with patch.object(views, "fetch_segment", AsyncMock(side_effect=slow)):
        # Fill every download slot, then queue one more.
        busy = [asyncio.create_task(manager.fetch(session, s)) for s in session.segments[: views.MAX_PARALLEL_FETCHES]]
        queued = asyncio.create_task(manager.fetch(session, session.segments[-1]))
        await asyncio.sleep(0.01)
        assert manager.stats()["fetches_in_flight"] == views.MAX_PARALLEL_FETCHES + 1
        queued.cancel()
        await asyncio.sleep(0.01)
        assert manager.stats()["fetches_in_flight"] == views.MAX_PARALLEL_FETCHES
        gate.set()
        await asyncio.gather(*busy)
    assert session.segments[-1].index not in started
    assert manager.stats()["cached_segments"] == views.MAX_PARALLEL_FETCHES


async def test_segment_cache_is_bounded(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    manager = hass.data[DATA_MANAGER]
    session = _session(setup_integration)
    fetch = AsyncMock(return_value=(b"", b"x" * 400))
    with patch.object(views, "SEGMENT_CACHE_BYTES", 1000), patch.object(views, "fetch_segment", fetch):
        for seg in session.segments[:4]:
            await manager.fetch(session, seg)
        assert manager.stats()["cached_segments"] == 2
        assert manager.stats()["cached_bytes"] == 800
        # The newest two are kept; the oldest is fetched again.
        await manager.fetch(session, session.segments[3])
        assert fetch.await_count == 4
        r = await manager.fetch(session, session.segments[0])
        assert fetch.await_count == 5
    assert manager.stats()["fetches_in_flight"] == 0


async def test_failed_fetch_is_not_remembered(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """An error is returned once; the next request for the segment tries again."""
    manager = hass.data[DATA_MANAGER]
    session = _session(setup_integration)
    fetch = AsyncMock(side_effect=[SSConnectionError("SYNO.SurveillanceStation.Recording", "Download", None), (b"i", b"m")])
    with patch.object(views, "fetch_segment", fetch):
        with pytest.raises(SSConnectionError):
            await manager.fetch(session, session.segments[0])
        assert await manager.fetch(session, session.segments[0]) == (b"i", b"m")
    assert manager.stats()["fetches_in_flight"] == 0


async def test_thumbnail_cache_is_bounded(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """By bytes, counting a fixed cost per entry, so misses (b"") count too."""
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    per = views.THUMBNAIL_ENTRY_BYTES
    snap = AsyncMock(side_effect=[b"j" * 400, None, b"j" * 400, None, None, None])
    with patch.object(views, "THUMBNAIL_CACHE_BYTES", 400 + 3 * per), patch.object(views, "fetch_snapshot", snap):
        for i in range(6):
            await manager.thumbnail(entry_id, 6, T0 + i)
    assert manager.stats()["cached_thumbnails"] == 3
    assert manager.stats()["cached_thumbnail_bytes"] == 3 * per


async def test_same_thumbnail_made_once(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    gate = asyncio.Event()

    async def slow(*args):
        await gate.wait()
        return b"j" * 400

    snap = AsyncMock(side_effect=slow)
    with patch.object(views, "fetch_snapshot", snap):
        both = asyncio.gather(manager.thumbnail(entry_id, 6, T0), manager.thumbnail(entry_id, 6, T0))
        await asyncio.sleep(0.01)
        gate.set()
        assert await both == [b"j" * 400] * 2
    assert snap.await_count == 1
    assert manager.stats()["cached_thumbnail_bytes"] == 400 + views.THUMBNAIL_ENTRY_BYTES


async def test_abandoned_thumbnail_is_cancelled(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    manager = hass.data[DATA_MANAGER]
    cancelled = asyncio.Event()

    async def slow(*args):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with patch.object(views, "fetch_snapshot", AsyncMock(side_effect=slow)):
        req = asyncio.create_task(manager.thumbnail(setup_integration.entry_id, 6, T0))
        await asyncio.sleep(0.01)
        req.cancel()
        await asyncio.wait_for(cancelled.wait(), 1)
    assert manager.stats()["cached_thumbnails"] == 0


async def test_thumbnail_miss_expires(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """"Nothing recorded" is re-checked later: the recording may not be listed yet."""
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    snap = AsyncMock(side_effect=[None, b"j"])
    now = views.time.monotonic()
    with patch.object(views, "fetch_snapshot", snap), patch.object(views.time, "monotonic") as clock:
        clock.return_value = now
        assert await manager.thumbnail(entry_id, 6, T0) is None
        assert await manager.thumbnail(entry_id, 6, T0) is None
        clock.return_value = now + views.THUMBNAIL_MISS_SECONDS + 1
        assert await manager.thumbnail(entry_id, 6, T0) == b"j"
    assert snap.await_count == 2


async def test_bookmark_fetch_shared_and_error_remembered(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client
) -> None:
    """Requests queued behind a hanging NAS share its one failure."""
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    gate = asyncio.Event()

    async def hang(ids):
        await gate.wait()
        raise SSConnectionError("SYNO.SurveillanceStation.ThirdParty.Bookmark", "List", None)

    mock_client.list_bookmarks.side_effect = hang
    calls = [asyncio.create_task(manager.bookmarks(entry_id, mock_client)) for _ in range(3)]
    await asyncio.sleep(0.01)
    gate.set()
    results = await asyncio.gather(*calls, return_exceptions=True)
    assert all(isinstance(r, SSConnectionError) for r in results)
    with pytest.raises(SSConnectionError):
        await manager.bookmarks(entry_id, mock_client)
    assert mock_client.list_bookmarks.await_count == 1


async def test_unreachable_logged_once(
    hass: HomeAssistant, setup_integration: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    caplog.set_level(logging.INFO, logger=views.__name__)
    down = AsyncMock(side_effect=SSConnectionError("SYNO.SurveillanceStation.Recording", "Download", None))
    with patch.object(views, "fetch_snapshot", down):
        for i in range(3):
            with pytest.raises(SSConnectionError):
                await manager.thumbnail(entry_id, 6, T0 + i)
    with patch.object(views, "fetch_snapshot", AsyncMock(return_value=b"j")):
        await manager.thumbnail(entry_id, 6, T0 + 10)
        await manager.thumbnail(entry_id, 6, T0 + 11)
    assert caplog.text.count("is unreachable") == 1
    assert caplog.text.count("reachable again") == 1


async def test_unload_answers_waiting_requests(hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client) -> None:
    """Unloading cancels the shared jobs; whoever waited gets an error, not a hang."""
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id

    async def hang(*args):
        await asyncio.Event().wait()

    mock_client.list_bookmarks.side_effect = hang
    with patch.object(views, "fetch_snapshot", AsyncMock(side_effect=hang)):
        waiting = [
            asyncio.create_task(manager.bookmarks(entry_id, mock_client)),
            asyncio.create_task(manager.thumbnail(entry_id, 6, T0)),
        ]
        await asyncio.sleep(0.01)
        manager.drop_entry(entry_id)
        results = await asyncio.gather(*waiting, return_exceptions=True)
    assert all(type(r) is SSError for r in results), results


async def test_thumbnails_outlive_a_restart(
    hass: HomeAssistant, setup_integration: MockConfigEntry, thumbnail_dir: Path
) -> None:
    """Made once, kept on disk: a new manager (HA restarted) serves it without the NAS, under the same URL."""
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    url = manager.sign_thumbnail(entry_id, 6, T0)
    snap = AsyncMock(side_effect=[b"j" * 400, None])
    with patch.object(views, "fetch_snapshot", snap):
        assert await manager.thumbnail(entry_id, 6, T0) == b"j" * 400
        assert await manager.thumbnail(entry_id, 6, T0 + 1) is None
    await manager.disk.settle()
    assert (thumbnail_dir / entry_id / "6" / f"{T0}.jpg").read_bytes() == b"j" * 400
    assert not (thumbnail_dir / entry_id / "6" / f"{T0 + 1}.jpg").exists()  # a miss isn't kept

    again = VodManager(hass)
    await again.async_load()
    assert again.stats()["disk_thumbnails"] == 1
    assert again.sign_thumbnail(entry_id, 6, T0) == url  # the browser's copy stays good
    with patch.object(views, "fetch_snapshot", AsyncMock(side_effect=AssertionError)):
        assert await again.thumbnail(entry_id, 6, T0) == b"j" * 400


async def test_disk_thumbnails_are_bounded(hass: HomeAssistant, tmp_path: Path) -> None:
    """Over the cap the least recently used go, also in the order read back after a restart."""
    store = ThumbnailStore(hass, str(tmp_path), 1000)
    for ts in (1, 2, 3):
        store.put(("E", 6, ts), b"x" * 400)
        await store.settle()
    assert len(store) == 2 and store.bytes == 800
    assert not (tmp_path / "E" / "6" / "1.jpg").exists()
    assert await store.get(("E", 6, 2)) == b"x" * 400  # now the most recently used
    os.utime(tmp_path / "E" / "6" / "3.jpg", (1, 1))
    (tmp_path / "E" / "6" / "9.jpg.123.tmp").write_bytes(b"half")  # a write cut short

    restarted = ThumbnailStore(hass, str(tmp_path), 1000)
    await restarted.load()
    assert restarted.bytes == 800
    assert not (tmp_path / "E" / "6" / "9.jpg.123.tmp").exists()
    restarted.put(("E", 6, 4), b"x" * 400)
    await restarted.settle()
    assert not (tmp_path / "E" / "6" / "3.jpg").exists()  # the one used longest ago
    assert (tmp_path / "E" / "6" / "2.jpg").exists()
    (tmp_path / "E" / "6" / "2.jpg").unlink()  # gone underneath: a miss
    assert await restarted.get(("E", 6, 2)) is None
    assert len(restarted) == 1


async def test_disk_thumbnail_paths_are_safe(hass: HomeAssistant, tmp_path: Path) -> None:
    store = ThumbnailStore(hass, str(tmp_path / "t"), 1000)
    store.put(("../x", 6, 1), b"j")
    store.put(("E", 6, 1), b"j")
    await store.settle()
    assert [p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.jpg")] == ["t/E/6/1.jpg"]


async def test_removing_the_entry_deletes_its_thumbnails(
    hass: HomeAssistant, setup_integration: MockConfigEntry, thumbnail_dir: Path
) -> None:
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    with patch.object(views, "fetch_snapshot", AsyncMock(return_value=b"j")):
        await manager.thumbnail(entry_id, 6, T0)
    assert (thumbnail_dir / entry_id).is_dir()
    await hass.config_entries.async_remove(entry_id)
    await hass.async_block_till_done()
    assert not (thumbnail_dir / entry_id).exists()
    assert manager.stats()["disk_thumbnails"] == 0


async def test_thumbnail_kept_even_if_nobody_waits_any_more(
    hass: HomeAssistant, setup_integration: MockConfigEntry, thumbnail_dir: Path
) -> None:
    """Made, then abandoned (scrolled past) while being written: on disk and in the index, never half."""
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    gate = __import__("threading").Event()
    real_write = __import__("custom_components.surveillance_station.thumbnail_store", fromlist=["_write"])._write

    def slow_write(path, data):
        gate.wait(5)
        real_write(path, data)

    with (
        patch.object(views, "fetch_snapshot", AsyncMock(return_value=b"j" * 400)),
        patch("custom_components.surveillance_station.thumbnail_store._write", slow_write),
    ):
        req = asyncio.create_task(manager.thumbnail(entry_id, 6, T0))
        await asyncio.sleep(0.05)
        req.cancel()
        gate.set()
        await manager.disk.settle()
    assert (thumbnail_dir / entry_id / "6" / f"{T0}.jpg").read_bytes() == b"j" * 400
    assert manager.stats()["disk_thumbnails"] == 1
    assert manager.stats()["disk_thumbnail_bytes"] == 400
    assert not list(thumbnail_dir.rglob("*.tmp"))


async def test_unwritable_thumbnail_dir(hass: HomeAssistant, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Can't write: nothing indexed, a warning, and the rest carries on."""
    (tmp_path / "file").write_bytes(b"")
    store = ThumbnailStore(hass, str(tmp_path / "file" / "t"), 1000)  # under a file: never a directory
    store.put(("E", 6, 1), b"j")
    await store.settle()
    assert len(store) == 0 and store.bytes == 0
    assert await store.get(("E", 6, 1)) is None
    assert "Can't keep thumbnails" in caplog.text


async def test_evicted_while_written_is_removed(hass: HomeAssistant, tmp_path: Path) -> None:
    store = ThumbnailStore(hass, str(tmp_path), 500)
    store.put(("E", 6, 1), b"x" * 400)
    store.put(("E", 6, 2), b"x" * 400)  # evicts 1, still being written
    await store.settle()
    assert sorted(p.name for p in tmp_path.rglob("*.jpg")) == ["2.jpg"]
    assert store.bytes == 400


async def test_bad_stored_key_is_replaced(hass: HomeAssistant, hass_storage: dict) -> None:
    hass_storage["surveillance_station.thumbnail_key"] = {"version": 1, "key": "surveillance_station.thumbnail_key", "data": {"key": ""}}
    manager = VodManager(hass)
    await manager.async_load()
    assert len(manager._thumb_key) == 32
    await hass.async_block_till_done()


async def test_dropping_an_entry_waits_for_its_writes(hass: HomeAssistant, tmp_path: Path) -> None:
    store = ThumbnailStore(hass, str(tmp_path), 1000)
    store.put(("E", 6, 1), b"j")
    await store.drop_entry("E")  # the write was still going on
    store.put(("E", 6, 2), b"j")  # a job finishing late: refused
    await store.settle()
    assert not (tmp_path / "E").exists()
    assert len(store) == 0
