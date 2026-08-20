#!/bin/sh
# Drop to the PUID/PGID Unraid passes in, so files unfuckarr writes (transcodes,
# the recycle bin) end up owned the same way the *arrs own everything else.
# Getting this wrong is the classic "Sonarr can no longer delete its own file".
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-002}"
umask "$UMASK"

if [ "$(id -u)" != "0" ]; then
    # Already unprivileged (docker run --user). Nothing to adjust.
    exec "$@"
fi

if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -g "$PGID" unfuckarr
fi
GROUP_NAME="$(getent group "$PGID" | cut -d: -f1)"

if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -M -s /usr/sbin/nologin unfuckarr
fi
USER_NAME="$(getent passwd "$PUID" | cut -d: -f1)"

mkdir -p /config
# Somewhere for the user to write caches. Without it Mesa cannot open a shader
# cache and says so on *every* ffmpeg invocation — which is not merely untidy:
# that warning lands at the front of any stderr the application captures, and
# pushed the real cause out of every truncated error message it reported.
# Giving it a real directory also lets the cache do its job.
APP_HOME=/config/.home
mkdir -p "$APP_HOME/.cache"
export HOME="$APP_HOME"
export XDG_CACHE_HOME="$APP_HOME/.cache"

# Only the config volume — never the media mounts. Recursively chowning a
# 40 TB array on every container start is how people lose an evening.
chown "$PUID:$PGID" /config 2>/dev/null || true
find /config -maxdepth 2 -not -user "$PUID" -exec chown "$PUID:$PGID" {} + 2>/dev/null || true

# Render nodes come from the host with the host's group; add the user to it so
# VAAPI/QSV works without --privileged.
for dev in /dev/dri/*; do
    [ -e "$dev" ] || continue
    DEV_GID="$(stat -c '%g' "$dev")"
    if ! getent group "$DEV_GID" >/dev/null 2>&1; then
        groupadd -g "$DEV_GID" "render$DEV_GID" 2>/dev/null || true
    fi
    DEV_GROUP="$(getent group "$DEV_GID" | cut -d: -f1)"
    [ -n "$DEV_GROUP" ] && usermod -aG "$DEV_GROUP" "$USER_NAME" 2>/dev/null || true
done

echo "unfuckarr starting as ${USER_NAME}:${GROUP_NAME} (${PUID}:${PGID}), umask ${UMASK}"
exec gosu "$PUID:$PGID" "$@"
