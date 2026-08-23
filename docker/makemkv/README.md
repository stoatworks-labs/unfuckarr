# Building unfuckarr with MakeMKV

Disc conversion needs a `makemkvcon`. unfuckarr does not ship one and cannot:
MakeMKV's binary half is not redistributable, so an image containing it must
not be pushed to a public registry. This directory builds that image locally.

```sh
docker build -f docker/makemkv/Dockerfile -t unfuckarr-makemkv:edge .
```

Then run your container from `unfuckarr-makemkv:edge` instead of
`ghcr.io/stoatworks-labs/unfuckarr:edge`, and set:

| Setting | Value |
|---|---|
| `makemkv.enabled` | `true` |
| `makemkv.command` | `makemkvcon` |
| `policy.disc_action` | `convert` |

## Why not point at a MakeMKV container instead

You can — `makemkv.command` is a whole command line precisely so that
`docker run --rm -v /mnt/user/media:/media jlesage/makemkv /opt/makemkv/bin/makemkvcon`
is expressible. Two things make it the worse option:

- **unfuckarr would need the Docker socket**, which is root-equivalent access
  to the host, given to a container that deletes media unattended and has no
  authentication until an API key is set.
- **The paths have to match.** `makemkvcon` is handed the path unfuckarr sees,
  so the shim's mounts must reproduce it exactly. A typical
  Community Applications MakeMKV container maps `/mnt/user` to `/storage`,
  which does not — and MakeMKV's error for a path that does not exist is that
  it cannot open the disc, with nothing in it about mounts.

Borrowing the binaries out of such a container does not work either: those
images are Alpine, and musl-linked binaries will not run on this one.

## The beta key

MakeMKV is free while in beta, and the beta key expires roughly monthly. The
`makemkvcon` on the PATH here is a wrapper that fetches a current key when the
one on disk is missing or more than a week old, and writes it to
`$HOME/.MakeMKV/settings.conf` — `/config/.home/.MakeMKV/settings.conf`, which
is inside appdata and survives a recreate.

Set `MAKEMKV_APP_KEY` to a purchased key and the wrapper writes that instead
and never contacts the forum.

Scraping a forum thread is fragile. When it breaks the wrapper does not guess:
the real `makemkvcon` runs with whatever key is on disk and says plainly that
it has expired, and unfuckarr treats that as a problem with the installation
rather than with the disc — no image is ever written off for it.

## Rebuilding

This image is pinned to a base image and a MakeMKV version:

```sh
docker build -f docker/makemkv/Dockerfile \
    --build-arg BASE_IMAGE=ghcr.io/stoatworks-labs/unfuckarr:edge \
    --build-arg MAKEMKV_VERSION=1.18.4 \
    -t unfuckarr-makemkv:edge .
```

Rebuild when unfuckarr updates — the whole point of the derived image is that
it tracks the published one rather than forking it — and when MakeMKV releases,
which is roughly monthly alongside the key.

## What has been checked, and what has not

The image has been built on the target with the two upstream downloads stubbed
out, which exercises everything after them: the runtime packages resolve on
Ubuntu 24.04, `COPY --from=build` lands, and `makemkvcon` on the PATH is the
wrapper rather than the binary. That found a real omission — the application
image does not ship `curl`, so the wrapper could never have fetched a key, and
its failure path is quiet by design.

The wrapper itself is verified end to end against that image: it fetches a key
when none is on disk, leaves a fresh one alone, honours `MAKEMKV_APP_KEY`
without contacting anything, refreshes a stale one, never leaves two `app_Key`
lines, and when the forum is unreachable it runs anyway, warns on stderr, keeps
the existing key and bumps the mtime so it does not retry on every call.

**The real build has never completed.** www.makemkv.com has been returning
Cloudflare 525 to everyone, so the source tarballs cannot be fetched from
anywhere. Note that the forum is a different host and has stayed up throughout —
so the key half of this works even while the build half does not.

## Do not push this image

It contains MakeMKV. Keep it local, or in a registry only you can reach.
