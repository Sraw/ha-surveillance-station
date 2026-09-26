"""fetch_segment / fetch_snapshot: the download + ffmpeg remux pipeline.

ffmpeg itself is mocked (asyncio.create_subprocess_exec): these tests cover
the glue (temp file handling, error mapping, cache of a "no recording" miss),
not ffmpeg's own behaviour.
"""

import asyncio
import glob
import os
from pathlib import Path
import struct
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from synology_ss_playback import RecordingInfo, SSError, Segment
from synology_ss_playback import segment as seg_mod


def _box(typ: str, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ.encode()) + payload


FMP4 = _box("ftyp", b"iso5") + _box("moov", b"x" * 8) + _box("moof", b"m") + _box("mdat", b"d" * 4)


def _seg(**kw) -> Segment:
    base = dict(
        index=0, recording_id=1, mount_id=1, wall_start=100.0, duration=10.0,
        offset_ms=0, media_start=0.0, discontinuity=False, new_map=True, hevc=True,
    )
    base.update(kw)
    return Segment(**base)


def _proc(returncode: int, out: bytes = b"", err: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(out, err))
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


async def test_fetch_segment_success() -> None:
    client = MagicMock()
    client.download = AsyncMock(return_value=b"raw-mp4")
    with patch.object(seg_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=_proc(0, FMP4))):
        init, media = await seg_mod.fetch_segment(client, _seg(), "ffmpeg")
    assert init == _box("ftyp", b"iso5") + _box("moov", b"x" * 8)
    assert media == _box("moof", b"m") + _box("mdat", b"d" * 4)
    client.download.assert_awaited_once_with(1, 1, 0, 10 * 1000 + 1000)


async def test_fetch_segment_without_audio_mp4_cannot_carry(monkeypatch: pytest.MonkeyPatch) -> None:
    """ffmpeg refuses the camera's audio (G.711 in MP4): the segment again without it, not a failure."""
    client = MagicMock()
    client.download = AsyncMock(return_value=b"raw-mp4")
    monkeypatch.setattr(seg_mod, "_NO_AUDIO", seg_mod.OrderedDict())
    run = AsyncMock(side_effect=[_proc(1, b"", b"Could not find tag for codec pcm_mulaw"), _proc(0, FMP4)])
    with patch.object(seg_mod.asyncio, "create_subprocess_exec", run):
        init, media = await seg_mod.fetch_segment(client, _seg(), "ffmpeg")
    assert media == _box("moof", b"m") + _box("mdat", b"d" * 4)
    first, second = (c.args for c in run.await_args_list)
    assert "0:a:0?" in first and "0:a:0?" not in second


async def test_audio_refused_once_per_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recording's later segments go without audio straight away (one ffmpeg run, one consistent init)."""
    monkeypatch.setattr(seg_mod, "_NO_AUDIO", seg_mod.OrderedDict())
    client = MagicMock()
    client.download = AsyncMock(return_value=b"raw-mp4")
    run = AsyncMock(side_effect=[_proc(1, b"", b"Could not find tag for codec pcm_alaw"), _proc(0, FMP4), _proc(0, FMP4)])
    with patch.object(seg_mod.asyncio, "create_subprocess_exec", run):
        await seg_mod.fetch_segment(client, _seg(), "ffmpeg")
        await seg_mod.fetch_segment(client, _seg(), "ffmpeg")
    assert run.await_count == 3 and "0:a:0?" not in run.await_args.args


