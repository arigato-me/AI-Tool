# Note run-test v1 — Mock test toàn bộ reup pipeline

Ngày chạy: 2026-07-15. Máy test: remote `hmtran@100.99.150.90` (RTX 3050 4GB VRAM). Người thực hiện: Claude Code, theo yêu cầu chạy mock test full luồng + tự fix bug + bypass node cần API key trả phí.

## 1. Môi trường đã cài / thay đổi

- **NVIDIA Container Toolkit** chưa có trên remote trước đó (`docker info` không thấy runtime `nvidia`) → đã cài (user tự chạy qua SSH tương tác, cần sudo password):
  ```bash
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
  sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
  ```
  Sau đó `docker info | grep -i runtime` phải thấy `nvidia`.
- **`repo_github/VieNeu-TTS`** đã pull từ `3.0.12` (2026-07-06) lên **`3.2.3`** (2026-07-13, 43 commit). Trước khi pull dính lỗi git do 117 file bị đổi mode 644→755 (do ai đó `chmod -R` trước đây) — đã fix bằng `git config core.fileMode false && git checkout -- .` rồi mới pull được (nội dung file không đổi, chỉ mode, an toàn để discard).
- Image `vieneu-tts-gpu:local` cũ (3.0.12) đã được giữ lại dưới tag `vieneu-tts-gpu:v3.0.12` trước khi build đè, để có đường lùi nếu bản mới lỗi.

## 2. Bug tìm thấy & đã fix

### 2.1. `duration_s` trong `tts_cli.py` đo sai — báo compute time, không phải audio length (đã fix, GPU)

File: `docker_vieneu-tts_gpu/scripts/tts_cli.py`. Field `duration_s` thực chất là `time.time() - start` (thời gian generate), KHÔNG phải độ dài audio. Verify chéo bằng `ffprobe` phát hiện audio thật dài gấp 2.4x–8.8x số CLI báo (không theo tỉ lệ cố định — tuỳ tải GPU tại thời điểm chạy).

**Fix đã áp dụng**: đổi tên `duration_s` → `synthesis_time_s`, thêm field mới `audio_duration_s` tính đúng bằng `len(audio) / tts.sample_rate`. Đã verify khớp chính xác với `ffprobe` (lệch ~0.1-0.2s do làm tròn frame).

```json
// trước: {"duration_s": 242.54, ...}   // sai, đây là thời gian generate
// sau:   {"synthesis_time_s": 61.64, "audio_duration_s": 571.08, ...}  // đúng
```

**⚠️ Chưa fix**: `docker_vieneu-tts/scripts/tts_cli.py` (bản CPU) có **y hệt bug này**, chưa đụng tới trong lần chạy này (ngoài phạm vi yêu cầu ban đầu). Nên fix tương tự khi có dịp.

### 2.2. Không phải bug — hiểu lầm ban đầu về "giọng Thục Đoan nói nhanh hơn"

Lúc benchmark 10 giọng, giọng Thục Đoan (chạy cuối) có `wall_s` thấp bất thường (83s so với 250-350s của giọng khác). Điều tra ra nguyên nhân: build image `3.2.3` mới (có tính năng `auto-batch v3 Turbo on GPU`) hoàn tất **giữa lúc** batch test 10 giọng đang chạy — tag `vieneu-tts-gpu:local` bị đổi qua bản mới ngay trong lúc test, khiến 2 giọng cuối (Mai Anh, Thục Đoan) chạy trên code khác 8 giọng đầu. Không so sánh chéo được kết quả benchmark đó. Bài học: **không build/rebuild image đang được dùng bởi 1 loop test đang chạy** — cần khoá version hoặc chờ loop xong.

## 3. Tính năng mới đã build: `segments_cli.py`

CLAUDE.md ghi rõ gap: "chưa có script nối các file .wav TTS theo từng segment thành 1 track hoàn chỉnh khớp timestamp trước khi đưa vào `ffmpeg-edit`". Đã build để lấp gap này phục vụ mock test full pipeline.

**File mới**: `docker_vieneu-tts_gpu/scripts/segments_cli.py`, gọi qua entrypoint mới `docker compose run vieneu-tts-gpu segments -i <translated.json> -o <track.wav> --voice <tên> --style <tu_nhien|tin_tuc|doc_truyen>`.

