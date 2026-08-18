from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pyJianYingDraft-fork"))

from bridge_safety import SafetyGateError, guarded_child, validate_draft_name
from jianying_to_fcp7 import CaptionCue, ReverseClip, ReverseIR, build_fcp7_xml, build_srt
from jianying_xml_adapter import (
    GateFailure,
    checked_us_to_frame,
    frame_to_us,
    parse_fcp7_xml,
    validate_media_duration,
)


class FrameTests(unittest.TestCase):
    def test_frame_round_trip(self) -> None:
        for frame in (0, 1, 29, 30, 30000):
            self.assertEqual(checked_us_to_frame(frame_to_us(frame, 30), 30, "frame"), frame)

    def test_off_grid_value_is_rejected(self) -> None:
        with self.assertRaises(GateFailure):
            checked_us_to_frame(frame_to_us(30, 30) + 10_000, 30, "off-grid")


class MediaDurationCompatibilityTests(unittest.TestCase):
    def test_exact_duration_is_accepted(self) -> None:
        result = validate_media_duration(300, 300, 290)
        self.assertEqual(result["policy"], "exact")
        self.assertEqual(result["difference_frames"], 0)

    def test_one_unused_trailing_frame_is_accepted(self) -> None:
        result = validate_media_duration(300, 301, 300)
        self.assertEqual(result["policy"], "one_unused_trailing_frame")
        self.assertEqual(result["difference_frames"], 1)

    def test_actual_media_shorter_by_one_frame_is_rejected(self) -> None:
        with self.assertRaises(GateFailure):
            validate_media_duration(300, 299, 290)

    def test_actual_media_longer_by_two_frames_is_rejected(self) -> None:
        with self.assertRaises(GateFailure):
            validate_media_duration(300, 302, 290)

    def test_source_range_beyond_xml_declared_duration_is_rejected(self) -> None:
        with self.assertRaises(GateFailure):
            validate_media_duration(300, 301, 301)

    def test_non_positive_duration_is_rejected(self) -> None:
        with self.assertRaises(GateFailure):
            validate_media_duration(0, 1, 0)


class PathSafetyTests(unittest.TestCase):
    def test_safe_draft_name(self) -> None:
        self.assertEqual(validate_draft_name("Episode_01"), "Episode_01")

    def test_path_like_name_is_rejected(self) -> None:
        for name in ("..", "folder/name", "folder\\name", "CON"):
            with self.subTest(name=name), self.assertRaises(SafetyGateError):
                validate_draft_name(name)

    def test_guarded_child_stays_in_root(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(guarded_child(root, "Episode_01").parent, root.resolve())


class SerializationTests(unittest.TestCase):
    def make_ir(self, media_path: str) -> ReverseIR:
        return ReverseIR(
            draft_path=r"C:\JYBridge\profiles\Synthetic",
            draft_name="Synthetic",
            timeline_id="00000000-0000-0000-0000-000000000001",
            sequence_name="Synthetic Timeline",
            fps=30,
            fps_source="test",
            width=1920,
            height=1080,
            duration_frames=90,
            media_path=media_path,
            media_duration_frames=300,
            media_duration_us=10_000_000,
            audio_channels=2,
            audio_sample_rate=48_000,
            video_track_id="video-track",
            caption_track_id="caption-track",
            clips=[
                ReverseClip(0, "segment-1", 0, 30, 60, 90),
                ReverseClip(1, "segment-2", 30, 90, 120, 180),
            ],
            captions=[CaptionCue(1, "caption-1", 0, 1_000_000, "公开测试字幕")],
            identity_aux_counts={},
            material_duration_frames=[300],
        )

    def test_srt_is_utf8_bom_and_one_line(self) -> None:
        payload = build_srt(self.make_ir(r"C:\JYBridge\fixtures\source.mp4").captions)
        self.assertTrue(payload.startswith(b"\xef\xbb\xbf"))
        self.assertIn("公开测试字幕".encode("utf-8"), payload)

    def test_fcp7_xml_is_deterministic_and_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            media = str(Path(folder) / "source.mp4")
            ir = self.make_ir(media)
            first = build_fcp7_xml(ir)
            second = build_fcp7_xml(ir)
            self.assertEqual(first, second)
            xml_path = Path(folder) / "timeline.xml"
            xml_path.write_bytes(first)
            parsed = parse_fcp7_xml(xml_path)
            self.assertEqual(parsed.fps, 30)
            self.assertEqual(parsed.duration_frames, 90)
            self.assertEqual(len(parsed.clips), 2)
            self.assertEqual(
                [(clip.target_start, clip.target_end, clip.source_in, clip.source_out) for clip in parsed.clips],
                [(0, 30, 60, 90), (30, 90, 120, 180)],
            )


if __name__ == "__main__":
    unittest.main()
