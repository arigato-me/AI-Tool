# reup-notify-node

Gửi thông báo/kết quả (text + file) qua Telegram, WhatsApp, Zalo, Facebook Messenger (tài
khoản cá nhân) hoặc webhook tới webapp tự phát triển. Đúng convention job-queue chung (api +
worker dùng chung `reup-broker`) — xem `CLAUDE.md` gốc repo.

**Chưa nối vào `reup-orchestrator-node`** — node đứng độc lập, gọi tay qua `POST /jobs` để
test. Nối vào pipeline (gửi tự động sau khi video/audio xong) là bước làm sau.

## 2 nhóm platform — rủi ro khác hẳn nhau

- **`telegram`**: Bot API chính chủ — an toàn, không cookie/session.
- **`webhook:<name>`**: POST tới webapp tự phát triển của mày — dùng token/header đã lưu sẵn.
- **`whatsapp`/`zalo`/`messenger`**: tài khoản cá nhân, KHÔNG có API chính chủ cho mục đích
  này — dùng Playwright lái trình duyệt (headless Chromium) nạp session đã lưu để bỏ qua login,
  giống hệt "đăng nhập bằng cookie". Rủi ro khoá tài khoản thật, đặc biệt **Facebook Messenger**
  (Meta chống automation gắt nhất, DOM đổi liên tục, có luồng 2FA duyệt-từ-thiết-bị-khác không
  script được). Build/test WhatsApp và Zalo trước.

## Setup credential (lần đầu)

```bash
cp telegram.yaml.example data/config/telegram.yaml            # điền bot_token (tạo qua @BotFather) + default_chat_id
cp webhooks.yaml.example data/config/webhooks.yaml             # nếu dùng webhook:<name>
cp browser_targets.yaml.example data/config/browser_targets.yaml  # nếu dùng whatsapp/zalo/messenger
```

Cả 3 file trên nằm trong `data/config/` — đã gitignore qua rule `*/data/` sẵn có ở repo root,
không commit nhầm.

## Build & chạy (remote host, cần docker — máy dev không build được)

```bash
cd reup-broker && docker compose up -d   # nếu chưa up
cd ../reup-notify-node
docker compose build
docker compose up -d
```

## Bootstrap session whatsapp/zalo/messenger (1 lần, máy remote chỉ SSH không màn hình)

```bash
docker compose run --rm worker cli browser-login whatsapp   # hoặc zalo, messenger
```

Script chạy Chromium headless, chụp `data/config/browser_sessions/<platform>_login.png` định
kỳ trong lúc chờ — xem ảnh bằng:

```bash
scp hmtran@100.99.150.90:/home/hmtran/Projects/docker_build/reup-notify-node/data/config/browser_sessions/whatsapp_login.png .
```

Quét mã QR trong ảnh bằng điện thoại (WhatsApp/Zalo). Riêng `messenger` sẽ hỏi
email/mật khẩu ngay trong terminal SSH trước (không cần quét QR, nhưng có thể dính checkpoint
2FA — xem cảnh báo ở trên). Đăng nhập xong, script tự lưu
`data/config/browser_sessions/<platform>.json` — dùng lại cho mọi lần gửi sau, tự refresh sau
mỗi lần `send()` (giống cookiejar tự ghi lại của `reup-ytdlp-node`). Khi session chết thật
(logout/đổi mật khẩu), lỗi trả về sẽ nói rõ cần chạy lại lệnh trên — không có auto re-login.

## API

### `POST /jobs`

```bash
curl -s -X POST http://localhost:8109/jobs -H 'Content-Type: application/json' \
  -d '{"platforms": ["telegram", "webhook:myapp"], "message": "Video xong rồi", "file_path": "/source/final.mp4"}'
# -> {"ok": true, "job_id": "..."}
```

`platforms`: danh sách `"telegram"` / `"whatsapp"` / `"zalo"` / `"messenger"` /
`"webhook:<name>"` (name phải có sẵn trong `webhooks.yaml`, check ngay lúc submit). `file_path`
là path **trong container này** (`/source/...`) — copy file cần gửi vào `data/source/` trước.
`chat_id` (tuỳ chọn) override `default_chat_id` của Telegram cho riêng job đó.

1 job có thể gửi nhiều platform cùng lúc — platform nào lỗi không kéo job fail, xem field
`results` trong response `GET /jobs/{id}`:

```json
{"result": {"ok": true, "sent": 1, "total": 2, "results": {
  "telegram": {"ok": true},
  "whatsapp": {"ok": false, "error": "chưa đăng nhập whatsapp — chạy: docker compose run --rm worker cli browser-login whatsapp"}
}}}
```

### `GET /jobs/{id}`, `GET /health`

Giống mọi node khác — xem `reup-translate-node/README.md`.

## Volume

`./data/source` (file cần gửi), `./data/config` (credential: `telegram.yaml`, `webhooks.yaml`,
`browser_targets.yaml`, `browser_sessions/*.json`), `./data/logs` (structured log 2 ngày, giống
mọi node khác).

## Test logic dispatch (không cần Redis/Playwright/credential thật)

```bash
cd scripts && python3 test_notify_cli.py
```
