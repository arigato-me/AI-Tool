"""Unit test thuần stdlib cho image_to_video_cli.py — chỉ test hàm build command (string),
không chạy ffmpeg thật, cùng convention test_concat_video_cli.py. Chạy:
python3 -m unittest test_image_to_video_cli -v"""
from __future__ import annotations

import unittest
from pathlib import Path

from image_to_video_cli import build_image_to_video_command


class BuildImageToVideoCommandTests(unittest.TestCase):
    def test_loops_image_for_duration(self):
        cmd = build_image_to_video_command("cover.jpg", Path("out.mp4"), 1080, 1920, 5.0, 30, 20)
        self.assertEqual(cmd[:6], ["ffmpeg", "-y", "-loop", "1", "-i", "cover.jpg"])
        self.assertIn("-t", cmd)
        self.assertEqual(cmd[cmd.index("-t") + 1], "5.0")
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("scale=1080:1920", vf)
        self.assertIn("fps=30", vf)
        self.assertIn("-an", cmd)
        self.assertIn("yuv420p", cmd)
        self.assertEqual(cmd[-1], "out.mp4")

    def test_crf_applied(self):
        cmd = build_image_to_video_command("a.png", Path("out.mp4"), 640, 480, 3.0, 25, 18)
        self.assertEqual(cmd[cmd.index("-crf") + 1], "18")


if __name__ == "__main__":
    unittest.main()
