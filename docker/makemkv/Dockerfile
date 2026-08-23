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
#   docker build -f docker/makemkv/Dockerfile -t unfuckarr-makemkv:edge .
#
# Then, in the settings page:
#
#   makemkv.enabled    = true
#   makemkv.command    = makemkvcon
#   policy.disc_action = convert
#
# See docker/makemkv/README.md for the rebuild and the beta key.

ARG BASE_IMAGE=ghcr.io/stoatworks-labs/unfuckarr:edge
FROM ${BASE_IMAGE}

# MakeMKV comes from the long-running Launchpad PPA rather than being built
# from makemkv.com's tarballs. Not a preference — building from source is the
# obvious thing and was the first attempt — but www.makemkv.com answers
# Cloudflare 525 for everyone at the time of writing, so the tarballs cannot be
# fetched from anywhere. The PPA carries the same 1.18.4 for noble, which is
# what this image is, on both amd64 and arm64.
#
# It is added as a signed apt source, with the PPA's own key, rather than
# fetching .deb files over HTTPS and hoping: apt then verifies what it
# installs, resolves the dependencies against Ubuntu's own archive, and the
# version stays pinnable. The fingerprint below is the one Launchpad publishes
# for ~heyarje/makemkv-beta.
#
# Note what this changes about the licence position: the PPA is a third party
# redistributing MakeMKV's proprietary half. Using it is a decision for whoever
# builds this image. It does not make the result any more shareable — see the
# bottom of this file.
ARG MAKEMKV_VERSION=1.18.4-1~noble
ARG MAKEMKV_PPA_FINGERPRINT=94B56C64CA7278ECFC34E8808540356019F7E55B
ARG UBUNTU_SERIES=noble

USER root

# `curl` is not one of MakeMKV's dependencies — it is the key wrapper's, and
# the application image does not ship it. Without it the wrapper takes its
# "could not fetch a key" path on every single call, which is a working failure
# mode and a useless one: there would never be a key to fall back to.
# ca-certificates is already in the base image.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl gnupg; \
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${MAKEMKV_PPA_FINGERPRINT}" \
      | gpg --dearmor -o /etc/apt/keyrings/makemkv-ppa.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/makemkv-ppa.gpg] https://ppa.launchpadcontent.net/heyarje/makemkv-beta/ubuntu ${UBUNTU_SERIES} main" \
      > /etc/apt/sources.list.d/makemkv-beta.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        "makemkv-bin=${MAKEMKV_VERSION}" "makemkv-oss=${MAKEMKV_VERSION}"; \
    apt-get purge -y gnupg; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*; \
    # Fail the build rather than ship an image whose whole purpose is missing.
    test -x /usr/bin/makemkvcon

# `makemkvcon` on the PATH is a wrapper that makes sure a key is in place
# before running the real one at /usr/bin/makemkvcon. Deliberately not an
# entrypoint change: the live deployment replaces the entrypoint with Unraid's
# tailscale hook and passes the original through ORG_ENTRYPOINT, so an image
# that added its own would have to be undone by hand at every recreate. Doing
# it here means the key is fetched by the right user, with HOME already set, at
# the only moment it is needed.
COPY docker/makemkv/makemkvcon-wrapper.sh /usr/local/bin/makemkvcon
RUN chmod +x /usr/local/bin/makemkvcon

LABEL org.opencontainers.image.title="unfuckarr with MakeMKV" \
      org.opencontainers.image.description="Local build only — contains MakeMKV, which may not be redistributed." \
      com.stoatworks.redistributable="false"
