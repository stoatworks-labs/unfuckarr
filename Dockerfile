# unfuckarr
#
# Single stage on purpose: the runtime needs ffmpeg regardless, and ffmpeg is
# most of the image, so a builder stage would save nothing worth the
# complexity. Python deps are wheels; there is nothing to compile.

FROM python:3.12-slim-bookworm

ARG TARGETARCH
ARG VERSION=dev

LABEL org.opencontainers.image.title="unfuckarr" \
      org.opencontainers.image.description="Validates a Sonarr/Radarr library against Emby, then transcodes or re-downloads what is broken." \
      org.opencontainers.image.source="https://github.com/stoatworks-labs/unfuckarr" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UNFUCKARR_CONFIG_DIR=/config \
    UNFUCKARR_PORT=6969 \
    PUID=99 \
    PGID=100 \
    UMASK=002

# ffmpeg is the whole point; gosu drops to the PUID/PGID Unraid expects.
# The VAAPI/QSV drivers are amd64-only — an arm64 build installs neither and
# hardware transcoding is simply unavailable there.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        gosu \
        tini \
        ca-certificates; \
    if [ "${TARGETARCH}" = "amd64" ]; then \
        apt-get install -y --no-install-recommends \
            intel-media-va-driver \
            i965-va-driver \
            mesa-va-drivers \
            vainfo; \
    fi; \
    rm -rf /var/lib/apt/lists/*; \
    ffmpeg -version | head -1; \
    ffprobe -version | head -1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY unfuckarr/ ./unfuckarr/
COPY web/ ./web/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Fail the build rather than shipping an image whose web UI is missing —
# a broken COPY otherwise only shows up as a 500 after deploy.
RUN test -f /app/web/index.html && test -f /app/web/app.js \
    && python -c "import unfuckarr.api" \
    && echo "web assets and app import OK"

VOLUME ["/config"]
EXPOSE 6969

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"UNFUCKARR_PORT\",\"6969\")}/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["python", "-m", "unfuckarr"]
