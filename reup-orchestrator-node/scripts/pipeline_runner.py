"""Logic sequencing pipeline reup (thay vai trò n8n) — gọi API các node khác qua HTTP
(`node_client.submit_and_wait`), tự copy file giữa data dir riêng của từng node. Đọc được các
dir đó là nhờ `docker-compose.yml` của chính orchestrator bind-mount thêm `data/source`/
`data/outputs` (và `downloads`) của cả 5 node kia vào container này dưới path riêng
(`/nodes/<service>/...`) — xem "Quyết định kiến trúc #1" trong plan. Theo đúng 2 nhánh mô tả ở
CLAUDE.md "Reup pipeline" (review mặc định — mute audio gốc / dialogue — giữ nền).

Mọi path trả về trong `result` của từng node (`result["output"]`, và riêng transcribe
`result["instrumental"]`/`result["original"]` khi mode=dialogue) được dùng thẳng để biết tên
file thật — không tự suy đoán tên, tránh lệch khi logic đặt tên ở node đó đổi.
"""
from __future__ import annotations

import base64
import glob
import os
import re
import shutil
import time
from pathlib import Path

import queue_lib as q
from node_client import NodeCancelled, NodeJobError, submit_and_wait, wait_for_job


class PipelineCancelled(RuntimeError):
    """Raise khi phát hiện cờ huỷ (`q.is_cancel_requested`) trước lúc chạy 1 bước — khác
    `NodeJobError` (lỗi thật) để `worker.py` ghi status 'cancelled' thay vì 'failed'."""


def _safe_music_seg(seg: str) -> str:
    """Chặn path traversal cho 1 segment (slug project/tên track) tới từ HTTP body — cùng tinh
    thần `Path(...).name` đã dùng cho `music_preset` legacy, nhưng raise rõ ràng thay vì âm
    thầm cắt còn phần cuối như `Path("../../etc/passwd").name` -> "passwd"."""
    seg = (seg or "").strip()
    if not seg or seg in (".", "..") or seg != Path(seg).name:
        raise NodeJobError(f"tên nhạc nền không hợp lệ: {seg!r}")
    return seg


def _sanitize_video_name(name: str | None) -> str | None:
    """Chuẩn hoá tên video người dùng nhập thành tên file an toàn (bỏ ký tự nguy hiểm cho
    path/shell, khoảng trắng -> underscore, giới hạn độ dài). Trả None nếu không nhập hoặc
    sau khi lọc rỗng — giữ nguyên hành vi cũ (đặt tên theo pipeline_id) khi đó."""
    if not name or not name.strip():
        return None
    cleaned = re.sub(r"[/\\\x00-\x1f]", "", name.strip())
    cleaned = re.sub(r"\s+", "_", cleaned)[:60]
    return cleaned or None

YTDLP_URL = os.environ.get("YTDLP_URL", "http://reup-ytdlp-node-api:8000")
TRANSCRIBE_URL = os.environ.get("TRANSCRIBE_URL", "http://reup-transcribe-node-api:8000")
TRANSLATE_URL = os.environ.get("TRANSLATE_URL", "http://reup-translate-node-api:8000")
TTS_URL = os.environ.get("TTS_URL", "http://reup-tts-gpu-node-api:8000")
EDITOR_URL = os.environ.get("EDITOR_URL", "http://reup-editor-node-api:8000")

NODES = {
    "ytdlp": {"downloads": Path("/nodes/ytdlp/downloads")},
    "transcribe": {"source": Path("/nodes/transcribe/source"), "outputs": Path("/nodes/transcribe/outputs")},
    "translate": {"source": Path("/nodes/translate/source"), "outputs": Path("/nodes/translate/outputs")},
    "tts": {"source": Path("/nodes/tts/source"), "outputs": Path("/nodes/tts/outputs")},
    "editor": {"source": Path("/nodes/editor/source"), "outputs": Path("/nodes/editor/outputs")},
}
OWN_OUTPUTS = Path("/outputs")


def _copy(src: Path, dst_dir: Path, dst_name: str | None = None) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / (dst_name or src.name)
    shutil.copy2(src, dst)
    return dst


