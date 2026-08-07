# reup-ui

Giao diện vận hành — gọi thẳng API `reup-orchestrator-node` (không docker.sock, không Execute
Command node). React + Vite SPA, build multi-stage (`node:20-alpine` build → `nginx:alpine`
serve static + reverse-proxy `/api/*`). (`docker_n8n`, giao diện vận hành trước đây, đã archive
vào `legacy/`, không còn deploy.)

## Kiến trúc

- **Không dùng router library** (react-router...) — tự viết hash-routing tối giản trong
  `App.tsx` (`#/submit`, `#/import`, `#/jobs`, `#/monitor`, `#/job/<id>`) để không thêm
  dependency không cần thiết.
- **`nginx.conf`** reverse-proxy `location /api/` → `http://reup-orchestrator-node-api:8000/` —
  trình duyệt chỉ thấy 1 origin (chính `reup-ui`), không cần bật CORS trên FastAPI của
  orchestrator.
- 6 trang:
  - `SubmitJob` (`#/submit`) — form tạo 1 pipeline: URL, tên video (tuỳ chọn, xuyên suốt file
    trung gian + file xuất cuối), chọn nhánh review/dialogue, **select voice** (load thật từ
    `GET /voices`, hiện số lượng) + option "Khác — Clone giọng" (upload file WAV mẫu 3-5s, gửi
    base64), **select style** (`GET /styles`), subtitle mode, **chọn nhạc nền** (chỉ hiện ở
    nhánh review — chọn project/track từ thư viện qua `GET /music/projects*`, link sang
    `#/music` để quản lý thư viện). Nhánh **mix** (ghép nhiều video + nhiều nhạc nối tiếp, không
    transcribe/dịch/TTS/sub) đổi hẳn sang danh sách item thêm/xoá được thay vì 1 ô URL — mỗi
    dòng chọn kiểu nguồn (URL/upload/tái dùng pipeline_id cũ/thư viện nhạc nền cho audio).
  - `ImportCsv` (`#/import`) — nạp file CSV (`STT,video_name,link`, có nút tải template),
    cài đặt chung (nhánh/voice/style/subtitle/nhạc nền, dùng chung cho cả danh sách) áp cho mọi
    dòng, bảng xem trước + nút Chạy/Hủy, submit tuần tự qua đúng `POST /pipelines` (hàng đợi
    backend tự xử lý lần lượt, không cần chờ xong job này mới tạo job kia).
  - `MusicLibrary` (`#/music`) — quản lý thư viện nhạc nền theo project/theme: tạo project
    (`POST /music/projects`), upload track (`POST /music/projects/{slug}/tracks`, gửi base64),
    nghe thử trực tiếp trong trình duyệt (`GET /music/projects/{slug}/tracks/{track}/raw`).
  - `Jobs` (`#/jobs`) — bảng job gần đây (tên video, mode, trạng thái), poll 5s.
  - `Monitor` (`#/monitor`) — dashboard: pending/processing/tổng job, đèn xanh/đỏ 6 worker
    (`GET /nodes/status`), số video xử lý xong hôm nay/tuần này/tháng này, bảng job phân trang
    10/trang (tối đa 200, `GET /pipelines?limit=200`).
  - `JobDetail` (`#/job/<id>`) — poll 3s, hiển thị từng stage qua `StageTimeline`, phát/tải
    video final khi `status=finished` (tên file `<mode>_<tên_video>.mp4`).

## Build & chạy (phải deploy cùng host với `reup-orchestrator-node`, cùng network `reup-net` — `ssh hmtran@100.99.150.90`, không chạy trên máy sửa code)

```bash
cd /u01/reup_tool/docker_build/reup-ui
docker compose build
docker compose up -d
```

Mở `http://<host>:8107`.

## Phát triển local (không qua Docker)

```bash
npm install
npm run dev   # cần sửa proxy dev server trỏ tới orchestrator nếu test ngoài Docker network
```

## Volume

Không có — SPA tĩnh, không lưu trạng thái, mọi dữ liệu lấy qua API orchestrator lúc chạy.

## Troubleshooting

| Vấn đề | Cách xử lý |
|---|---|
| Trang trắng, console lỗi 404 `/api/...` | `reup-orchestrator-node-api` chưa `up`, hoặc chưa cùng network `reup-net` |
| Submit job báo lỗi network | Kiểm tra `docker compose logs ui` (nginx) và `reup-orchestrator-node-api` logs |
| Video final không phát được | Job `status` phải là `finished`; nếu `failed`, xem `error`/từng `stage.error` trên trang detail |
