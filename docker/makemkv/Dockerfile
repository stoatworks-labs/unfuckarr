# unfuckarr + MakeMKV
#
# Not the published image, and it never can be: MakeMKV's binary half is under
# a licence that does not permit redistribution, so an image with it inside
# must not be pushed to a public registry. Build this one locally, from the
# published image, and run your container from the result.
#
# The alternative — handing unfuckarr the Docker socket so it can drive a
# separate MakeMKV container — works, and gives a service that deletes media
# unattended root-equivalent access to the host. This is the same capability
# without that, and without the path-mapping trap that comes with a shim: here
# `makemkvcon` sees exactly the paths unfuckarr sees, because it is the same
# container.
#
#   docker build -f docker/makemkv/Dockerfile \
#       --build-arg MAKEMKV_EULA=accepted -t unfuckarr-makemkv:edge .
#
# Then, in the settings page:
#
#   makemkv.enabled    = true
#   makemkv.command    = makemkvcon
#   policy.disc_action = convert
#
# See docker/makemkv/README.md for the rebuild and the beta key.

ARG BASE_IMAGE=ghcr.io/stoatworks-labs/unfuckarr:edge
# Must be the distribution BASE_IMAGE is built on: what is compiled here is
# linked against its libavcodec, OpenSSL and expat, and runs there.
ARG BUILD_IMAGE=ubuntu:24.04

# MakeMKV is built from makemkv.com's own tarballs, pinned by version and
# sha256. It used to come from the ~heyarje/makemkv-beta PPA, because
# www.makemkv.com answered Cloudflare 525 to everyone in August 2026. That PPA
# keeps one version per series: on 2026-09-27 it replaced 1.18.4 with 2.0.0,
# and Launchpad purged the 1.18.4 files. MakeMKV 2.0.0 segfaults — a NULL read
# inside makemkvcon, straight after "AACS directory not present" — on any
# Blu-ray without an AACS folder, which is every decrypted image and so every
# disc in a library like this one: 93 of the live box's 95 images, the other
# two not UDF at all. Same on Windows, no fix and no reply from upstream
# (forum.makemkv.com, "Version 2.0.0 won't open the disk structure";
# github.com/flathub/com.makemkv.MakeMKV/issues/123). An image built on 2.0.0
# converts nothing.
#
# makemkv.com keeps every superseded release under /download/old/ and
# publishes a PGP-signed list of checksums per release
# (makemkv-sha-<version>.txt, signed by 2ECF 2330 5F1F C0B3 2001 6733 94E3
# 083A 1804 2697, "MakeMKV (signature)"). The hashes below are from 1.18.4's,
# its signature checked on 2026-10-14. Moving to another version: check that
# file's signature, take its two hashes, and before deploying make sure the new
# makemkvcon reaches TCOUNT on `info iso:<a real Blu-ray image>`.
FROM ${BUILD_IMAGE} AS makemkv
ARG MAKEMKV_VERSION=1.18.4
ARG MAKEMKV_OSS_SHA256=8590063648d42ec2a958b74573d7022f0f4c334e4e4fe7dd53b70c6e748ba453
ARG MAKEMKV_BIN_SHA256=cee56de0baa5531abed16bd862742d308d772b4ab4dae16ee865bf74f04a1608
# makemkv-bin's installer stops until someone types "yes" to GuinpinSoft's
# EULA (src/eula_en_linux.txt in the bin tarball). A build has no one to type
# it, so whoever builds the image says so here — the build does not say it for
# them.
ARG MAKEMKV_EULA=

RUN set -eux; \
    if [ "${MAKEMKV_EULA}" != "accepted" ]; then \
      echo "makemkv-bin is licensed under GuinpinSoft's EULA (src/eula_en_linux.txt in makemkv-bin-${MAKEMKV_VERSION}.tar.gz)." >&2; \
      echo "Read it, and build with --build-arg MAKEMKV_EULA=accepted if you accept it." >&2; \
      exit 1; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl build-essential pkg-config \
        libssl-dev libexpat1-dev zlib1g-dev libavcodec-dev libavutil-dev; \
    cd /tmp; \
    # The current release lives in /download/, superseded ones in /old/; the
    # checksum, not the folder, says it is the right file.
    for part in oss bin; do \
      f="makemkv-${part}-${MAKEMKV_VERSION}.tar.gz"; \
      curl -fsSLO "https://www.makemkv.com/download/old/${f}" \
        || curl -fsSLO "https://www.makemkv.com/download/${f}"; \
    done; \
    printf '%s  %s\n' \
      "${MAKEMKV_OSS_SHA256}" "makemkv-oss-${MAKEMKV_VERSION}.tar.gz" \
      "${MAKEMKV_BIN_SHA256}" "makemkv-bin-${MAKEMKV_VERSION}.tar.gz" \
      | sha256sum -c -; \
    tar xzf "makemkv-oss-${MAKEMKV_VERSION}.tar.gz"; \
    tar xzf "makemkv-bin-${MAKEMKV_VERSION}.tar.gz"; \
    cd "makemkv-oss-${MAKEMKV_VERSION}"; \
    ./configure --prefix=/usr --disable-gui; \
    make -j"$(nproc)"; \
    make install DESTDIR=/out; \
    cd "../makemkv-bin-${MAKEMKV_VERSION}"; \
    mkdir -p tmp; \
    echo accepted > tmp/eula_accepted; \
    make install DESTDIR=/out PREFIX=/usr

FROM ${BASE_IMAGE}
ARG MAKEMKV_VERSION=1.18.4

USER root

# The runtime halves of what the build stage linked against, which the
# application image does not otherwise carry. `curl` is not one of MakeMKV's
# dependencies — it is the key wrapper's. Without it the wrapper takes its
# "could not fetch a key" path on every single call, which is a working failure
# mode and a useless one: there would never be a key to fall back to.
# ca-certificates is already in the base image.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        curl libavcodec60 libavutil58 libexpat1 libssl3t64 zlib1g; \
    rm -rf /var/lib/apt/lists/*

COPY --from=makemkv /out/usr/ /usr/

# Fail the build rather than ship an image whose whole purpose is missing — or
# one whose makemkvcon cannot load, which would only show up at the first disc.
RUN set -eux; \
    ldconfig; \
    test -x /usr/bin/makemkvcon; \
    if ldd /usr/bin/makemkvcon /usr/lib/libmakemkv.so.1 /usr/lib/libdriveio.so.0 \
           /usr/lib/libmmbd.so.0 /usr/bin/mmccextr | grep 'not found'; then \
      exit 1; \
    fi

# `makemkvcon` on the PATH is a wrapper that makes sure a key is in place
# before running the real one at /usr/bin/makemkvcon. Deliberately not an
# entrypoint change: the live deployment replaces the entrypoint with Unraid's
# tailscale hook and passes the original through ORG_ENTRYPOINT, so an image
# that added its own would have to be undone by hand at every recreate. Doing
# it here means the key is fetched by the right user, with HOME already set, at
# the only moment it is needed.
COPY --chmod=755 docker/makemkv/makemkvcon-wrapper.sh /usr/local/bin/makemkvcon

LABEL org.opencontainers.image.title="unfuckarr with MakeMKV" \
      org.opencontainers.image.description="Local build only — contains MakeMKV, which may not be redistributed." \
      com.stoatworks.makemkv-version="${MAKEMKV_VERSION}" \
      com.stoatworks.redistributable="false"
