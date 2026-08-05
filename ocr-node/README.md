# ocr-node

Node job-queue (api + worker, dùng chung `reup-broker`) — trích văn bản từ file pdf/docx/pptx/
xlsx/ảnh thành transcript JSON, đứng TRƯỚC `reup-translate-node` trong nhánh "Sách → Audio" (xem
CLAUDE.md "Reup pipeline"). Tham khảo từ project `ocr-service` (remote
`hmtran@100.99.150.90:~/Projects/ocr-service`) — phần engine Tier 2 (`ocr_engine.py`) port gần
như nguyên xi từ đó (đã verify chạy thật trên card 4GB), phần Tier 1 + orchestrate 2 tầng là
code mới viết riêng cho node này.

## Kiến trúc 2 tầng

- **Tier 1 — MarkItDown + PyMuPDF (CPU, không đụng GPU):** docx/pptx/xlsx qua
  `MarkItDown().convert_local()` (KHÔNG cài extra `markitdown-ocr`/`[all]` — dùng LLM Vision
  cloud, trái nguyên tắc OCR 100% local). PDF mở bằng PyMuPDF, mỗi trang đọc `text_layer` —
  trang có đủ chữ (`>= SCAN_CHAR_THRESHOLD` ký tự) giữ nguyên text layer, không tốn GPU.
- **Tier 2 — PaddleOCR + VietOCR (GPU, chỉ chạy khi cần):** trang PDF gần như trống chữ (scan
  thuần/ảnh) được render ra ảnh rồi OCR nguyên trang; file ảnh (.jpg/.png/.bmp/.tiff/.webp) OCR
  thẳng. `source_lang="vi"`: PaddleOCR chỉ detect box chữ (bộ recognizer "vi" gốc thiếu dấu
  tiếng Việt), giao mỗi dòng cắt ra cho VietOCR nhận dạng — `en`/`fr` dùng thẳng pipeline
  detect+recognize gốc PaddleOCR.

Khoá GPU dùng CHUNG với `reup-transcribe-node`/`reup-tts-gpu-node` (Redis Lua script ưu tiên —
xem `queue_lib.py::gpu_lock_acquire`), priority **2.0** (thấp nhất, tts-gpu=0/transcribe=1) —
việc nền, nhường GPU cho 2 node kia khi tranh chấp. Khác 2 node đó (giữ khoá suốt cả job vài
phút + thread gia hạn nền), ở đây mỗi lần giữ khoá chỉ quanh ĐÚNG 1 lần OCR 1 trang/1 ảnh (vài
giây) — 1 lần acquire với lease đủ dài là an toàn, không cần thread gia hạn.

## Timestamp giả

Output JSON cùng schema `reup-transcribe-node` (`{language, duration_s, segments:
[{id,start,end,text}]}`) để `reup-translate-node`/`reup-tts-gpu-node` dùng thẳng không cần sửa —
nhưng `start`/`end` ở đây là **ước lượng theo tốc độ đọc** (~14 ký tự/giây, sàn 1.0s/đoạn),
KHÔNG phải audio thật. Chỉ để tương thích các chỗ `translate_cli.py` dùng `end-start` làm gợi ý
độ dài câu cho Deepseek + chia lại segment theo câu — nhánh sách không có audio gốc để đồng bộ
như nhánh video.

## Build & chạy (bắt buộc trên máy có GPU — `ssh hmtran@100.99.150.90`, không build/run trên máy sửa code)

```bash
# 1. Broker phải up trước (tạo network reup-net)
cd /u01/reup_tool/docker_build/reup-broker && docker compose up -d

# 2. Build + chạy node này
cd /u01/reup_tool/docker_build/ocr-node
docker compose build
docker compose up -d

# 3. Test tay 1 file, không qua queue (dev/test only)
docker compose run --rm worker cli -i /source/sach.pdf -o /outputs/transcript.json --source-lang vi
```

## API

```bash
curl -s -X POST http://localhost:8108/jobs -H 'Content-Type: application/json' -d '{
  "input": "/source/sach.pdf",
  "output": "/outputs/transcript.json",
  "source_lang": "vi"
}'
# -> {"ok": true, "job_id": "..."}

curl -s http://localhost:8108/jobs/<job_id>
```

`source_lang`: `vi` (mặc định, PaddleOCR detect + VietOCR recognize) | `en`/`fr` (PaddleOCR
detect+recognize gốc). Định dạng input nhận `.pdf`/`.docx`/`.pptx`/`.xlsx`/ảnh
(`.jpg`/`.jpeg`/`.png`/`.bmp`/`.tiff`/`.tif`/`.webp`) — đuôi khác bị từ chối ngay (`RuntimeError`
rõ ràng, không âm thầm bỏ qua).

## Structured log 2 ngày

`worker` ghi JSONL vào `./data/logs/YYYY-MM-DD.jsonl` (mount `/logs`), cùng convention mọi node
khác — `job_start`/`job_done`/`job_failed`, kèm `pipeline_id`/`video_name` nếu gọi từ
`reup-orchestrator-node`. Tắt bằng env `EVENT_LOG_ENABLED=false`.

## Mounts

`data/source` (input pdf/docx/ảnh), `data/outputs` (transcript JSON), `data/cache` (cache model
HuggingFace/VietOCR — persist để không tải lại), `data/logs` (structured log).

## Việc chưa làm (biết trước, chưa cần cho MVP)

Không streaming per-page cho PDF hàng nghìn trang (mở nguyên file bằng PyMuPDF, xử tuần tự từng
trang qua vòng lặp Python thường — RAM tăng theo số PARAGRAPH giữ trong list, không phải theo
số trang đã xử lý xong, nên vẫn ổn cho sách vài trăm trang; sách vài nghìn trang cần đo thật
trước khi kết luận có cần streaming ra đĩa ngay hay không). Không CUDA-OOM auto-retry/giảm batch,
không timeout cứng per-page, không audit-trail per-trang riêng (lỗi 1 trang hiện làm fail cả
job, không có cờ trạng thái từng trang) — thêm khi gặp lỗi thật trên sách dài, chưa build trước
cho lỗi chưa quan sát được.
