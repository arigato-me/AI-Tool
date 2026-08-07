"""FastAPI — chỉ nói chuyện với Redis, không tự gọi node nào (worker mới làm việc đó) —
đúng nguyên tắc tách api/worker của mọi node khác trong kiến trúc này. Mount `/outputs` static
để `reup-ui`/trình duyệt tải hoặc phát trực tiếp video final."""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests
import redis as redis_lib
from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import queue_lib as q
from node_client import NodeJobError, submit_and_wait
from trim_lib import TrimError, trim_media

REDIS_URL = os.environ.get("REDIS_URL", "redis://reup-redis:6379/0")
ROLE = "orchestrator-worker"
TTS_URL = os.environ.get("TTS_URL", "http://reup-tts-gpu-node-api:8000")
EDITOR_URL = os.environ.get("EDITOR_URL", "http://reup-editor-node-api:8000")

# Tên hiển thị + URL health của 6 node kia, dùng cho GET /nodes/status (đèn xanh/đỏ dashboard
# monitor). Orchestrator tự thêm chính nó vào (đọc thẳng Redis, không cần HTTP tới chính mình).
OTHER_NODES = {
    "ytdlp": os.environ.get("YTDLP_URL", "http://reup-ytdlp-node-api:8000"),
    "transcribe": os.environ.get("TRANSCRIBE_URL", "http://reup-transcribe-node-api:8000"),
    "translate": os.environ.get("TRANSLATE_URL", "http://reup-translate-node-api:8000"),
    "tts-gpu": TTS_URL,
    "editor": os.environ.get("EDITOR_URL", "http://reup-editor-node-api:8000"),
    "ocr": os.environ.get("OCR_URL", "http://ocr-node-api:8000"),
}

# Đọc trực tiếp filesystem (mount read-only riêng cho api, xem docker-compose.yml) thay vì đi
# qua job queue như GET /voices — chỉ là liệt kê tên file tĩnh trong data/music của
# reup-editor-node, không cần round-trip Redis/worker cho việc này.
MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "/nodes/editor/music"))

# Cùng lý do/pattern với MUSIC_DIR ở trên — mount read-only riêng cho api, đọc thẳng file
# <voice_id>.wav do `reup-tts-gpu-node/scripts/generate_voice_samples.py` tạo sẵn (offline,
# không qua job queue) để `reup-ui` cho nghe thử giọng trước khi chọn.
VOICE_SAMPLES_DIR = Path(os.environ.get("VOICE_SAMPLES_DIR", "/nodes/tts/voice_samples"))

# Cùng danh sách STYLES cứng trong reup-tts-gpu-node/scripts/tts_cli.py — không import chéo
# (standalone scaffold convention), chỉ trùng lặp 1 hằng số nhỏ, ít thay đổi.
STYLES = [
    {"id": "tu_nhien", "label": "Tự nhiên"},
    {"id": "tin_tuc", "label": "Tin tức"},
    {"id": "doc_truyen", "label": "Đọc truyện"},
]

conn = redis_lib.Redis.from_url(REDIS_URL)
app = FastAPI()


class BookDocument(BaseModel):
    data_b64: str
    ext: str


class MixItem(BaseModel):
    """1 nguồn video/audio trong mode="mix" — xem pipeline_runner.py::_resolve_mix_item.
    type="url": tải qua yt-dlp. type="upload": data_b64+ext, giống BookDocument. type="reuse":
    tái dùng file đã tải sẵn của 1 pipeline_id cũ (không tải lại). type="library": CHỈ hợp lệ
    trong audio_items — track có sẵn trong thư viện nhạc nền (music_project/music_track).
    type="image": CHỈ hợp lệ trong video_items — ảnh tĩnh chuyển thành clip video (xem
    reup-editor-node/scripts/image_to_video_cli.py). `duration` (giây) bắt buộc nếu video_items
    có TRỘN ảnh với video thật; nếu TOÀN BỘ video_items là ảnh, để trống thì chia đều theo tổng
    độ dài audio_items (xem _run_mix_pipeline)."""
    type: str
    url: str | None = None
    data_b64: str | None = None
    ext: str | None = None
    pipeline_id: str | None = None
    music_project: str | None = None
    music_track: str | None = None
    duration: float | None = None


