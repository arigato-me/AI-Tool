"""Unit test thuần stdlib cho concat_audio_cli.py — chỉ test hàm build command (string), không
chạy ffmpeg thật, cùng convention test_edit_cli.py. Chạy: python3 -m unittest test_concat_audio_cli -v"""
from __future__ import annotations

import unittest
from pathlib import Path

from concat_audio_cli import build_concat_audio_command


class BuildConcatAudioCommandTests(unittest.TestCase):
    def test_two_inputs_aformat_then_concat(self):
        cmd = build_concat_audio_command(["a.mp3", "b.wav"], Path("out.wav"), 44100)
        self.assertEqual(cmd[:5], ["ffmpeg", "-y", "-i", "a.mp3", "-i"])
        self.assertIn("b.wav", cmd)
        filter_idx = cmd.index("-filter_complex") + 1
        filter_str = cmd[filter_idx]
        self.assertIn("[0:a:0]aformat=sample_rates=44100:channel_layouts=stereo", filter_str)
        self.assertIn("[1:a:0]aformat=sample_rates=44100:channel_layouts=stereo", filter_str)
        self.assertIn("concat=n=2:v=0:a=1[outa]", filter_str)
        self.assertEqual(cmd[-1], "out.wav")
        self.assertIn("pcm_s16le", cmd)

    def test_video_input_still_selects_a0(self):
        """Item type="reuse" có thể trỏ vào 1 file video — vẫn phải chọn được a:0."""
        cmd = build_concat_audio_command(["clip.mp4"], Path("out.wav"), 44100)
        filter_str = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("[0:a:0]", filter_str)
        self.assertIn("concat=n=1:v=0:a=1[outa]", filter_str)


if __name__ == "__main__":
    unittest.main()