Cơ chế: load model `Vieneu()` **1 lần duy nhất** (tránh reload tốn thời gian mỗi segment), lặp qua từng segment trong JSON, TTS riêng từng câu, ghi vào buffer numpy tại đúng vị trí sample ứng với `start` (im lặng ở phần chưa ghi), tự mở rộng buffer nếu câu cuối vượt độ dài hiện có, rồi save 1 file wav duy nhất.

**Giới hạn đã biết**: nếu 1 câu TTS ra dài hơn khoảng `end - start` được cấp, nó sẽ đè lên phần đầu câu kế tiếp (không dịch chuyển câu sau ra) — chấp nhận được cho mock test, nhưng với transcript thật (Deepgram timestamp sát câu) cần theo dõi thêm, xem mục Đề xuất §6.

## 4. Kết quả test từng node (Phase A)

| Node | Kết quả | Ghi chú |
|---|---|---|
| `docker_yt-dlp` | ✅ PASS | Tải thật từ Douyin (`https://www.douyin.com/video/7661585197841829126`), xác nhận patch Douyin (abogus/cookie tự sinh) còn hoạt động. Video 1920x1080 HEVC, 6:39.98, 89.66MiB. |
| `docker_transcribe` | ✅ PASS (structural) | Không có `DEEPGRAM_API_KEY` thật → không gọi API thật được. Test lỗi thiếu key trả về đúng message + exit code 1. |
| `docker_translate` | ✅ PASS (structural) | Tương tự, thiếu `DEEPSEEK_API_KEY`, lỗi đúng chuẩn. |
| `docker_vieneu-tts_gpu` | ✅ PASS | Test kỹ trong session (10 giọng, sau đó fix bug §2.1, retest 1 giọng xác nhận đúng). |
| `docker_ffmpeg-edit` | ✅ PASS | Cả `srt` và `edit --subtitle-mode soft` chạy đúng, output đủ video+audio+subtitle stream (verify bằng ffprobe). |

Không phát sinh bug code thật nào ở Phase A (chỉ có 1 lần tôi tự trỏ sai path lúc test tay, không phải bug trong CLI).

## 5. Kết quả full pipeline (Phase D)

Vì `docker_transcribe`/`docker_translate` cần API key trả phí (Deepgram, Deepseek) mà remote chưa cấu hình `.env` thật, 2 bước này được **bypass bằng dữ liệu giả lập đúng schema** thay vì gọi API thật:

1. `docker_yt-dlp` tải video Douyin thật → `source.mp4` (6:39.98, 1080p HEVC + AAC).
2. Fabricate `transcript.json` (schema `transcribe_cli.py`) + `translated.json` (schema `translate_cli.py`) — 56 câu, timestamp chia đều theo đúng `duration_s` thật (399.98s) của `source.mp4`, nội dung tiếng Việt tái dùng từ bài lịch sử nhà Trần đã viết trong session (chỉ để test cơ chế, không liên quan nội dung video gốc).
3. `docker_vieneu-tts_gpu segments` (script mới §3) → `track.wav`: 56 segment, `synthesis_time_s=224.28`, `audio_duration_s=405.92` (verify ffprobe khớp `00:06:45.92`).
4. `docker_ffmpeg-edit srt` → `final.srt` từ `translated.json`.
5. `docker_ffmpeg-edit edit --subtitle-mode soft` mux `source.mp4` (chỉ hình) + `track.wav` + `final.srt` → `final.mp4`.
6. Verify `final.mp4` bằng ffprobe: **PASS** — đủ 3 stream (Video hevc 1920x1080, Audio aac 48kHz mono, Subtitle mov_text), duration 00:06:33.04 (hơi ngắn hơn cả video gốc lẫn audio mới do `-shortest` + giới hạn cắt tại keyframe của `-c:v copy`, không phải lỗi).

Toàn bộ output nằm ở:
- `docker_yt-dlp/data/downloads/source.mp4`
- `docker_transcribe/data/outputs/transcript.json` (fabricated)
- `docker_translate/data/outputs/translated.json` (fabricated)
- `docker_vieneu-tts_gpu/data/outputs/track.wav`
- `docker_ffmpeg-edit/data/outputs/final.srt`, `final.mp4`

