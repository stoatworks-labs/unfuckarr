# unfuckarr
#
# Two stages, and only because ffmpeg is not something a distro can supply here.
#
# The application needs one ffmpeg that can do two things at once: encode with
# VAAPI, and measure quality with libvmaf. No distribution ships that. Debian
# bookworm's 5.1.9 and trixie's 7.1.5 both build without libvmaf *and* both
# have a VAAPI bug that pads a 1080p encode to 1088 without signalling a
# conformance window — so the output decodes eight rows taller than its source
# while the container metadata still says 1080, which means ffprobe reports the
# right number and nothing looks wrong. That silently breaks every quality
# comparison and, worse, would replace people's files with mis-shaped ones.
#
# ffmpeg 8 fixes the padding, and linuxserver.io publish an 8.x build with
# --enable-vaapi and --enable-libvmaf together, multi-arch, which is the same
# ffmpeg Shrinkray uses to do VAAPI encodes on this hardware. It also bundles
# its own VA drivers (radeonsi, iHD, i965), so this image no longer installs
# Mesa at all — which retires the bookworm-backports workaround that used to
# be needed for any AMD GPU newer than RDNA2.
#
# Ubuntu 24.04 rather than a python: image because that is what the ffmpeg
# build is compiled against (glibc 2.39), and it ships Python 3.12 natively.

ARG FFMPEG_IMAGE=lscr.io/linuxserver/ffmpeg:8.0.1-cli-ls58
FROM ${FFMPEG_IMAGE} AS ffmpeg

FROM ubuntu:24.04

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
    UMASK=002 \
    PATH=/opt/venv/bin:/usr/local/bin:$PATH \
    LD_LIBRARY_PATH=/usr/local/lib \
    LIBVA_DRIVERS_PATH=/usr/local/lib/x86_64-linux-gnu/dri:/usr/local/lib/aarch64-linux-gnu/dri

# ffmpeg, ffprobe, and everything they link — including the VA drivers, which
# is why no Mesa package is installed below.
COPY --from=ffmpeg /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /usr/local/bin/
COPY --from=ffmpeg /usr/local/lib /usr/local/lib

# gosu drops to the PUID/PGID Unraid expects; tini reaps the ffmpeg children.
#
# The long list after them is what that ffmpeg build and its VA drivers link
# against on Ubuntu. libllvm18 is the one nobody guesses: Mesa's radeonsi
# compiles shaders through LLVM, so without it the driver loads and then fails
# at device creation with nothing more useful than "unknown libva error".
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        gosu \
        tini \
        ca-certificates \
        libllvm18 \
        libdrm2 \
        libelf1 \
        libexpat1 \
        libpciaccess0 \
        libxshmfence1 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcb-shm0 \
        libxcb-shape0 \
        libxcb-xfixes0 \
        libxcb-dri3-0 \
        libxcb-present0 \
        libxcb-randr0 \
        libxcb-sync1 \
        libxext6 \
        libxfixes3 \
        libasound2t64 \
        libglib2.0-0t64 \
        libgomp1 \
        libbrotli1 \
        libv4l-0t64 \
        libxml2 \
        ocl-icd-libopencl1; \
    rm -rf /var/lib/apt/lists/*; \
    ldconfig

# Everything the transcoding stack has to be able to do, asserted in the layer
# that could have broken it rather than discovered on someone's server:
#
#   * every shared library resolves — a missing one turns into a runtime
#     "cannot open shared object file" that looks like a broken install;
#   * libvmaf is present, because without it shrinking silently falls back to
#     SSIM, which is a weaker guarantee than the one the user asked for;
#   * the VAAPI encoder exists at all;
#   * and `subfile`, which is how a DVD image is read.
#
# `bluray` is NOT required, and its absence is reported rather than fatal. This
# build does not have libbluray, so Blu-ray images cannot be opened: they are
# reported as `disc_not_inspectable` and nothing acts on them, which is safe
# but leaves the largest files in a library unmeasured. DVD images are
# unaffected — that path parses ISO9660 here and reads the VOB through
# `subfile`, needing nothing from the ffmpeg build.
RUN set -eux; \
    ! ldd /usr/local/bin/ffmpeg | grep -q 'not found'; \
    ! ldd /usr/local/bin/ffprobe | grep -q 'not found'; \
    ffmpeg -hide_banner -filters | grep -q ' libvmaf '; \
    ffmpeg -hide_banner -encoders | grep -q hevc_vaapi; \
    ffmpeg -hide_banner -protocols | grep -qw subfile; \
    if ffmpeg -hide_banner -protocols | grep -qw bluray; then \
        echo "bluray protocol: present"; \
    else \
        echo "bluray protocol: ABSENT - Blu-ray images will be reported as not inspectable"; \
    fi; \
    ffmpeg -version | head -1; \
    ffprobe -version | head -1

# Ubuntu marks its Python as externally managed (PEP 668), so the application's
# dependencies go in a virtualenv rather than fighting the distro over
# site-packages. PATH puts it first, which is what makes `python` in CMD and
# the healthcheck resolve here.
RUN python3 -m venv /opt/venv

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
    CMD python -m unfuckarr.healthcheck

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["python", "-m", "unfuckarr"]