def run_pipeline(pipeline_id: str, payload: dict, conn=None, resume_stages: dict | None = None) -> dict:
    """Wrapper ghi log tổng kết pipeline (`pipeline_done`/`pipeline_failed`, kèm `stages` thu
    thập được dù thành công hay lỗi giữa chừng) — logic thật nằm ở `_run_pipeline_impl()`.
    `conn` (Redis client của worker.py) dùng để ghi `current_stage` real-time vào job hash —
    cho phép UI hiển thị "đang xử lý ở node nào" ngay cả khi job còn `started` (`result` đầy đủ
    chỉ có khi xong hết cả pipeline). `resume_stages` (từ `partial_stages` của lần fail trước,
    xem `POST /pipelines/{id}/retry`) cho các bước đã `finished` được bỏ qua, không tải/xử lý
    lại từ đầu — xem kiểm tra resume trong `_run_stage()`.

    `stages` được gắn vào exception (thuộc tính `.stages`) trước khi raise lại — để
    `worker.py` lấy được tiến độ MỚI NHẤT (không chỉ log JSONL) mà lưu vào `mark_failed()`,
    làm cơ sở cho lần retry tiếp theo."""
    stages: dict[str, dict] = {}
    video_name_for_log = _sanitize_video_name(payload.get("video_name"))
    try:
        result = _run_pipeline_impl(pipeline_id, payload, stages, conn, resume_stages)
        q.log_event({
            "pipeline_id": pipeline_id, "video_name": result.get("video_name"),
            "event": "pipeline_done", "url": payload.get("url"), "mode": payload.get("mode"),
            "stages": stages,
        })
        return result
    except PipelineCancelled as e:
        q.log_event({
            "pipeline_id": pipeline_id, "video_name": video_name_for_log,
            "event": "pipeline_cancelled", "url": payload.get("url"), "mode": payload.get("mode"),
            "stages": stages,
        })
        e.stages = stages  # type: ignore[attr-defined]
        raise
    except Exception as e:
        q.log_event({
            "pipeline_id": pipeline_id, "video_name": video_name_for_log,
            "event": "pipeline_failed", "url": payload.get("url"), "mode": payload.get("mode"),
            "stages": stages, "error": str(e),
        })
        e.stages = stages  # type: ignore[attr-defined]
        raise


