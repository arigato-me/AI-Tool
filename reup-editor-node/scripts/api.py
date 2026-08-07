"""1 API cho cả 3 subcommand cũ (edit/srt/mix-dialogue) — phân biệt qua field `cmd`
trong body /jobs, worker dispatch sang đúng hàm run_*()."""
from __future__ import annotations

import os

import redis as redis_lib
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel

import music_library as ml
import queue_lib as q
from edit_cli import (
    DEFAULT_SUB_STYLE,
    clear_default_sub_style,
    get_default_sub_style,
    render_preview_png,
    set_default_sub_style,
)

REDIS_URL = os.environ.get("REDIS_URL", "redis://reup-redis:6379/0")
ROLE = "editor-worker"

conn = redis_lib.Redis.from_url(REDIS_URL)
app = FastAPI()

VALID_CMDS = {"edit", "srt", "mix-dialogue", "mix-music", "concat-video", "concat-audio", "image-to-video"}


class EditJobRequest(BaseModel):
    cmd: str  # "edit" | "srt" | "mix-dialogue" | "mix-music" | "concat-video" | "concat-audio" | "image-to-video"
    params: dict
    pipeline_id: str | None = None  # correlation ID cho log — orchestrator truyền vào, không bắt buộc
    video_name: str | None = None


class CreateMusicProjectRequest(BaseModel):
    display_name: str


class UploadMusicTrackRequest(BaseModel):
    filename: str
    data_b64: str


class SetDefaultMusicRequest(BaseModel):
    project: str
    track: str


class SubtitleStylePreviewRequest(BaseModel):
    bold: bool = DEFAULT_SUB_STYLE["bold"]
    text_color: str = DEFAULT_SUB_STYLE["text_color"]
    outline_color: str = DEFAULT_SUB_STYLE["outline_color"]
    outline_width: int = DEFAULT_SUB_STYLE["outline_width"]
    background_enabled: bool = DEFAULT_SUB_STYLE["background_enabled"]
    background_color: str = DEFAULT_SUB_STYLE["background_color"]
    background_opacity: float = DEFAULT_SUB_STYLE["background_opacity"]


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": q.SERVICE,
        "worker_alive": q.worker_alive(conn, ROLE),
        **q.queue_depth(conn),
    }


@app.post("/jobs")
def submit_job(req: EditJobRequest) -> dict:
    if req.cmd not in VALID_CMDS:
        return {"ok": False, "error": f"cmd không hợp lệ: {req.cmd!r} (chỉ nhận {sorted(VALID_CMDS)})"}
    job_id = q.new_job(conn, {"cmd": req.cmd, "params": req.params,
                              "pipeline_id": req.pipeline_id, "video_name": req.video_name})
    return {"ok": True, "job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = q.get_job(conn, job_id)
    if job is None:
        return {"ok": False, "error": "job không tồn tại hoặc đã hết hạn (TTL 48h)"}
    return {"ok": True, "job_id": job_id, **job}


@app.post("/subtitle-style/preview")
def preview_subtitle_style(req: SubtitleStylePreviewRequest) -> Response:
    """REST đồng bộ, KHÔNG qua job queue — render 1 ảnh demo bằng Pillow (không gọi ffmpeg,
    xem docstring render_preview_png) nên đủ nhanh để gọi lại mỗi lần người dùng chỉnh style
    trên form, không cần worker xử lý nền như edit/mix-dialogue."""
    png_bytes = render_preview_png(req.model_dump())
    return Response(content=png_bytes, media_type="image/png")


# Style sub người dùng đã "Lưu làm mặc định" — con trỏ đơn (không phải nhiều preset), cùng
# pattern /music/default bên dưới. reup-ui đọc lúc mount SubmitJob để tiền điền form; job burn
# thật không tra cứu cái này (xem docstring get_default_sub_style trong edit_cli.py).


@app.get("/subtitle-style/default")
def get_subtitle_style_default() -> dict:
    return {"ok": True, "default": get_default_sub_style()}


@app.post("/subtitle-style/default")
def set_subtitle_style_default(req: SubtitleStylePreviewRequest) -> dict:
    return {"ok": True, "default": set_default_sub_style(req.model_dump())}


@app.delete("/subtitle-style/default")
def clear_subtitle_style_default() -> dict:
    clear_default_sub_style()
    return {"ok": True}


# --- Thư viện nhạc nền (project/theme) — REST đồng bộ, KHÔNG qua Redis job queue: đây là
# thao tác filesystem nhẹ (tạo thư mục, ghi 1 file vài MB), không cần worker xử lý nền như
# edit/mix-dialogue (vốn tốn giây/phút). editor-node là chủ ghi duy nhất của data/music.


@app.get("/music/projects")
def list_music_projects() -> dict:
    return {"ok": True, "projects": ml.list_projects()}


@app.get("/music/projects/{slug}/tracks")
def list_music_tracks(slug: str) -> dict:
    try:
        return {"ok": True, "tracks": ml.list_tracks(slug)}
    except ml.MusicLibraryError as e:
        return {"ok": False, "error": str(e)}


@app.post("/music/projects")
def create_music_project(req: CreateMusicProjectRequest) -> dict:
    try:
        return {"ok": True, "project": ml.create_project(req.display_name)}
    except ml.MusicLibraryError as e:
        return {"ok": False, "error": str(e)}


@app.post("/music/projects/{slug}/tracks")
def upload_music_track(slug: str, req: UploadMusicTrackRequest) -> dict:
    try:
        return {"ok": True, "track": ml.save_track(slug, req.filename, req.data_b64)}
    except ml.MusicLibraryError as e:
        return {"ok": False, "error": str(e)}


@app.delete("/music/projects/{slug}/tracks/{track}")
def delete_music_track(slug: str, track: str) -> dict:
    try:
        ml.delete_track(slug, track)
        return {"ok": True}
    except ml.MusicLibraryError as e:
        return {"ok": False, "error": str(e)}


@app.delete("/music/projects/{slug}")
def delete_music_project(slug: str) -> dict:
    try:
        ml.delete_project(slug)
        return {"ok": True}
    except ml.MusicLibraryError as e:
        return {"ok": False, "error": str(e)}


# Track nhạc nền mặc định cho nhánh review — con trỏ đơn toàn thư viện (xem
# music_library.get_default docstring), không phải per-project.


@app.get("/music/default")
def get_default_music() -> dict:
    return {"ok": True, "default": ml.get_default()}


@app.post("/music/default")
def set_default_music(req: SetDefaultMusicRequest) -> dict:
    try:
        return {"ok": True, "default": ml.set_default(req.project, req.track)}
    except ml.MusicLibraryError as e:
        return {"ok": False, "error": str(e)}


@app.delete("/music/default")
def clear_default_music() -> dict:
    ml.clear_default()
    return {"ok": True}
