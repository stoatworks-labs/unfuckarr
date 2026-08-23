# Building unfuckarr with MakeMKV

Disc conversion needs a `makemkvcon`. unfuckarr does not ship one and cannot:
MakeMKV's binary half is not redistributable, so an image containing it must
not be pushed to a public registry. This directory builds that image locally.

MakeMKV comes from the long-running `~heyarje/makemkv-beta` PPA rather than
from makemkv.com's source tarballs. That was not the first choice — building
from source was — but www.makemkv.com answers Cloudflare 525 for everyone at
the time of writing, so the tarballs cannot be fetched from anywhere. The PPA
carries the same 1.18.4 for noble, which is what this image is, on both amd64
and arm64. It is added as a *signed* apt source using the PPA's own key, so apt
verifies what it installs and resolves the dependencies against Ubuntu's own
archive.

Note what that changes about the licence position: the PPA is a third party
redistributing MakeMKV's proprietary half. Using it is a decision for whoever
builds the image, and it does not make the result any more shareable.

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
    --build-arg MAKEMKV_VERSION=1.18.4-1~noble \
    -t unfuckarr-makemkv:edge .
```

`MAKEMKV_VERSION` is the PPA's version string, not MakeMKV's own — it carries
the Ubuntu series suffix, and `UBUNTU_SERIES` has to match the base image
(`noble` for 24.04).

Rebuild when unfuckarr updates — the whole point of the derived image is that
it tracks the published one rather than forking it — and when MakeMKV releases,
which is roughly monthly alongside the key. A rebuild is not what fixes an
expired key; the wrapper does that at runtime.

## What has been checked

Built on the target and run against real Blu-ray images from a live library:

- **The build works.** apt verifies the PPA signature, `gnupg` is purged
  afterwards, and the build fails rather than shipping an image whose whole
  purpose is missing (`test -x /usr/bin/makemkvcon`).
- **unfuckarr's own code drives it.** `makemkv.available` reports
  `MakeMKV v1.18.4 linux(x64-release)`; `makemkv.titles` read 51 titles off a
  UHD disc and `makemkv.select` picked the 163.6 min feature with its 20
  chapters and six extras between 4 and 47 minutes.
- **`makemkv.rip` produces a correct file.** A 4.2 min extra came out in 5
  seconds with 126 progress callbacks ending at 1.00, and `rip` returned the
  path MakeMKV chose (`No Time to Die_t01.mkv`) rather than one predicted for
  it — which is why it looks for what appeared rather than guessing the name.
- **The result is what a stream copy should be**: HEVC 3840x2160 10-bit,
  AC3 5.1 tagged `eng`, 251.8s against the 251s MakeMKV promised — 0.32% drift,
  well inside `output_tolerance_pct`. Nothing re-encoded.

That last point is worth drawing out: on a UHD disc the video is *already*
HEVC, so converting one gives HEVC in Matroska with no encode at all, and the
HDR comes across untouched because nothing decodes it. `allow_hdr` gates the
*shrink*, not this.

The key wrapper is verified end to end against the same image: it fetches when
there is no key, leaves a fresh one alone, honours `MAKEMKV_APP_KEY` without
contacting anything, refreshes a stale one, never leaves two `app_Key` lines,
and when the forum is unreachable it runs anyway, warns on stderr, keeps the
existing key and bumps the mtime so it does not retry on every call.

## What has not

**No disc has been converted end to end.** Everything above is the read path
and a single short extra. A real conversion replaces a 90 GB image with the
feature, writes the extras, recycles the original and tells the *arr — none of
which has run against real media.

**No seamless-branching disc has been tested.** Every feature measured so far
is a single segment, which the old largest-`.m2ts` heuristic would also have
found. The discs that justify MakeMKV are the ones not yet tried.

## Do not push this image

## Do not push this image

It contains MakeMKV. Keep it local, or in a registry only you can reach.
