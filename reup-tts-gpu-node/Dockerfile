FROM nvidia/cuda:12.8.0-runtime-ubuntu24.04

ENV TZ=Asia/Ho_Chi_Minh \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/cache/huggingface \
    PHONEMIZER_ESPEAK_LIBRARY=/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    VIENEU_VOICE="Phạm Tuyên" \
    VIENEU_STYLE=tu_nhien

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.12 \
        python3.12-dev \
        espeak-ng \
        libespeak-ng1 \
        git \
        curl \
        wget \
        ca-certificates \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY repo_github/VieNeu-TTS /workspace
WORKDIR /workspace

RUN uv sync --no-dev --group gpu
# api+worker cần fastapi/uvicorn/redis — cài thêm vào cùng venv qua `uv pip install` thay vì
# sửa pyproject.toml của VieNeu-TTS (file vendored dùng chung với docker_vieneu-tts CPU, có
# git riêng — không đụng để tránh lệch với upstream / rò rỉ dependency sang bản CPU).
RUN uv pip install fastapi>=0.115 "uvicorn[standard]>=0.32" pydantic>=2.9 redis>=5.0 "librosa>=0.11.0"
# librosa: dùng cho time-stretch audio segment bị overrun trong segments_cli.py (xem đó) — có
# trong pyproject.toml gốc nhưng ở extra [gpu]/group dev khác với group "gpu" mà `uv sync` ở
# trên cài (không trùng), nên phải cài thêm riêng giống fastapi/uvicorn/pydantic/redis.

RUN mkdir -p /cache/huggingface /source /outputs /voice_samples

COPY reup-tts-gpu-node/scripts/ /opt/scripts/
COPY reup-tts-gpu-node/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /opt/scripts/tts_cli.py /opt/scripts/segments_cli.py /opt/scripts/generate_voice_samples.py

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
