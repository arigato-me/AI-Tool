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
import json
import os
import re
import shutil
import subprocess
import tempfile
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
OCR_URL = os.environ.get("OCR_URL", "http://ocr-node-api:8000")

NODES = {
    "ytdlp": {"downloads": Path("/nodes/ytdlp/downloads")},
    "transcribe": {"source": Path("/nodes/transcribe/source"), "outputs": Path("/nodes/transcribe/outputs")},
    "translate": {"source": Path("/nodes/translate/source"), "outputs": Path("/nodes/translate/outputs")},
    "tts": {"source": Path("/nodes/tts/source"), "outputs": Path("/nodes/tts/outputs")},
    "editor": {"source": Path("/nodes/editor/source"), "outputs": Path("/nodes/editor/outputs")},
    "ocr": {"source": Path("/nodes/ocr/source"), "outputs": Path("/nodes/ocr/outputs")},
}
OWN_OUTPUTS = Path("/outputs")

NARRATOR_SPEAKER = "narrator"

# mode="mix" — sàn tối thiểu cho ảnh khi chia đều audio_duration cho N ảnh chưa nhập duration
# (xem _run_mix_pipeline) — tránh 0s/âm nếu tổng duration đã nhập tay của các ảnh khác đã vượt
# quá audio (hiếm nhưng có thể, người dùng nhập tay sai).
MIN_IMAGE_DURATION_FALLBACK_S = 3.0


def _get_voice_pool() -> list[str]:
    """Danh sách id giọng có sẵn — dùng để auto gán giọng cho từng nhân vật (mode="book",
    xem _assign_speaker_voices() bên dưới). Lỗi (tts-gpu-node down...) trả rỗng, để caller tự
    fallback về giọng mặc định thay vì phá cả pipeline chỉ vì bước phụ trợ này."""
    try:
        result = submit_and_wait(TTS_URL, {"cmd": "list-voices", "params": {}}, timeout=60)
    except NodeJobError:
        return []
    return [v["id"] for v in result.get("voices", []) if v.get("id")]


def _assign_speaker_voices(segments: list[dict], default_voice: str | None) -> dict[str, str]:
    """Map speaker ('narrator' hoặc tên nhân vật, gán bởi reup-translate-node::tag_speakers) ->
    voice id. `narrator` luôn dùng `default_voice` (giọng người dùng chọn trên form, hoặc giọng
    đầu tiên trong pool nếu không chọn) — các nhân vật khác auto gán round-robin từ pool giọng
    còn lại (KHÔNG có UI để người dùng tự gán tay từng nhân vật ở bản MVP này — thêm sau nếu cần
    đích danh 1 giọng cho nhân vật chính). Hết pool thì vòng lại từ đầu, chấp nhận trùng giọng
    giữa các nhân vật phụ."""
    pool = _get_voice_pool()
    narrator_voice = default_voice or (pool[0] if pool else "")
    other_pool = [v for v in pool if v != narrator_voice] or pool or ([default_voice] if default_voice else [])

    speaker_voice: dict[str, str] = {NARRATOR_SPEAKER: narrator_voice}
    seen_others: list[str] = []
    for seg in segments:
        sp = seg.get("speaker") or NARRATOR_SPEAKER
        if sp != NARRATOR_SPEAKER and sp not in seen_others:
            seen_others.append(sp)
    for i, sp in enumerate(seen_others):
        speaker_voice[sp] = other_pool[i % len(other_pool)] if other_pool else narrator_voice
    return speaker_voice


# ~2 trang giấy A4 (~3000 ký tự/trang, ước lượng phổ biến trong xuất bản) — ngưỡng để quyết
# định có trả thẳng nội dung text trong result JSON (reup-ui hiện box đọc ngay) hay không (sách
# dài thì chỉ có file .txt tải riêng, không nhét text lớn vào Redis job hash).
BOOK_TEXT_PREVIEW_MAX_CHARS = 6000


def _format_book_text(segments: list[dict]) -> str:
    """Ghép segments đã dịch+gán vai thành 1 văn bản đọc được — dòng của narrator để trần,
    dòng nhân vật khác có nhãn `[Tên]` đứng trước để dễ theo dõi ai đang nói (khớp mp3 nhiều
    giọng, không phải chỉ 1 giọng đọc thẳng)."""
    lines = []
    for seg in segments:
        speaker = seg.get("speaker") or NARRATOR_SPEAKER
        text = seg.get("text", "")
        if speaker == NARRATOR_SPEAKER:
            lines.append(text)
        else:
            lines.append(f"[{speaker}] {text}")
    return "\n".join(lines)


