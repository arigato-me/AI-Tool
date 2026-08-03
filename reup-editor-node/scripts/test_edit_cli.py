"""Unit test thuần stdlib (unittest) cho phần style sub burn-in mới thêm ở edit_cli.py —
cùng convention test_music_library.py (không pytest, không cần ffmpeg/media thật). Chạy:
python3 -m unittest test_edit_cli -v"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import edit_cli
from edit_cli import (
    DEFAULT_SUB_STYLE,
    _hex_to_ass_color,
    _hex_to_ffmpeg_color,
    build_ass_and_boxes,
    clear_default_sub_style,
    get_default_sub_style,
    merge_sub_style,
    set_default_sub_style,
)


class MergeSubStyleTests(unittest.TestCase):
    def test_none_returns_defaults(self):
        self.assertEqual(merge_sub_style(None), DEFAULT_SUB_STYLE)

    def test_partial_dict_fills_missing_fields_from_default(self):
        merged = merge_sub_style({"text_color": "#FFD400"})
        self.assertEqual(merged["text_color"], "#FFD400")
        self.assertEqual(merged["background_opacity"], DEFAULT_SUB_STYLE["background_opacity"])
        self.assertEqual(merged["bold"], DEFAULT_SUB_STYLE["bold"])


class HexToAssColorTests(unittest.TestCase):
    def test_white(self):
        self.assertEqual(_hex_to_ass_color("#FFFFFF"), "&H00FFFFFF")

    def test_black(self):
        self.assertEqual(_hex_to_ass_color("#000000"), "&H00000000")

    def test_reverses_to_bgr_and_upcases(self):
        self.assertEqual(_hex_to_ass_color("#aabbcc"), "&H00CCBBAA")


class HexToFfmpegColorTests(unittest.TestCase):
    def test_white_55_percent(self):
        self.assertEqual(_hex_to_ffmpeg_color("#FFFFFF", 55), "0xFFFFFF@0.55")

    def test_clamps_out_of_range_opacity(self):
        self.assertEqual(_hex_to_ffmpeg_color("#000000", 250), "0x000000@1.00")
        self.assertEqual(_hex_to_ffmpeg_color("#000000", -10), "0x000000@0.00")


class BuildAssAndBoxesTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.ass_path = Path(self._tmpdir.name) / "out.ass"
        self.segments = [{"start": 0.0, "end": 1.5, "text": "Xin chào"}]

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_background_enabled_produces_drawbox(self):
        boxes = build_ass_and_boxes(self.segments, 1080, 1920, self.ass_path, DEFAULT_SUB_STYLE)
        self.assertIn("drawbox", boxes)

    def test_background_disabled_produces_no_drawbox(self):
        style = {**DEFAULT_SUB_STYLE, "background_enabled": False}
        boxes = build_ass_and_boxes(self.segments, 1080, 1920, self.ass_path, style)
        self.assertEqual(boxes, "")

    def test_ass_style_line_reflects_bold_color_and_outline(self):
        style = {**DEFAULT_SUB_STYLE, "bold": False, "text_color": "#00FF00", "outline_width": 3}
        build_ass_and_boxes(self.segments, 1080, 1920, self.ass_path, style)
        style_line = next(l for l in self.ass_path.read_text(encoding="utf-8").splitlines() if l.startswith("Style:"))
        fields = style_line.split(",")
        self.assertEqual(fields[3], _hex_to_ass_color("#00FF00"))  # PrimaryColour
        self.assertEqual(fields[7], "0")  # Bold
        self.assertEqual(fields[16], "3")  # Outline width


class DefaultSubStylePersistenceTests(unittest.TestCase):
    """Ghi đè STYLE_DIR sang thư mục tạm — không đụng /style thật."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_dir = edit_cli.STYLE_DIR
        edit_cli.STYLE_DIR = Path(self._tmpdir.name)

    def tearDown(self):
        edit_cli.STYLE_DIR = self._orig_dir
        self._tmpdir.cleanup()

    def test_no_file_returns_none(self):
        self.assertIsNone(get_default_sub_style())

    def test_set_then_get_roundtrips_merged(self):
        set_default_sub_style({"text_color": "#FFD400"})
        saved = get_default_sub_style()
        self.assertEqual(saved["text_color"], "#FFD400")
        self.assertEqual(saved["bold"], DEFAULT_SUB_STYLE["bold"])  # field thiếu -> merge default

    def test_clear_removes_file(self):
        set_default_sub_style({"bold": False})
        clear_default_sub_style()
        self.assertIsNone(get_default_sub_style())


if __name__ == "__main__":
    unittest.main()
