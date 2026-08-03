# images/

Lưu trữ tập trung các Docker image đã export (`.tar`).

## File hiện có

| File | Image |
|---|---|
| `yt-dlp_local.tar` | `yt-dlp:local` |
| `vieneu-tts_local.tar` | `vieneu-tts:local` |
| `n8n_latest.tar` | `docker.n8n.io/n8nio/n8n:latest` |
| `transcribe_local.tar` | `transcribe:local` |
| `translate_local.tar` | `translate:local` |
| `ffmpeg-edit_local.tar` | `ffmpeg-edit:local` |
| `vieneu-tts-gpu_local.tar` | `vieneu-tts-gpu:local` |

## Lệnh

```bash
# Export image ra folder này
./images/save-images.sh

# Import image từ folder này
./images/load-images.sh
```

## Import thủ công

```bash
docker load -i images/yt-dlp_local.tar
docker load -i images/vieneu-tts_local.tar
docker load -i images/n8n_latest.tar
docker load -i images/transcribe_local.tar
docker load -i images/translate_local.tar
docker load -i images/ffmpeg-edit_local.tar
docker load -i images/vieneu-tts-gpu_local.tar
```
