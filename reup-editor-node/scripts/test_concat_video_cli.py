"""Unit test thuần stdlib cho concat_video_cli.py — chỉ test hàm build command (string), không
chạy ffmpeg thật, cùng convention test_edit_cli.py. Chạy: python3 -m unittest test_concat_video_cli -v"""
from __future__ import annotations

import unittest
from pathlib import Path

from concat_video_cli import build_concat_video_command


class BuildConcatVideoCommandTests(unittest.TestCase):
    def test_two_inputs_scale_pad_fps_then_concat(self):
        cmd = build_concat_video_command(["a.mp4", "b.mp4"], Path("out.mp4"), 1080, 1920, 30, 20)
        self.assertEqual(cmd[:5], ["ffmpeg", "-y", "-i", "a.mp4", "-i"])
        self.assertIn("b.mp4", cmd)
        filter_idx = cmd.index("-filter_complex") + 1
        filter_str = cmd[filter_idx]
        self.assertIn("[0:v:0]scale=1080:1920", filter_str)
        self.assertIn("[1:v:0]scale=1080:1920", filter_str)
        self.assertIn("fps=30", filter_str)
        self.assertIn("concat=n=2:v=1:a=0[outv]", filter_str)
        self.assertEqual(cmd[-1], "out.mp4")
        self.assertIn("-an", cmd)

    def test_single_input(self):
        cmd = build_concat_video_command(["only.mp4"], Path("out.mp4"), 640, 480, 25, 18)
        filter_str = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("concat=n=1:v=1:a=0[outv]", filter_str)
        self.assertIn("-crf", cmd)
        self.assertEqual(cmd[cmd.index("-crf") + 1], "18")


if __name__ == "__main__":
    unittest.main()
