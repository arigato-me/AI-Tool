"""HTTP client mỏng gọi API các reup-*-node khác — mọi node Phase 1 đều tuân theo cùng 1
hợp đồng (`POST /jobs` -> {"ok","job_id"}, `GET /jobs/{id}` -> {"status","result"|"error"}),
nên chỉ cần 1 hàm submit+poll dùng chung cho cả 5 node, không cần client riêng từng node.

Retry: lỗi mạng thoáng qua (node con đang restart giữa lúc poll, socket timeout...) KHÔNG được
coi là job thất bại — bug thật gặp: `reup-tts-gpu-node` restart đúng lúc orchestrator poll
`GET /jobs/{id}`, 1 `ConnectionError` duy nhất làm cả pipeline bị đánh `failed` dù job con vẫn
chạy tiếp bình thường trong queue của nó. `wait_for_job` giờ giữ nguyên trong `deadline` sẵn có
(không thêm ngân sách thời gian mới) thay vì raise ngay ở lần lỗi đầu. HTTP >=500 coi như thoáng
qua tương tự (node con có thể đang khởi động lại). 4xx là lỗi thật (body sai/job bị từ chối),
raise ngay, retry không giúp gì.

`submit_job` (POST) KHÔNG áp dụng retry-dài như GET vì không idempotent — retry sau khi request
đã tới server nhưng response bị rớt giữa đường có thể tạo job trùng. Chỉ retry vài lần ngắn
(`_POST_RETRIES`) đủ vượt cửa sổ restart rất ngắn, không che lỗi node down thật sự."""
from __future__ import annotations

import sys
import time
from typing import Callable

import requests

_TRANSIENT_NET_EXC = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
_POST_RETRIES = 3
_POST_RETRY_DELAY_S = 2.0


class NodeJobError(RuntimeError):
    pass


class NodeCancelled(NodeJobError):
    """Job con báo `status: 'cancelled'` (hiện chỉ `ytdlp` hỗ trợ kill thật giữa lúc tải, xem
    `reup-ytdlp-node/scripts/worker.py`) — subclass của `NodeJobError` nên code cũ chỉ bắt
    `NodeJobError` vẫn không vỡ, còn `pipeline_runner.py` bắt riêng để báo 'cancelled' thay vì
    'failed' cho cả pipeline."""


def _is_transient_status(status_code: int) -> bool:
    return status_code >= 500


def submit_job(base_url: str, body: dict) -> str:
    """POST /jobs -> job_id. Retry giới hạn cho lỗi mạng/5xx thoáng qua, 4xx raise ngay."""
    last_exc: Exception | None = None
    for attempt in range(_POST_RETRIES):
        is_last = attempt == _POST_RETRIES - 1
        try:
            resp = requests.post(f"{base_url}/jobs", json=body, timeout=30)
        except _TRANSIENT_NET_EXC as e:
            last_exc = e
            if is_last:
                break
            print(f"[node_client] lỗi mạng thoáng qua POST {base_url}/jobs: {e} — thử lại "
                  f"({attempt + 1}/{_POST_RETRIES})", file=sys.stderr)
            time.sleep(_POST_RETRY_DELAY_S)
            continue
        if _is_transient_status(resp.status_code) and not is_last:
            print(f"[node_client] POST {base_url}/jobs trả {resp.status_code} — thử lại "
                  f"({attempt + 1}/{_POST_RETRIES})", file=sys.stderr)
            time.sleep(_POST_RETRY_DELAY_S)
            continue
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise NodeJobError(f"{base_url}/jobs từ chối job: {data.get('error')}")
        return data["job_id"]
    raise NodeJobError(f"{base_url}/jobs không phản hồi sau {_POST_RETRIES} lần thử: {last_exc}")


def wait_for_job(base_url: str, job_id: str, poll_interval: float = 2.0, timeout: float = 3600.0) -> dict:
    """GET /jobs/{id} lặp tới khi xong. Dùng để nối lại 1 job đã biết `job_id` từ trước (resume,
    xem `pipeline_runner._run_stage`) hoặc gọi qua `submit_and_wait` ngay sau khi submit mới.

    1800s -> 3600s (2026-08-01, stress-test Phase F PIPELINE_CONCURRENCY=10): submit 4 pipeline
    đồng thời (2 dialogue nặng gấp đôi vì có bước tách nhạc nền), 1 job transcribe bị timeout ở
    1800s trong khi worker vẫn SỐNG và đang tính (log mới liên tục, VRAM 2249/4096 MiB — không
    OOM, không kẹt khoá GPU) — job tự xong 2 phút sau đó. Không phải hang/deadlock, chỉ đơn giản
    chậm hơn dự tính dưới tải 4 pipeline cùng lúc. Nâng trần lên gấp đôi để có margin an toàn cho
    đúng kịch bản tải nặng nhất này, không đổi gì hành vi các job bình thường (vẫn trả kết quả
    ngay khi xong, không phải luôn đợi hết 3600s)."""
    deadline = time.time() + timeout
    status = None
    while time.time() < deadline:
        try:
            resp = requests.get(f"{base_url}/jobs/{job_id}", timeout=30)
        except _TRANSIENT_NET_EXC as e:
            print(f"[node_client] lỗi mạng thoáng qua GET {base_url}/jobs/{job_id}: {e} — thử lại",
                  file=sys.stderr)
            time.sleep(poll_interval)
            continue
        if _is_transient_status(resp.status_code):
            print(f"[node_client] GET {base_url}/jobs/{job_id} trả {resp.status_code} — thử lại",
                  file=sys.stderr)
            time.sleep(poll_interval)
            continue
        resp.raise_for_status()
        job = resp.json()
        if not job.get("ok", True):
            # Node con trả {"ok": false} (vd job_id không tồn tại/hết TTL 48h) — lỗi thật, không
            # phải "chưa xong", raise ngay thay vì lặp vô ích tới hết `timeout`.
            raise NodeJobError(f"{base_url} job {job_id}: {job.get('error')}")
        status = job.get("status")
        if status == "finished":
            return job["result"]
        if status == "cancelled":
            raise NodeCancelled(f"{base_url} job {job_id} đã bị huỷ")
        if status == "failed":
            raise NodeJobError(f"{base_url} job {job_id} lỗi: {job.get('error')}")
        time.sleep(poll_interval)
    raise NodeJobError(f"{base_url} job {job_id} timeout sau {timeout}s (status cuối: {status})")


def submit_and_wait(base_url: str, body: dict, poll_interval: float = 2.0, timeout: float = 3600.0,
                     on_submitted: Callable[[str], None] | None = None) -> dict:
    """Giữ nguyên chữ ký cũ cho các chỗ gọi không cần biết `job_id` sớm (vd `api.py` list-voices).
    `on_submitted(job_id)`, nếu truyền, được gọi ngay sau khi tạo job xong — TRƯỚC khi vào vòng
    poll — để caller lưu `job_id` vào `partial_stages` sớm (xem `pipeline_runner._run_stage`),
    cho phép lần resume sau nối lại đúng job đang chạy thay vì submit job mới, tốn thêm 1 lượt
    GPU vô ích nếu job cũ thật ra đã/đang xong."""
    job_id = submit_job(base_url, body)
    if on_submitted is not None:
        on_submitted(job_id)
    return wait_for_job(base_url, job_id, poll_interval=poll_interval, timeout=timeout)
