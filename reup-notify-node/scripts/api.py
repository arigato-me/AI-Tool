"""FastAPI — chỉ nói chuyện với Redis, không import Playwright/adapter nặng (giữ đúng nguyên
tắc tách api/worker của mọi node khác: api luôn nhẹ, restart/scale độc lập với worker).
Validate cú pháp `platforms` + tồn tại của webhook target ở đây (fail nhanh trước khi enqueue);
check credential sâu hơn (thiếu bot_token, thiếu session file...) để trong adapter, không lặp
lại logic 2 nơi."""
from __future__ import annotations

import os
import re

import redis as redis_lib
import yaml
from fastapi import FastAPI
from pydantic import BaseModel, field_validator

import queue_lib as q

REDIS_URL = os.environ.get("REDIS_URL", "redis://reup-redis:6379/0")
ROLE = "notify-worker"
WEBHOOKS_PATH = "/config/webhooks.yaml"

PLATFORM_RE = re.compile(r"^(telegram|whatsapp|zalo|messenger|webhook:.+)$")

conn = redis_lib.Redis.from_url(REDIS_URL)
app = FastAPI()


def _webhook_names() -> set[str]:
    if not os.path.exists(WEBHOOKS_PATH):
        return set()
    with open(WEBHOOKS_PATH, encoding="utf-8") as f:
        targets = yaml.safe_load(f) or []
    return {t.get("name") for t in targets if t.get("name")}


class NotifyJobRequest(BaseModel):
    platforms: list[str]
    message: str
    file_path: str | None = None
    chat_id: str | None = None
    pipeline_id: str | None = None  # correlation ID cho log — orchestrator truyền vào, không bắt buộc
    video_name: str | None = None

    @field_validator("platforms")
    @classmethod
    def _validate_platforms(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("thiếu platforms — vd [\"telegram\"]")
        for p in v:
            if not PLATFORM_RE.match(p):
                raise ValueError(
                    f"platform không hợp lệ: {p!r} — chỉ nhận telegram/whatsapp/zalo/messenger/webhook:<name>"
                )
        return v


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": q.SERVICE,
        "worker_alive": q.worker_alive(conn, ROLE),
        **q.queue_depth(conn),
    }


@app.post("/jobs")
def submit_job(req: NotifyJobRequest) -> dict:
    webhook_names = _webhook_names()
    for p in req.platforms:
        if p.startswith("webhook:"):
            name = p.partition(":")[2]
            if name not in webhook_names:
                return {"ok": False, "error": f"webhook '{name}' chưa cấu hình trong {WEBHOOKS_PATH}"}

    job_id = q.new_job(conn, req.model_dump())
    return {"ok": True, "job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = q.get_job(conn, job_id)
    if job is None:
        return {"ok": False, "error": "job không tồn tại hoặc đã hết hạn (TTL 48h)"}
    return {"ok": True, "job_id": job_id, **job}
