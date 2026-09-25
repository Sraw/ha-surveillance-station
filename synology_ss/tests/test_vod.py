"""Tests for the pure VOD planning logic (run: python3 -m unittest discover -s synology_ss/tests)."""

import pathlib
import struct
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from synology_ss_playback import vod  # noqa: E402
from synology_ss_playback.vod import Recording  # noqa: E402

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


    def test_segment_seconds_below_the_minimum_stops_rather_than_looping(self):
        """A degenerate grid (segment_seconds < MIN_SEGMENT_SECONDS): the
        planner stops instead of emitting a zero/negative-length segment."""
        rec = Recording(id=1, start=0.0, end=100.0)
        segs = vod.plan_segments([rec], 0.0, 100.0, NOW, segment_seconds=0.9)
        self.assertLess(sum(s.duration for s in segs), 100.0)
        self.assertTrue(all(s.duration >= vod.MIN_SEGMENT_SECONDS - 1e-9 for s in segs))

    def test_overlapping_recordings_play_each_moment_once(self):
        a = Recording(id=1, start=0.0, end=60.0)
        b = Recording(id=2, start=40.0, end=100.0)
        segs = vod.plan_segments([a, b], 0.0, 100.0, NOW)
        walls = [(s.wall_start, s.wall_start + s.duration) for s in segs]
        for (s1, e1), (s2, _) in zip(walls, walls[1:]):
            self.assertGreaterEqual(s2, e1)
        self.assertEqual(sum(s.duration for s in segs), 100.0)
        self.assertEqual(segs[-1].recording_id, 2)


class LivePlanning(unittest.TestCase):
    def test_live_edge_is_on_the_grid_behind_the_margin(self):
        self.assertEqual(vod.live_edge(1000.0), 990.0)
        self.assertEqual(vod.live_edge(1005.0), 1000.0)
        self.assertEqual(vod.live_edge(1004.9), 990.0)

    def extend(self, old, recordings, now):
        """What VodManager.extend does: append after the last published segment."""
        last = old[-1]
        return old + vod.plan_segments(recordings, last.wall_start, vod.live_edge(now), now, after=last)

    def assert_well_formed(self, segs):
        self.assertEqual([s.index for s in segs], list(range(len(segs))))
        for a, b in zip(segs, segs[1:]):
            self.assertAlmostEqual(b.media_start, a.media_start + a.duration)
            self.assertGreaterEqual(b.wall_start, a.wall_start + a.duration - 1e-9)
            self.assertEqual(b.discontinuity, b.wall_start - (a.wall_start + a.duration) > vod.GAP_TOLERANCE_SECONDS)
            self.assertEqual(b.new_map, b.discontinuity or b.recording_id != a.recording_id)

    def test_extension_continues_across_a_rollover(self):
        start, now1, now2 = 903.0, 1000.0, 1100.0
        old = vod.plan_segments([Recording(1, 0.0, now1, live=True)], start, vod.live_edge(now1), now1)
        late = [Recording(1, 0.0, 1003.7), Recording(2, 1003.7, now2, live=True)]
        new = self.extend(old, late, now2)
        self.assertEqual(new[: len(old)], old)
        self.assertEqual(new[-1].wall_start + new[-1].duration, vod.live_edge(now2))
        self.assert_well_formed(new)
        self.assertEqual(vod.plan_segments(late, start, vod.live_edge(now2), now2), new)

    def test_late_reported_rollover_never_rewrites_or_replays(self):
        # SS still called file 1 live at 1000, but it had really closed at 985.
        start, now1, now2 = 903.0, 1000.0, 1100.0
        old = vod.plan_segments([Recording(1, 0.0, now1, live=True)], start, vod.live_edge(now1), now1)
        published_end = old[-1].wall_start + old[-1].duration  # 990
        truth = [Recording(1, 0.0, 985.0), Recording(2, 985.0, now2, live=True)]
        new = self.extend(old, truth, now2)
        self.assertEqual(new[: len(old)], old)
        self.assertEqual(new[len(old)].wall_start, published_end)  # file 2 picks up at 990, no replay
        self.assertEqual(new[len(old)].recording_id, 2)
        self.assertTrue(new[len(old)].new_map)
        self.assert_well_formed(new)

    def test_extension_across_a_gap_is_a_discontinuity(self):
        start, now1, now2 = 903.0, 1000.0, 1100.0
        old = vod.plan_segments([Recording(1, 0.0, now1, live=True)], start, vod.live_edge(now1), now1)
        gap = [Recording(1, 0.0, 990.0), Recording(2, 1031.0, now2, live=True)]
        new = self.extend(old, gap, now2)
        added = new[len(old)]
        self.assertEqual(added.wall_start, 1031.0)
        self.assertTrue(added.discontinuity)
        self.assertEqual(len(vod.runs_from_segments(new)), 2)
        self.assert_well_formed(new)


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


    def test_live_playlist_is_an_open_event_playlist(self):
        segs = vod.plan_segments([Recording(id=1, start=0.0, end=30.0)], 0.0, 30.0, NOW)
        live = vod.render_playlist(segs, live=True)
        self.assertIn("#EXT-X-PLAYLIST-TYPE:EVENT", live)
        self.assertNotIn("#EXT-X-ENDLIST", live)
        closed = vod.render_playlist(segs)
        self.assertIn("#EXT-X-PLAYLIST-TYPE:VOD", closed)
        self.assertTrue(closed.rstrip().endswith("#EXT-X-ENDLIST"))