def _concat_wavs_to_mp3(wav_paths: list[Path], output_mp3: Path) -> None:
    """Nối các file wav (mỗi segment 1 file, xem bước tts trong nhánh mode="book") thành 1 mp3
    cuối — ffmpeg concat demuxer + re-encode `libmp3lame` (không dùng `-c copy`, tránh yêu cầu
    mọi input phải khớp y hệt sample rate/kênh — ffmpeg tự decode từng file trước khi encode lại
    1 lần). Không chèn khoảng lặng giữa các đoạn (đơn giản nhất chạy được trước — thêm nếu nghe
    bị cụt/gấp giữa các câu)."""
    output_mp3.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as list_file:
        for wav in wav_paths:
            list_file.write(f"file '{wav.resolve()}'\n")
        list_path = list_file.name
    try:
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
               "-c:a", "libmp3lame", "-q:a", "2", str(output_mp3)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            raise NodeJobError(f"ffmpeg concat (nhánh sách) lỗi: {proc.stderr[-2000:]}")
    finally:
        Path(list_path).unlink(missing_ok=True)


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


def _make_run_stage(
    pipeline_id: str, video_name: str | None, stages: dict[str, dict], conn, resume_stages: dict,
):
    """Factory trả về `(_run_stage, _resumed_result, _mark_resumed)` — tách ra thành hàm dùng
    chung cho cả `_run_pipeline_impl()` (5 bước video review/dialogue/subtitle) và
    `_run_book_pipeline()` (nhánh sách->audio, xem bên dưới) thay vì định nghĩa lặp lại 2 lần
    logic resume/cancel/persist-job-id/error-mapping giống hệt nhau. `_resumed_result`/
    `_mark_resumed` được trả riêng (không chỉ gói trong `_run_stage`) vì bước `ytdlp` cần gọi
    trực tiếp 2 hàm này — resume check của nó dựa trên glob file đã tải (không phải 1 path cố
    định), không đi qua tham số `resume_check` chuẩn của `_run_stage`."""

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
        # (translate/transcribe/tts/editor/ytdlp/ocr) tra ngược được về đúng pipeline_id/video_name
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

    return _run_stage, _resumed_result, _mark_resumed


