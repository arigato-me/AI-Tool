"""Unit test thuần stdlib (unittest) cho segments_cli.py — không cần GPU/model VieNeu-TTS thật
(FakeTTS tiêm qua tham số `tts=` của run_segments()). Chạy: python3 -m unittest test_segments_cli
-v (trong container tts-gpu, cần numpy/librosa đã có sẵn). Tập trung vào guard hallucination mới
thêm — xem docstring HALLUCINATION_* trong segments_cli.py để biết bối cảnh (bug thật: audio
23.92s cho câu 2 ký tự, gây drift 17s cascading qua hết video)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

import segments_cli as sc

SAMPLE_RATE = 1000  # số nhỏ cho test nhanh, không cần audio nghe được


class FakeTTS:
    """`durations_s` map text -> list độ dài audio (giây) trả về qua từng lần gọi infer() liên
    tiếp với ĐÚNG text đó (lần cuối lặp lại nếu gọi nhiều hơn số phần tử) — mô phỏng cả trường
    hợp bình thường (1 giá trị) lẫn hallucination-rồi-retry-ổn (2 giá trị, giá trị 2 nhỏ)."""

    sample_rate = SAMPLE_RATE

    def __init__(self, durations_s: dict[str, list[float]]):
        self._durations_s = durations_s
        self.call_counts: dict[str, int] = {}

    def list_preset_voices(self):
        return [("Voice A", "voice-a")]

    def infer(self, text, voice=None, style=None, ref_audio=None):
        n = self.call_counts.get(text, 0)
        self.call_counts[text] = n + 1
        seq = self._durations_s[text]
        duration = seq[min(n, len(seq) - 1)]
        return np.zeros(int(round(duration * self.sample_rate)), dtype=np.float32)

    def save(self, buffer, path):
        Path(path).write_bytes(b"")


def _write_transcript(tmpdir: Path, segments: list[dict]) -> Path:
    p = tmpdir / "transcript.json"
    p.write_text(json.dumps({"segments": segments}, ensure_ascii=False), encoding="utf-8")
    return p


class HallucinationGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_normal_segment_passes_through_once(self):
        text = "Xin chào các bạn."
        input_p = _write_transcript(self.tmpdir, [
            {"id": 1, "start": 0.0, "end": 3.0, "text": text},
        ])
        tts = FakeTTS({text: [1.5]})
        result = sc.run_segments(str(input_p), str(self.tmpdir / "out.wav"), voice="Voice A", tts=tts)
        self.assertEqual(tts.call_counts[text], 1)  # không retry vì audio hợp lý
        self.assertEqual(result["drifted_segments"], 0)

    def test_hallucination_recovers_on_retry(self):
        # Lần 1 hallucinate (24s cho câu 2 ký tự), lần 2 (retry) ra audio bình thường.
        text = "Ơ."
        input_p = _write_transcript(self.tmpdir, [
            {"id": 1, "start": 0.0, "end": 1.0, "text": text},
        ])
        tts = FakeTTS({text: [24.0, 0.5]})
        result = sc.run_segments(str(input_p), str(self.tmpdir / "out.wav"), voice="Voice A", tts=tts)
        self.assertEqual(tts.call_counts[text], 2)  # đã retry đúng 1 lần
        self.assertEqual(result["segments"], 1)
        windows = json.loads((self.tmpdir / "out_windows.json").read_text(encoding="utf-8"))
        self.assertEqual(len(windows["segments"]), 1)  # segment vẫn được đặt (không bị skip)

    def test_persistent_hallucination_is_skipped_not_drifted(self):
        # Cả 2 lần đều hallucinate -> phải bỏ qua (để khoảng lặng), KHÔNG được lôi 24s audio
        # rác vào buffer (đó chính là bug gốc gây drift cascading 17s qua hết video).
        text = "Ơ."
        next_text = "Câu tiếp theo đúng giờ."
        input_p = _write_transcript(self.tmpdir, [
            {"id": 1, "start": 0.0, "end": 1.0, "text": text},
            {"id": 2, "start": 1.0, "end": 4.0, "text": next_text},
        ])
        tts = FakeTTS({text: [24.0, 23.0], next_text: [2.5]})
        result = sc.run_segments(str(input_p), str(self.tmpdir / "out.wav"), voice="Voice A", tts=tts)
        self.assertEqual(tts.call_counts[text], 2)  # thử đúng 1 lần rồi bỏ cuộc, không lặp mãi
        # Segment sau KHÔNG bị đẩy lùi vì segment hallucination đã bị skip (không chiếm buffer)
        self.assertEqual(result["drifted_segments"], 0)
        self.assertEqual(result["max_drift_s"], 0.0)
        windows = json.loads((self.tmpdir / "out_windows.json").read_text(encoding="utf-8"))
        skipped_window = windows["segments"][0]
        self.assertEqual(skipped_window["start"], 0.0)  # windows dùng timestamp GỐC khi skip
        self.assertEqual(skipped_window["end"], 1.0)


if __name__ == "__main__":
    unittest.main()