class PipelineRequest(BaseModel):
    # Bắt buộc cho mọi mode TRỪ "book" (nhánh sách dùng `documents` thay vì url — xem validate ở
    # submit_pipeline() bên dưới, Pydantic không ép được ràng buộc "1 trong 2 tuỳ mode" gọn hơn
    # if/else thường).
    url: str | None = None
    video_name: str | None = None
    mode: str = "review"
    # mode="book" — 1 HOẶC NHIỀU file pdf/docx/pptx/xlsx/ảnh upload tay (base64), xem
    # reup-orchestrator-node/scripts/pipeline_runner.py::_run_book_pipeline. List (không phải 1
    # field đơn) để hỗ trợ sách chụp nhiều ảnh (1 ảnh/trang) — gộp theo đúng thứ tự mảng.
    documents: list[BookDocument] = []
    ocr_lang: str = "vi"  # "vi" (PaddleOCR detect + VietOCR) | "en"/"fr" (PaddleOCR gốc) — xem ocr-node
    ytdlp_args: list[str] = []
    align: bool = False
    source_lang: str = "zh"  # "zh" (Paraformer) | "other" (whisper) — chọn thẳng trên web,
    # không còn tự đoán ngôn ngữ (xem reup-transcribe-node/scripts/pipeline.py)
    voice: str | None = None
    style: str | None = None
    ref_audio_b64: str | None = None
    ref_audio_ext: str | None = None
    subtitle_mode: str = "burn"
    # Style chữ khi subtitle_mode="burn" (bold/màu chữ/viền/nền — xem
    # reup-editor-node/scripts/edit_cli.py DEFAULT_SUB_STYLE). None -> editor-node tự dùng
    # default y hệt style đã verify trước đây (không đổi hành vi nếu FE không gửi field này).
    sub_style: dict | None = None
    target_lang: str = "tiếng Việt"
    batch_size: int = 20
    # Nhạc nền — CHỈ áp dụng cho mode="review" (dialogue đã có instrumental tách từ audio
    # gốc, xem pipeline_runner.py). music_preset: tên file trong MUSIC_DIR (do người vận
    # hành thả sẵn). music_b64/music_ext: người dùng tự upload — cùng pattern ref_audio_b64,
    # ưu tiên hơn music_preset nếu cả 2 cùng gửi (xem _run_pipeline_impl).
    music_preset: str | None = None
    music_b64: str | None = None
    music_ext: str | None = None
    music_level: float | None = None
    # Chọn nhạc nền theo project/theme (thư viện nhạc mới) — 2 field này ưu tiên HƠN
    # music_preset (legacy, phẳng) nhưng vẫn THUA music_b64 (upload tức thời lúc submit) nếu cả
    # 3 cùng gửi. Xem thứ tự ưu tiên đầy đủ ở pipeline_runner.py.
    music_project: str | None = None
    music_track: str | None = None
    # mode="mix" — ghép nhiều video nối tiếp + nhiều audio nối tiếp, không transcribe/dịch/TTS/
    # sub. Xem pipeline_runner.py::_run_mix_pipeline.
    video_items: list[MixItem] = []
    audio_items: list[MixItem] = []


class TrimBody(BaseModel):
    start: str | None = None  # giây hoặc "HH:MM:SS" — cắt ĐẦU (bỏ phần trước mốc này)
    end: str | None = None    # giây hoặc "HH:MM:SS" — cắt ĐUÔI (bỏ phần sau mốc này)


class CreateMusicProjectBody(BaseModel):
    display_name: str


class UploadMusicTrackBody(BaseModel):
    filename: str
    data_b64: str