def _run_book_pipeline(
    pipeline_id: str, payload: dict, stages: dict[str, dict], conn=None, resume_stages: dict | None = None,
) -> dict:
    """Nhánh "Sách → Audio" (mode="book") — input là 1 HOẶC NHIỀU file pdf/docx/pptx/xlsx/ảnh
    upload tay (`documents`: list `[{"data_b64","ext"}, ...]`, xem `_merge_transcripts()`), KHÔNG
    có url/video nguồn — hợp trường hợp sách chụp nhiều ảnh (1 ảnh/trang). Chuỗi bước: `ocr-node`
    gọi TUẦN TỰ từng file (trích text, xem ocr-node/README.md) -> gộp transcript theo đúng thứ
    tự file -> `reup-translate-node` (dịch + gán vai `tag_speakers=True`, xem
    translate_cli.py::tag_speakers) -> `reup-tts-gpu-node` gọi TUẦN TỰ subcommand `tts` cho TỪNG
    segment (đổi `voice` theo speaker->voice map, xem `_assign_speaker_voices()`) -> nối tất cả
    wav bằng ffmpeg (`_concat_wavs_to_mp3()`) thành 1 file mp3 cuối. KHÔNG qua `reup-editor-node`
    (không có video để mux/burn sub) — final output là mp3 thẳng.

    Chưa hỗ trợ clone giọng (`ref_audio_b64`) ở nhánh này — giọng clone chỉ là 1 giọng, không
    dùng round-robin nhiều nhân vật được, để sau nếu thật sự cần."""
    resume_stages = resume_stages or {}
    documents = payload.get("documents") or []
    if not documents:
        raise ValueError("mode='book' cần 'documents' (list [{data_b64, ext}], ít nhất 1 file)")
    ocr_lang = payload.get("ocr_lang", "vi")
    if ocr_lang not in ("vi", "en", "fr"):
        raise ValueError(f"ocr_lang không hợp lệ: {ocr_lang}")
    voice = payload.get("voice")
    style = payload.get("style")
    target_lang = payload.get("target_lang", "tiếng Việt")
    batch_size = int(payload.get("batch_size", 20))

    video_name = _sanitize_video_name(payload.get("video_name"))
    stem = f"{video_name}_{pipeline_id[:8]}" if video_name else pipeline_id
    export_stem = f"book_{video_name}" if video_name else "book_final"
    job_out_dir = OWN_OUTPUTS / pipeline_id

    _run_stage, _, _ = _make_run_stage(pipeline_id, video_name, stages, conn, resume_stages)

    # 1+2. Mỗi file: ghi vào /source của ocr-node (idempotent, resume không ghi đè/tải lại) +
    # copy 1 bản sang /outputs của CHÍNH pipeline này (để reup-ui hiển thị lại ảnh/file đã import
    # — khác /source của ocr-node, thư mục đó không được mount phục vụ HTTP cho reup-ui) + gọi
    # ocr-node trích text — mỗi file 1 stage riêng (`ocr_00`, `ocr_01`, ...) nên resume/cancel
    # per-file, giống hệt cơ chế `tts_seg_NNNN` bên dưới.
    transcript_paths: list[Path] = []
    source_files: list[str] = []
    for i, doc in enumerate(documents):
        ext = (doc.get("ext") or "").lstrip(".").lower()
        data_b64 = doc.get("data_b64")
        if not ext or not data_b64:
            raise ValueError(f"documents[{i}] thiếu 'ext'/'data_b64'")
        doc_name = f"{stem}_source_{i:02d}.{ext}"
        doc_path = NODES["ocr"]["source"] / doc_name
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        if not doc_path.exists():
            doc_path.write_bytes(base64.b64decode(data_b64))
        source_out = _copy(doc_path, job_out_dir, doc_name)
        source_files.append(str(source_out))

        transcript_name = f"{stem}_transcript_{i:02d}.json"
        _run_stage(f"ocr_{i:02d}", OCR_URL, {
            "input": f"/source/{doc_name}",
            "output": f"/outputs/{transcript_name}",
            "source_lang": ocr_lang,
        }, resume_check=NODES["ocr"]["outputs"] / transcript_name)
        transcript_paths.append(NODES["ocr"]["outputs"] / transcript_name)

    # Gộp transcript nhiều file thành 1 — nối segment ĐÚNG thứ tự file (1 ảnh = 1 trang), dồn
    # timestamp giả liên tục xuyên suốt (không reset về 0 mỗi file) để giữ ý nghĩa "tăng dần" của
    # start/end như tài liệu 1 file, đánh số lại id 1..N cho toàn bộ.
    merged_segments: list[dict] = []
    running_offset = 0.0
    for tp in transcript_paths:
        t_data = json.loads(tp.read_text(encoding="utf-8"))
        t_segments = t_data.get("segments") or []
        for seg in t_segments:
            seg["start"] = round(seg.get("start", 0.0) + running_offset, 2)
            seg["end"] = round(seg.get("end", 0.0) + running_offset, 2)
            merged_segments.append(seg)
        running_offset = merged_segments[-1]["end"] if merged_segments else running_offset
    for new_id, seg in enumerate(merged_segments, start=1):
        seg["id"] = new_id
    if not merged_segments:
        raise NodeJobError("ocr không trả về segment nào cho nhánh sách")

    transcript_name = f"{stem}_transcript.json"
    transcript_path = NODES["ocr"]["outputs"] / transcript_name
    transcript_path.write_text(
        json.dumps({"language": ocr_lang, "duration_s": running_offset, "segments": merged_segments},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 3. translate — LUÔN qua Deepseek dù ocr_lang trùng target_lang (rollback 2026-08-04: từng
    # tắt translate khi ocr_lang="vi" để tiết kiệm token, gây bug thật — text OCR tiếng Việt hay
    # thiếu dấu, bỏ qua Deepseek verify thì TTS đọc thẳng bản không dấu). tag_speakers=True gán
    # narrator/tên nhân vật mỗi segment (xem translate_cli.py::tag_speakers), khác hẳn nhánh
    # video (không bật cờ này).
    translate_in = _copy(transcript_path, NODES["translate"]["source"])
    translated_name = f"{stem}_translated.json"
    _run_stage("translate", TRANSLATE_URL, {
        "input": f"/source/{translate_in.name}",
        "output": f"/outputs/{translated_name}",
        "target_lang": target_lang,
        "batch_size": batch_size,
        "tag_speakers": True,
    }, resume_check=NODES["translate"]["outputs"] / translated_name)
    translated_path = NODES["translate"]["outputs"] / translated_name

    translated_data = json.loads(translated_path.read_text(encoding="utf-8"))
    segments = translated_data.get("segments") or []
    if not segments:
        raise NodeJobError("translate không trả về segment nào cho nhánh sách")

    # 4. Map speaker -> voice — 1 lần duy nhất TRƯỚC khi loop TTS (không hỏi lại /voices mỗi
    # segment). Đọc lại từ `resume_stages` nếu đã tính lúc chạy trước (retry) để speaker->voice
    # KHÔNG đổi giữa các lần thử — đổi map giữa chừng sẽ làm 1 nhân vật đổi giọng nửa chừng sách.
    cached_map = (resume_stages.get("_speaker_voice_map") or {}).get("result")
    if cached_map:
        speaker_voice = cached_map
    else:
        speaker_voice = _assign_speaker_voices(segments, voice)
        stages["_speaker_voice_map"] = {"status": "finished", "result": speaker_voice, "elapsed_s": 0.0}
        if conn is not None:
            q.save_stage_progress(conn, pipeline_id, stages)
    q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                 "event": "book_speaker_voice_map", "map": speaker_voice})

    # 5. TTS tuần tự TỪNG segment — mỗi segment là 1 stage riêng (`tts_seg_NNNN`), tự động thừa
    # hưởng resume/cancel per-segment của `_run_stage()` (sách dở fail giữa chừng, retry chỉ tổng
    # hợp lại từ đúng segment còn thiếu, không synth lại từ đầu). Chậm hơn hẳn cách gọi 1 lần
    # `segments` (timeline-based, không hợp cho sách không có audio gốc) — đánh đổi chấp nhận
    # được để tái dùng nguyên xi contract `cmd="tts"` đã có, không sửa gì reup-tts-gpu-node.
    seg_wavs: list[Path] = []
    for seg in segments:
        seg_id = seg["id"]
        seg_voice = speaker_voice.get(seg.get("speaker") or NARRATOR_SPEAKER, voice)
        text_name = f"{stem}_seg{seg_id:04d}.txt"
        text_path = NODES["tts"]["source"] / text_name
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(seg["text"], encoding="utf-8")
        wav_name = f"{stem}_seg{seg_id:04d}.wav"
        tts_params = {"input_path": f"/source/{text_name}", "output_path": f"/outputs/{wav_name}"}
        if seg_voice:
            tts_params["voice"] = seg_voice
        if style:
            tts_params["style"] = style
        _run_stage(f"tts_seg_{seg_id:04d}", TTS_URL, {"cmd": "tts", "params": tts_params},
                   resume_check=NODES["tts"]["outputs"] / wav_name)
        seg_wavs.append(NODES["tts"]["outputs"] / wav_name)

    # 6. Nối tất cả wav thành 1 mp3 cuối — KHÔNG qua reup-editor-node (không có video/hình).
    final_path = job_out_dir / f"{export_stem}.mp3"
    _concat_wavs_to_mp3(seg_wavs, final_path)

    # 7. Xuất kèm file text (nội dung đã đọc, có nhãn [Tên nhân vật] cho câu thoại — narrator
    # để trần) cạnh mp3 — trước đây chỉ có audio, không có cách nào đọc lại nội dung đã tạo ra
    # mà không nghe hết file. Đặt ngay CẠNH mp3 (không qua node nào khác, ghi trực tiếp).
    text_path = job_out_dir / f"{export_stem}.txt"
    full_text = _format_book_text(segments)
    text_path.write_text(full_text, encoding="utf-8")

    result: dict = {
        "ok": True, "output": str(final_path), "text_output": str(text_path),
        "video_name": video_name, "stages": stages, "source_files": source_files,
    }
    # Đoạn text NGẮN (< ~2 trang giấy) trả thẳng trong JSON để reup-ui hiện ngay 1 box đọc được,
    # khỏi phải tải file .txt riêng — sách dài thì bỏ qua (result đã đủ nặng vì stages/segments,
    # không nhét thêm text lớn vào Redis job hash).
    if len(full_text) <= BOOK_TEXT_PREVIEW_MAX_CHARS:
        result["text"] = full_text
    return result


def _resolve_mix_item(
    kind: str, idx: int, item: dict, stem: str, pipeline_id: str,
    _run_stage, _resumed_result, _mark_resumed,
) -> Path | str:
    """Quy 1 video/audio item (mode="mix") về 1 file cục bộ đã copy sẵn vào /source của
    editor-node (Path), hoặc 1 container-path thẳng (str, chỉ type="library" — track nhạc nền
    đọc thẳng từ /music read-only, không copy qua đâu cả). `kind`: "v" (video_items) | "a"
    (audio_items) — chỉ dùng đặt tên stage/sub-id, tránh đụng độ khi có nhiều item cùng loại
    trong 1 pipeline."""
    item_type = item.get("type")
    if item_type == "url":
        # sub-id riêng mỗi item (không dùng thẳng pipeline_id như bước ytdlp chính) — 1 pipeline
        # mix có thể có nhiều item url, mỗi item cần tên file tải về khác nhau.
        sub_id = f"{pipeline_id}_{kind}{idx}"
        stage_name = f"ytdlp_{kind}{idx}"
        existing = sorted(glob.glob(str(NODES["ytdlp"]["downloads"] / f"{sub_id}.*")))
        resumed = _resumed_result(stage_name, None) if existing else None
        if resumed is not None:
            _mark_resumed(stage_name, resumed)
            matches = existing
        else:
            url_match = re.search(r'https?://\S+', item.get("url") or "")
            if not url_match:
                raise ValueError(f"{kind}_items[{idx}] thiếu url hợp lệ")
            # audio_items type="url": ép yt-dlp tự trích + convert mp3 ngay lúc tải, y hệt
            # mode="audio" ở _run_pipeline_impl — khỏi viết ffmpeg -vn riêng.
            ytdlp_args = ["-x", "--audio-format", "mp3"] if kind == "a" else []
            out_template = f"/downloads/{sub_id}.%(ext)s"
            ytdlp_args = ["-o", out_template] + ytdlp_args + [url_match.group(0)]
            _run_stage(stage_name, YTDLP_URL, {"args": ytdlp_args})
            matches = sorted(glob.glob(str(NODES["ytdlp"]["downloads"] / f"{sub_id}.*")))
        if not matches:
            raise NodeJobError(f"ytdlp không sinh ra file nào khớp {sub_id}.* ({kind}_items[{idx}])")
        src = Path(matches[0])
        return _copy(src, NODES["editor"]["source"], f"{stem}_{kind}{idx:02d}{src.suffix}")
    if item_type == "upload":
        ext = (item.get("ext") or "").lstrip(".").lower()
        data_b64 = item.get("data_b64")
        if not ext or not data_b64:
            raise ValueError(f"{kind}_items[{idx}] thiếu 'ext'/'data_b64'")
        dst = NODES["editor"]["source"] / f"{stem}_{kind}{idx:02d}_upload.{ext}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            dst.write_bytes(base64.b64decode(data_b64))
        return dst
    if item_type == "reuse":
        # Tái dùng file 1 pipeline cũ đã tải (KHÔNG tạo job ytdlp mới) — file vẫn nằm sẵn ở
        # downloads (đặt tên theo pipeline_id, đúng convention bước ytdlp chính bên dưới).
        ref_id = item.get("pipeline_id")
        if not ref_id:
            raise ValueError(f"{kind}_items[{idx}] type='reuse' thiếu pipeline_id")
        matches = sorted(glob.glob(str(NODES["ytdlp"]["downloads"] / f"{ref_id}.*")))
        if not matches:
            raise NodeJobError(f"không tìm thấy file đã tải cho pipeline_id={ref_id!r} ({kind}_items[{idx}])")
        src = Path(matches[0])
        return _copy(src, NODES["editor"]["source"], f"{stem}_{kind}{idx:02d}{src.suffix}")
    if item_type == "library" and kind == "a":
        project, track = item.get("music_project"), item.get("music_track")
        if not project or not track:
            raise ValueError(f"audio_items[{idx}] type='library' thiếu music_project/music_track")
        return f"/music/{_safe_music_seg(project)}/{_safe_music_seg(track)}"
    if item_type == "image" and kind == "v":
        # Ảnh tĩnh -> 1 clip video giữ nguyên ảnh trong `duration` giây (đã được set sẵn ở
        # _run_mix_pipeline TRƯỚC khi gọi hàm này — validate ở api.py hoặc tính mặc định chia
        # đều theo audio nếu cả video_items toàn ảnh). Trả về container-path thẳng trong
        # /outputs của editor-node (giống "library") — KHÔNG phải /source, vì file này do chính
        # editor-node sinh ra (job image-to-video), không phải copy từ orchestrator qua.
        ext = (item.get("ext") or "").lstrip(".").lower()
        data_b64 = item.get("data_b64")
        if not ext or not data_b64:
            raise ValueError(f"{kind}_items[{idx}] type='image' thiếu 'ext'/'data_b64'")
        duration = item.get("duration")
        if not duration or duration <= 0:
            raise ValueError(f"{kind}_items[{idx}] type='image' thiếu duration hợp lệ (> 0)")
        img_name = f"{stem}_{kind}{idx:02d}_image.{ext}"
        img_dst = NODES["editor"]["source"] / img_name
        img_dst.parent.mkdir(parents=True, exist_ok=True)
        if not img_dst.exists():
            img_dst.write_bytes(base64.b64decode(data_b64))
        stage_name = f"image_to_video_{kind}{idx}"
        out_name = f"{stem}_{kind}{idx:02d}_image.mp4"
        _run_stage(stage_name, EDITOR_URL, {
            "cmd": "image-to-video",
            "params": {"image": f"/source/{img_name}", "output": f"/outputs/{out_name}", "duration": duration},
        }, resume_check=NODES["editor"]["outputs"] / out_name)
        return f"/outputs/{out_name}"
    raise ValueError(f"{kind}_items[{idx}].type không hợp lệ: {item_type!r}")


def _run_mix_pipeline(
    pipeline_id: str, payload: dict, stages: dict[str, dict], conn=None, resume_stages: dict | None = None,
) -> dict:
    """mode="mix" — ghép N video nối tiếp + N audio nối tiếp, KHÔNG transcribe/dịch/TTS/sub gì
    cả (khác hẳn review/dialogue/subtitle). Mỗi item video_items/audio_items resolve qua
    `_resolve_mix_item()` (url/upload/reuse/library/image), ghép video/audio riêng bằng 2 cmd mới
    concat-video/concat-audio của editor-node, rồi mux lại bằng đúng cmd="edit" có sẵn — `-shortest`
    đã có sẵn trong `run_edit()` (edit_cli.py) chính là cơ chế "track ngắn hơn thắng" cần, không
    viết lại mux. audio_items resolve + concat-audio chạy TRƯỚC video_items (khác thứ tự bản đầu)
    — cần biết tổng độ dài audio để tính mặc định duration cho ảnh tĩnh (video_items type="image"
    chưa nhập duration, chỉ áp dụng khi TOÀN BỘ video_items là ảnh) trước khi sinh clip từ ảnh."""
    resume_stages = resume_stages or {}
    video_items = payload.get("video_items") or []
    audio_items = payload.get("audio_items") or []
    if not video_items:
        raise ValueError("mode='mix' cần video_items (ít nhất 1)")
    if not audio_items:
        raise ValueError("mode='mix' cần audio_items (ít nhất 1)")

    video_name = _sanitize_video_name(payload.get("video_name"))
    stem = f"{video_name}_{pipeline_id[:8]}" if video_name else pipeline_id
    export_stem = f"mix_{video_name}" if video_name else "mix_final"
    job_out_dir = OWN_OUTPUTS / pipeline_id

    _run_stage, _resumed_result, _mark_resumed = _make_run_stage(pipeline_id, video_name, stages, conn, resume_stages)

    def _to_container_path(resolved: Path | str) -> str:
        return resolved if isinstance(resolved, str) else f"/source/{resolved.name}"

    # audio_items resolve + concat-audio chạy TRƯỚC video_items — cần biết tổng độ dài audio
    # (duration_s trong result concat-audio) để tính mặc định cho ảnh tĩnh (type="image") chưa
    # nhập duration, TRƯỚC khi resolve video_items (ảnh cần duration đã biết mới sinh clip được).
    audio_inputs = [
        _to_container_path(_resolve_mix_item("a", i, it, stem, pipeline_id, _run_stage, _resumed_result, _mark_resumed))
        for i, it in enumerate(audio_items)
    ]
    concat_audio_name = f"{stem}_concat_audio.wav"
    concat_audio_result = _run_stage("editor_concat_audio", EDITOR_URL, {
        "cmd": "concat-audio",
        "params": {"inputs": audio_inputs, "output": f"/outputs/{concat_audio_name}"},
    }, resume_check=NODES["editor"]["outputs"] / concat_audio_name)
    audio_duration_s = concat_audio_result.get("duration_s")

    # Ảnh tĩnh chưa nhập duration — chỉ xảy ra khi TOÀN BỘ video_items là ảnh (đã validate ở
    # api.py::submit_pipeline, trộn ảnh+video thật bắt buộc nhập tay từ trước) — chia đều PHẦN
    # audio còn lại (sau khi trừ các ảnh đã tự nhập duration) cho các ảnh còn thiếu.
    if all(it.get("type") == "image" for it in video_items):
        explicit_total = sum(it["duration"] for it in video_items if it.get("duration"))
        unset_items = [it for it in video_items if not it.get("duration")]
        if unset_items and audio_duration_s:
            per_item = max(audio_duration_s - explicit_total, 0) / len(unset_items)
            for it in unset_items:
                it["duration"] = per_item or MIN_IMAGE_DURATION_FALLBACK_S

    video_inputs = [
        _to_container_path(_resolve_mix_item("v", i, it, stem, pipeline_id, _run_stage, _resumed_result, _mark_resumed))
        for i, it in enumerate(video_items)
    ]
    concat_video_name = f"{stem}_concat_video.mp4"
    _run_stage("editor_concat_video", EDITOR_URL, {
        "cmd": "concat-video",
        "params": {"inputs": video_inputs, "output": f"/outputs/{concat_video_name}"},
    }, resume_check=NODES["editor"]["outputs"] / concat_video_name)

    # Mux lại bằng đúng cmd="edit" có sẵn (không viết lại logic mux/-shortest) — video/audio input
    # đọc lại từ /outputs của CHÍNH editor-node (kết quả 2 bước concat trên), không phải /source.
    final_name = f"mix_{stem}_final.mp4"
    _run_stage("editor_edit", EDITOR_URL, {
        "cmd": "edit",
        "params": {
            "video": f"/outputs/{concat_video_name}",
            "audio": f"/outputs/{concat_audio_name}",
            "output": f"/outputs/{final_name}",
            "subtitles": None,
            "subtitle_mode": "none",
        },
    }, resume_check=NODES["editor"]["outputs"] / final_name)
    final_video_path = NODES["editor"]["outputs"] / final_name

    final_out = _copy(final_video_path, job_out_dir, f"{export_stem}.mp4")
    return {"ok": True, "output": str(final_out), "video_name": video_name, "stages": stages}


def _run_pipeline_impl(
    pipeline_id: str, payload: dict, stages: dict[str, dict], conn=None, resume_stages: dict | None = None,
) -> dict:
    resume_stages = resume_stages or {}
    mode = payload.get("mode", "review")
    if mode not in ("review", "dialogue", "subtitle", "audio", "video", "book", "mix"):
        raise ValueError(f"mode không hợp lệ: {mode}")

    # mode="book": KHÔNG có url/video nguồn — input là 1 file pdf/docx/pptx/xlsx/ảnh upload
    # tay, nhánh riêng hoàn toàn tách khỏi luồng ytdlp/transcribe/editor bên dưới (không có video
    # để mux/burn sub, không cần đồng bộ audio gốc). Tách sớm, TRƯỚC khi đọc payload["url"] (nhánh
    # book không có field này).
    if mode == "book":
        return _run_book_pipeline(pipeline_id, payload, stages, conn, resume_stages)
    # mode="mix": ghép N video + N audio nối tiếp, cũng KHÔNG có url đơn — tách sớm y hệt book.
    if mode == "mix":
        return _run_mix_pipeline(pipeline_id, payload, stages, conn, resume_stages)

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

    _run_stage, _resumed_result, _mark_resumed = _make_run_stage(pipeline_id, video_name, stages, conn, resume_stages)

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
