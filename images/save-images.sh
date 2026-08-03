#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/images"

mkdir -p "$OUT"

save() {
  local image="$1"
  local file="$2"
  echo "==> Saving $image -> $file"
  docker save "$image" -o "$OUT/$file"
}

save "yt-dlp:local" "yt-dlp_local.tar"
save "vieneu-tts:local" "vieneu-tts_local.tar"
save "n8n-orchestrator:local" "n8n-orchestrator_local.tar"
save "transcribe:local" "transcribe_local.tar"
save "translate:local" "translate_local.tar"
save "ffmpeg-edit:local" "ffmpeg-edit_local.tar"
save "vieneu-tts-gpu:local" "vieneu-tts-gpu_local.tar"

echo "Done. Files in $OUT:"
ls -lh "$OUT"/*.tar