class SetDefaultMusicBody(BaseModel):
    project: str
    track: str


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": q.SERVICE,
        "worker_alive": q.worker_alive(conn, ROLE),
        "total": q.total_count(conn),
        **q.queue_depth(conn),
    }


@app.get("/nodes/status")
def nodes_status() -> dict:
    """Đèn xanh/đỏ cho dashboard monitor — health của cả 5 node kia (HTTP, timeout ngắn để
    1 node down không treo request) + chính orchestrator (đọc thẳng Redis, không cần HTTP)."""
    nodes = [{
        "name": "orchestrator",
        "alive": q.worker_alive(conn, ROLE),
        **q.queue_depth(conn),
    }]
    for name, url in OTHER_NODES.items():
        try:
            r = requests.get(f"{url}/health", timeout=3)
            r.raise_for_status()
            data = r.json()
            nodes.append({
                "name": name,
                "alive": bool(data.get("worker_alive")),
                "pending": data.get("pending", 0),
                "processing": data.get("processing", 0),
            })
        except Exception as e:
            nodes.append({"name": name, "alive": False, "pending": None, "processing": None, "error": str(e)})
    return {"ok": True, "nodes": nodes}


@app.get("/stats/translate-tokens")
def translate_tokens_stats() -> dict:
    """Token Deepseek đã dùng ở node translate, cộng dồn theo ngày (xem
    queue_lib.add_translate_tokens, gọi từ pipeline_runner.py mỗi lần stage translate chạy
    live) — không phụ thuộc job list 200 job/TTL 48h như thống kê 'video đã xử lý xong'."""
    return {"ok": True, **q.translate_tokens_stats(conn)}


@app.post("/pipelines")
def submit_pipeline(req: PipelineRequest) -> dict:
    if req.mode not in ("review", "dialogue", "subtitle", "audio", "video", "book", "mix"):
        return {"ok": False, "error": f"mode không hợp lệ: {req.mode}"}
    if req.mode == "book":
        if not req.documents:
            return {"ok": False, "error": "mode='book' cần documents (ít nhất 1 file)"}
        if req.ocr_lang not in ("vi", "en", "fr"):
            return {"ok": False, "error": f"ocr_lang không hợp lệ: {req.ocr_lang!r} (chỉ nhận 'vi'/'en'/'fr')"}
    elif req.mode == "mix":
        if not req.video_items:
            return {"ok": False, "error": "mode='mix' cần video_items (ít nhất 1)"}
        if not req.audio_items:
            return {"ok": False, "error": "mode='mix' cần audio_items (ít nhất 1)"}
        for i, it in enumerate(req.video_items):
            if it.type not in ("url", "upload", "reuse", "image"):
                return {"ok": False, "error": f"video_items[{i}].type không hợp lệ: {it.type!r}"}
        # type="image" thiếu duration CHỈ hợp lệ khi TOÀN BỘ video_items là ảnh (mặc định chia
        # đều theo audio lúc chạy, xem _run_mix_pipeline) — trộn ảnh với video thật thì mỗi ảnh
        # bắt buộc tự nhập duration, không có "audio dùng chung" hợp lý khi có video thật khác.
        all_images = all(it.type == "image" for it in req.video_items)
        if not all_images:
            for i, it in enumerate(req.video_items):
                if it.type == "image" and not (it.duration and it.duration > 0):
                    return {"ok": False, "error": f"video_items[{i}] type='image' trộn cùng video thật cần duration (giây) > 0"}
        for i, it in enumerate(req.audio_items):
            if it.type not in ("url", "upload", "reuse", "library"):
                return {"ok": False, "error": f"audio_items[{i}].type không hợp lệ: {it.type!r}"}
    elif not req.url:
        return {"ok": False, "error": "thiếu url"}
    job_id = q.new_job(conn, req.model_dump())
    return {"ok": True, "pipeline_id": job_id}