async def test_audio_refused_is_remembered_per_nas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recording ids are per NAS: one NAS's G.711 recording says nothing about another's with the same id."""
    monkeypatch.setattr(seg_mod, "_NO_AUDIO", seg_mod.OrderedDict())
    first, second = MagicMock(nas="a:5000"), MagicMock(nas="b:5000")
    for c in (first, second):
        c.download = AsyncMock(return_value=b"raw-mp4")
    run = AsyncMock(side_effect=[_proc(1, b"", b"Could not find tag for codec pcm_alaw"), _proc(0, FMP4), _proc(0, FMP4)])
    with patch.object(seg_mod.asyncio, "create_subprocess_exec", run):
        await seg_mod.fetch_segment(first, _seg(), "ffmpeg")
        await seg_mod.fetch_segment(second, _seg(), "ffmpeg")
    assert "0:a:0?" in run.await_args.args


@pytest.mark.parametrize(
    "stderr", [b"Invalid data found when processing input", b"Could not find codec parameters for stream 0"]
)
async def test_other_ffmpeg_failure_is_not_retried_without_audio(stderr: bytes) -> None:
    """Damaged input, even worded with "codec": an error, not a recording marked audio-less."""
    client = MagicMock()
    client.download = AsyncMock(return_value=b"raw-mp4")
    run = AsyncMock(return_value=_proc(1, b"", stderr))
    with patch.object(seg_mod.asyncio, "create_subprocess_exec", run):
        with pytest.raises(SSError, match=stderr.decode()[:12]):
            await seg_mod.fetch_segment(client, _seg(), "ffmpeg")
    assert run.await_count == 1


async def test_fetch_segment_timeout_is_not_retried() -> None:
    client = MagicMock()
    client.download = AsyncMock(return_value=b"raw-mp4")
    with patch.object(seg_mod, "_run_ffmpeg", AsyncMock(side_effect=SSError("ffmpeg", "run", None, "timed out"))) as run:
        with pytest.raises(SSError, match="timed out"):
            await seg_mod.fetch_segment(client, _seg(), "ffmpeg")
    assert run.await_count == 1


async def test_fetch_segment_empty_remux_output_is_an_error() -> None:
    client = MagicMock()
    client.download = AsyncMock(return_value=b"raw-mp4")
    # No ftyp/moov in the output: split_fmp4 returns an empty init.
    junk = _box("moof", b"m") + _box("mdat", b"d")
    with patch.object(seg_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=_proc(0, junk))):
        with pytest.raises(SSError, match="empty output"):
            await seg_mod.fetch_segment(client, _seg(), "ffmpeg")


async def test_run_ffmpeg_nonzero_exit() -> None:
    with patch.object(seg_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=_proc(1, err=b"bad input"))):
        with pytest.raises(SSError, match="bad input"):
            await seg_mod._run_ffmpeg("ffmpeg", b"raw", lambda src: ["ffmpeg", src], timeout=5)


async def test_run_ffmpeg_timeout_kills_the_process() -> None:
    proc = MagicMock()
    proc.returncode = None
    proc.kill = MagicMock()
    proc.wait = AsyncMock()

    async def hang(*a, **kw):
        await asyncio.sleep(10)

    proc.communicate = hang
    with patch.object(seg_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
        with pytest.raises(SSError, match="timed out"):
            await seg_mod._run_ffmpeg("ffmpeg", b"raw", lambda src: ["ffmpeg", src], timeout=0.05)
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


async def test_fetch_snapshot_found() -> None:
    client = MagicMock()
    now = time.time()
    client.recordings = AsyncMock(
        return_value=[RecordingInfo(id=1, camera_id=6, start=now - 100, end=now - 10, mount_id=1, live=False, hevc=True)]
    )
    client.download = AsyncMock(return_value=b"raw")
    jpg = b"\xff\xd8jpeg"
    with patch.object(seg_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=_proc(0, jpg))):
        result = await seg_mod.fetch_snapshot(client, 6, now - 50, "ffmpeg")
    assert result == jpg


async def test_fetch_snapshot_uses_now_as_the_end_of_a_live_recording() -> None:
    client = MagicMock()
    now = time.time()
    client.recordings = AsyncMock(
        return_value=[RecordingInfo(id=1, camera_id=6, start=now - 100, end=now - 100, mount_id=1, live=True, hevc=True)]
    )
    client.download = AsyncMock(return_value=b"raw")
    with patch.object(seg_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=_proc(0, b"\xff\xd8"))):
        result = await seg_mod.fetch_snapshot(client, 6, now - 50, "ffmpeg")
    assert result == b"\xff\xd8"
    client.download.assert_awaited_once()


async def test_fetch_snapshot_nothing_recorded() -> None:
    client = MagicMock()
    client.recordings = AsyncMock(return_value=[])
    client.download = AsyncMock()
    result = await seg_mod.fetch_snapshot(client, 6, time.time(), "ffmpeg")
    assert result is None
    client.download.assert_not_called()


async def test_fetch_snapshot_empty_frame_is_none() -> None:
    """The cut held no decodable frame (the last second of a file)."""
    client = MagicMock()
    now = time.time()
    client.recordings = AsyncMock(
        return_value=[RecordingInfo(id=1, camera_id=6, start=now - 100, end=now - 10, mount_id=1, live=False, hevc=True)]
    )
    client.download = AsyncMock(return_value=b"raw")
    with patch.object(seg_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=_proc(0, b""))):
        assert await seg_mod.fetch_snapshot(client, 6, now - 50, "ffmpeg") is None


def test_remove_stale_temp_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # remove_stale_temp_files() sweeps tempfile.gettempdir() itself; point it
    # at an empty scratch dir so this can't touch a real leftover scratch
    # file (the crash-recovery case this feature exists for) elsewhere on disk.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    paths = []
    for _ in range(2):
        fd, path = tempfile.mkstemp(prefix=seg_mod.TEMP_PREFIX, suffix=".mp4")
        os.close(fd)
        paths.append(path)
    assert seg_mod.remove_stale_temp_files() >= 2
    assert not any(os.path.exists(p) for p in paths)


def test_remove_stale_temp_files_ignores_a_file_already_gone() -> None:
    fd, path = tempfile.mkstemp(prefix=seg_mod.TEMP_PREFIX, suffix=".mp4")
    os.close(fd)
    with patch.object(seg_mod.os, "unlink", side_effect=OSError("gone")):
        assert seg_mod.remove_stale_temp_files() == 0
    os.unlink(path)


def test_unlink_swallows_missing_file() -> None:
    seg_mod._unlink("/nonexistent/path/for/sure.mp4")  # no FileNotFoundError raised


async def test_run_ffmpeg_missing_binary_is_an_sserror() -> None:
    with patch.object(seg_mod.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError(2, "No such file"))):
        with pytest.raises(SSError, match="FileNotFoundError"):
            await seg_mod._run_ffmpeg("ffmpeg", b"raw", lambda src: ["ffmpeg", src], timeout=5)


async def test_run_ffmpeg_no_scratch_space_is_an_sserror() -> None:
    with patch.object(seg_mod.tempfile, "mkstemp", side_effect=OSError(28, "No space left on device")):
        with pytest.raises(SSError, match="scratch file"):
            await seg_mod._run_ffmpeg("ffmpeg", b"raw", lambda src: ["ffmpeg", src], timeout=5)


async def test_run_ffmpeg_scratch_write_error_is_an_sserror() -> None:
    with patch.object(seg_mod, "_write_and_close", side_effect=OSError(28, "No space left on device")):
        with pytest.raises(SSError, match="OSError"):
            await seg_mod._run_ffmpeg("ffmpeg", b"raw", lambda src: ["ffmpeg"], 5)
