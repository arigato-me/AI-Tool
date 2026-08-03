# reup-ytdlp-node

Bản kiến trúc job-queue (api + worker, dùng chung `reup-broker`) của `docker_yt-dlp` — tải
video (patch Douyin: `a_bogus`/cookie tự sinh). **`docker_yt-dlp` (bản cũ) vẫn giữ nguyên làm
backup**, không bị đụng tới.

yt-dlp là CLI bên thứ 3 hoàn chỉnh, không có 1 hàm Python để import gọi thẳng như
`translate`/`editor` — worker chạy `yt-dlp` qua `subprocess`, giữ nguyên đúng cách dùng
args-passthrough của bản gốc (mọi tham số CLI hiện có dùng lại y hệt).

## Build & chạy

```bash
cd /u01/reup_tool/docker_build/reup-broker && docker compose up -d   # nếu chưa up

cd /u01/reup_tool/docker_build/reup-ytdlp-node
docker compose build
docker compose up -d
```

## API

### `POST /jobs`

```bash
curl -s -X POST http://localhost:8102/jobs \
  -H 'Content-Type: application/json' \
  -d '{"args": ["-o", "/downloads/%(title)s.%(ext)s", "https://www.douyin.com/video/..."]}'
# -> {"ok": true, "job_id": "..."}
```

`args` = đúng danh sách tham số CLI như dùng với `docker_yt-dlp` gốc (`-a /config/urls.txt`,
`--cookies /config/cookies.txt`, v.v.). Field tuỳ chọn `pipeline_id`/`video_name` (correlation
ID cho structured log — xem mục dưới) — `reup-orchestrator-node` tự gắn, không bắt buộc khi
gọi tay.

### `GET /jobs/{id}`

```bash
curl -s http://localhost:8102/jobs/<job_id>
# result khi finished: {"ok", "returncode", "args", "stdout_tail", "stderr_tail", "elapsed_s"}
```

### `GET /health`

### `POST /pipelines/{pipeline_id}/cancel` — huỷ job đang tải

```bash
curl -s -X POST http://localhost:8102/pipelines/<pipeline_id>/cancel
# -> {"ok": true, "pipeline_id": "..."}
```

Đặt cờ huỷ theo `pipeline_id` (đúng field `reup-orchestrator-node` đã gắn vào mọi job qua
`POST /jobs`, xem `YtdlpJobRequest.pipeline_id`) — `worker.py` chạy `yt-dlp` qua `Popen` +
polling `communicate(timeout=1s)` thay vì `subprocess.run()` chặn cứng, nên **kill được tiến
trình yt-dlp thật ngay khi phát hiện cờ** (SIGTERM, sau 5s không thoát thì SIGKILL) — khác cờ
hợp tác "chờ bước sau" của tầng orchestrator (xem `reup-orchestrator-node/README.md` mục "Huỷ
job"). Job chuyển status `cancelled` (không phải `failed`), gọi được `POST /pipelines/{id}/retry`
ở tầng orchestrator để tải lại từ đầu.

Nếu cờ huỷ tới trước cả khi worker rảnh tay xử lý job đó (còn `pending`), job bị huỷ ngay,
không spawn `yt-dlp` phút nào.

## Chạy CLI tay (debug, không qua queue)

```bash
docker compose run --rm api cli -o "/downloads/%(title)s.%(ext)s" "<url>"
```

## Structured log 2 ngày

`worker` ghi JSONL vào `./data/logs/YYYY-MM-DD.jsonl` (mount `/logs`): `job_start`/`job_done`/
`job_failed` mỗi lần tải video, kèm `pipeline_id`/`video_name` nếu có. Giữ 2 ngày, tự xoá qua
thread nền. Tắt bằng env `EVENT_LOG_ENABLED=false` (mặc định `true`).

## Volume

`./data/downloads`, `./data/config` (cookies.txt, urls.txt), `./data/logs` (structured log) —
bind-mount, giống convention gốc.

## Troubleshooting

Xem `reup-translate-node/README.md` — cùng pattern api/worker/queue, các lỗi thường gặp
(worker chết, không kết nối Redis, job kẹt pending) giống nhau ở mọi node.