def _run_pipeline_impl(
    pipeline_id: str, payload: dict, stages: dict[str, dict], conn=None, resume_stages: dict | None = None,
) -> dict:
    resume_stages = resume_stages or {}
    mode = payload.get("mode", "review")
    if mode not in ("review", "dialogue", "subtitle", "audio", "video"):
        raise ValueError(f"mode không hợp lệ: {mode}")
    url = payload["url"]
    # Client thường paste nguyên văn bảng share (đt Douyin/TikTok...) kèm rác quanh link
    # (vd "3.82 复制打开抖音... https://v.douyin.com/xxx/ v@s.RX...") — tách link http(s) đầu
    # tiên ra trước khi đưa cho yt-dlp, tránh lỗi "[generic] '<toàn bộ chuỗi>' is not a valid URL".
    url_match = re.search(r'https?://\S+', url)
    if url_match:
        url = url_match.group(0)
    align = bool(payload.get("align", False))
    source_lang = payload.get("source_lang", "zh")
    if source_lang not in ("zh", "other"):
        raise ValueError(f"source_lang không hợp lệ: {source_lang}")
    voice = payload.get("voice")
    style = payload.get("style")
    ref_audio_b64 = payload.get("ref_audio_b64")
    ref_audio_ext = payload.get("ref_audio_ext") or "wav"
    subtitle_mode = payload.get("subtitle_mode", "burn")
    sub_style = payload.get("sub_style")
    target_lang = payload.get("target_lang", "tiếng Việt")
    batch_size = int(payload.get("batch_size", 20))

    # Tên chuẩn xuyên suốt pipeline: nếu người dùng đặt tên video, dùng làm prefix cho mọi
    # file trung gian (kèm 8 ký tự đầu pipeline_id để không đụng file giữa các lần chạy trên
    # cùng dir chia sẻ của từng node) và làm tên file xuất cuối (final.mp4 -> <tên>.mp4). Không
    # đặt tên -> giữ nguyên hành vi cũ (prefix = pipeline_id, xuất "final.mp4"/"final.srt").
    video_name = _sanitize_video_name(payload.get("video_name"))
    stem = f"{video_name}_{pipeline_id[:8]}" if video_name else pipeline_id
    # File xuất cuối luôn có tag nhánh (review_/dialogue_) đứng trước tên video — dễ phân biệt
    # 2 nhánh khi nhìn thẳng vào tên file, không cần mở JSON payload.
    export_stem = f"{mode}_{video_name}" if video_name else f"{mode}_final"

    def _resumed_result(name: str, check_path: Path | None) -> dict | None:
        """Trả `result` cũ nếu stage `name` đã `finished` ở lần chạy trước (đọc từ
        `resume_stages`, tức `partial_stages` lưu lúc fail) VÀ file output tương ứng vẫn còn
        tồn tại trên đĩa (an toàn trước trường hợp bị dọn tay giữa 2 lần thử) — None nếu không
        đủ điều kiện, gọi node chạy lại bình thường."""
        entry = resume_stages.get(name)
        if not entry or entry.get("status") != "finished":
            return None
        if check_path is not None and not check_path.exists():
            return None
        return entry.get("result")

    def _mark_resumed(name: str, result: dict) -> None:
        stages[name] = {"status": "finished", "result": result, "elapsed_s": 0.0, "resumed": True}
        if conn is not None:
            q.update_stage(conn, pipeline_id, name)
            q.save_stage_progress(conn, pipeline_id, stages)
        q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                     "event": "stage_skipped_resume", "stage": name})

    def _run_stage(name: str, base_url: str, body: dict, resume_check: Path | None = None) -> dict:
        resumed = _resumed_result(name, resume_check)
        if resumed is not None:
            _mark_resumed(name, resumed)
            return resumed
        # Kiểm tra cờ huỷ NGAY TRƯỚC lúc gọi node con — cooperative cancel, không dừng được bước
        # đang chạy dở (xem q.request_cancel docstring), chỉ chặn không cho pipeline tiến thêm
        # bước mới. Đặt sau nhánh resume ở trên để không huỷ oan 1 bước vốn đã xong, miễn phí.
        if conn is not None and q.is_cancel_requested(conn, pipeline_id):
            raise PipelineCancelled(f"huỷ trước khi chạy bước '{name}'")
        # Gắn correlation ID vào MỌI stage tại đây (1 chỗ duy nhất) — để log của node con
        # (translate/transcribe/tts/editor/ytdlp) tra ngược được về đúng pipeline_id/video_name
        # khi debug lỗi chất lượng (xem plan "Structured log 2 ngày").
        body = {**body, "pipeline_id": pipeline_id, "video_name": video_name}
        if conn is not None:
            q.update_stage(conn, pipeline_id, name)
        t0 = time.time()

        def _persist_started(job_id: str) -> None:
            # Ghi job_id NGAY sau khi submit xong, TRƯỚC lúc poll — nếu lần chạy này bị cắt
            # ngang giữa chừng (orchestrator crash, hoặc lỗi mạng dài hơn cả ngân sách retry
            # trong node_client), lần retry kế tiếp nối lại ĐÚNG job này (nhánh resume_job_id bên
            # dưới) thay vì submit job mới — job cũ có thể đã/đang chạy xong, tránh tốn thêm 1
            # lượt GPU vô ích (bug thật gặp: pipeline d2bd708a bị 'failed' do lỗi mạng thoáng qua
            # trong lúc tts-gpu-node-worker đang chạy job vẫn xong bình thường, nhưng resume trước
            # đó sẽ không biết mà chạy lại từ đầu).
            stages[name] = {"status": "started", "job_id": job_id, "elapsed_s": None}
            if conn is not None:
                q.save_stage_progress(conn, pipeline_id, stages)

        try:
            resume_entry = resume_stages.get(name)
            if resume_entry and resume_entry.get("status") == "started" and resume_entry.get("job_id"):
                # Lần chạy trước đã submit job này rồi nhưng không rõ kết quả (xem docstring
                # _persist_started) — nối lại đúng job_id đó, KHÔNG submit job mới. Nếu job_id
                # không còn tồn tại nữa ở node con (TTL 48h hết, hiếm khi resume trong khung đó),
                # `wait_for_job` raise NodeJobError như lỗi thật, không tự fallback sang submit
                # mới ở đây — tránh double-submit ẩn, để lần retry kế tiếp (do người dùng bấm)
                # tự nhiên rơi vào nhánh submit mới vì entry cũ đã bị ghi đè thành "failed".
                result = wait_for_job(base_url, resume_entry["job_id"])
            else:
                result = submit_and_wait(base_url, body, on_submitted=_persist_started)
            stages[name] = {"status": "finished", "result": result, "elapsed_s": round(time.time() - t0, 1)}
            if conn is not None:
                q.save_stage_progress(conn, pipeline_id, stages)
                # Đếm token Deepseek NGAY ĐÂY (nhánh vừa chạy live) chứ không phải ở nơi gọi
                # `_run_stage("translate", ...)` — nhánh resume ở đầu hàm return sớm trước khi
                # tới đây, nên đặt ở đây tự động tránh đếm trùng khi retry 1 job đã dịch xong.
                if name == "translate":
                    total_tokens = ((result or {}).get("usage") or {}).get("total_tokens")
                    if isinstance(total_tokens, (int, float)):
                        q.add_translate_tokens(conn, int(total_tokens))
            return result
        except NodeCancelled as e:
            # Node con kill được tiến trình thật giữa chừng (hiện chỉ `ytdlp`, xem
            # reup-ytdlp-node/scripts/worker.py) — báo 'cancelled' cho cả pipeline thay vì
            # 'failed', khác nhánh NodeJobError bên dưới.
            stages[name] = {"status": "cancelled", "error": str(e), "elapsed_s": round(time.time() - t0, 1)}
            raise PipelineCancelled(f"bước '{name}' bị huỷ giữa chừng: {e}") from e
        except NodeJobError as e:
            stages[name] = {"status": "failed", "error": str(e), "elapsed_s": round(time.time() - t0, 1)}
            raise
        except Exception as e:
            # Lỗi không lường trước, không phải NodeJobError/NodeCancelled (vd JSONDecodeError,
            # lỗi copy file...). TRƯỚC ĐÂY loại lỗi này bay thẳng ra ngoài mà KHÔNG được ghi vào
            # `stages` — bug thật gặp: 1 ConnectionError thô lúc poll làm bước 'tts' biến mất
            # hoàn toàn khỏi `partial_stages` của pipeline d2bd708a, dù job con vẫn chạy dở. Bọc
            # lại thành NodeJobError để nhánh trên luôn ghi được "failed", đảm bảo resume sau này
            # luôn biết chính xác bước cuối cùng đã thử tới đâu.
            stages[name] = {"status": "failed", "error": str(e), "elapsed_s": round(time.time() - t0, 1)}
            raise NodeJobError(str(e)) from e

    # 1. ytdlp — ép output đặt tên theo pipeline_id (ytdlp_runner chỉ wrap subprocess, không
    # trả "output" trong result) để glob tìm lại chắc chắn. Resume: nếu stage này đã "finished"
    # ở lần trước VÀ file tải về (đặt tên theo pipeline_id, không đổi khi resume) vẫn còn trên
    # đĩa -> bỏ qua tải lại, đúng tinh thần "không lãng phí tài nguyên đã xử lý xong".
    existing_matches = sorted(glob.glob(str(NODES["ytdlp"]["downloads"] / f"{pipeline_id}.*")))
    resumed_ytdlp = _resumed_result("ytdlp", None) if existing_matches else None
    if resumed_ytdlp is not None:
        _mark_resumed("ytdlp", resumed_ytdlp)
        matches = existing_matches
    else:
        ytdlp_args = list(payload.get("ytdlp_args") or [])
        # mode="audio": ép yt-dlp tự trích + convert mp3 ngay lúc tải (ffmpeg có sẵn trong
        # image ytdlp-node) — file glob ra dưới sẽ có đuôi .mp3 luôn, không cần bước riêng.
        if mode == "audio":
            ytdlp_args = ["-x", "--audio-format", "mp3"] + ytdlp_args
        out_template = f"/downloads/{pipeline_id}.%(ext)s"
        ytdlp_args = ["-o", out_template] + ytdlp_args + [url]
        _run_stage("ytdlp", YTDLP_URL, {"args": ytdlp_args})
        matches = sorted(glob.glob(str(NODES["ytdlp"]["downloads"] / f"{pipeline_id}.*")))
    if not matches:
        raise NodeJobError(f"ytdlp không sinh ra file nào khớp {pipeline_id}.*")
    source_video = Path(matches[0])

    # mode="audio"/"video": dừng ngay sau ytdlp — không qua transcribe/translate/tts/editor.
    # Trả sớm tại đây để 3 nhánh review/dialogue/subtitle bên dưới không bị đụng.
    if mode == "audio":
        job_out_dir = OWN_OUTPUTS / pipeline_id
        final_out = _copy(source_video, job_out_dir, f"{export_stem}.mp3")
        return {"ok": True, "output": str(final_out), "video_name": video_name, "stages": stages}
    # mode="video": y hệt "audio" nhưng KHÔNG ép `-x --audio-format mp3` ở bước ytdlp phía trên
    # (nhánh `if mode == "audio"` riêng đó không khớp "video" nên yt-dlp tải nguyên file gốc) —
    # giữ đúng đuôi file yt-dlp tải về (thường .mp4, có thể .webm/.mkv tuỳ nguồn) thay vì áp cứng
    # ".mp4", tránh copy nhầm đuôi cho 1 file thật ra không phải mp4.
    if mode == "video":
        job_out_dir = OWN_OUTPUTS / pipeline_id
        final_out = _copy(source_video, job_out_dir, f"{export_stem}{source_video.suffix}")
        return {"ok": True, "output": str(final_out), "video_name": video_name, "stages": stages}

    # 2. transcribe
    transcribe_in = _copy(source_video, NODES["transcribe"]["source"], f"{stem}_source{source_video.suffix}")
    transcribe_out_name = f"{stem}_transcript.json"
    # "subtitle" là mode riêng của orchestrator (chỉ mux sub, giữ audio gốc) — transcribe-node
    # chỉ biết "review"/"dialogue" (api.py validate chặt), và hành vi cần ở đây giống hệt
    # "review" (không tách nguồn, không cần instrumental/original) nên map xuống "review".
    transcribe_mode = "review" if mode == "subtitle" else mode
    transcribe_result = _run_stage("transcribe", TRANSCRIBE_URL, {
        "input": f"/source/{transcribe_in.name}",
        "output": f"/outputs/{transcribe_out_name}",
        "align": align,
        "mode": transcribe_mode,
        "source_lang": source_lang,
    }, resume_check=NODES["transcribe"]["outputs"] / transcribe_out_name)
    transcript_path = NODES["transcribe"]["outputs"] / transcribe_out_name

    # 3. translate
    translate_in = _copy(transcript_path, NODES["translate"]["source"])
    translated_name = f"{stem}_translated.json"
    translate_result = _run_stage("translate", TRANSLATE_URL, {
        "input": f"/source/{translate_in.name}",
        "output": f"/outputs/{translated_name}",
        "target_lang": target_lang,
        "batch_size": batch_size,
    }, resume_check=NODES["translate"]["outputs"] / translated_name)
    translated_path = NODES["translate"]["outputs"] / translated_name
    # Tóm tắt bối cảnh video (đã tính sẵn lúc dịch để context-aware, xem
    # reup-translate-node/scripts/translate_cli.py::summarize_video_context) — trả kèm ra
    # output cuối làm mô tả video khi đăng bài, không tốn thêm lệnh gọi Deepseek nào.
    video_context = translate_result.get("video_context") or ""

    # 4. tts-gpu segments — BỎ QUA cho mode="subtitle": nhánh này giữ nguyên giọng gốc, không
    # cần TTS, tiết kiệm GPU/thời gian đúng lý do nhánh này tồn tại (chỉ thêm sub).
    track_path: Path | None = None
    tts_windows_path: Path | None = None
    if mode != "subtitle":
        tts_in = _copy(translated_path, NODES["tts"]["source"])
        track_name = f"{stem}_track.wav"
        tts_params = {"input_path": f"/source/{tts_in.name}", "output_path": f"/outputs/{track_name}"}
        if ref_audio_b64:
            ref_name = f"{stem}_ref_audio.{ref_audio_ext}"
            ref_dst = NODES["tts"]["source"] / ref_name
            ref_dst.parent.mkdir(parents=True, exist_ok=True)
            ref_dst.write_bytes(base64.b64decode(ref_audio_b64))
            tts_params["ref_audio"] = f"/source/{ref_name}"
        elif voice:
            tts_params["voice"] = voice
        if style:
            tts_params["style"] = style
        tts_result = _run_stage("tts", TTS_URL, {"cmd": "segments", "params": tts_params},
                                 resume_check=NODES["tts"]["outputs"] / track_name)
        track_path = NODES["tts"]["outputs"] / track_name
        # *_windows.json (segments_cli.py) — vị trí THẬT từng segment sau cascading placement,
        # chỉ nhánh dialogue cần (mix-dialogue mute/duck audio gốc theo đúng chỗ TTS thật sự
        # đứng, xem load_mix_windows() bên mix_dialogue_cli.py). File luôn được tạo (kể cả
        # review) nhưng review không dùng tới — không cần if mode== ở đây, đơn giản hơn.
        windows_container_path = tts_result.get("windows_path")
        if windows_container_path:
            candidate = NODES["tts"]["outputs"] / Path(windows_container_path).name
            if candidate.is_file():
                tts_windows_path = candidate

    # 5. editor: srt trước (dùng chung cả 3 nhánh)
    editor_translated = _copy(translated_path, NODES["editor"]["source"])
    editor_track = _copy(track_path, NODES["editor"]["source"]) if track_path is not None else None
    editor_video = _copy(source_video, NODES["editor"]["source"], f"{stem}_source{source_video.suffix}")
    # Copy *_windows.json SỚM (trước khi gọi editor_srt) — trước đây chỉ copy muộn, ngay trước
    # bước mix-dialogue, nên sub luôn sinh từ timestamp GỐC dù TTS đã bị "cascading placement"
    # đẩy trễ. Bug thật phát hiện khi soi video Douyin test (2026-08-01): cuối video sub hiện
    # TRƯỚC audio TTS tới 3.92s (174/210 segment bị đẩy trễ cộng dồn) — cùng lớp bug đã fix cho
    # mix-dialogue (load_mix_windows()) nhưng chưa từng áp dụng cho SRT ở CẢ 2 nhánh review lẫn
    # dialogue. subtitle-mode không có TTS nên tts_windows_path luôn None, không đổi gì ở đó.
    editor_windows_name: str | None = None
    if tts_windows_path is not None:
        editor_windows = _copy(tts_windows_path, NODES["editor"]["source"])
        editor_windows_name = editor_windows.name
    # Tag nhánh (review_/dialogue_) đứng trước tên video áp dụng luôn cho file final.mp4/.srt
    # SINH RA Ở EDITOR-NODE (không chỉ bản copy đổi tên ở `job_out_dir` bên dưới) — trước đây
    # chỉ bản copy cuối cùng có tag, còn file gốc trong data/outputs của editor-node (vd
    # "video_20260722_1_33bc40cb_final.mp4") không có, dễ nhầm là thiếu tag khi xem trực tiếp
    # thư mục output của editor-node thay vì qua UI.
    srt_name = f"{mode}_{stem}_final.srt"
    srt_params = {"input_path": f"/source/{editor_translated.name}", "output_path": f"/outputs/{srt_name}"}
    if editor_windows_name is not None:
        srt_params["windows"] = f"/source/{editor_windows_name}"
    _run_stage("editor_srt", EDITOR_URL, {
        "cmd": "srt",
        "params": srt_params,
    }, resume_check=NODES["editor"]["outputs"] / srt_name)
    srt_path = NODES["editor"]["outputs"] / srt_name
    subtitles_param = f"/outputs/{srt_name}"  # editor container tự đọc lại /outputs của chính nó

    # subtitle: giữ nguyên audio gốc — trỏ audio_for_edit về CHÍNH file video gốc, ffmpeg
    # (edit_cli.py) tự lấy stream audio riêng từ input thứ 2 dù đó là cùng 1 file với video.
    # review/dialogue: mặc định track TTS, có thể bị 2 nhánh elif bên dưới ghi đè thêm.
    audio_for_edit = f"/source/{editor_video.name}" if mode == "subtitle" else f"/source/{editor_track.name}"
    if mode == "dialogue":
        instrumental_container_path = transcribe_result.get("instrumental")
        original_container_path = transcribe_result.get("original")
        if not instrumental_container_path or not original_container_path:
            raise NodeJobError("transcribe mode=dialogue không trả instrumental/original trong result")
        instrumental_path = NODES["transcribe"]["outputs"] / Path(instrumental_container_path).name
        original_path = NODES["transcribe"]["outputs"] / Path(original_container_path).name
        editor_instrumental = _copy(instrumental_path, NODES["editor"]["source"])
        editor_original = _copy(original_path, NODES["editor"]["source"])
        final_track_name = f"{stem}_final_track.wav"
        mix_dialogue_params = {
            "original": f"/source/{editor_original.name}",
            "instrumental": f"/source/{editor_instrumental.name}",
            "tts": f"/source/{editor_track.name}",
            "transcript": f"/source/{editor_translated.name}",
            "output": f"/outputs/{final_track_name}",
        }
        if editor_windows_name is not None:
            mix_dialogue_params["windows"] = f"/source/{editor_windows_name}"
        _run_stage("editor_mix_dialogue", EDITOR_URL, {
            "cmd": "mix-dialogue",
            "params": mix_dialogue_params,
        }, resume_check=NODES["editor"]["outputs"] / final_track_name)
        audio_for_edit = f"/outputs/{final_track_name}"
    elif mode == "review":
        # Nhạc nền CHỈ áp dụng nhánh review (dialogue đã có instrumental tách từ audio gốc,
        # không cần thêm nhạc ngoài). music_b64 (người dùng tự upload) ưu tiên hơn
        # music_preset (chọn từ file có sẵn trong /music của editor-node) nếu cả 2 cùng gửi.
        music_b64 = payload.get("music_b64")
        music_ext = (payload.get("music_ext") or "mp3").lstrip(".")
        music_project = payload.get("music_project")
        music_track = payload.get("music_track")
        music_preset = payload.get("music_preset")
        music_level = payload.get("music_level")

        music_src_param: str | None = None
        if music_b64:
            music_name = f"{stem}_music.{music_ext}"
            music_dst = NODES["editor"]["source"] / music_name
            music_dst.parent.mkdir(parents=True, exist_ok=True)
            music_dst.write_bytes(base64.b64decode(music_b64))
            music_src_param = f"/source/{music_name}"
        elif music_project and music_track:
            # Thư viện nhạc theo project/theme (mới) — nằm sẵn trong /music (read-only) NGAY
            # TRONG container editor, dùng thẳng path đó như music_preset, không copy qua
            # /source. Ưu tiên HƠN music_preset (legacy phẳng) nếu cả 2 cùng gửi.
            music_src_param = f"/music/{_safe_music_seg(music_project)}/{_safe_music_seg(music_track)}"
        elif music_preset:
            # Preset nằm sẵn trong /music (read-only) NGAY TRONG container editor — dùng
            # thẳng path đó, không copy qua /source. Path(...).name chặn path traversal
            # (vd "../../etc/passwd" -> chỉ còn "passwd") vì field này tới từ HTTP body.
            music_src_param = f"/music/{Path(music_preset).name}"

        if music_src_param:
            music_track_name = f"{stem}_music_track.wav"
            mix_music_params = {
                "tts": f"/source/{editor_track.name}",
                "music": music_src_param,
                "output": f"/outputs/{music_track_name}",
            }
            if music_level is not None:
                mix_music_params["music_level"] = music_level
            _run_stage("editor_mix_music", EDITOR_URL, {
                "cmd": "mix-music", "params": mix_music_params,
            }, resume_check=NODES["editor"]["outputs"] / music_track_name)
            audio_for_edit = f"/outputs/{music_track_name}"

    final_name = f"{mode}_{stem}_final.mp4"
    _run_stage("editor_edit", EDITOR_URL, {
        "cmd": "edit",
        "params": {
            "video": f"/source/{editor_video.name}",
            "audio": audio_for_edit,
            "output": f"/outputs/{final_name}",
            "subtitles": subtitles_param,
            "subtitle_mode": subtitle_mode,
            "sub_style": sub_style,
        },
    }, resume_check=NODES["editor"]["outputs"] / final_name)
    final_video_path = NODES["editor"]["outputs"] / final_name

    job_out_dir = OWN_OUTPUTS / pipeline_id
    final_out = _copy(final_video_path, job_out_dir, f"{export_stem}.mp4")
    _copy(srt_path, job_out_dir, f"{export_stem}.srt")
    if video_context:
        (job_out_dir / f"{export_stem}_mo_ta.txt").write_text(video_context, encoding="utf-8")

    return {
        "ok": True, "output": str(final_out), "video_name": video_name, "stages": stages,
        "video_context": video_context,
    }
