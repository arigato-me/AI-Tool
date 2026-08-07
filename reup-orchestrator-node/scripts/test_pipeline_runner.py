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

import base64
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


class MixModeTests(unittest.TestCase):
    """mode="mix" — ghép N video + N audio nối tiếp, không transcribe/dịch/TTS. Mock cả
    ytdlp lẫn editor qua CÙNG 1 `submit_and_wait` (phân biệt bằng body: có "args" -> ytdlp,
    có "cmd" -> editor) — đủ để kiểm 4 loại item resolve đúng, không cần dựng node thật."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self._orig = {
            "ytdlp_downloads": pr.NODES["ytdlp"]["downloads"],
            "editor_source": pr.NODES["editor"]["source"],
            "editor_outputs": pr.NODES["editor"]["outputs"],
            "own_outputs": pr.OWN_OUTPUTS,
        }
        pr.NODES["ytdlp"]["downloads"] = tmp_path / "ytdlp_downloads"
        pr.NODES["editor"]["source"] = tmp_path / "editor_source"
        pr.NODES["editor"]["outputs"] = tmp_path / "editor_outputs"
        pr.OWN_OUTPUTS = tmp_path / "outputs"
        self.pipeline_id = "mixabcd1234"
        self._fake_audio_duration_s: float | None = 10.0
        self.image_to_video_calls: list[dict] = []

    def tearDown(self):
        pr.NODES["ytdlp"]["downloads"] = self._orig["ytdlp_downloads"]
        pr.NODES["editor"]["source"] = self._orig["editor_source"]
        pr.NODES["editor"]["outputs"] = self._orig["editor_outputs"]
        pr.OWN_OUTPUTS = self._orig["own_outputs"]
        self._tmp.cleanup()

    def _fake_submit_and_wait(self, base_url, body, on_submitted=None, **kw):
        if "args" in body:
            out_idx = body["args"].index("-o") + 1
            sub_id = Path(body["args"][out_idx]).name.split(".")[0]
            pr.NODES["ytdlp"]["downloads"].mkdir(parents=True, exist_ok=True)
            (pr.NODES["ytdlp"]["downloads"] / f"{sub_id}.mp4").write_bytes(b"fake")
            return {"ok": True}
        cmd = body.get("cmd")
        if cmd == "image-to-video":
            self.image_to_video_calls.append(body["params"])
        out_name = Path(body["params"]["output"]).name
        pr.NODES["editor"]["outputs"].mkdir(parents=True, exist_ok=True)
        (pr.NODES["editor"]["outputs"] / out_name).write_bytes(b"fake")
        result = {"ok": True, "cmd": cmd}
        if cmd == "concat-audio":
            result["duration_s"] = self._fake_audio_duration_s
        return result

    @patch("pipeline_runner.submit_and_wait")
    def test_upload_and_library_items_resolve_and_run_concat_then_edit(self, mock_submit_and_wait):
        mock_submit_and_wait.side_effect = self._fake_submit_and_wait
        payload = {
            "mode": "mix",
            "video_items": [{"type": "upload", "data_b64": base64.b64encode(b"video-bytes").decode(), "ext": "mp4"}],
            "audio_items": [{"type": "library", "music_project": "chill", "music_track": "song.mp3"}],
        }
        result = pr.run_pipeline(self.pipeline_id, payload)
        self.assertTrue(result["ok"])
        self.assertTrue(result["output"].endswith("mix_final.mp4"), result["output"])
        # upload/library không tạo job ytdlp nào — chỉ 3 stage editor.
        self.assertEqual(
            {"editor_concat_video", "editor_concat_audio", "editor_edit"}, set(result["stages"].keys()),
        )

    @patch("pipeline_runner.submit_and_wait")
    def test_reuse_item_does_not_submit_new_ytdlp_job(self, mock_submit_and_wait):
        mock_submit_and_wait.side_effect = self._fake_submit_and_wait
        ref_id = "oldpipeline0001"
        pr.NODES["ytdlp"]["downloads"].mkdir(parents=True, exist_ok=True)
        (pr.NODES["ytdlp"]["downloads"] / f"{ref_id}.mp4").write_bytes(b"already-downloaded")
        payload = {
            "mode": "mix",
            "video_items": [{"type": "reuse", "pipeline_id": ref_id}],
            "audio_items": [{"type": "reuse", "pipeline_id": ref_id}],
        }
        result = pr.run_pipeline(self.pipeline_id, payload)
        self.assertTrue(result["ok"])
        self.assertFalse(any(name.startswith("ytdlp_") for name in result["stages"]))

    @patch("pipeline_runner.submit_and_wait")
    def test_url_item_downloads_via_ytdlp_with_own_sub_id(self, mock_submit_and_wait):
        mock_submit_and_wait.side_effect = self._fake_submit_and_wait
        payload = {
            "mode": "mix",
            "video_items": [{"type": "url", "url": "https://example.com/v1"}],
            "audio_items": [{"type": "url", "url": "https://example.com/a1"}],
        }
        result = pr.run_pipeline(self.pipeline_id, payload)
        self.assertTrue(result["ok"])
        self.assertIn("ytdlp_v0", result["stages"])
        self.assertIn("ytdlp_a0", result["stages"])

    @patch("pipeline_runner.submit_and_wait")
    def test_all_image_video_items_split_audio_duration_evenly(self, mock_submit_and_wait):
        mock_submit_and_wait.side_effect = self._fake_submit_and_wait
        self._fake_audio_duration_s = 10.0
        img_b64 = base64.b64encode(b"fake-image-bytes").decode()
        payload = {
            "mode": "mix",
            "video_items": [
                {"type": "image", "data_b64": img_b64, "ext": "jpg"},
                {"type": "image", "data_b64": img_b64, "ext": "jpg"},
            ],
            "audio_items": [{"type": "library", "music_project": "chill", "music_track": "song.mp3"}],
        }
        result = pr.run_pipeline(self.pipeline_id, payload)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.image_to_video_calls), 2)
        for call in self.image_to_video_calls:
            self.assertAlmostEqual(call["duration"], 5.0)

    @patch("pipeline_runner.submit_and_wait")
    def test_all_image_video_items_respects_explicit_duration_for_remaining_split(self, mock_submit_and_wait):
        mock_submit_and_wait.side_effect = self._fake_submit_and_wait
        self._fake_audio_duration_s = 10.0
        img_b64 = base64.b64encode(b"fake-image-bytes").decode()
        payload = {
            "mode": "mix",
            "video_items": [
                {"type": "image", "data_b64": img_b64, "ext": "jpg", "duration": 4.0},
                {"type": "image", "data_b64": img_b64, "ext": "jpg"},
            ],
            "audio_items": [{"type": "library", "music_project": "chill", "music_track": "song.mp3"}],
        }
        result = pr.run_pipeline(self.pipeline_id, payload)
        self.assertTrue(result["ok"])
        durations = sorted(call["duration"] for call in self.image_to_video_calls)
        # item đã nhập 4.0 giữ nguyên, item còn lại lấy hết phần audio còn lại (10 - 4 = 6).
        self.assertEqual(durations, [4.0, 6.0])

    @patch("pipeline_runner.submit_and_wait")
    def test_image_item_mixed_with_real_video_without_duration_raises(self, mock_submit_and_wait):
        mock_submit_and_wait.side_effect = self._fake_submit_and_wait
        # item "reuse" ở index 0 phải resolve XONG (file có sẵn) mới tới lượt item "image" ở
        # index 1 raise — tạo sẵn file để không bị NodeJobError (thiếu file) che mất lỗi thật
        # đang test (thiếu duration).
        ref_id = "some-old-pipeline"
        pr.NODES["ytdlp"]["downloads"].mkdir(parents=True, exist_ok=True)
        (pr.NODES["ytdlp"]["downloads"] / f"{ref_id}.mp4").write_bytes(b"already-downloaded")
        img_b64 = base64.b64encode(b"fake-image-bytes").decode()
        payload = {
            "mode": "mix",
            "video_items": [
                {"type": "reuse", "pipeline_id": ref_id},
                {"type": "image", "data_b64": img_b64, "ext": "jpg"},  # thiếu duration, trộn video thật
            ],
            "audio_items": [{"type": "library", "music_project": "chill", "music_track": "song.mp3"}],
        }
        with self.assertRaises(ValueError):
            pr.run_pipeline(self.pipeline_id, payload)

    def test_empty_video_items_raises(self):
        payload = {"mode": "mix", "video_items": [],
                   "audio_items": [{"type": "library", "music_project": "a", "music_track": "b.mp3"}]}
        with self.assertRaises(ValueError):
            pr.run_pipeline(self.pipeline_id, payload)

    def test_empty_audio_items_raises(self):
        payload = {"mode": "mix", "video_items": [{"type": "reuse", "pipeline_id": "x"}], "audio_items": []}
        with self.assertRaises(ValueError):
            pr.run_pipeline(self.pipeline_id, payload)


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