## 6. Hướng dẫn chạy lại toàn bộ pipeline

```bash
# 1. Tải video nguồn
cd docker_yt-dlp
docker compose run --rm yt-dlp '<URL>' -o '/downloads/source.%(ext)s'

# 2. (Có API key thật) Transcribe:
cd ../docker_transcribe
docker compose run --rm transcribe -i /source/source.mp4 -o /outputs/transcript.json
# (Không có key: tự fabricate transcript.json theo schema {"language","duration_s","segments":[{"id","start","end","text"}]})

# 3. (Có API key thật) Translate:
cd ../docker_translate
docker compose run --rm translate -i /source/transcript.json -o /outputs/translated.json
# (Không có key: tự fabricate translated.json — copy transcript.json, thêm "text_original" = text gốc, "text" = bản dịch)

# 4. TTS ghép theo timestamp (script mới):
cd ../docker_vieneu-tts_gpu
docker compose run --rm vieneu-tts-gpu segments -i /source/translated.json -o /outputs/track.wav --voice "Phạm Tuyên" --style tin_tuc

# 5. Sinh srt + mux video final:
cd ../docker_ffmpeg-edit
docker compose run --rm ffmpeg-edit srt -i /source/translated.json -o /outputs/final.srt
docker compose run --rm ffmpeg-edit edit -v /source/source.mp4 -a /source/track.wav -s /outputs/final.srt --subtitle-mode soft -o /outputs/final.mp4
```

Lưu ý: mỗi service chỉ thấy được file trong `data/source` và `data/outputs` của chính nó — phải tự copy file output của bước trước vào `data/source` của bước sau (n8n Execute Command node sẽ tự làm việc này khi orchestrate thật).

## 7. Đề xuất nâng cấp

1. **Fix `duration_s` bug ở bản CPU** (`docker_vieneu-tts/scripts/tts_cli.py`) — y hệt lỗi đã fix ở bản GPU (§2.1), hiện chưa đụng tới.
2. **Test lại Phase B bằng API key thật** khi có — hiện tại chỉ verify được structural (lỗi thiếu key), chưa verify được response schema thật của Deepgram (`results.utterances`) và Deepseek (`{"translations":[...]}`) có khớp đúng những gì code kỳ vọng hay không. Đây là rủi ro lớn nhất chưa được test.
3. **n8n orchestrate chưa được test thật** — container `n8n` đang chạy (`Up 2 hours`) nhưng lần này toàn bộ pipeline chạy tay qua `docker compose run`, chưa xác nhận Execute Command node trong n8n gọi đúng các lệnh này với path tuyệt đối đúng như README mô tả.
4. **Quy ước tag image theo version** thay vì `:local` cố định — hiện tại rebuild sẽ ghi đè, dễ mất bản cũ nếu quên tag tay trước (đã gặp trong session này, phải tự nhớ tag `v3.0.12` trước khi build).
5. **`segments_cli.py` (script mới)**: giới hạn đã biết ở §3 — câu TTS dài hơn slot thời gian cấp sẽ đè lên câu kế tiếp thay vì đẩy lùi toàn bộ track. Với transcript Deepgram thật (timestamp theo utterance, thường khá sát câu nói thật) rủi ro này thấp hơn so với dữ liệu giả lập chia đều, nhưng nên thêm cảnh báo log khi phát hiện đè (`end_sample` của segment N > `start_sample` của segment N+1) để dễ debug khi dùng dữ liệu thật.
6. **Nên có script health-check nhanh** cho từng service (`--version`/`--list-voices`/dry-run) để không phải tự suy luận cách test mỗi lần như lần này.
7. **VieNeu-TTS nay đã ở 3.2.3** — có tính năng auto-batch GPU mới, nên retest lại hiệu năng CPU/ONNX build (`docker_vieneu-tts`) xem có cải thiện tương tự không, vì hiện tại `docker_vieneu-tts` (CPU) còn chưa từng được build/test trong repo này.

## 8. Cập nhật vòng 2 (2026-07-15)

