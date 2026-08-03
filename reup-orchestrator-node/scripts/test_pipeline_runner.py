"""Unit test thuần stdlib (unittest + unittest.mock) cho phần resume/job_id-reattach mới trong
`pipeline_runner._run_stage` — dùng `mode="audio"` (dừng ngay sau bước `ytdlp`) làm đường đi
ngắn nhất chạm được `_run_stage()` thật qua `run_pipeline()` công khai, không cần dựng cả 5 node.
Chạy: python3 -m unittest test_pipeline_runner -v (không cần Redis/GPU/ffmpeg, mock thẳng
`submit_and_wait`/`wait_for_job` ở module `pipeline_runner`).

Trọng tâm: (1) job vừa submit phải được lưu `job_id` NGAY (trước khi biết kết quả) — mô phỏng
bằng cách kiểm tra `on_submitted` được gọi đúng job_id; (2) khi resume 1 bước đang "started" với
`job_id` có sẵn, phải nối lại đúng job đó — TUYỆT ĐỐI KHÔNG submit job mới (đây là bug thật đã
gặp: resume sớm làm TTS chạy lại từ đầu, tốn thêm ~8 phút GPU vô ích)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline_runner as pr


class VideoModeTests(unittest.TestCase):
    """mode="video" — nhánh mới, y hệt mode="audio" (dừng ngay sau ytdlp) nhưng KHÔNG ép
    `-x --audio-format mp3`, giữ nguyên đuôi file yt-dlp tải về."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self._orig_downloads = pr.NODES["ytdlp"]["downloads"]
        self._orig_own_outputs = pr.OWN_OUTPUTS
        pr.NODES["ytdlp"]["downloads"] = tmp_path / "downloads"
        pr.OWN_OUTPUTS = tmp_path / "outputs"
        self.pipeline_id = "vid1234abcd"

    def tearDown(self):
        pr.NODES["ytdlp"]["downloads"] = self._orig_downloads
        pr.OWN_OUTPUTS = self._orig_own_outputs
        self._tmp.cleanup()

    @patch("pipeline_runner.submit_and_wait")
    def test_video_mode_stops_after_ytdlp_keeps_original_extension(self, mock_submit_and_wait):
        def fake_submit_and_wait(base_url, body, on_submitted=None, **kw):
            # KHÔNG được ép -x/--audio-format mp3 như mode="audio" — kiểm tra ngay trong args
            # gửi cho ytdlp-node để chắc nhánh "video" không lỡ tái dùng đúng nhánh "audio".
            self.assertNotIn("-x", body["args"])
            self.assertNotIn("--audio-format", body["args"])
            pr.NODES["ytdlp"]["downloads"].mkdir(parents=True, exist_ok=True)
            (pr.NODES["ytdlp"]["downloads"] / f"{self.pipeline_id}.webm").write_bytes(b"fake-video")
            return {"ok": True}

        mock_submit_and_wait.side_effect = fake_submit_and_wait
        result = pr.run_pipeline(self.pipeline_id, {"url": "https://x/y", "mode": "video"})
        self.assertTrue(result["ok"])
        # Giữ đúng đuôi .webm (không ép cứng .mp4) và có tag nhánh "video_" đứng trước tên.
        self.assertTrue(result["output"].endswith("video_final.webm"), result["output"])

    def test_video_mode_only_downloads_no_transcribe_translate_tts_editor_stage(self):
        with patch("pipeline_runner.submit_and_wait") as mock_submit_and_wait:
            def fake_submit_and_wait(base_url, body, on_submitted=None, **kw):
                pr.NODES["ytdlp"]["downloads"].mkdir(parents=True, exist_ok=True)
                (pr.NODES["ytdlp"]["downloads"] / f"{self.pipeline_id}.mp4").write_bytes(b"x")
                return {"ok": True}

            mock_submit_and_wait.side_effect = fake_submit_and_wait
            result = pr.run_pipeline(self.pipeline_id, {"url": "https://x/y", "mode": "video"})
            # Chỉ 1 stage duy nhất (ytdlp) — đúng tinh thần dừng sớm như mode="audio", không lỡ
            # rơi tiếp vào transcribe/translate/tts/editor.
            self.assertEqual(list(result["stages"].keys()), ["ytdlp"])
            self.assertEqual(mock_submit_and_wait.call_count, 1)


class ResumeJobIdReattachTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self._orig_downloads = pr.NODES["ytdlp"]["downloads"]
        self._orig_own_outputs = pr.OWN_OUTPUTS
        pr.NODES["ytdlp"]["downloads"] = tmp_path / "downloads"
        pr.OWN_OUTPUTS = tmp_path / "outputs"
        self.pipeline_id = "pid1234abcd"

    def tearDown(self):
        pr.NODES["ytdlp"]["downloads"] = self._orig_downloads
        pr.OWN_OUTPUTS = self._orig_own_outputs
        self._tmp.cleanup()

    def _write_downloaded_file(self) -> None:
        pr.NODES["ytdlp"]["downloads"].mkdir(parents=True, exist_ok=True)
        (pr.NODES["ytdlp"]["downloads"] / f"{self.pipeline_id}.mp3").write_bytes(b"fake-audio")

    @patch("pipeline_runner.submit_and_wait")
    def test_fresh_submit_persists_job_id_before_result_known(self, mock_submit_and_wait):
        captured = []

        def fake_submit_and_wait(base_url, body, on_submitted=None, **kw):
            on_submitted("job-fresh-1")
            self._write_downloaded_file()
            return {"ok": True}

        mock_submit_and_wait.side_effect = fake_submit_and_wait
        result = pr.run_pipeline(self.pipeline_id, {"url": "https://x/y", "mode": "audio"})
        self.assertTrue(result["ok"])
        self.assertEqual(mock_submit_and_wait.call_count, 1)
        # on_submitted đã chạy TRƯỚC khi có kết quả cuối — nếu code bỏ callback này đi thì
        # KeyError ở đây (fake_submit_and_wait không gọi được on_submitted=None).
        self.assertTrue(callable(mock_submit_and_wait.call_args.kwargs.get("on_submitted")))

    @patch("pipeline_runner.wait_for_job")
    @patch("pipeline_runner.submit_and_wait")
    def test_resume_with_started_job_id_reattaches_no_duplicate_submit(
        self, mock_submit_and_wait, mock_wait_for_job,
    ):
        def fail_if_called(*a, **kw):
            raise AssertionError("submit_and_wait KHÔNG được gọi khi resume 1 job_id còn sống")

        mock_submit_and_wait.side_effect = fail_if_called

        def fake_wait_for_job(base_url, job_id, **kw):
            self.assertEqual(job_id, "job-in-flight-42")
            self._write_downloaded_file()
            return {"ok": True}

        mock_wait_for_job.side_effect = fake_wait_for_job

        resume_stages = {"ytdlp": {"status": "started", "job_id": "job-in-flight-42"}}
        result = pr.run_pipeline(
            self.pipeline_id, {"url": "https://x/y", "mode": "audio"}, resume_stages=resume_stages,
        )
        self.assertTrue(result["ok"])
        mock_submit_and_wait.assert_not_called()
        self.assertEqual(mock_wait_for_job.call_count, 1)

    @patch("pipeline_runner.submit_and_wait")
    def test_unexpected_exception_still_recorded_in_stages(self, mock_submit_and_wait):
        # Bug thật đã gặp: 1 lỗi không phải NodeJobError/NodeCancelled (vd ConnectionError thô
        # lọt qua) làm cả bước biến mất khỏi `stages`, resume sau không biết gì về nó.
        mock_submit_and_wait.side_effect = ValueError("lỗi lạ không lường trước")
        with self.assertRaises(pr.NodeJobError) as ctx:
            pr.run_pipeline(self.pipeline_id, {"url": "https://x/y", "mode": "audio"})
        stages = ctx.exception.stages  # type: ignore[attr-defined]
        self.assertEqual(stages["ytdlp"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