class Boxes(unittest.TestCase):
    def test_split_fmp4(self):
        data = box("ftyp", b"iso5") + box("moov", b"x" * 20) + box("moof", b"m") + box("mdat", b"d" * 5) + box("mfra")
        init, media = vod.split_fmp4(data)
        self.assertEqual(init, box("ftyp", b"iso5") + box("moov", b"x" * 20))
        self.assertEqual(media, box("moof", b"m") + box("mdat", b"d" * 5))

    def test_truncated_box_stops_cleanly(self):
        data = box("ftyp") + struct.pack(">I4s", 100, b"moov") + b"short"
        self.assertEqual(list(t for t, _ in vod.iter_boxes(data)), ["ftyp"])

    def test_largesize_box(self):
        # size == 1: the real (64-bit) size follows the type as a big-endian Q.
        payload = b"x" * 20
        data = struct.pack(">I4sQ", 1, b"mdat", 16 + len(payload)) + payload
        [(typ, raw)] = list(vod.iter_boxes(data))
        self.assertEqual((typ, raw), ("mdat", data))

    def test_truncated_largesize_header_stops_cleanly(self):
        data = box("ftyp") + struct.pack(">I4s", 1, b"mdat") + b"short"
        self.assertEqual(list(t for t, _ in vod.iter_boxes(data)), ["ftyp"])

    def test_zero_size_box_extends_to_the_end(self):
        # size == 0: the last box in the file, extending to its end.
        data = box("ftyp") + struct.pack(">I4s", 0, b"mdat") + b"tail-data"
        types_and_ends = [(t, len(raw)) for t, raw in vod.iter_boxes(data)]
        self.assertEqual(types_and_ends, [("ftyp", 8), ("mdat", 8 + len(b"tail-data"))])

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


class LocalTime(unittest.TestCase):
    def test_nas_local_iso_to_epoch(self):
        from zoneinfo import ZoneInfo
        from synology_ss_playback.client import _local_ts

        # Values seen from SS 9.3 (NAS in US/Pacific) for the same bookmark.
        self.assertEqual(_local_ts("2026-09-24T16:40:58", ZoneInfo("US/Pacific")), 1790293258)
        self.assertEqual(_local_ts("2026-01-15T08:00:00", ZoneInfo("US/Pacific")), 1768492800)


if __name__ == "__main__":
    unittest.main()