@app.post("/pipelines/{pipeline_id}/retry")
def retry_pipeline(pipeline_id: str) -> dict:
    """Chạy lại 1 pipeline đã 'failed'/'cancelled' — GIỮ NGUYÊN pipeline_id (khác `POST
    /pipelines` luôn tạo id mới) để `pipeline_runner.py` nhận diện đúng file trung gian đã xử lý
    xong (mọi tên file trong pipeline đều prefix theo pipeline_id/video_name) mà bỏ qua, chỉ
    chạy lại từ đúng bước dở dang — không tải/xử lý lại từ đầu, không lãng phí tài nguyên các
    bước đã pass. 'cancelled' dùng chung đường này với 'failed' vì cùng cơ chế `partial_stages`."""
    job = q.get_job(conn, pipeline_id)
    if job is None:
        return {"ok": False, "error": "pipeline không tồn tại hoặc đã hết hạn (TTL 48h)"}
    if job.get("status") not in ("failed", "cancelled"):
        return {"ok": False, "error": f"chỉ retry được job đang 'failed'/'cancelled' (hiện tại: {job.get('status')})"}
    q.requeue_for_retry(conn, pipeline_id)
    return {"ok": True, "pipeline_id": pipeline_id}


@app.post("/pipelines/{pipeline_id}/cancel")
def cancel_pipeline(pipeline_id: str) -> dict:
    """Huỷ 1 pipeline đang 'pending' (chưa worker nào chạm tới — gỡ thẳng khỏi hàng đợi, huỷ
    tức thì) hoặc 'started' (đặt cờ hợp tác, `pipeline_runner._run_stage()` tự dừng TRƯỚC bước
    kế tiếp — xem giới hạn ở README mục "Huỷ job"). Nhánh `cancel_pending` trả False khi thua
    race với `pop_job()` (BLMOVE) — rơi xuống case 'started' luôn, không cần if/else tách riêng
    theo `status` đọc lúc đầu (có thể đã đổi giữa lúc đọc và lúc gọi).

    Riêng bước `ytdlp`: cờ hợp tác không đủ (`submit_and_wait` đang chặn cứng chờ HTTP response
    từ node con, tải xong mới kiểm cờ) — gọi kèm sang `POST {ytdlp}/pipelines/{id}/cancel` để
    kill THẬT tiến trình yt-dlp đang tải (xem `reup-ytdlp-node/scripts/worker.py`), thay vì đợi
    tải xong (có khi vài phút, tốn băng thông/thời gian vô ích) mới dừng được."""
    job = q.get_job(conn, pipeline_id)
    if job is None:
        return {"ok": False, "error": "pipeline không tồn tại hoặc đã hết hạn (TTL 48h)"}
    if job.get("status") not in ("pending", "started"):
        return {"ok": False, "error": f"chỉ huỷ được job đang 'pending'/'started' (hiện tại: {job.get('status')})"}
    if q.cancel_pending(conn, pipeline_id):
        q.mark_cancelled(conn, pipeline_id)
        return {"ok": True, "pipeline_id": pipeline_id, "status": "cancelled"}
    q.request_cancel(conn, pipeline_id)
    if job.get("current_stage") == "ytdlp":
        try:
            requests.post(f"{OTHER_NODES['ytdlp']}/pipelines/{pipeline_id}/cancel", timeout=5)
        except requests.RequestException:
            pass  # cờ hợp tác ở orchestrator vẫn có hiệu lực cho bước kế tiếp dù gọi thẳng thất bại
    return {"ok": True, "pipeline_id": pipeline_id, "status": "cancelling"}