Theo yêu cầu user: **bypass hoàn toàn bản CPU** (đề xuất #1, #7 ở trên — KHÔNG làm vòng này), thực hiện các đề xuất còn lại. Toàn bộ test chạy trên `hmtran@100.99.150.90`.

### Đã làm

- **#5 — `segments_cli.py` cảnh báo đè segment**: thêm log stderr khi segment hiện tại bắt đầu trước khi audio segment trước kết thúc (`start_sample < prev_end_sample`), không dừng chương trình. Verify bằng file test 2 segment cố ý chồng nhau: in đúng `Cảnh báo: segment id=1 start=2 bị đè 0.96s bởi audio segment trước...`, output wav vẫn sinh ra bình thường, `"ok": true`.
- **#6 — `healthcheck.sh`** (file mới, root repo): chạy lệnh nhẹ không tốn phí cho cả 6 service (`--version`/`--help`/`--list-voices`/`docker version` qua n8n). Chạy trên remote: **PASS cả 6/6** (yt-dlp, transcribe, translate, vieneu-tts-gpu, ffmpeg-edit, n8n).
- **#4 — Quy ước tag version**: đã áp dụng thật trên remote — `yt-dlp:2026-07-15`, `transcribe:2026-07-15`, `translate:2026-07-15`, `ffmpeg-edit:2026-07-15`, `n8n-orchestrator:2026-07-15`, `vieneu-tts-gpu:v3.2.3` (bên cạnh các tag `:local` hiện có). Quy ước đã ghi vào CLAUDE.md.
- **#2 (phần translate) — Test thật Deepseek API**: tạo `.env` thật trên remote (`docker_translate/.env`, không sync về local/không commit), fabricate `transcript_zh.json` (5 câu tiếng Trung ngắn). Chạy `translate` thật: `{"ok": true, "segments": 5, "elapsed_s": 1.76}`. Kiểm tra output: dịch Trung→Việt đúng nghĩa, tự nhiên, giữ nguyên `start`/`end`, `text_original` đúng câu tiếng Trung gốc. **Schema response Deepseek khớp hoàn toàn kỳ vọng của code** (`{"translations":[...]}`, đúng số lượng). File test đã dọn sau khi xong.
  - **#2 (phần transcribe/Deepgram) vẫn CHƯA test thật** — không có key, vẫn treo như vòng 1.
- **#3 — Test thật n8n orchestrate**: ban đầu chạy `docker compose exec n8n sh -c 'cd .../docker_vieneu-tts_gpu && docker compose run --rm vieneu-tts-gpu tts --list-voices'` — thành công, in đúng 14 voice y hệt chạy trực tiếp, xác nhận DooD + GPU passthrough hoạt động khi gọi từ n8n.
  - Phát hiện thêm (không phải bug): README mẫu dùng path `/u01/reup_tool/docker_build`, remote thực tế dùng `/home/hmtran/docker_build` và gid `973` (không phải `986` trong ví dụ) — deployment trên remote đã tự đúng, chỉ dễ nhầm nếu copy lệnh y nguyên từ README. Đã ghi chú vào CLAUDE.md.

### 🐛 Bug nghiêm trọng phát hiện thêm: n8n v2 tắt Execute Command node mặc định

Khi thử dựng 1 workflow **thật** (7 node Execute Command nối tiếp: fabricate transcript → translate → copy → tts segments → copy → srt → mux) và chạy bằng `n8n execute --id ...` để kiểm tra engine orchestrate thật (không chỉ `docker compose exec` tay), gặp lỗi:

```
Unrecognized node type: n8n-nodes-base.executeCommand
```

Đào ra nguyên nhân: từ n8n v2, `Execute Command` và `Local File Trigger` bị **tắt mặc định vì lý do bảo mật** (`@n8n/config` `NodesConfig.exclude = ['n8n-nodes-base.executeCommand', 'n8n-nodes-base.localFileTrigger']`), áp dụng bất kể chạy qua UI hay CLI. Toàn bộ thiết kế orchestrate của project này (`docker_n8n/README.md` — "Cơ chế orchestrate") **dựa hoàn toàn vào Execute Command node** — nghĩa là với bản n8n hiện tại (`n8n --version` = `2.30.4`), orchestrate qua n8n **hoàn toàn không hoạt động** cho tới khi phát hiện và fix lỗi này, dù image build/run bình thường không báo lỗi gì (chỉ lộ ra khi thật sự tạo/chạy workflow có Execute Command node).

**Fix đã áp dụng**: thêm `NODES_EXCLUDE=[]` vào `environment` của `docker_n8n/docker-compose.yml` (cả local và remote) để override default exclude list thành rỗng. Đã `docker compose up -d` lại để áp dụng, verify lại bằng cách chạy full workflow — thành công (xem bên dưới). Đã ghi vào CLAUDE.md + `docker_n8n/README.md` (mục mới "Cơ chế orchestrate" điểm bắt buộc thứ 3) để không ai bị dính lại lỗi này khi nâng cấp image n8n trong tương lai.

### Chạy full pipeline thật qua n8n execution engine (không phải gọi tay)

Sau khi fix `NODES_EXCLUDE`, import + chạy workflow 7 node (`Manual Trigger` → fabricate transcript tiếng Trung (bypass Deepgram) → `translate` thật (Deepseek key thật, đã lưu vào `docker_translate/.env` cả local lẫn remote) → copy → `vieneu-tts-gpu segments` (GPU) → copy → `ffmpeg-edit srt` → `ffmpeg-edit edit --subtitle-mode soft`) bằng `docker compose run --rm n8n execute --id reup-pipeline-test-001` (phải `stop` container `up -d` chính trước do CLI `execute` tự mở Task Broker port 5679, xung đột nếu instance chính đang chạy — `stop`/`run --rm ... execute`/`up -d` lại, không mất dữ liệu vì DB là SQLite trong volume `data/`).

**Kết quả: `"status": "success"`, cả 7/7 node `executionStatus: "success"`, `exitCode: 0`**, tổng thời gian ~27s. Verify `final_n8n.mp4` bằng ffprobe: `Duration: 00:00:15.20`, đủ 3 stream (`Video hevc 1920x1080`, `Audio aac 48000Hz mono`, `Subtitle mov_text`) — đúng thiết kế (video hình gốc + audio TTS mới + sub mềm). Đây là bài test mạnh nhất cho đề xuất #3: chứng minh **toàn bộ pipeline chạy được thật sự qua chính engine thực thi của n8n**, không chỉ gọi tay `docker compose exec`.

Workflow test còn nằm trong DB n8n trên remote với id `reup-pipeline-test-001` (tên "Reup Pipeline Test (n8n orchestrate)") — có thể mở qua UI (`http://100.99.150.90:5678`) để xem trực quan, hoặc xoá nếu không cần giữ.

### Bug thêm: không login được UI qua http (secure cookie)

Sau khi orchestrate đã chạy được, phát hiện UI n8n (`http://100.99.150.90:5678`) không login được — trang login load bình thường nhưng submit xong quay vòng lại login. Nguyên nhân: n8n mặc định set cookie session dạng `Secure`, browser không gửi lại cookie này qua kết nối http thường (không có TLS) → mất session ngay sau khi login.

**Fix**: thêm `N8N_SECURE_COOKIE=false` vào `environment` của `docker_n8n/docker-compose.yml` (cả local và remote), `docker compose up -d` lại. Verify: `curl -I http://100.99.150.90:5678/` trả `200 OK`. Đã ghi vào CLAUDE.md + README (điểm bắt buộc thứ 4 trong "Cơ chế orchestrate").

### Bug thêm: đổi path repo trên remote (`/home/hmtran/docker_build` → `/home/hmtran/Projects/docker_build`), n8n mount lệch

User đổi vị trí thư mục repo trên remote. Kiểm tra phát hiện:
- `docker_n8n/docker-compose.yml` volume bind-mount vẫn trỏ path CŨ (`/home/hmtran/docker_build:/home/hmtran/docker_build`) — vì file này nằm bên trong chính thư mục bị move nên nội dung không tự cập nhật theo.
- Container `n8n` (`restart: unless-stopped`) đã **tự Exited** (path cũ mất, mount gãy) — service orchestrate đã ngưng hoạt động từ lúc move tới lúc phát hiện.
- **Rủi ro suýt xảy ra**: nếu chạy `docker compose up -d` mà KHÔNG sửa path trước, container sẽ sống lại nhưng mount sai chỗ (`/home/hmtran/docker_build` không tồn tại thật trên host) — mọi lệnh Execute Command gọi `docker compose run` cho service khác sẽ khiến Docker daemon **tự tạo thư mục rỗng ở path cũ** để bind-mount (hành vi mặc định khi bind source không tồn tại), ghi output vào chỗ chết, không báo lỗi gì — đúng kiểu lỗi CLAUDE.md đã cảnh báo trước ("Critical convention"). Đã kiểm tra: **chưa có lệnh orchestrate nào chạy trong khoảng thời gian path bị lệch**, nên không có dữ liệu bị ghi nhầm chỗ — bắt kịp trước khi phát sinh hậu quả.

**Fix**: sửa `docker_n8n/docker-compose.yml` volume mount sang path mới (`/home/hmtran/Projects/docker_build:/home/hmtran/Projects/docker_build`), `docker compose up -d` lại (container tự Recreate, `docker inspect` xác nhận mount đúng path mới). Sửa lại path trong workflow test `reup-pipeline-test-001` (các node Execute Command đang hardcode path cũ), re-import. Chạy lại `n8n execute --id reup-pipeline-test-001` toàn bộ 7 node **thành công**, verify `final_n8n.mp4` bằng ffprobe khớp y hệt lần trước (15.20s, đủ 3 stream). Verify thêm: `/home/hmtran/docker_build` (path cũ) vẫn không tồn tại — xác nhận không có thư mục rác nào bị tạo nhầm trong lúc test.

**Lưu ý cho tương lai**: mỗi lần đổi vị trí thư mục repo trên bất kỳ host nào đang chạy n8n, PHẢI sửa `docker_n8n/docker-compose.yml` (dòng volume bind-mount tuyệt đối) và `docker compose up -d` lại **trước khi** chạy bất kỳ workflow nào — nếu không sẽ ghi dữ liệu vào thư mục ma âm thầm. Nên cân nhắc thêm 1 bước kiểm tra định kỳ (`docker inspect n8n` so mount path với `pwd` thật) vào `healthcheck.sh`.

### Rủi ro/giới hạn từng node (tổng hợp từ đọc code + test thật vòng này)

- **yt-dlp**: patch Douyin (`abogus.py`/`douyin_cookies.py`) tự sinh `a_bogus`/cookie bằng reverse-engineering riêng, không official — nếu Douyin đổi cơ chế anti-bot, patch có thể hỏng bất cứ lúc nào, không có test tự động phát hiện việc này ngoài chạy thử tay.
- **transcribe**: 1 lần gọi Deepgram duy nhất (không chunk), timeout 600s, load toàn bộ audio đã trích vào RAM dạng `bytes` trước khi gửi — video rất dài/audio lớn có thể tốn RAM và dễ timeout hơn. Không có retry khi lỗi mạng/rate-limit.
- **translate**: batch 20 segment/lần, timeout 180s/batch; nếu 1 batch bất kỳ trả sai số lượng câu, **toàn bộ lệnh lỗi ngay, không lưu các batch trước đã dịch thành công** — với transcript nhiều batch, 1 lần Deepseek "lỗi đếm" ở batch cuối làm mất công dịch các batch đầu. Không có retry/backoff khi Deepseek rate-limit (429).
- **vieneu-tts_gpu segments**: segment TTS dài hơn slot thời gian sẽ đè lên đầu segment kế tiếp — giờ đã log cảnh báo (không tự động dịch chuyển timeline). Chạy tuần tự từng segment (gọi `tts.infer()` từng câu một) dù engine v3 Turbo có tính năng auto-batch GPU — chưa tận dụng để tăng thông lượng khi ghép nhiều segment.
- **ffmpeg-edit**: `-shortest` cắt output theo track ngắn hơn, và dưới `-c:v copy` còn bị giới hạn thêm bởi keyframe boundary gần nhất — thời lượng final.mp4 có thể ngắn hơn cả video gốc lẫn audio mới vài giây (đã gặp thật, không phải lỗi nhưng cần biết trước). `burn` mode luôn re-encode (chậm, tốn CPU). Escape path phụ đề chỉ xử lý `:`/`\`/`'`.
- **n8n**: cấu hình `group_add` gid + bind-mount path phải khớp tuyệt đối theo từng host triển khai (đã xác nhận thực tế remote khác local) — không có cơ chế tự phát hiện sai gid/path, sai thì mọi Execute Command node âm thầm fail hoặc mount nhầm chỗ.
