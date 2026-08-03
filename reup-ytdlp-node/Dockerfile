FROM python:3.11-slim-bookworm

ENV TZ=Asia/Ho_Chi_Minh \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    YTDLP_HOME=/config

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

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