@app.post("/pipelines/{pipeline_id}/trim")
def trim_pipeline_output(pipeline_id: str, req: TrimBody) -> dict:
    """Cắt đầu/đuôi output (mp3 HOẶC video mp4/webm/mkv) của 1 job đã 'finished'. Chạy ffmpeg
    trực tiếp tại đây (không qua job queue), ghi file `<tên>_trimmed<đuôi gốc>` cạnh file gốc —
    không đụng file gốc, gọi lại nhiều lần (đổi start/end thử) chỉ ghi đè đúng file `_trimmed`.

    Định theo ĐUÔI FILE output, không theo `mode` — tự đúng cho mọi nhánh xuất video (review/
    dialogue/subtitle/video/mix, kể cả mode mới thêm sau này) mà không cần liệt kê cứng danh
    sách mode. Riêng mp3: GIỮ NGUYÊN gate cũ `mode="audio"` — không mở rộng cho mp3 của mode
    khác (vd mode="book" cũng xuất mp3, cố tình KHÔNG cho trim ở đây, ngoài phạm vi yêu cầu)."""
    job = q.get_job(conn, pipeline_id)
    if job is None:
        return {"ok": False, "error": "pipeline không tồn tại hoặc đã hết hạn (TTL 48h)"}
    if job.get("status") != "finished":
        return {"ok": False, "error": f"chỉ cắt được job đã 'finished' (hiện tại: {job.get('status')})"}
    payload = job.get("payload") or {}
    output = (job.get("result") or {}).get("output")
    if not output:
        return {"ok": False, "error": "job này không có output để cắt"}
    ext = Path(output).suffix.lower()
    if ext == ".mp3":
        if payload.get("mode") != "audio":
            return {"ok": False, "error": "chỉ hỗ trợ cắt mp3 cho job nhánh mode='audio'"}
    elif ext not in (".mp4", ".webm", ".mkv"):
        return {"ok": False, "error": f"định dạng không hỗ trợ cắt: {ext or '(không rõ)'}"}

    in_path = Path(output)
    if not in_path.is_file():
        return {"ok": False, "error": f"file không tồn tại trên đĩa: {output}"}
    out_path = in_path.with_name(in_path.stem + "_trimmed" + in_path.suffix)
    try:
        result = trim_media(str(in_path), str(out_path), req.start, req.end)
    except TrimError as e:
        return {"ok": False, "error": str(e)}

    # Cache-buster (?v=timestamp) — tên file KHÔNG đổi giữa các lần cắt (luôn ghi đè
    # "<tên>_trimmed.mp3"), nên nếu URL y hệt lần trước, browser coi là request đã có sẵn (đã
    # tải audio này rồi) và phát lại bản CACHE cũ thay vì file mới vừa ghi đè trên đĩa — bug thật
    # gặp: cắt lại lần 2 xong, `<audio>` vẫn phát nội dung cắt lần 1. Đổi query string mỗi lần
    # cắt để trình duyệt luôn coi là URL mới, buộc tải lại. `time.time()` (không ép `int`) —
    # bug thật gặp lúc test: ép `int` chỉ còn độ phân giải GIÂY, 2 lần cắt liên tiếp trong cùng
    # 1 giây (thao tác tay bình thường, không phải edge case hiếm) ra CÙNG giá trị, vô hiệu hoá
    # đúng cache-buster vừa thêm.
    out_url = f"/outputs/{pipeline_id}/{out_path.name}?v={time.time()}"
    q.set_trim_result(conn, pipeline_id, out_url)
    return {"ok": True, "output": out_url, "start": req.start, "end": req.end, "duration_s": result.get("duration_s")}


@app.get("/pipelines/{pipeline_id}")
def pipeline_status(pipeline_id: str) -> dict:
    job = q.get_job(conn, pipeline_id)
    if job is None:
        return {"ok": False, "error": "pipeline không tồn tại hoặc đã hết hạn (TTL 48h)"}
    return {"ok": True, "pipeline_id": pipeline_id, **job}


