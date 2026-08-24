#!/bin/sh
# makemkvcon, with a key in place before it runs.
#
# MakeMKV's free beta key expires roughly monthly, which is a poor dependency
# for a service that runs unattended: without this, disc conversion stops one
# morning with nothing else changed. unfuckarr already tells that failure apart
# from a bad disc (`makemkv.KeyExpired` is never recorded against an image),
# but telling it apart is not the same as fixing it.
#
# So: the key is fetched when it is missing or stale, from the forum thread
# MakeMKV publishes it in, and written to $HOME/.MakeMKV/settings.conf. $HOME
# is /config/.home — set on the account by the image's entrypoint, because gosu
# discards an exported one — so the key survives a container recreate.
#
# Scraping a forum page is fragile by nature. When it breaks, nothing here
# guesses: the real makemkvcon runs anyway with whatever key is on disk, and
# says clearly that it has expired. Set MAKEMKV_APP_KEY to a purchased key and
# none of this runs at all.
set -eu

REAL=/usr/bin/makemkvcon
SETTINGS_DIR="${HOME:-/config/.home}/.MakeMKV"
SETTINGS="$SETTINGS_DIR/settings.conf"
BETA_URL="https://forum.makemkv.com/forum/viewtopic.php?f=5&t=1053"
# The beta key is reissued monthly; checking weekly finds a new one well before
# the old one lapses, without hitting the forum on every conversion.
MAX_AGE_DAYS="${MAKEMKV_KEY_MAX_AGE_DAYS:-7}"

write_key() {
    mkdir -p "$SETTINGS_DIR"
    # Rewrite rather than append: two app_Key lines and MakeMKV takes the
    # first, which is the stale one.
    if [ -f "$SETTINGS" ]; then
        grep -v '^app_Key' "$SETTINGS" > "$SETTINGS.new" 2>/dev/null || true
    else
        : > "$SETTINGS.new"
    fi
    printf 'app_Key = "%s"\n' "$1" >> "$SETTINGS.new"
    mv "$SETTINGS.new" "$SETTINGS"
}

if [ -n "${MAKEMKV_APP_KEY:-}" ]; then
    [ -f "$SETTINGS" ] && grep -q "$MAKEMKV_APP_KEY" "$SETTINGS" || write_key "$MAKEMKV_APP_KEY"
elif [ ! -f "$SETTINGS" ] || [ -n "$(find "$SETTINGS" -mtime "+$MAX_AGE_DAYS" 2>/dev/null)" ]; then
    KEY="$(curl -fsSL --max-time 30 "$BETA_URL" 2>/dev/null \
           | grep -oE 'T-[A-Za-z0-9@_%+/-]{40,}' | head -1 || true)"
    if [ -n "$KEY" ]; then
        write_key "$KEY"
    else
        echo "makemkvcon: could not fetch a beta key from $BETA_URL;" \
             "running with whatever is already configured" >&2
        # Touch it anyway, or a failed fetch is retried on every single call.
        [ -f "$SETTINGS" ] && touch "$SETTINGS"
    fi
fi

exec "$REAL" "$@"
