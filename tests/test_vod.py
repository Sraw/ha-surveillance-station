"""Tests for the pure VOD planning logic (run: python3 -m unittest discover tests)."""

import pathlib
import struct
import sys
import unittest

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "surveillance_station")
)

import vod  # noqa: E402
from vod import Recording  # noqa: E402

NOW = 1_000_000.0


def box(typ: str, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ.encode()) + payload


class PlanSegments(unittest.TestCase):
    def test_grid_aligned_segments_inside_one_recording(self):
        rec = Recording(id=1, start=100.0, end=200.0)
        segs = vod.plan_segments([rec], 123.0, 157.0, NOW)
        self.assertEqual([s.wall_start for s in segs], [123.0, 130.0, 140.0, 150.0])
        self.assertEqual([s.duration for s in segs], [7.0, 10.0, 10.0, 7.0])
        self.assertEqual([s.offset_ms for s in segs], [23000, 30000, 40000, 50000])
        self.assertEqual([s.media_start for s in segs], [0.0, 7.0, 17.0, 27.0])
        self.assertTrue(segs[0].new_map)
        self.assertFalse(any(s.new_map for s in segs[1:]))
        self.assertFalse(any(s.discontinuity for s in segs))

    def test_split_at_recording_boundary_without_discontinuity(self):
        a = Recording(id=1, start=0.0, end=95.0)
        b = Recording(id=2, start=95.0, end=300.0)
        segs = vod.plan_segments([b, a], 80.0, 120.0, NOW)
        self.assertEqual([(s.recording_id, s.wall_start, s.duration) for s in segs],
                         [(1, 80.0, 10.0), (1, 90.0, 5.0), (2, 95.0, 5.0), (2, 100.0, 10.0), (2, 110.0, 10.0)])
        self.assertEqual(segs[2].offset_ms, 0)
        self.assertTrue(segs[2].new_map)
        self.assertFalse(segs[2].discontinuity)

    def test_gap_between_recordings_is_a_discontinuity(self):
        a = Recording(id=1, start=0.0, end=50.0)
        b = Recording(id=2, start=83.0, end=200.0)
        segs = vod.plan_segments([a, b], 30.0, 100.0, NOW)
        first_b = next(s for s in segs if s.recording_id == 2)
        self.assertTrue(first_b.discontinuity)
        self.assertEqual(first_b.wall_start, 83.0)
        runs = vod.runs_from_segments(segs)
        self.assertEqual(runs, [vod.Run(30.0, 0.0, 20.0), vod.Run(83.0, 20.0, 17.0)])

    def test_live_recording_stops_behind_now(self):
        rec = Recording(id=9, start=NOW - 100, end=NOW, live=True)
        segs = vod.plan_segments([rec], NOW - 100, NOW + 50, NOW)
        last = segs[-1]
        self.assertAlmostEqual(last.wall_start + last.duration, NOW - vod.LIVE_MARGIN_SECONDS)

    def test_slivers_are_folded_or_dropped(self):
        rec = Recording(id=1, start=0.0, end=100.4)
        segs = vod.plan_segments([rec], 9.6, 100.4, NOW)
        # 9.6..10 is a 0.4 s sliver: folded into the next grid cell.
        self.assertEqual(segs[0].wall_start, 9.6)
        self.assertAlmostEqual(segs[0].duration, 10.4)
        # 100..100.4 at the end is dropped.
        self.assertAlmostEqual(segs[-1].wall_start + segs[-1].duration, 100.0)

    def test_nothing_outside_recordings(self):
        rec = Recording(id=1, start=0.0, end=10.0)
        self.assertEqual(vod.plan_segments([rec], 20.0, 30.0, NOW), [])


class Playlist(unittest.TestCase):
    def test_render(self):
        a = Recording(id=1, start=0.0, end=20.0)
        b = Recording(id=2, start=40.0, end=60.0)
        text = vod.render_playlist(vod.plan_segments([a, b], 0.0, 60.0, NOW))
        self.assertTrue(text.startswith("#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:10\n"))
        self.assertIn('#EXT-X-MAP:URI="init/0.mp4"', text)
        self.assertIn("#EXT-X-DISCONTINUITY\n#EXT-X-MAP:URI=\"init/2.mp4\"\n#EXT-X-PROGRAM-DATE-TIME:1970-01-01T00:00:40.000Z", text)
        self.assertEqual(text.count("#EXTINF:10.000,"), 4)
        self.assertTrue(text.rstrip().endswith("#EXT-X-ENDLIST"))


class Boxes(unittest.TestCase):
    def test_split_fmp4(self):
        data = box("ftyp", b"iso5") + box("moov", b"x" * 20) + box("moof", b"m") + box("mdat", b"d" * 5) + box("mfra")
        init, media = vod.split_fmp4(data)
        self.assertEqual(init, box("ftyp", b"iso5") + box("moov", b"x" * 20))
        self.assertEqual(media, box("moof", b"m") + box("mdat", b"d" * 5))

    def test_truncated_box_stops_cleanly(self):
        data = box("ftyp") + struct.pack(">I4s", 100, b"moov") + b"short"
        self.assertEqual(list(t for t, _ in vod.iter_boxes(data)), ["ftyp"])

    def test_ffmpeg_args_tag_only_hevc(self):
        self.assertIn("hvc1", vod.ffmpeg_remux_args("ffmpeg", "in.mp4", 10, 0, hevc=True))
        self.assertNotIn("hvc1", vod.ffmpeg_remux_args("ffmpeg", "in.mp4", 10, 0, hevc=False))

    def test_ffmpeg_args_keep_offset_in_tfdt(self):
        # Verified against ffmpeg 8.1: only delay_moov+frag_discont writes the
        # -output_ts_offset into tfdt; empty_moov silently rebases to 0.
        args = vod.ffmpeg_remux_args("ffmpeg", "in.mp4", 10, 20, hevc=True)
        flags = args[args.index("-movflags") + 1]
        self.assertIn("delay_moov", flags)
        self.assertIn("frag_discont", flags)
        self.assertNotIn("empty_moov", flags)
        self.assertEqual(args[args.index("-output_ts_offset") + 1], "20.000")


if __name__ == "__main__":
    unittest.main()
