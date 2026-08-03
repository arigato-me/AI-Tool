FROM nvidia/cuda:12.8.0-runtime-ubuntu24.04

ENV TZ=Asia/Ho_Chi_Minh \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# ubuntu24.04 base has Python 3.12 by default — no deadsnakes PPA needed
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.12 \
        python3.12-dev \
        ffmpeg \
        espeak-ng \
        libespeak-ng1 \
        git \
        curl \
        wget \
        ca-certificates \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Đặt sau bước apt-get để sửa cache-dir không phải build lại apt layer (chậm nhất phần đầu).
ENV MODEL_CACHE_DIR=/cache \
    HF_HOME=/cache/huggingface \
    MODELSCOPE_CACHE=/cache/modelscope

COPY reup-transcribe-node/pyproject.toml /workspace/pyproject.toml
WORKDIR /workspace
RUN uv sync --no-dev

RUN mkdir -p /source /outputs /cache
WORKDIR /work

EXPOSE 8000

COPY reup-transcribe-node/scripts/ /opt/scripts/
COPY reup-transcribe-node/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /opt/scripts/transcribe_cli.py

ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
