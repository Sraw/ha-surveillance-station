"""VodManager's bounds: the fetch queue, the segment and thumbnail caches."""

import asyncio
from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from synology_ss_playback import Bookmark, RecordingInfo, Segment, SSConnectionError, SSError

from custom_components.surveillance_station import bookmarks, manager as manager_mod, segments, thumbnails, tokens
from custom_components.surveillance_station.const import (
    DOMAIN,
    FRIGATE_ISSUE_AFTER_SECONDS,
    LIVE_TOKEN_TTL_SECONDS,
    MAX_PARALLEL_FETCHES,
    THUMBNAIL_ENTRY_BYTES,
    THUMBNAIL_MISS_SECONDS,
)
from custom_components.surveillance_station.manager import DATA_MANAGER, VodManager
from custom_components.surveillance_station.segments import VodSession
from custom_components.surveillance_station.thumbnail_store import ThumbnailStore
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

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
    with patch.object(segments, "fetch_segment", fetch):
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

    with patch.object(segments, "fetch_segment", AsyncMock(side_effect=slow)):
        # Fill every download slot, then queue one more.
        busy = [asyncio.create_task(manager.fetch(session, s)) for s in session.segments[:MAX_PARALLEL_FETCHES]]
        queued = asyncio.create_task(manager.fetch(session, session.segments[-1]))
        await asyncio.sleep(0.01)
        assert manager.stats()["fetches_in_flight"] == MAX_PARALLEL_FETCHES + 1
        queued.cancel()
        await asyncio.sleep(0.01)
        assert manager.stats()["fetches_in_flight"] == MAX_PARALLEL_FETCHES
        gate.set()
        await asyncio.gather(*busy)
    assert session.segments[-1].index not in started
    assert manager.stats()["cached_segments"] == MAX_PARALLEL_FETCHES


