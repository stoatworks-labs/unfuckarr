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
#       -t unfuckarr-makemkv:edge .
#
# Then, in the settings page:
#
#   makemkv.enabled    = true
#   makemkv.command    = makemkvcon
#   policy.disc_action = convert
#
# See docker/makemkv/README.md for the rebuild and the beta key.

ARG BASE_IMAGE=ghcr.io/stoatworks-labs/unfuckarr:edge
ARG MAKEMKV_VERSION=1.18.4

FROM ${BASE_IMAGE} AS base

# -- build stage ----------------------------------------------------------
# Ubuntu 24.04 to match the application image, so what this produces is linked
# against the glibc it will run on. That match is the whole reason for building
# rather than lifting the binaries out of jlesage/makemkv: that image is
# Alpine, and musl-linked binaries do not run here.
FROM ubuntu:24.04 AS build

ARG MAKEMKV_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config ca-certificates curl \
        libssl-dev libexpat1-dev zlib1g-dev \
        libavcodec-dev libavutil-dev libavformat-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN curl -fsSL -o oss.tar.gz \
        "https://www.makemkv.com/download/makemkv-oss-${MAKEMKV_VERSION}.tar.gz" \
    && curl -fsSL -o bin.tar.gz \
        "https://www.makemkv.com/download/makemkv-bin-${MAKEMKV_VERSION}.tar.gz" \
    && tar xzf oss.tar.gz \
    && tar xzf bin.tar.gz

# --disable-gui: this is a headless service, the Qt build is most of the
# dependencies, and makemkvcon is the entire interface unfuckarr uses.
RUN cd "makemkv-oss-${MAKEMKV_VERSION}" \
    && ./configure --prefix=/usr --disable-gui \
    && make -j"$(nproc)" \
    && make install DESTDIR=/staging

# The binary package's Makefile prompts for the EULA. The marker below accepts
# it non-interactively — which is the same acceptance, made by whoever runs
# this build, and it is the reason this image cannot be shared.
RUN cd "makemkv-bin-${MAKEMKV_VERSION}" \
    && mkdir -p tmp \
    && echo accepted > tmp/eula_accepted \
    && make install PREFIX=/usr DESTDIR=/staging

# -- application image ----------------------------------------------------
FROM base

USER root

# Runtime libraries only; the toolchain and the headers stay behind. `curl` is
# not one of MakeMKV's dependencies — it is the wrapper's, for fetching the beta
# key, and the application image does not ship it. Without it the wrapper takes
# its "could not fetch a key" path on every single call, which is a working
# failure mode and a useless one: there would never be a key to fall back to.
# ca-certificates is already in the base image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl3t64 libexpat1 zlib1g \
        libavcodec60 libavutil58 libavformat60 \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /staging/usr /usr

# `makemkvcon` on the PATH is a wrapper that makes sure a key is in place
# before running the real one, which lands at /usr/bin/makemkvcon. Deliberately
# not an entrypoint change: the live deployment replaces the entrypoint with
# Unraid's tailscale hook and passes the original through ORG_ENTRYPOINT, so an
# image that adds its own would have to be undone by hand at every recreate.
# Doing it here means the key is fetched by the right user, with HOME already
# set, at the only moment it is needed.
COPY docker/makemkv/makemkvcon-wrapper.sh /usr/local/bin/makemkvcon
RUN chmod +x /usr/local/bin/makemkvcon

LABEL org.opencontainers.image.title="unfuckarr with MakeMKV" \
      org.opencontainers.image.description="Local build only — contains MakeMKV, which may not be redistributed." \
      com.stoatworks.redistributable="false"