@app.get("/pipelines")
def list_pipelines(limit: int = 50) -> dict:
    ids = q.list_recent(conn, limit)
    items = []
    for pid in ids:
        job = q.get_job(conn, pid)
        if job:
            # Tổng elapsed_s các stage — tính SẴN ở đây rồi pop "result" (thay vì để FE tự cộng),
            # vì "result" đầy đủ (segments dịch, usage token, windows TTS...) khá nặng, không
            # muốn kéo hết ra chỉ để lấy 1 con số cho bảng danh sách 200 job.
            result = job.get("result") or {}
            stages = result.get("stages") or {}
            total_elapsed = sum(
                s.get("elapsed_s") or 0 for s in stages.values() if isinstance(s, dict)
            )
            job["total_elapsed_s"] = round(total_elapsed, 1) if total_elapsed else None
            job.pop("result", None)
            payload = job.get("payload") or {}
            job["payload"] = {
                "url": payload.get("url"),
                "mode": payload.get("mode"),
                "video_name": payload.get("video_name"),
                "voice": payload.get("voice"),
                "style": payload.get("style"),
                "subtitle_mode": payload.get("subtitle_mode"),
            }
            items.append({"pipeline_id": pid, **job})
    return {"ok": True, "items": items}


@app.get("/styles")
def list_styles() -> dict:
    return {"ok": True, "styles": STYLES}


@app.get("/voices")
def list_voices() -> dict:
    try:
        result = submit_and_wait(TTS_URL, {"cmd": "list-voices", "params": {}}, timeout=60)
    except NodeJobError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "voices": result.get("voices", []), "count": result.get("count", 0)}


@app.get("/voices/{voice_id}/sample")
def voice_sample(voice_id: str):
    """Stream file wav mẫu 15-20s (tạo sẵn offline, xem generate_voice_samples.py) để reup-ui
    cho nghe thử giọng — cùng pattern stream_music_track() bên dưới (đọc thẳng mount read-only,
    không round-trip job queue)."""
    try:
        safe_id = _safe_seg(voice_id)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    sample_path = VOICE_SAMPLES_DIR / f"{safe_id}.wav"
    if not sample_path.is_file():
        return {"ok": False, "error": f"chưa có sample cho voice: {voice_id}"}
    return FileResponse(sample_path)


@app.post("/subtitle-style/preview")
def preview_subtitle_style(req: dict):
    """Proxy đồng bộ sang editor-node (đúng pattern /music/projects* bên dưới) — trả PNG
    demo style sub burn, dùng cho panel chỉnh style ở reup-ui trước khi submit pipeline."""
    try:
        r = requests.post(f"{EDITOR_URL}/subtitle-style/preview", json=req, timeout=10)
    except requests.RequestException as e:
        return {"ok": False, "error": str(e)}
    return Response(content=r.content, media_type="image/png", status_code=r.status_code)


# Style sub người dùng đã "Lưu làm mặc định" — con trỏ đơn, cùng pattern /music/default bên
# dưới. reup-ui gọi GET lúc mount SubmitJob để tiền điền form, POST lúc bấm "Lưu làm mặc định".


@app.get("/subtitle-style/default")
def get_subtitle_style_default() -> dict:
    try:
        r = requests.get(f"{EDITOR_URL}/subtitle-style/default", timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"không gọi được editor-node: {e}"}


@app.post("/subtitle-style/default")
def set_subtitle_style_default(req: dict) -> dict:
    try:
        r = requests.post(f"{EDITOR_URL}/subtitle-style/default", json=req, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"không gọi được editor-node: {e}"}


@app.delete("/subtitle-style/default")
def clear_subtitle_style_default() -> dict:
    try:
        r = requests.delete(f"{EDITOR_URL}/subtitle-style/default", timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"không gọi được editor-node: {e}"}


@app.get("/music")
def list_music() -> dict:
    """LEGACY — danh sách preset nhạc nền kiểu phẳng (trước khi có thư viện project/theme).
    Giữ nguyên để không vỡ client cũ; `reup-ui` mới dùng `/music/projects*` bên dưới."""
    if not MUSIC_DIR.is_dir():
        return {"ok": True, "presets": []}
    presets = sorted(
        p.name for p in MUSIC_DIR.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() != ".md"
    )
    return {"ok": True, "presets": presets}