async def test_abandoned_download_finishes_into_the_cache(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Left by everyone once it reached the NAS: it finishes all the same, and the next request finds it."""
    manager = hass.data[DATA_MANAGER]
    session = _session(setup_integration)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow(client, seg, ffmpeg):
        started.set()
        await gate.wait()
        return b"i", b"m"

    fetch = AsyncMock(side_effect=slow)
    with patch.object(segments, "fetch_segment", fetch):
        req = asyncio.create_task(manager.fetch(session, session.segments[0]))
        await started.wait()
        req.cancel()
        await asyncio.sleep(0.01)
        assert manager.stats()["fetches_in_flight"] == 1
        gate.set()
        while manager.stats()["fetches_in_flight"]:
            await asyncio.sleep(0.01)
        assert await manager.fetch(session, session.segments[0]) == (b"i", b"m")
    assert fetch.await_count == 1
    assert manager.stats()["cached_segments"] == 1


async def test_segment_cache_is_bounded(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    session = _session(setup_integration)
    fetch = AsyncMock(return_value=(b"", b"x" * 400))
    with patch.object(segments, "SEGMENT_CACHE_BYTES", 1000), patch.object(segments, "fetch_segment", fetch):
        manager = VodManager(hass)
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
    with patch.object(segments, "fetch_segment", fetch):
        with pytest.raises(SSConnectionError):
            await manager.fetch(session, session.segments[0])
        assert await manager.fetch(session, session.segments[0]) == (b"i", b"m")
    assert manager.stats()["fetches_in_flight"] == 0


async def test_thumbnail_cache_is_bounded(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """By bytes, counting a fixed cost per entry, so misses (b"") count too."""
    entry_id = setup_integration.entry_id
    per = THUMBNAIL_ENTRY_BYTES
    snap = AsyncMock(side_effect=[b"j" * 400, None, b"j" * 400, None, None, None])
    with patch.object(thumbnails, "THUMBNAIL_CACHE_BYTES", 400 + 3 * per), patch.object(thumbnails, "fetch_snapshot", snap):
        manager = VodManager(hass)
        for i in range(6):
            await manager.thumbnail(entry_id, 6, T0 + i)
    assert manager.stats()["cached_thumbnails"] == 3
    assert manager.stats()["cached_thumbnail_bytes"] == 3 * per


async def test_same_thumbnail_made_once(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    gate = asyncio.Event()

    async def slow(*args, **kwargs):
        await gate.wait()
        return b"j" * 400

    snap = AsyncMock(side_effect=slow)
    with patch.object(thumbnails, "fetch_snapshot", snap):
        both = asyncio.gather(manager.thumbnail(entry_id, 6, T0), manager.thumbnail(entry_id, 6, T0))
        await asyncio.sleep(0.01)
        gate.set()
        assert await both == [b"j" * 400] * 2
    assert snap.await_count == 1
    assert manager.stats()["cached_thumbnail_bytes"] == 400 + THUMBNAIL_ENTRY_BYTES


async def test_abandoned_thumbnail_is_cancelled(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    manager = hass.data[DATA_MANAGER]
    cancelled = asyncio.Event()

    async def slow(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with patch.object(thumbnails, "fetch_snapshot", AsyncMock(side_effect=slow)):
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
    now = time.monotonic()
    with patch.object(thumbnails, "fetch_snapshot", snap), patch.object(thumbnails, "_monotonic") as clock:
        clock.return_value = now
        assert await manager.thumbnail(entry_id, 6, T0) is None
        assert await manager.thumbnail(entry_id, 6, T0) is None
        clock.return_value = now + THUMBNAIL_MISS_SECONDS + 1
        assert await manager.thumbnail(entry_id, 6, T0) == b"j"
    assert snap.await_count == 2


async def test_thumbnail_when_recorded_lists_recordings_once(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The recording that was found written is the one cut: SS lists recordings once, not again for the snapshot."""
    manager = hass.data[DATA_MANAGER]
    mock_client.download = AsyncMock(return_value=b"mp4")
    with patch("synology_ss_playback.segment._run_ffmpeg", AsyncMock(return_value=b"jpg")):
        assert await manager.thumbnail_when_recorded(setup_integration.entry_id, 6, T0 + 100, 5) == b"jpg"
    assert mock_client.recordings.await_count == 1
    assert mock_client.download.await_args.args[:3] == (100, 1, 100_000)  # recording 100, 100 s in


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


async def test_bookmarks_kept_for_a_minute(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client
) -> None:
    """The card asks every 5 s while live: the whole list comes from SS once a minute."""
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    now = [100.0]
    with patch.object(bookmarks, "_monotonic", lambda: now[0]):
        await manager.bookmarks(entry_id, mock_client)
        now[0] += 30
        await manager.bookmarks(entry_id, mock_client)
        assert mock_client.list_bookmarks.await_count == 1
        now[0] += 31
        assert [b.id for b in await manager.bookmarks(entry_id, mock_client)] == [3, 2, 1]
    assert mock_client.list_bookmarks.await_count == 2


async def test_bookmarks_indexed_off_the_loop(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client
) -> None:
    """Tens of thousands of bookmarks take a while to index: never on HA's event loop."""
    threads: list[threading.Thread] = []
    real = bookmarks.BookmarkIndex

    def index(found):
        threads.append(threading.current_thread())
        return real(found)

    with patch.object(bookmarks, "BookmarkIndex", index):
        await hass.data[DATA_MANAGER].bookmarks(setup_integration.entry_id, mock_client)
    assert threads and threads[0] is not threading.main_thread()


async def test_lasting_outage_is_a_repairs_issue(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """Unreachable when asked, and still 10 minutes later: an issue, gone once SS answers (or the entry is unloaded)."""
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    registry = ir.async_get(hass)
    issue_id = f"ss_unreachable_{entry_id}"
    down = SSConnectionError("SYNO.SurveillanceStation.Camera", "List", None)
    now = [1000.0]
    with patch.object(manager_mod, "_monotonic", lambda: now[0]):
        manager.track(entry_id, down)
        now[0] += FRIGATE_ISSUE_AFTER_SECONDS - 1
        manager.track(entry_id, down)
        assert registry.async_get_issue(DOMAIN, issue_id) is None
        now[0] += 1
        manager.track(entry_id, down)
        issue = registry.async_get_issue(DOMAIN, issue_id)
        assert issue is not None and issue.translation_key == "ss_unreachable"
        assert issue.translation_placeholders["name"] == setup_integration.title
        manager.track(entry_id, None)
        assert registry.async_get_issue(DOMAIN, issue_id) is None
        manager.track(entry_id, down)
        now[0] += FRIGATE_ISSUE_AFTER_SECONDS
        manager.track(entry_id, down)
        assert registry.async_get_issue(DOMAIN, issue_id) is not None
    assert await hass.config_entries.async_unload(entry_id)
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_unreachable_logged_once(
    hass: HomeAssistant, setup_integration: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    caplog.set_level(logging.INFO, logger=manager_mod.__name__)
    down = AsyncMock(side_effect=SSConnectionError("SYNO.SurveillanceStation.Recording", "Download", None))
    with patch.object(thumbnails, "fetch_snapshot", down):
        for i in range(3):
            with pytest.raises(SSConnectionError):
                await manager.thumbnail(entry_id, 6, T0 + i)
    with patch.object(thumbnails, "fetch_snapshot", AsyncMock(return_value=b"j")):
        await manager.thumbnail(entry_id, 6, T0 + 10)
        await manager.thumbnail(entry_id, 6, T0 + 11)
    assert caplog.text.count("is unreachable") == 1
    assert caplog.text.count("reachable again") == 1


async def test_unload_answers_waiting_requests(hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client) -> None:
    """Unloading cancels the shared jobs; whoever waited gets an error, not a hang."""
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    mock_client.list_bookmarks.side_effect = hang
    with patch.object(thumbnails, "fetch_snapshot", AsyncMock(side_effect=hang)):
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
    with patch.object(thumbnails, "fetch_snapshot", snap):
        assert await manager.thumbnail(entry_id, 6, T0) == b"j" * 400
        assert await manager.thumbnail(entry_id, 6, T0 + 1) is None
    await manager.disk.settle()
    assert (thumbnail_dir / entry_id / "6" / f"{T0}.jpg").read_bytes() == b"j" * 400
    assert not (thumbnail_dir / entry_id / "6" / f"{T0 + 1}.jpg").exists()  # a miss isn't kept

    again = VodManager(hass)
    await again.async_load()
    assert again.stats()["disk_thumbnails"] == 1
    assert again.sign_thumbnail(entry_id, 6, T0) == url  # the browser's copy stays good
    with patch.object(thumbnails, "fetch_snapshot", AsyncMock(side_effect=AssertionError)):
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
    with patch.object(thumbnails, "fetch_snapshot", AsyncMock(return_value=b"j")):
        await manager.thumbnail(entry_id, 6, T0)
    await manager.disk.settle()  # the write runs in the background
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
        patch.object(thumbnails, "fetch_snapshot", AsyncMock(return_value=b"j" * 400)),
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
    """A stored key that isn't one of ours: a new one, kept (the next start signs the same URLs)."""
    hass_storage["surveillance_station.thumbnail_key"] = {"version": 1, "key": "surveillance_station.thumbnail_key", "data": {"key": ""}}
    manager = VodManager(hass)
    await manager.async_load()
    await hass.async_block_till_done()
    url = manager.sign_thumbnail("E", 6, T0)
    path, query = url.split("?")
    exp, sig = (part.split("=")[1] for part in query.split("&"))
    assert manager.check_thumbnail(path, exp, sig)
    again = VodManager(hass)
    await again.async_load()
    assert again.sign_thumbnail("E", 6, T0) == url


async def test_dropping_an_entry_waits_for_its_writes(hass: HomeAssistant, tmp_path: Path) -> None:
    store = ThumbnailStore(hass, str(tmp_path), 1000)
    store.put(("E", 6, 1), b"j")
    await store.drop_entry("E")  # the write was still going on
    store.put(("E", 6, 2), b"j")  # a job finishing late: refused
    await store.settle()
    assert not (tmp_path / "E").exists()
    assert len(store) == 0


async def test_thumbnail_when_recorded(hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client) -> None:
    """Waits until SS reports the moment written, then makes the frame once."""
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    written = [
        [RecordingInfo(id=1, camera_id=6, start=T0, end=T0 + 5, mount_id=1, live=True, hevc=True)],
        [RecordingInfo(id=1, camera_id=6, start=T0, end=T0 + 11, mount_id=1, live=True, hevc=True)],
    ]
    mock_client.recordings = AsyncMock(side_effect=[SSConnectionError("x", "List", None), *written])
    snap = AsyncMock(return_value=b"jpg")
    with (
        patch.object(thumbnails, "fetch_snapshot", snap),
        patch.object(thumbnails, "THUMBNAIL_POLL_SECONDS", 0),
    ):
        assert await manager.thumbnail_when_recorded(entry_id, 6, T0 + 10, 20) == b"jpg"
    assert mock_client.recordings.await_count == 3
    snap.assert_awaited_once()


async def test_thumbnail_when_recorded_gives_up(hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client) -> None:
    """SS recording, but never getting that far: waited for until the deadline."""
    manager = hass.data[DATA_MANAGER]
    mock_client.recordings = AsyncMock(
        return_value=[RecordingInfo(id=1, camera_id=6, start=T0 - 100, end=T0 - 5, mount_id=1, live=True, hevc=True)]
    )
    clock = iter(range(0, 1000, 3))
    with (
        patch.object(thumbnails, "THUMBNAIL_POLL_SECONDS", 0),
        patch.object(thumbnails, "_monotonic", lambda: next(clock)),
    ):
        assert await manager.thumbnail_when_recorded(setup_integration.entry_id, 6, T0, 20) is None
    assert 3 <= mock_client.recordings.await_count <= 8


@pytest.mark.parametrize(
    "recordings",
    [
        [],  # not recorded at all
        [RecordingInfo(id=1, camera_id=6, start=T0 - 900, end=T0 - 300, mount_id=1, live=False, hevc=True)],  # motion-only, over
    ],
)
async def test_thumbnail_when_not_recording(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client, recordings
) -> None:
    """SS isn't recording the camera then: given up after a few seconds (a motion recording may start a
    moment late), not after the whole wait (a notification isn't held for it)."""
    mock_client.recordings = AsyncMock(return_value=recordings)
    clock = iter(range(0, 1000, 2))
    with patch.object(thumbnails, "THUMBNAIL_POLL_SECONDS", 0), patch.object(thumbnails, "_monotonic", lambda: next(clock)):
        assert await hass.data[DATA_MANAGER].thumbnail_when_recorded(setup_integration.entry_id, 6, T0, 20) is None
    assert 3 <= mock_client.recordings.await_count <= 5  # 8 s of 2 s polls, not 20


async def test_recent_miss_is_asked_again_soon(hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client) -> None:
    """No frame for a moment just past (a notification fetched before SS listed it): asked again in seconds."""
    manager = hass.data[DATA_MANAGER]
    snap = AsyncMock(side_effect=[None, b"jpg", None, b"jpg"])
    now = [100.0]
    with patch.object(thumbnails, "fetch_snapshot", snap), patch.object(thumbnails, "_monotonic", lambda: now[0]), patch.object(
        time, "time", return_value=T0 + 10
    ):
        assert await manager.thumbnail(setup_integration.entry_id, 6, T0) is None
        now[0] += 6
        assert await manager.thumbnail(setup_integration.entry_id, 6, T0) == b"jpg"
    with patch.object(thumbnails, "fetch_snapshot", snap), patch.object(thumbnails, "_monotonic", lambda: now[0]):
        assert await manager.thumbnail(setup_integration.entry_id, 6, T0 - 3600) is None  # long past
        now[0] += 6
        assert await manager.thumbnail(setup_integration.entry_id, 6, T0 - 3600) is None  # still the miss
    assert snap.await_count == 3


async def test_large_image(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_client_no_auth, thumbnail_dir: Path
) -> None:
    """A notification's image: the same moment, 1280 wide, its own URL (signed apart) and cache."""
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    small, large = manager.sign_thumbnail(entry_id, 6, T0), manager.sign_thumbnail(entry_id, 6, T0, large=True)
    assert large.split("?")[0].endswith(f"/6/{T0}-large.jpg")
    widths = []

    async def snap(client, camera_id, ts, ffmpeg, width, recording=None):
        widths.append(width)
        return b"L" if width == 1280 else b"s"

    http = await hass_client_no_auth()
    with patch.object(thumbnails, "fetch_snapshot", snap):
        assert await (await http.get(large)).read() == b"L"
        assert await (await http.get(small)).read() == b"s"
        # The small URL's signature doesn't open the large image.
        forged = large.split("?")[0] + "?" + small.split("?")[1]
        assert (await http.get(forged)).status == 404
    assert widths == [1280, 320]
    await manager.disk_large.settle()
    assert manager.stats()["disk_images"] == 1 and manager.stats()["disk_thumbnails"] == 1


async def test_client_is_none_for_an_unloaded_entry(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    manager = hass.data[DATA_MANAGER]
    assert manager.client("not-a-real-entry-id") is None
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    assert manager.client(setup_integration.entry_id) is None


async def test_thumbnail_fails_when_the_entry_is_not_loaded(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    manager = hass.data[DATA_MANAGER]
    with pytest.raises(SSError, match="not loaded"):
        await manager.thumbnail("not-a-real-entry-id", 6, T0)


async def test_fetch_fails_when_the_entry_is_not_loaded(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    manager = hass.data[DATA_MANAGER]
    session = _session(setup_integration)
    session.entry_id = "not-a-real-entry-id"
    with pytest.raises(SSError, match="not loaded"):
        await manager.fetch(session, session.segments[0])


async def test_drop_entry_forgets_its_live_tokens(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    token = manager.create_live_token(entry_id, 6)
    manager.drop_entry(entry_id)
    assert manager.take_live_token(token) is None
    assert manager.stats()["live_tokens"] == 0


async def test_expired_live_tokens_are_swept_on_create(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    with patch.object(time, "time", return_value=1_000_000.0):
        old = manager.create_live_token(entry_id, 6)
    with patch.object(time, "time", return_value=1_000_000.0 + LIVE_TOKEN_TTL_SECONDS + 1):
        new = manager.create_live_token(entry_id, 7)
        assert manager.stats()["live_tokens"] == 1
        assert manager.take_live_token(old) is None
        assert manager.take_live_token(new) == (entry_id, 7, None)


async def test_live_tokens_are_bounded(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """Unused tokens asked for faster than they expire: the oldest go."""
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    with patch.object(tokens, "LIVE_TOKENS_MAX", 3):
        made = [manager.create_live_token(entry_id, i) for i in range(5)]
    assert manager.stats()["live_tokens"] == 3
    assert [manager.take_live_token(t) for t in made[:2]] == [None, None]
    assert manager.take_live_token(made[-1]) == (entry_id, 4, None)


async def test_expired_sessions_are_swept_on_create(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    manager = hass.data[DATA_MANAGER]
    old = _session(setup_integration)
    old.expires = T0  # long expired
    old_token = manager.create_session(old)
    with patch.object(time, "time", return_value=T0 + 10):
        manager.create_session(_session(setup_integration))
    assert manager.get_session(old_token) is None
    assert manager.stats()["sessions"] == 1


async def test_sessions_are_bounded_by_count(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    manager = hass.data[DATA_MANAGER]
    with patch.object(manager_mod, "VOD_MAX_SESSIONS", 2), patch.object(time, "time", return_value=T0):
        first = manager.create_session(_session(setup_integration))
        manager.create_session(_session(setup_integration))
        manager.create_session(_session(setup_integration))  # evicts the first (LRU)
        assert manager.stats()["sessions"] == 2
        assert manager.get_session(first) is None


def _live_session(entry: MockConfigEntry, planned_end: float, max_end: float) -> VodSession:
    return VodSession(
        entry.entry_id, 6, T0, [_seg(i) for i in range(2)], expires=T0 + 3600,
        live=True, max_end=max_end, planned_end=planned_end,
    )


async def test_extend_appends_segments_recorded_since_the_last_plan(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    session = _live_session(setup_integration, planned_end=T0 + 20, max_end=T0 + 3600)
    mock_client.recordings.return_value = [
        RecordingInfo(id=100, camera_id=6, start=T0, end=T0 + 40, mount_id=1, live=True, hevc=True)
    ]
    manager = hass.data[DATA_MANAGER]
    with patch.object(time, "time", return_value=T0 + 35):
        await manager.extend(session)
    assert session.planned_end > T0 + 20
    assert len(session.segments) > 2
    assert session.live  # short of max_end: still growing


async def test_extend_stops_growing_at_max_end(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    session = _live_session(setup_integration, planned_end=T0 + 20, max_end=T0 + 30)
    mock_client.recordings.return_value = [
        RecordingInfo(id=100, camera_id=6, start=T0, end=T0 + 40, mount_id=1, live=True, hevc=True)
    ]
    manager = hass.data[DATA_MANAGER]
    with patch.object(time, "time", return_value=T0 + 3600):
        await manager.extend(session)
    assert session.planned_end == T0 + 30
    assert session.live is False
    # Still the EVENT playlist players have been re-fetching, now ended.
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" in session.playlist
    assert session.playlist.endswith("#EXT-X-ENDLIST\n")


async def test_extend_does_nothing_before_the_next_grid_line(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    session = _live_session(setup_integration, planned_end=T0 + 20, max_end=T0 + 3600)
    manager = hass.data[DATA_MANAGER]
    with patch.object(time, "time", return_value=T0 + 21):  # live_edge still <= planned_end
        await manager.extend(session)
    assert session.planned_end == T0 + 20
    mock_client.recordings.assert_not_awaited()


async def test_extend_does_nothing_if_the_entry_is_not_loaded(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    session = _live_session(setup_integration, planned_end=T0 + 20, max_end=T0 + 3600)
    session.entry_id = "not-a-real-entry-id"
    manager = hass.data[DATA_MANAGER]
    with patch.object(time, "time", return_value=T0 + 35):
        await manager.extend(session)  # no exception, nothing changes
    assert session.planned_end == T0 + 20


async def test_extend_leaves_the_playlist_unchanged_when_ss_is_unreachable(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=manager_mod.__name__)
    session = _live_session(setup_integration, planned_end=T0 + 20, max_end=T0 + 3600)
    mock_client.recordings.side_effect = SSConnectionError("SYNO.SurveillanceStation.Event", "List", None)
    manager = hass.data[DATA_MANAGER]
    with patch.object(time, "time", return_value=T0 + 35):
        await manager.extend(session)
    assert session.planned_end == T0 + 20
    assert "not extended" in caplog.text


def _bookmark(id: int, start: int = 0) -> Bookmark:
    return Bookmark(id=id, camera_id=6, name="", comment="", start=start, end=start)


async def test_set_frame_is_a_noop_when_the_moment_is_unchanged(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    with patch.object(manager_mod, "BOOKMARK_FRAMES_MAX", 2):
        manager.set_frame(entry_id, 1, 100)
        manager.set_frame(entry_id, 2, 200)
        manager.set_frame(entry_id, 1, 100)  # same ts as already stored: not re-recorded
        assert manager.frame(entry_id, _bookmark(1)) == 100
        manager.set_frame(entry_id, 3, 300)  # evicts the oldest: still bookmark 1 (not moved back to the end)
    assert manager.frame(entry_id, _bookmark(1, start=5)) == 6
    assert manager.frame(entry_id, _bookmark(2)) == 200


async def test_set_frame_evicts_the_oldest_past_the_cap(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    with patch.object(manager_mod, "BOOKMARK_FRAMES_MAX", 2):
        manager.set_frame(entry_id, 1, 10)
        manager.set_frame(entry_id, 2, 20)
        manager.set_frame(entry_id, 3, 30)  # evicts bookmark 1 (the oldest)
    assert manager.frame(entry_id, _bookmark(1, start=5)) == 6  # fallen back to start + 1
    assert manager.frame(entry_id, _bookmark(3)) == 30


async def test_removing_the_entry_forgets_its_frames(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """A removed entry's bookmark frames go, also from what the next start reads; another entry's stay."""
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    manager.set_frame(entry_id, 1, T0 + 7)
    manager.set_frame("other-entry", 1, T0 + 9)
    await hass.config_entries.async_remove(entry_id)
    await hass.async_block_till_done()
    assert manager.frame(entry_id, _bookmark(1, start=T0)) == T0 + 1
    again = VodManager(hass)
    await again.async_load()
    assert again.frame(entry_id, _bookmark(1, start=T0)) == T0 + 1
    assert again.frame("other-entry", _bookmark(1, start=T0)) == T0 + 9


async def test_image_url_is_the_same_all_day(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """One URL per frame from 00:00 to 23:59:59 UTC (the browser's cached copy stays good), a new one the next day."""
    manager = hass.data[DATA_MANAGER]
    entry_id = setup_integration.entry_id
    midnight = datetime(2026, 9, 22, tzinfo=UTC).timestamp()
    urls = []
    for t in (midnight, midnight + 86399.9, midnight + 86400):
        with patch.object(time, "time", return_value=t):
            urls.append(manager.sign_thumbnail(entry_id, 6, T0))
    assert urls[0] == urls[1] != urls[2]
    for url in urls:
        path, query = url.split("?")
        exp, sig = (part.split("=")[1] for part in query.split("&"))
        with patch.object(time, "time", return_value=midnight + 86400):
            assert manager.check_thumbnail(path, exp, sig)


async def test_overlong_expiry_is_a_404(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_client_no_auth
) -> None:
    """An exp longer than int() reads (4300 digits) is refused like any bad signature, not a 500."""
    manager = hass.data[DATA_MANAGER]
    path = manager.sign_thumbnail(setup_integration.entry_id, 6, T0).split("?")[0]
    http = await hass_client_no_auth()
    assert (await http.get(f"{path}?exp={'9' * 5000}&sig=00")).status == 404
