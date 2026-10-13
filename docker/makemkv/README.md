# Building unfuckarr with MakeMKV

Disc conversion needs a `makemkvcon`. unfuckarr does not ship one and cannot:
MakeMKV's binary half is not redistributable, so an image containing it must
not be pushed to a public registry. This directory builds that image locally.

MakeMKV 1.18.4 is built from makemkv.com's own tarballs, in a build stage, and
pinned by version and sha256. The hashes come from the release's PGP-signed
checksum list (`/download/old/makemkv-sha-1.18.4.txt`, signed by
`2ECF 2330 5F1F C0B3 2001 6733 94E3 083A 1804 2697`, "MakeMKV (signature)"),
whose signature was checked when they were pinned.

It used to come from the `~heyarje/makemkv-beta` PPA, because www.makemkv.com
answered Cloudflare 525 for everyone in August 2026. That PPA keeps one version
per series: on 2026-09-27 it replaced 1.18.4 with 2.0.0 and Launchpad purged
the old files, so from then until 2026-10-14 this image could not be built at
all and every deploy was an [overlay](#when-there-is-no-makemkv-to-build-the-overlay).

> **Not 2.0.0.** MakeMKV 2.0.0 segfaults — a NULL read inside `makemkvcon`,
> straight after `AACS directory not present, assuming unencrypted disc` — on
> any Blu-ray without an `AACS` folder: decrypted images and BDMV folders.
> Encrypted physical discs still work, which is no help here: 93 of the live
> library's 95 images have no `AACS` folder and the other two are not UDF at
> all. Measured on real images in this image and in a bare `ubuntu:24.04`;
> reported on Windows as well
> ([forum thread](https://forum.makemkv.com/forum/viewtopic.php?p=205783),
> [flathub/com.makemkv.MakeMKV#123](https://github.com/flathub/com.makemkv.MakeMKV/issues/123)),
> with no reply from upstream by 2026-10-14. Until a 2.0.x passes
> `makemkvcon -r info iso:<a real Blu-ray image>` — it must reach `TCOUNT` —
> stay on 1.18.4.

makemkv-bin's installer stops until someone types "yes" to GuinpinSoft's EULA
(`src/eula_en_linux.txt` in the bin tarball). A build has no one to type it, so
the Dockerfile refuses to build unless whoever builds it says so:

```sh
docker build -f docker/makemkv/Dockerfile \
    --build-arg MAKEMKV_EULA=accepted -t unfuckarr-makemkv:edge .
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
file on disk has no key in it, or is missing, or is more than a week old, and
writes it to `$HOME/.MakeMKV/settings.conf` —
`/config/.home/.MakeMKV/settings.conf`, which is inside appdata and survives a
recreate.

"Has no key in it" is the case that matters. When a key expires MakeMKV removes
it itself and rewrites the file, so by age alone the file looks freshly written
— and before the wrapper checked for the key, it stayed keyless for eight days.
That happened live: the key went on 2026-10-05, 1.18.4 carried on without one
until at least 10-08, and by 10-13 it refused every call with
`This application version is too old`, while the key on the forum made the
same 1.18.4 read discs normally.

Set `MAKEMKV_APP_KEY` to a purchased key and the wrapper writes that instead
and never contacts the forum.

Scraping a forum thread is fragile. When it breaks the wrapper does not guess:
the real `makemkvcon` runs with whatever key is on disk and says plainly that
it has expired, and unfuckarr treats that as a problem with the installation
rather than with the disc — no image is ever written off for it.

## Rebuilding

This image is pinned to a base image and a MakeMKV version:

```sh
docker pull ghcr.io/stoatworks-labs/unfuckarr:edge
docker build -f docker/makemkv/Dockerfile \
    --build-arg BASE_IMAGE=ghcr.io/stoatworks-labs/unfuckarr:edge \
    --build-arg MAKEMKV_EULA=accepted \
    -t unfuckarr-makemkv:edge .
```

Pull first: the build uses whatever `:edge` is already on the machine, and an
old one quietly ships old code.

`BUILD_IMAGE` (default `ubuntu:24.04`) must be the distribution the base image
is built on: MakeMKV's open half is compiled against its libavcodec, OpenSSL
and expat, and the final stage installs the runtime packages by their noble
names (`libavcodec60`, `libavutil58`, `libssl3t64`). The build fails if any
MakeMKV binary or library has an unresolved dependency.

To move to another MakeMKV, set `MAKEMKV_VERSION`, `MAKEMKV_OSS_SHA256` and
`MAKEMKV_BIN_SHA256` from that release's signed checksum list, after checking
its signature. A new MakeMKV is a reason to *try* a rebuild, not to deploy one:
check it opens a real image first (see above).

Rebuild when unfuckarr updates — the whole point of the derived image is that
it tracks the published one rather than forking it. A rebuild is not what
fixes an expired key; the wrapper does that at runtime.

### When there is no MakeMKV to build: the overlay

If makemkv.com cannot be reached, or a pinned tarball goes, the real build
fails. [`Dockerfile.overlay`](Dockerfile.overlay) keeps the MakeMKV of a local
image known to work and replaces only the application — `unfuckarr/`, `web/`
and the key wrapper:

```sh
docker build -f docker/makemkv/Dockerfile.overlay \
    --build-arg MAKEMKV_IMAGE=unfuckarr-makemkv:rollback-2026-10-12 \
    -t unfuckarr-makemkv:edge .
```

It is exact only while nothing else has moved: `requirements.txt`,
`docker/entrypoint.sh` and the base image's ffmpeg must be what they were when
the image it sits on was built. Anything there needs the real build, which
means a MakeMKV that works. Keep the image it names — deleting the last local
image with a working MakeMKV in it leaves nothing to overlay.

## What has been checked

Built on the target and run against real Blu-ray images from a live library:

- **The build works.** Since 2026-10-14 from source: both tarballs pass their
  pinned sha256, the build refuses without `MAKEMKV_EULA=accepted`, and it fails
  rather than shipping an image whose whole purpose is missing
  (`test -x /usr/bin/makemkvcon`, and no `not found` from `ldd` on any MakeMKV
  binary or library). The result is 910 MB against the PPA image's 1.16 GB, as
  it no longer pulls in Qt for a GUI nothing runs.
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
existing key and bumps the mtime so it does not retry on every call. Since
2026-10-13 it also refetches into a file MakeMKV has emptied — verified by
starting a throwaway container from a copy of the live, keyless
`settings.conf`: the old wrapper left it keyless and 1.18.4 answered "too old";
the new one fetched a key on the first call. The tests run the wrapper itself
against that same file.

### Since then (2026-10-13)

A full conversion ran through unfuckarr's own `check_file` → `decide` →
`apply` on a 94.5 GB UHD Blu-ray image (bind-mounted read-only, output to
scratch), on 1.18.4, starting from a copy of the live box's keyless
`settings.conf`: the wrapper fetched a key, MakeMKV listed 77 titles, and the
8020 s, 20-chapter feature came out as HEVC 2160p 10-bit PQ with TrueHD 7.1,
8020.01 s against the 8020 s promised, with 24 extras — in 1,307 s.

MakeMKV 2.0.0 did not get that far: it segfaults on `info iso:` for every
Blu-ray image tried (see the top of this file).

### The source build against the PPA's (2026-10-14)

The tarball's `makemkvcon` is not byte-identical to the PPA's (the packaging
strips it), so the source-built image was checked against the PPA-built one
that was live, side by side, from throwaway containers with the library
mounted read-only. Through unfuckarr's own `titles` and `select`, both gave the
same answer on Mortal Kombat: Annihilation (one title, 5685 s, 17 chapters) and
on Jurassic World Rebirth (30 titles over 120 s, feature title 0 at 8020 s / 20
chapters, 13 extras, 13.1 GB). Both `rip`s of Rebirth's title 3 (173 s) came to
339,061,527 bytes with **identical sha256 on all five streams**; the files
differ only in container metadata. The wrapper fetched a key into a
`settings.conf` with none, and the image's ffmpeg 8 is unaffected by the
libavcodec60 installed beside it.

## What has not

**No seamless-branching disc has been tested.** Every feature measured so far
is a single segment, which the old largest-`.m2ts` heuristic would also have
found. The discs that justify MakeMKV are the ones not yet tried.

**The overlap-based extras selection has not been through a rip.** On the
disc above the old selection wrote 31.6 GB of extras from nested playlists (a
56 min "play all" and featurettes that are subsets of it), leaving the feature
plus extras 13 GB larger than the image. `select` now keeps an extra only for
what it adds; replayed against that disc's real output it keeps 14 extras,
about 13 GB by MakeMKV's estimate — an estimate until a conversion writes them.

**Nothing from MakeMKV 2.0 has been parsed**, because it has never got as far
as printing a title.

## Do not push this image

It contains MakeMKV. Keep it local, or in a registry only you can reach.