def _safe_seg(seg: str) -> str:
    """Chặn path traversal cho 1 segment (slug project/tên track) tới từ path param HTTP —
    bản sao nhỏ của `music_library._safe_slug` (không import chéo, đúng convention standalone
    scaffold — xem STYLES ở trên cũng trùng lặp có chủ đích với reup-tts-gpu-node)."""
    seg = (seg or "").strip()
    if not seg or seg in (".", "..") or seg != Path(seg).name:
        raise ValueError(f"tên không hợp lệ: {seg!r}")
    return seg


@app.get("/music/projects")
def list_music_projects() -> dict:
    """Proxy đồng bộ sang editor-node — 1 nguồn logic duy nhất (music_library.py), tránh
    drift giữa 2 nơi liệt kê project/track."""
    try:
        r = requests.get(f"{EDITOR_URL}/music/projects", timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"không gọi được editor-node: {e}"}


@app.get("/music/projects/{slug}/tracks")
def list_music_tracks(slug: str) -> dict:
    try:
        r = requests.get(f"{EDITOR_URL}/music/projects/{slug}/tracks", timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"không gọi được editor-node: {e}"}


@app.post("/music/projects")
def create_music_project(req: CreateMusicProjectBody) -> dict:
    try:
        r = requests.post(f"{EDITOR_URL}/music/projects", json=req.model_dump(), timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"không gọi được editor-node: {e}"}


@app.post("/music/projects/{slug}/tracks")
def upload_music_track(slug: str, req: UploadMusicTrackBody) -> dict:
    # timeout dài hơn (file vài MB base64 qua JSON, cold-path 1 operator — chấp nhận chờ).
    try:
        r = requests.post(
            f"{EDITOR_URL}/music/projects/{slug}/tracks", json=req.model_dump(), timeout=60,
        )
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"không gọi được editor-node: {e}"}


@app.delete("/music/projects/{slug}/tracks/{track}")
def delete_music_track(slug: str, track: str) -> dict:
    try:
        r = requests.delete(f"{EDITOR_URL}/music/projects/{slug}/tracks/{track}", timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"không gọi được editor-node: {e}"}


@app.delete("/music/projects/{slug}")
def delete_music_project(slug: str) -> dict:
    try:
        r = requests.delete(f"{EDITOR_URL}/music/projects/{slug}", timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"không gọi được editor-node: {e}"}


@app.get("/music/default")
def get_default_music() -> dict:
    """Track nhạc nền mặc định cho nhánh review — `reup-ui` SubmitJob gọi lúc load form để tự
    chọn sẵn đúng track này thay vì heuristic "track đầu tiên" cũ (xem music_library.py)."""
    try:
        r = requests.get(f"{EDITOR_URL}/music/default", timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"không gọi được editor-node: {e}"}


@app.post("/music/default")
def set_default_music(req: SetDefaultMusicBody) -> dict:
    try:
        r = requests.post(f"{EDITOR_URL}/music/default", json=req.model_dump(), timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"không gọi được editor-node: {e}"}


@app.delete("/music/default")
def clear_default_music() -> dict:
    try:
        r = requests.delete(f"{EDITOR_URL}/music/default", timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"không gọi được editor-node: {e}"}


UNGROUPED_SLUG = "_ungrouped"  # khớp music_library.UNGROUPED_SLUG (không import chéo)


@app.get("/music/projects/{slug}/tracks/{track}/raw")
def stream_music_track(slug: str, track: str):
    """Serve thẳng bytes từ mount read-only `/nodes/editor/music` (không proxy qua editor-node
    — tránh double-hop nhị phân cho preview audio trong UI)."""
    try:
        safe_slug, safe_track = _safe_seg(slug), _safe_seg(track)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    project_dir = MUSIC_DIR if safe_slug == UNGROUPED_SLUG else MUSIC_DIR / safe_slug
    track_path = project_dir / safe_track
    if not track_path.is_file():
        return {"ok": False, "error": f"track không tồn tại: {slug}/{track}"}
    return FileResponse(track_path)


app.mount("/outputs", StaticFiles(directory="/outputs"), name="outputs")
