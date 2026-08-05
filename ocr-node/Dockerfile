FROM nvidia/cuda:12.8.0-runtime-ubuntu24.04

ENV TZ=Asia/Ho_Chi_Minh \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# ubuntu24.04 base có sẵn Python 3.12 — không cần deadsnakes PPA.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.12 \
        python3.12-dev \
        git \
        curl \
        ca-certificates \
        build-essential \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
# libgl1/libglib2.0-0: opencv-python-headless tự nó không cần libGL, nhưng PaddleOCR/PaddleX kéo
# theo 1 import cv2 đầy đủ (không phải headless) đâu đó trong cây phụ thuộc — bug thật gặp lúc
# chạy worker lần đầu: "ImportError: libGL.so.1: cannot open shared object file".

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV MODEL_CACHE_DIR=/cache \
    HF_HOME=/cache/huggingface

COPY ocr-node/pyproject.toml /workspace/pyproject.toml
WORKDIR /workspace
RUN uv sync --no-dev

RUN mkdir -p /source /outputs /cache
WORKDIR /work

EXPOSE 8000

COPY ocr-node/scripts/ /opt/scripts/
COPY ocr-node/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /opt/scripts/ocr_cli.py

ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
