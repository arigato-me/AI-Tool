FROM python:3.11-slim-bookworm

ENV TZ=Asia/Ho_Chi_Minh \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    YTDLP_HOME=/config

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp cần 1 JS runtime thật để giải "n challenge" của YouTube (đổi định kỳ, tự viết lại bằng
# Python không theo kịp) — thiếu thì client "web" chỉ lấy được ảnh storyboard, không lấy được
# format video/audio thật (bug thật gặp: video yêu cầu đăng nhập, có cookie hợp lệ vẫn chỉ ra
# "Only images are available" tới khi cài deno). Không ảnh hưởng Douyin (không cần JS runtime).
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && deno --version

COPY repo_github/yt-dlp /src/yt-dlp
WORKDIR /src/yt-dlp

RUN python -m pip install --upgrade pip \
    && python devscripts/make_lazy_extractors.py \
    && python -m pip install ".[default]"

COPY reup-ytdlp-node/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN mkdir -p /downloads /config
WORKDIR /downloads

COPY reup-ytdlp-node/scripts/ /opt/scripts/
COPY reup-ytdlp-node/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
