# Hướng dẫn triển khai & sử dụng — Reup Pipeline

Kiến trúc: 7 node Docker độc lập, mỗi node build/chạy riêng, không phụ thuộc runtime lẫn nhau. `n8n` đóng vai trò nhạc trưởng — gọi từng node qua Docker-out-of-Docker (DooD), không nhúng logic của node nào vào n8n.

## 1. Trạng thái build hiện tại

| Node | Image | Trạng thái | Vai trò |
|---|---|---|---|
| yt-dlp | `yt-dlp:local` | đã build | tải video gốc |
| transcribe | `transcribe:local` | đã build | video → transcript JSON (Deepgram) |
| translate | `translate:local` | đã build | transcript JSON → JSON tiếng Việt (Deepseek) |
| vieneu-tts | `vieneu-tts:local` | đã build | text → wav (CPU/ONNX) |
| vieneu-tts-gpu | `vieneu-tts-gpu:local` | đã build, **không chạy được** | máy này không có GPU (`nvidia-smi` không tồn tại) |
| ffmpeg-edit | `ffmpeg-edit:local` | đã build | mux/burn audio+sub vào video, xuất srt |
| n8n | `n8n-orchestrator:local` | đã build, đang chạy | điều phối toàn bộ, UI http://localhost:5678 |

Rebuild từng node (khi sửa code):
```bash
cd docker_<ten-node>/ && docker compose build
```

## 2. Cấu hình API key (bắt buộc trước khi transcribe/translate chạy thật)

```bash
cd docker_transcribe && cp .env.example .env   # điền DEEPGRAM_API_KEY
cd docker_translate  && cp .env.example .env   # điền DEEPSEEK_API_KEY
```

`docker compose run` tự đọc `.env` trong cùng thư mục service.

## 3. Chạy tay từng node (kiểm thử / dùng độc lập, không qua n8n)

```bash
# 1. Tải video
cd docker_yt-dlp
docker compose run --rm yt-dlp -o "/downloads/%(id)s.%(ext)s" "<URL video>"

# 2. Transcript
cd ../docker_transcribe
docker compose run --rm transcribe -i /source/<id>.mp4 -o /outputs/<id>.json

# 3. Dịch
cd ../docker_translate
docker compose run --rm translate -i /source/<id>.json -o /outputs/<id>_vi.json

# 4. Sinh phụ đề .srt từ transcript đã dịch
cd ../docker_ffmpeg-edit
docker compose run --rm ffmpeg-edit srt -i /source/<id>_vi.json -o /outputs/<id>.srt

# 5. TTS từng đoạn text -> wav (xem mục 5, chưa có script ghép tự động)
cd ../docker_vieneu-tts
docker compose run --rm vieneu-tts tts -i /source/<id>_seg01.txt -o /outputs/<id>_seg01.wav --voice "Phạm Tuyên" --style tu_nhien

# 6. Ghép video gốc + audio mới + srt
cd ../docker_ffmpeg-edit
docker compose run --rm ffmpeg-edit edit -v /source/<id>.mp4 -a /source/<id>_full.wav -o /outputs/<id>_final.mp4 -s /outputs/<id>.srt --subtitle-mode soft
```

Mỗi service có `data/source` (input) và `data/outputs` (output) **riêng biệt** — không share ổ đĩa giữa các node. Copy file thủ công (hoặc qua n8n, xem mục 4) giữa `outputs` của node trước và `source` của node sau.

## 4. n8n orchestrate — cách hoạt động

`n8n-orchestrator:local` = image n8n gốc + docker CLI + compose plugin (copy binary tĩnh từ image `docker:cli`, vì image n8n gốc là Alpine hardened, không có `apk`). Container này:

- Mount `/var/run/docker.sock` → gọi thẳng daemon Docker của **host**.
- `group_add: ["986"]` (gid nhóm `docker` trên host) → user `node` trong container đọc/ghi được socket mà không cần chạy root (n8n cấm chạy root).
- Mount `/u01/reup_tool/docker_build` vào **đúng path đó** bên trong container → khi Execute Command node gọi `docker compose run` cho node khác, daemon (chạy trên host) resolve `./data/...` theo working-dir hiện tại, phải trùng path thật trên host thì bind-mount mới đúng chỗ.

Verify:
```bash
cd docker_n8n
docker compose exec n8n docker compose version
docker compose exec n8n sh -c 'cd /u01/reup_tool/docker_build/docker_transcribe && docker compose run --rm transcribe --help'
```

### Workflow n8n mẫu (Execute Command node nối tiếp nhau)

Mỗi bước = 1 Execute Command node, lệnh dạng `cp <output node trước> <source node sau> && cd <thư mục node> && docker compose run --rm <service> ...`. Ví dụ node 2 (transcribe), lấy `<id>` từ output node 1:

```bash
cp /u01/reup_tool/docker_build/docker_yt-dlp/data/downloads/{{$json.id}}.mp4 \
   /u01/reup_tool/docker_build/docker_transcribe/data/source/ && \
cd /u01/reup_tool/docker_build/docker_transcribe && \
docker compose run --rm transcribe -i /source/{{$json.id}}.mp4 -o /outputs/{{$json.id}}.json
```

Mỗi CLI in 1 dòng JSON kết quả ra stdout — n8n parse bằng node "Code" hoặc field `stdout` của Execute Command để lấy path output truyền sang node kế.

## 5. Known gap — chưa tự động hóa hoàn toàn

Chain đứt ở bước TTS → ghép audio: `docker_vieneu-tts` chỉ có CLI 1 file text → 1 file wav, **chưa có script** lặp qua từng segment của transcript JSON, gọi TTS cho từng đoạn, rồi ghép lại thành 1 track dài đúng theo `start`/`end` timestamp (chèn khoảng lặng — silence padding) trước khi đưa vào `ffmpeg-edit edit`. Hiện phải làm tay bước này (hoặc viết thêm 1 CLI mới, chưa nằm trong scope build lần này — nói nếu muốn làm tiếp).

## 6. Export / import toàn bộ image

```bash
cd images
./save-images.sh   # -> images/*.tar (bao gồm n8n-orchestrator_local.tar)
./load-images.sh   # nạp lại tar -> docker images
```

## 7. Ghi chú ổn định từng node

- yt-dlp, transcribe, translate, ffmpeg-edit: container **one-shot**, chạy xong tự exit, không giữ state — an toàn gọi lặp lại nhiều lần song song (mỗi lần 1 container mới).
- vieneu-tts, n8n: `restart: unless-stopped`, chạy nền dài hạn (web UI).
- vieneu-tts-gpu: build được nhưng **không khởi động được trên máy này** — thiếu GPU/NVIDIA Container Toolkit. Nếu sau này chuyển sang máy có GPU: cài NVIDIA Container Toolkit rồi `docker compose up -d` bình thường, không cần build lại.
