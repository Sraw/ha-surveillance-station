"""ThumbnailStore's disk scan and write/unlink error handling.

test_manager.py covers the store's normal behaviour (eviction, restarts,
cancellation); this file is the messy-filesystem edge cases: unreadable
directories, stray non-thumbnail entries, and OSError during write/unlink.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_components.surveillance_station import thumbnail_store as ts_mod
from custom_components.surveillance_station.thumbnail_store import ThumbnailStore
from homeassistant.core import HomeAssistant


def _os_like(**overrides: MagicMock) -> MagicMock:
    """A stand-in for the ``os`` module, scoped to thumbnail_store's own
    reference to it (patched in with patch.object(ts_mod, "os", ...) below):
    real (wraps the actual module) except for the given overrides, so it
    can't affect anything else in the process still using the real module.
    Only for what a real file system can't be made to do here: the tests run
    as root, whom utime and unlink never refuse."""
    return MagicMock(wraps=os, **overrides)


async def test_scan_skips_an_unreadable_root(
    hass: HomeAssistant, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A root that exists but can't be listed (not just missing) is a warning, not a crash."""
    (tmp_path / "root").write_bytes(b"a file, not a directory")
    store = ThumbnailStore(hass, str(tmp_path / "root"), 1000)
    await store.load()
    assert len(store) == 0
    assert "Can't read the thumbnail cache" in caplog.text


async def test_scan_ignores_entries_that_are_not_thumbnails(hass: HomeAssistant, tmp_path: Path) -> None:
    """An unsafe entry_id, an entry_id that's a file, a non-numeric camera
    folder, and a file where a camera folder should be: all skipped rather
    than erroring."""
    (tmp_path / "bad entry!").mkdir()  # fails _SAFE_ID
    (tmp_path / "bad entry!" / "6").mkdir()
    (tmp_path / "bad entry!" / "6" / "1.jpg").write_bytes(b"x")
    (tmp_path / "FILEENTRY").write_bytes(b"not a directory")  # a safe id, but not a folder

    good = tmp_path / "GOODENTRY"
    good.mkdir()
    (good / "not-a-number").mkdir()  # camera folder must be all digits
    (good / "not-a-number" / "1.jpg").write_bytes(b"x")
    (good / "6").write_bytes(b"not a directory")  # os.listdir on this raises OSError
    (good / "7").mkdir()
    (good / "7" / "2.jpg").write_bytes(b"y")  # the one real thumbnail

    store = ThumbnailStore(hass, str(tmp_path), 1000)
    await store.load()
    assert len(store) == 1
    assert store.bytes == 1


async def test_scan_skips_a_file_it_cannot_stat(hass: HomeAssistant, tmp_path: Path) -> None:
    """A file that can't be stat()ed (it vanished after scandir(); here a dangling link) is skipped."""
    entry = tmp_path / "E"
    (entry / "6").mkdir(parents=True)
    (entry / "6" / "1.jpg").symlink_to(tmp_path / "gone.jpg")
    (entry / "6" / "2.jpg").write_bytes(b"x")
    store = ThumbnailStore(hass, str(tmp_path), 1000)
    await store.load()
    assert len(store) == 1


async def test_read_ignores_a_failure_to_update_the_access_time(hass: HomeAssistant, tmp_path: Path) -> None:
    """os.utime is only for eviction order; its failure doesn't lose the read."""
    store = ThumbnailStore(hass, str(tmp_path), 1000)
    store.put(("E", 6, 1), b"data")
    await store.settle()
    with patch.object(ts_mod, "os", _os_like(utime=MagicMock(side_effect=OSError("read-only fs")))):
        assert await store.get(("E", 6, 1)) == b"data"


async def test_write_failure_after_the_temp_file_is_cleaned_up(
    hass: HomeAssistant, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """os.replace failing (e.g. the destination is a directory) still cleans up the temp file."""
    dest_dir = tmp_path / "E" / "6"
    dest_dir.mkdir(parents=True)
    (dest_dir / "1.jpg").mkdir()  # a directory sits where the JPEG should go

    store = ThumbnailStore(hass, str(tmp_path), 1000)
    store.put(("E", 6, 1), b"data")
    await store.settle()
    assert len(store) == 0  # forgotten: the write failed
    assert "Can't keep thumbnails" in caplog.text
    assert not list(dest_dir.glob("*.tmp"))  # the scratch file didn't leak


async def test_write_failure_when_the_temp_file_cleanup_also_fails(
    hass: HomeAssistant, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Both the write and its cleanup fail: still just a warning, not a crash."""
    dest_dir = tmp_path / "E" / "6"
    dest_dir.mkdir(parents=True)
    (dest_dir / "1.jpg").mkdir()

    store = ThumbnailStore(hass, str(tmp_path), 1000)
    with patch.object(ts_mod, "os", _os_like(unlink=MagicMock(side_effect=OSError("also gone")))):
        store.put(("E", 6, 1), b"data")
        await store.settle()
    assert len(store) == 0
    assert "Can't keep thumbnails" in caplog.text


async def test_kept_for_this_user_only(hass: HomeAssistant, tmp_path: Path) -> None:
    """Camera frames: not readable by other users of the host."""
    store = ThumbnailStore(hass, str(tmp_path), 1000)
    store.put(("E", 6, 1), b"data")
    await store.settle()
    assert (tmp_path / "E" / "6" / "1.jpg").stat().st_mode & 0o077 == 0


def test_unlink_swallows_a_missing_or_locked_file(tmp_path: Path) -> None:
    """Eviction racing a manual delete (or a permission error) is not fatal."""
    ts_mod._unlink([str(tmp_path / "already-gone.jpg")])  # no exception
