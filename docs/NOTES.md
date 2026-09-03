# Notes

Working notes for this repo: status, decisions, and the traps that have actually bitten.
Migrated out of Claude Code's memory on 2026-08-24, so they are written in the first
person and dated by when each thing was learned — that date is usually the useful part.

Cross-cutting notes that are not specific to this repo live in
[fleet-notes](https://github.com/stoatworks-labs/fleet-notes).

*unfuckarr — PUBLIC Sonarr/Radarr library validator + auto-repair service, LIVE tailnet-only at unfk:6969 against a real 17k-file library. 2026-08-19: gained VMAF-targeted shrinking + disc-image (ISO) support on PR #3 (pushed, NOT merged/deployed); recycle bin moved off the cache; 338 GB of leaked temp files and untracked bin files cleaned up. Scans STILL disabled until PR #3 is deployed.*

`~/Projects/unfuckarr` — **github.com/stoatworks-labs/unfuckarr, PUBLIC, MIT, on `main`**,
created 2026-08-08. Scans a Sonarr/Radarr library, decides whether each file is intact and
whether Emby will direct play it, then transcodes / remuxes / deletes-and-re-searches
**fully automatically** (Allan's explicit choice when asked). Python 3.12 + FastAPI + SQLite,
vanilla-JS web UI with **no build step**, ffmpeg via subprocess. Host port **6969** (checked
clear of the 13 ports in `stoatworks-unraid/templates`).

**It was private for a few hours on 2026-08-08 and that is worth remembering, because
`docker login ghcr.io` on Unraid is NOT a workable answer:** Unraid's `/root` is a RAM disk,
so the login is lost on every reboot and the next pull 403s. There is also no registry
credential UI. Allan made the repo public instead.

**Repo visibility and PACKAGE visibility are independent.** Going public did NOT make the
GHCR package public — an anonymous manifest GET still returned 403, and there is **no REST
endpoint** for container package visibility, so it is a manual flip in the GHCR web UI.
Probe recipe in [stoatworks unraid](https://github.com/stoatworks-labs/stoatworks-unraid/blob/main/docs/NOTES.md) (`stoatworks-unraid`).

**`latest` must never be set by a main build.** `docker/metadata-action`'s default
`latest=auto` already applies it to semver tags; a raw `latest` rule on main *also* did, and
main is amd64-only — so `latest` flipped between multi-arch and amd64-only depending on which
job ran last. `latest` = newest release, **`edge`** = rolling main. arm64 (QEMU, ~10x slower)
is built **only on a `v*` tag**.

**It is in the CA repo, and it sets all THREE opt-out flags** — `hasOwnDocker`,
`hasOwnWorkflow`, `hasOwnTemplate`. The latter two were **added to the generator for this app**
(2026-08-08). `hasOwnTemplate` copies `unfuckarr/unraid/unfuckarr.xml` verbatim into
`stoatworks-unraid/templates/` and **rewrites `<TemplateURL>`** to the copy CA serves — so the
app repo's copy leaves that element EMPTY. `hasOwnWorkflow` stops the generated `docker.yml`
racing unfuckarr's own CI to push the same tags. hostPort **6969**. See
[stoatworks unraid](https://github.com/stoatworks-labs/stoatworks-unraid/blob/main/docs/NOTES.md) (`stoatworks-unraid`).

## The design decisions worth not re-litigating

- **Emby is asked, not modelled.** `POST /Items/{id}/PlaybackInfo` with a device profile
  returns Emby's own `TranscodeReasons`. When Emby answers, the local codec table is skipped
  entirely so the two cannot contradict each other in the UI.
- **Deletes go through the *arr API, then `POST /history/failed/{id}`** — that blocklists the
  release *and* searches. A plain search re-grabs the same broken file. `os.unlink` leaves
  Sonarr believing the episode is present until its next rescan.
- **Three brakes**, because unattended deletion has to be wrong safely: recycle bin with
  retention + restore, `max_actions_per_scan`, and an abort that changes **nothing** when >50%
  of a library fails at once (that is an unmounted array, not broken media).
- **A transcode is re-checked afterwards** (`_confirm_fixed`) and `files.fix_attempts` caps
  retries at 2. Without it a transcode that does not clear the finding is redone on every scan
  for ever, burning a CPU on one file with nothing in the log.

## Verified vs assumed — the gap is the whole point

**Verified:** 78 tests green in CI against files ffmpeg actually renders; a full local run
(MPEG-2 AVI → transcoded → verified → original recycled → re-check passes); both brakes; the
watch-folder settle timer ignoring `.part` then firing after the rename; the web UI driven in
a browser including a settings round-trip persisted to disk. **The Docker image is built and
smoke-tested in CI** — it boots, drops to PUID/PGID, serves the UI, has ffmpeg. That is the
only container proof available, per [no container runtime](https://github.com/stoatworks-labs/fleet-notes/blob/main/notes/reference_no_container_runtime.md). Screenshots are
regenerated from committed `scripts/seed_demo.py` + `scripts/demo_services.py` (a real HTTP
stub of the three services, so a green connection panel means the client code actually ran) via
`cdpshot` — see [cdpshot tool](https://github.com/stoatworks-labs/fleet-notes/blob/main/notes/reference_cdpshot_tool.md).

**Two real bugs were found by making things presentable, not by testing.** Writing the
"transcode did not fix it" log line exposed that nothing stopped an infinite re-transcode loop
(`fix_attempts`, cap 2). Setting up an honest screenshot exposed that `last_scan_finished` only
lived in memory — a restart reported "No scan yet" AND `_recompute_next_scan` fell back to
`time.time()`, so a nightly restart meant the schedule never fired at all.

## VAAPI — tested on real hardware 2026-08-09 (lilnasx is **AMD**, not Intel)

lilnasx's GPU is a **Radeon 880M / Strix, gfx1150** — so `qsv` can NEVER work there; vaapi via
Mesa radeonsi is the only path. Verified end-to-end through unfuckarr's own
`plan`→`build_command`→`run`: 1080p MPEG-2 → HEVC at ~12x realtime. Three separate things were
broken and each hid the next:

1. **`/dev/dri` was never passed into the container** (the original included) — vaapi was
   selected in settings but the render node wasn't there, so it had never run once. Needs
   `--device /dev/dri`; the entrypoint then makes a `render<GID>` group and adds the user (works).
2. **Debian bookworm's Mesa 22.3 cannot init any AMD GPU newer than RDNA2** —
   `amdgpu: unknown (family_id, chip_external_rev): (150, 20)`, vaInitialize error 2. Fixed by
   installing `mesa-va-drivers` from **bookworm-backports** (25.0.7) in the Dockerfile.
3. **unfuckarr's own command was wrong**: `-hwaccel vaapi -hwaccel_output_format vaapi` +
   `scale_vaapi` asks the GPU to filter frames it never decoded. Hardware decode is per-codec and
   **no recent AMD part decodes MPEG-2** — the canonical file this tool fixes — so decode fell
   back to software and the filter died PARTWAY THROUGH with "Failed to inject frame into filter
   network: Function not implemented" (reads like a driver fault, isn't). Now `-vaapi_device` +
   `-vf format=nv12,hwupload`: software decode, GPU encode, works whatever the decoder did.
   Regression test asserts no `hwaccel_output_format`/`scale_vaapi` in the built command.

`qsv`/`nvenc`/`videotoolbox` remain command-line construction only.

## Live use — and the 2026-08-15 audit that corrected it

**It runs LIVE** against real Sonarr + Radarr + Emby and a ~17,000-file library. But the
earlier reading of `/api/activity` ("127 remuxes across 22 files") was **the loop, misread as
success**. Audited 2026-08-15 (`/api/scans` + the SQLite DB direct over ssh):

**No scan has EVER applied an action** — 8 scans, `actions=0` on every row. Every repair in the
log came from the **watch folder**, and 415 of 505 action rows were one film (21 Jump Street)
ping-ponging with its own temp file. Two independent causes, both fixed on branch
`fix/abort-ratio-and-nominal-runtime` (commit 478c90f, pushed, **PR not yet opened**):

1. **The abort brake was self-locking.** `_remediate` divided by the *worklist*, but
   `needs_check` re-queues every known-bad file — so the pass after a scan that finds problems
   is made of nothing else, reads 100%, and aborts for ever. Live it escalated **62% → 100%**
   as the worklist shrank to exactly the files already flagged. Fix: denominator is now the
   population of files whose state the pass knows (skipped-because-fine included). The brake
   itself is unchanged — invariant 4 still holds.
2. **`duration_mismatch` treated the *arr runtime as measured.** It is **nominal** — for TV the
   broadcast slot, ad breaks included. A 22 min sitcom vs a 25 min slot = 12% short; a 44 min
   US drama vs 60 min = 27%. That condemned **2,924 of 3,153** TV files (Blu-ray remuxes
   included) as corrupt → delete-and-re-search. Fix: new `duration_truncated_pct` (50%) raises
   the error; the gap below it is an info-level `duration_below_expected` that no policy acts
   on. Movies had only 22 such findings total, so no per-library split was worth the plumbing.

Merged as **PR #2 → `025bd4c`, built as `:edge` and DEPLOYED to `unfk` 2026-08-15**. Verified
live: a Parks and Rec Bluray remux went `corrupt` → `hygiene`, `duration_mismatch` (error) →
`duration_below_expected` (info).

**Scans are STILL DISABLED on the live instance** (`schedule.scan_enabled=false`). The ISO
blocker is fixed in code but **the running container is still the old 1.0.0-era `:edge` image**,
so re-enabling scans before deploying PR #3 would resume deleting BR-DISKs. Deploy first. The
watcher is live and unaffected.

**★ 2026-08-19 — disabling scans did NOT contain the ISO risk, and the recycle bin took the
NAS down.** Both facts found while diagnosing a failed `git push` to lilnasx.

*The watcher does the same destructive actions the scanner would.* `scan_enabled=false` but
both `watch_folders` (`/media/tv`, `/media/movies`, recursive, 60 s settle) are **enabled**,
and that path fires independently of the schedule. It had recycled BR-DISK `.iso` and
Remux-2160p files that same day (14:27 and 17:14). **The 126-ISO false positive below is
live, not parked** — "scans are off" only covers the scheduled sweep. To actually contain it,
disable the watch folders too, or fix the ISO path first.

*`policy.recycle_bin_path` is `""`, so the bin defaults into appdata — which is on the NVMe
cache pool.* Full-size media (48 GB ISOs) parked on a 7.3 TB SSD pool at `recycle_bin_days:
14` filled it to **100%, 128K free**, which fails every appdata write on the box — the git
push was just the symptom that surfaced it. The array had 25 TB free the whole time.
**Point `recycle_bin_path` at an array path.** Emptying the bin is a temporary fix: daily
volumes were 2.7 TB / 2.6 TB / 1.5 TB, so at 14-day retention it refills within days.

### The ISO false positive — FIXED on `feat/quality-targeted-shrink` (PR #3, 2026-08-19)

It had already fired before the fix: **21 Jump Street's 42 GB BR-DISK was deleted and
re-searched** (replaced by a 54 GB mkv on 08-16), and three identical copies of it sat in the
recycle bin. `unfuckarr/disc.py` now opens disc images without mounting — Blu-ray through
ffmpeg's `bluray:` protocol, DVD by parsing ISO9660 and using `subfile` byte ranges. An image
nothing can open raises an **info** `disc_not_inspectable` and no action, because "I cannot read
this" and "this is broken" are different sentences. Full technical detail is in the repo's
AGENTS.md; don't duplicate it here.

Measured against the real library: **104 disc images, 99 readable, 5 refused by libudfread**
(UDF 2.50 metadata partitions). One is an 88 GiB 4K HDR disc at 89 Mbps — the best shrink
candidate in the collection. Most 4K discs are HDR, and `allow_hdr` is off by default, so out of
the box they report `hdr_not_shrunk` and are left alone.
- **`emby/not_in_emby` covers 17,569 of 17,715 files** — the Emby path mapping does not resolve,
  so invariant 7 (Emby's verdict wins) almost never fires and compat comes from the local codec
  table. The *connection* is fine (version reports); the lookup isn't. That makes the projected
  **3,860 compat transcodes unvalidated by Emby**.
- **Post-fix projection: 7,943 destructive of 17,715 = 44.8%** — under the 0.5 abort threshold,
  so scans will now proceed. But it is *close*, so a drift upward re-trips the brake. Split:
  466 redownload, 3,860 compat transcode, 3,617 hygiene transcode. At 50 actions/scan and 24h
  that is ~159 days to work through.

### Operating traps found while deploying

- **`/api/scan/stop` does not stop the work.** It sets `aborted`, which *is* enough to guarantee
  no remediation (`_run_inner` returns before `_remediate`) — but `pool.map` submits all futures
  up front and `ThreadPoolExecutor.__exit__` waits for every one, so the probes keep running for
  hours. **Only a container restart actually stops it.**
- **`state.paused` is IN-MEMORY only** — it does NOT survive a restart, and on restart an
  overdue `next_scan_at` starts a scan immediately. To durably hold scans use
  `schedule.scan_enabled` (persisted to `/mnt/user/appdata/unfuckarr/config.json`).
- **Recreate recipe is saved on lilnasx at `/root/recreate-unfk.sh`**, with the pre-deploy
  container config at `/root/unfk-inspect-pre-025bd4c.json` and a full DB backup (db + **-wal**
  + -shm; the WAL held newer data than the .db) at `/root/unfk-backup-pre-025bd4c/`. Derive the
  env to re-pass by diffing the container's env against the *new image's* baked env — otherwise
  you pin stale `PYTHON_VERSION`/`PYTHON_SHA256` from the old image.

**The 86x remux loop is FIXED and deployed** (commit 5427095, in the running `:edge` image;
last temp-file event 08-09 11:50, image built 15:11). Don't re-report it as live.

Still untested: hardware *encode* (all observed repairs were stream-copy remuxes), the CA
template install path, huge-library perf.

**Lesson worth keeping:** "it is running in production" is not "the code path has run". The
activity log looked like healthy repair activity and was one file in a loop plus a brake that
had silently disabled the whole scanner.

**The live container (unraid name `unfk`, NOT "unfuckarr") is TAILNET-ONLY since 2026-08-09:**
reachable at `http://unfk:6969` only; `lilnasx:6969` refuses. Runs `:edge` (template
`my-unfk.xml` now says edge — `latest` would roll it back to 1.0.0 without the bind feature).
dockerman-managed but recreated by hand: the recreate needs `--cap-add NET_ADMIN` (tailscale
hook silently fails without it), `--device /dev/net/tun`, entrypoint `/opt/unraid/tailscale`
+ `ORG_ENTRYPOINT`/`ORG_CMD` env, and the labels `net.unraid.docker.managed=dockerman` +
`net.unraid.docker.tailscale.hostname=unfk`. Tailscale state persists in
`/config/.tailscale_state` (appdata bind) so recreates keep the node identity. **The image
healthcheck had to learn about interface binding** — a hard-coded 127.0.0.1 probe marks a
tailnet-bound server unhealthy; `unfuckarr/healthcheck.py` (commit 5511c1d) probes
`resolve_host()`'s address instead. Bind option is deliberately env/template-only, NOT on the
settings page (a wrong bind saved via the UI would take down the UI that fixes it).

**Tailnet-only policy + `UNFUCKARR_BIND_INTERFACE` (2026-08-09):** README leads with a warning —
tailnet/private network only, no independent human security review, never the public internet —
also in the CA template Overview (stoatworks-unraid copy regenerated + pushed).
`__main__.resolve_host` resolves an interface name (e.g. `tailscale0`) to its IPv4 via
SIOCGIFADDR ioctl (Linux + Darwin, same ifreq offset 20:24), **waits** up to
`UNFUCKARR_BIND_WAIT` (60s) for the interface, and **fails the start rather than falling back to
0.0.0.0** — that fail-closed behaviour is deliberate (AGENTS.md invariant 10), don't "fix" it.
Beats `UNFUCKARR_HOST`; both env-only. Binding an interface needs it in the container's netns
(host network / tailscale sidecar / Unraid's per-container Tailscale toggle) — on bridge,
publish on the tailscale IP instead. Tests in `tests/test_main.py` (loopback + fail-closed).

**README screenshots are now from the live instance** (cdpshot against lilnasx:6969, media
names blurred at capture: CSS on `.what`/`.path`/`#drawerTitle`/`td:has(>.path)>div` + a
regex sweep incl. `/media|tv|movies|downloads/` path prefixes re-applied via MutationObserver —
finding messages QUOTE full paths, plain extension/SxxExx regexes miss truncated ones). The
demo pipeline (seed_demo/demo_services) remains for UI dev.

Related: [stoatworks unraid](https://github.com/stoatworks-labs/stoatworks-unraid/blob/main/docs/NOTES.md) (`stoatworks-unraid`), [video toolkit ffmpeg nostdin](https://github.com/stoatworks-labs/fleet-notes/blob/main/notes/reference_video_toolkit_ffmpeg_nostdin.md) (`-nostdin`
is on every ffmpeg call here too), [agents md convention](https://github.com/stoatworks-labs/fleet-notes/blob/main/notes/reference_agents_md_convention.md).

## 2026-08-19: space saving, the recycle bin, and 338 GB of leaked files

**`feat/quality-targeted-shrink` → PR #3, pushed, NOT merged, NOT deployed.** Adds
shrinkray-style VMAF-targeted transcoding (`quality.py` bisects CRF against a measured quality
target), the `efficiency` check and `shrink` action, disc-image support, and the recycle-bin
work below. 173 tests. The whole design and every trap is written up in the repo's AGENTS.md —
read that, not this.

**The recycle bin was quietly filling the cache.** `/config` is appdata, which on lilnasx is
`shareUseCache="only"`, so every recycled file was *physically copied* onto the NVMe — 139 GB of
it. Fixed by pointing it at **`/media/.recycle`**, inside the existing media bind mount: measured
on the box, 200 MB moved in **50 ms** and stayed on disk3, because within one shfs share a move
is a same-disk rename. Set live in `config.json` (works on the old image too) and added to
`/root/recreate-unfk.sh` as `UNFUCKARR_RECYCLE_BIN_PATH`. Note `media` is `shareUseCache="yes"`,
so a *newly created* directory lands on the cache — but a renamed file follows its source disk,
which is what actually matters.

**Two leaks, both invisible by design, both found only by looking at the live box:**

- **199 GB of abandoned transcode outputs sat inside the library** (4 files: Batman 1989 74 GB,
  Ex Machina 68 GB, 2 Fast 2 Furious 57 GB, BTTF II 14.5 GB), plus **507 metadata files Emby had
  written treating each as a separate film**. `is_temp_output` makes the scanner and watcher
  ignore `*.unfuckarr.*` — which also stopped anyone noticing. Now cleared at startup, where
  nothing can be in flight by definition.
- **139 GB of recycle-bin files the database had no row for.** `sweep()` walked the *database*,
  so an untracked file was never looked at again. 126 GB of it was three byte-identical copies of
  one 42 GB ISO. Now swept.

**All 338 GB was deleted 2026-08-19** after checking every leftover had a surviving source;
manifest at `/root/unfk-cleanup-2026-08-19.json`. Cache went 689 G → 554 G. Emby still shows
phantom entries for the four temp files until its next library scan.

**A transcode job had been stuck at `running` for four hours** (Ex Machina, job 547 — ffmpeg
finished writing at 17:12, job never completed) and was ended by the container restart.

**ssh to lilnasx is `root@lilnasx`**, not `allansargeant@`.

**Do not put a `#` comment inside a `docker run \` continuation** in `/root/recreate-unfk.sh`:
it comments out the rest of the logical line and `sh -n` still passes. Same trap as a Dockerfile
`RUN`, except Docker's parser strips those and the shell's does not.

**CORRECTION 2026-08-20: PR #3 IS MERGED.** `git log` on `main` shows
`207fbe4 Measured space saving, readable disc images, and a recycle bin that
stays put (#3)`, and the local checkout matches `origin/main`. Any statement
above that the VMAF shrinking work is "pushed, NOT merged" is stale — re-check
the branch before acting on the scans-must-stay-disabled caveat.

Knock-on: `unfuckarr/unraid/unfuckarr.xml` gained the space-saving prose and the
`UNFUCKARR_RECYCLE_BIN_PATH` / `_DAYS` Config blocks in that merge, but
stoatworks-unraid's verbatim copy could not pick them up while its generator's
repo resolution was broken, so the CA store served the old template until
2026-08-20. See [stoatworks unraid](https://github.com/stoatworks-labs/stoatworks-unraid/blob/main/docs/NOTES.md) (`stoatworks-unraid`).


## 2026-08-23: disc → MKV conversion (PR #4, open, NOT merged)

`feat/disc-image-conversion` adds a **`convert`** action: MakeMKV stream-copies the feature out
of an `.iso` to Matroska, bonus features to `extras/`, image recycled. Nothing re-encoded — the
existing shrink path takes the MKV to HEVC afterwards. **Menus cannot come across** (no Matroska
holds a DVD VM program or BD-J title; Emby never rendered one) — don't re-litigate that. Off by
default at both switches. Design and traps are in the repo's AGENTS.md; don't duplicate here.

**The lilnasx MakeMKV wiring is NOT usable as it stands, and each fact hides the next:**

- The `MakeMKV` container (`jlesage/makemkv`) exists but has **never run** — state `Created`.
- **Its beta key is EXPIRED.** A real run against a library Blu-ray gave `MSG:5073` "Your
  temporary key has expired and was removed" + `MSG:5021` "application version is too old" and
  **zero TINFO records** — which reads exactly like an unreadable disc. That is why
  `makemkv.KeyExpired` exists; without it the first disc converted would have been written off
  permanently.
- **`makemkv.com` was returning Cloudflare 525 to everyone on 2026-08-23**, so neither the key
  nor the source tarballs could be fetched. Re-check before assuming a build failure is ours.
- **Mount paths do not line up:** MakeMKV maps `/mnt/user`→`/storage`, `unfk` maps
  `/mnt/user/media`→`/media`. Same file, two paths — and MakeMKV's error for a path that does
  not exist is only "cannot open disc".
- **`unfk` has no Docker socket** (and no docker CLI), so it cannot drive that container at all.
- **The binaries cannot be borrowed:** `jlesage/makemkv` is Alpine/musl, unfuckarr's image is
  Ubuntu 24.04/glibc.

Chosen route (Allan, asked): **`docker/makemkv/Dockerfile`** — a *local-only* derived image,
`FROM` the published one, MakeMKV built from source. **Never push it** (not redistributable).
`makemkvcon` on the PATH is a wrapper that refreshes the beta key into
`/config/.home/.MakeMKV/settings.conf`; deliberately **not** an entrypoint change, because the
live deploy replaces the entrypoint with the tailscale hook via `ORG_ENTRYPOINT`.
**The build is unverified past the source download** — see the 525 above.

**No disc has ever actually been converted**, and "the longest title is the feature" is a
heuristic that anthologies, one-disc TV seasons and concert films will break.

### What the real discs proved (2026-08-23, key fetched from the forum)

⚠️ **`forum.makemkv.com` stays UP while `www.makemkv.com` is 525** — so a beta key is
fetchable even when the source tarballs are not. Do not conclude MakeMKV is unusable from a
`www` outage.

The READ path is verified against real Blu-rays; the WRITE path still is not.
- *Mortal Kombat: Annihilation* — 3 titles, picked the 94.8 min / 17-chapter feature.
- *No Time to Die* — **54 titles**, picked the 163.6 min / 20-chapter / 83.8 GB feature,
  6 extras, 43 under the floor, **4 duplicates**. Titles 1 and 52 are the SAME 83.77 GB under
  two playlists — without the segment-map dedupe a conversion writes **167 GB for one film**.
- **Bug found: only one of that pair carries the chapter marks**, and the dedupe kept the right
  one purely by index order. `select` now breaks a tie on chapter count first.
- **Neither disc proves the case for MakeMKV** — both features are a single segment, so the old
  largest-`.m2ts` heuristic would have found them too. Seamless branching is still untested.

### The image is BUILT and the rip path WORKS (2026-08-23)

**MakeMKV comes from the `~heyarje/makemkv-beta` PPA, not makemkv.com** (Allan's call, after the
525 outage). Same **1.18.4** for **noble** = the base image's Ubuntu 24.04, on amd64 AND arm64.
Added as a **signed apt source** (fingerprint `94B56C64CA7278ECFC34E8808540356019F7E55B`), so apt
verifies it; `gnupg` purged after. `MAKEMKV_VERSION` is the **PPA** version string
(`1.18.4-1~noble`), not MakeMKV's, and `UBUNTU_SERIES` must match the base.

Built on lilnasx and driven by unfuckarr's OWN code against real library Blu-rays:
`titles` → 51 titles off the No Time to Die UHD, `select` → the 163.6 min / 20-chapter feature +
6 extras, `rip` → a 4.2 min extra in **5 seconds**, output **HEVC 3840x2160 10-bit + AC3 5.1**,
**251.8s vs the 251s promised = 0.32% drift** (that IS `_verify_conversion`'s primary check).

⚠️ **Decides what the feature is FOR: a UHD disc is ALREADY HEVC**, so a conversion is a stream
copy giving HEVC-in-MKV with **no encode**, and the **HDR survives because nothing decodes it**.
`allow_hdr` gates the SHRINK, never the conversion. Don't conflate them.

The built image is left on lilnasx as `unfuckarr-makemkv:edge` (1.16 GB) but its base is `main`'s
`:edge`, which has **no `makemkv.py`** — rebuild after #4 merges and `:edge` is republished.

Still untested: a conversion END TO END, and **any seamless-branching disc** — every feature
measured so far is a single segment, which the old largest-`.m2ts` heuristic would also have found.

⚠️ **The unfuckarr image has NO `curl`.** Found by building `docker/makemkv/Dockerfile` with the
downloads stubbed. The key wrapper would have taken its (deliberately quiet) "could not fetch"
path on every call, with nothing to fall back to, and read as MakeMKV being broken. `curl` added
to the runtime install; `ca-certificates` was already there. Everything after the download step
is now verified: runtime libs resolve on Ubuntu 24.04, PATH puts the wrapper ahead of the binary,
and the wrapper's fetch / fresh / purchased-key / stale / offline paths all behave.

**PR #5** (`feat/header-totals-and-footer`) is STACKED on #4, not on `main`: the header counters
include `discs_converted` and the tests reuse #4's MakeMKV stand-in. Adds the `totals` table
(counters, NOT sums over `jobs`/`activity`/`recycle` — all three are pruned or swept) and the
vendored Stoatworks support footer. A companion branch `chore/sync-footer-unfuckarr` in
stoatworks-backend adds `unfuckarr:web` to the sync table.


## 2026-08-24: DISC CONVERSION IS LIVE

PRs **#4 and #5 MERGED** to `main`. `unfk` recreated from **`unfuckarr-makemkv:edge`** — a
**LOCAL** image (published `:edge` + MakeMKV from the PPA), so **dockerman cannot pull it** and it
must be **rebuilt by hand after every unfuckarr update**; `/root/recreate-unfk.sh` now names it and
carries the recipe in a comment. Backup at `/root/unfk-backup-pre-convert-2026-08-24/`
(config + db + **-wal** + -shm + inspect).

Live settings: `disc_action=convert`, `makemkv.enabled=true`, **`keep_disc_image=false`** (Allan's
call — the recycle bin at `/media/.recycle`, 14 days, is the safety net), `max_conversions_per_scan=2`.
**102 disc images, 6.1 TB — about 51 days to work through at 2 a scan.** Three sampled discs all
decide `convert`. The beta key persists at `/mnt/user/appdata/unfuckarr/.home/.MakeMKV/settings.conf`
owned `nobody:users` — ⚠️ **`docker exec` without `-u 99:100` writes it to /root instead** and it
is lost on recreate.

⚠️ **Memory was STALE and cost a wrong risk assessment: scans are ENABLED, and `corrupt_action` is
`flag`** — so nothing has been deleting anything. Re-read `config.json` before trusting any claim
about the live posture.

⚠️ **The first end-to-end conversion found the feature could not fire.** A good 17 GB Blu-ray read
as **`corrupt`** from **207 decode errors that were pure read artefacts** (DTS `Error submitting
packet to decoder`, h264 `error while decoding MB` at seek points) — and `corrupt` decides first,
so it went to `repair` (a remux a disc cannot have) and then a **redownload that deletes the disc**.
MakeMKV's stream copy of the same disc probes with **zero**. Fixed two ways: `is_noise(line,
on_disc=True)` filters those artefacts **only for discs** (they are the primary damage signal in an
ordinary file), and `decide` now sends a disc with integrity findings to `convert`. 2 of 15 sampled
discs hit this — it depends on the disc's audio layout.

⚠️ **The counters started at zero on an install that had reclaimed 2.27 TB across 386 files** — a
header reading "0 saved" is the exact lie they exist to prevent. **PR #6** backfills `files_shrunk`
and `bytes_saved` once from `files.shrunk`/`shrunk_from` (the only two recoverable — jobs/activity
are pruned); already applied by hand to the live DB, so the code's guard will skip it there.

Conversion timing measured: **17 GB Blu-ray in 121 seconds**. A converted MKV correctly falls
through to `shrink` afterwards.

## 2026-08-26: it logged problems and fixed nothing — the hygiene path

Reported as "logging issues but not actually doing fixes", and it was true for one whole
category. Diagnosed against the live instance (`/api/activity`, `/api/settings`, and the DB
over ssh), fixed on `fix/hygiene-actions-that-cannot-fix`.

**Only 3 of the 8 hygiene codes had a fix behind them.** `checks/hygiene.py` raises
`audio_missing_language`, `subtitle_missing_language`, `no_default_audio`,
`multiple_default_audio`, `image_subtitles_only`, `all_subtitles_forced`, `unusual_frame_rate`
and `very_low_bitrate`. `transcode.plan` acted on the first three. `decide` did not care — any
hygiene warning returned `transcode` — so the other five built a plan with no work in it, which
falls through to `is_remux = True` and a **pure stream copy**: every byte rewritten, the original
recycled to `/media/.recycle`, and the same finding on the far side. Invariant 9 counts that as a
failed attempt, so it happened **twice per file** before `MAX_FIX_ATTEMPTS` gave up.

Measured live before the fix:
- **269 files parked at `fix_attempts` 2** — 240 `image_subtitles_only`, 26
  `multiple_default_audio`, plus a few `very_low_bitrate` / `all_subtitles_forced`.
- **2,733 of the 3,749 hygiene files** had *no* code the planner could act on, i.e. were queued
  to join them.
- **39 `transcode_did_not_fix` events in nine hours**, 33 of them `multiple_default_audio`.
- Compat transcodes were fine throughout (only 3 `incompatible` files at the cap), which is why
  the header still looked healthy — 3.46 TB reclaimed, shrink working. The failure was confined
  to hygiene, and hygiene is most of what the UI lists.

Three separate defects, and the middle one is the embarrassing one:

1. **`multiple_default_audio` was simply omitted.** It sits directly below `no_default_audio` in
   the check and appeared nowhere in the planner, so the remux copied both `default` flags
   through unchanged. Now fixed, choosing **among the tracks already flagged default** (highest
   channel count) rather than promoting one the file never marked — only the ambiguity needs
   settling. Verified end to end against real ffmpeg: finding raised → plan says "set default
   audio" → `-disposition:a:0 default -disposition:a:1 0` → re-check clean.
2. **`all_subtitles_forced` had no fix either**, though it is the same one-line disposition
   mechanism. Now clears the forced flag on every kept subtitle track (skipping dropped ones by
   output index, as the language tagging already did). Also verified end to end.
3. **`image_subtitles_only`, `very_low_bitrate` and `unusual_frame_rate` have no fix and cannot
   have one** — burning in PGS means re-encoding the video *and* permanently removing the
   viewer's ability to switch subtitles off, and the other two are statements about how the
   source was made. `decide` now returns **`flag`** for a warning set containing none of
   `transcode.HYGIENE_FIXABLE`, with the reason "nothing a rewrite can change". One fixable code
   in the set still earns the transcode and the rest ride along — the rewrite is already paid for.

Now **invariant 22**: an action is only offered when there is a fix behind it, and
`test_every_fixable_code_is_one_the_planner_acts_on` asserts `HYGIENE_FIXABLE` and
`apply_hygiene_fixes` cannot drift apart. The shrink ride-along in `remediation.py` had its own
partial copy of the default-audio logic (also missing `multiple_default_audio`); it now calls the
shared helper. 265 tests green.

**Still open, found while diagnosing and NOT fixed here:**

- ⚠️ **The Emby path mapping is malformed in the live settings** and has been all along. The
  single row reads `from: /mnt/user/media/movies`, `to: "/media/movies, /mnt/user/media/tv =
  /media/tv"` — two mappings typed into one `to` field. `not_in_emby` covers **18,338 of 18,405
  files**, so invariant 7 almost never fires and every compat verdict comes from the local codec
  table. This is the same symptom NOTES recorded in August as "the path mapping does not
  resolve"; the cause is now known and it is **data, not code**. Fixing it needs a second mapping
  row for tv, not an edit to the `to` value.
- **`max_actions_per_scan` is now 100000** (was the 50 default) but scans 20–22 each stopped at
  exactly 50, so the raise post-dates them. Expect the next scan to do far more work — worth
  watching rather than assuming.
- The 269 files already at `fix_attempts` 2 will **stay** given up on: nothing resets the counter
  short of a redownload. **28 of them are now genuinely fixable** (`multiple_default_audio` /
  `all_subtitles_forced`) and want their counter cleared by hand before they will be retried.

Measured on the live DB, what the fix changes: **238 hygiene files become genuinely fixable**
(they carry `multiple_default_audio` or `all_subtitles_forced` and nothing the planner already
handled), and **2,495 stop being rewritten twice for nothing** and are flagged honestly instead.
Neither number includes the files that were already being fixed correctly.

## 2026-08-27: Emby answers, but this build never says why

Found while preparing to fix the malformed Emby path mapping recorded above — and it is the
reason that mapping must **not** be corrected as a config-only change.

**The mapping is wrong in a knowable way.** Emby reports every item under `/mnt/user/media/...`
(measured: **19,774 of 19,774**, 19,429 tv + 345 movies, nothing outside it), unfuckarr sees
`/media/...`. The live settings hold one row, `from: /mnt/user/media/movies`,
`to: "/media/movies, /mnt/user/media/tv = /media/tv"` — two mappings typed into a single `to`
field, so movies map to a nonsense prefix and tv does not match at all. The correct answer is a
single row **`/mnt/user/media` → `/media`**, which resolves **160 of 160** sampled library paths
against the live index. Two rows would also work; one is the actual bind mount and cannot drift.

**⚠️ But Emby 4.9.5.0 returns `TranscodeReasons: None` on every refusal.** Measured across a
160-file sample: Emby answered all 160, refused 63, and gave reasons for **zero** of them. Example
— an AVI carrying mpeg4 ASP + mp3, which is genuinely undirectplayable:
`{'SupportsDirectPlay': False, 'SupportsDirectStream': False, 'TranscodeReasons': None}`.

That matters because **invariant 7 skips the local codec table entirely when Emby answers**, and
`transcode.plan` derives `needs_video` / `bad_audio_codec` / `bad_container` from the reasons and
nothing else. No reasons → no codes → `video copy + audio copy` → `is_remux = True` → a stream
copy that cannot clear the verdict, counted as a failed attempt by invariant 9, run twice, then
given up on. **For every incompatible file in the library.** Compat repair works *today* only
because the mapping is broken and Emby never answers at all.

Fixed before touching the mapping: `scanner.check_file` now also runs the local codec table when
Emby refused without reasons (`_verdict_without_reasons`). Invariant 7 is intact — the two only
ever run together when they already agree the file is bad, so they cannot contradict each other.
And `decide` flags rather than transcodes when the *only* compat evidence is a reasonless refusal
the local table could not corroborate: no named defect, no plan, invariant 22.

What the sample says the corrected mapping will do, once this is deployed:

| Emby says | currently `incompatible` (60) | currently `ok` (60) | currently `hygiene` (40) |
|---|---|---|---|
| needs transcode | 53 | 5 | 5 |
| direct play | 7 | 55 | 35 |

So roughly **12% of the incompatible backlog gets cleared** (Emby direct-plays them; ~480 files
stop being transcoded), and **~1,300 files move the other way** — but the ones the local table
cannot corroborate are *flagged*, not rewritten. Expect the `incompatible` count to rise while the
amount of actual transcoding falls.

⚠️ Watch the abort brake on the first scan after the mapping change: files Emby refuses carry an
**error**-severity finding, so they count as failures in the ratio, and `abort_if_failure_ratio_over`
is 0.5.

### Same day, caught on the live box: the guard was reading the wrong list

The first cut of the invariant-22 guards asked whether the *errors* contained anything
actionable. `plan` builds its `codes` from **`result.findings`** — every finding, whatever the
severity — so the guards and the planner were reading two different lists.

Found by rechecking a real file rather than trusting the unit tests: an X-Files Blu-ray remux
that Emby refused without reasons, whose only actionable code was **`mixed_audio_support`** — a
*warning*. The guard saw compat errors of `[no_direct_play]` alone and flagged it, while `plan`
would have re-encoded the audio and fixed it. Under-acting instead of over-acting, but wrong the
same way.

Both guards now call `transcode.plan_has_work(result.findings)`, which is the single answer to
"would a plan built from this do anything" — `COMPAT_ACTIONABLE`, `HYGIENE_FIXABLE`, or a
`no_direct_play` that actually named its reasons. `decide` cannot simply build the plan and look:
it is deliberately a pure function of the result and the policy, with no `MediaInfo` to hand.

**Worth keeping:** the unit tests all passed on the broken version, because every one of them used
an *error*-severity finding. The live recheck is what found it.

## 2026-08-27: deployed — hygiene fix, Emby mapping, counters reset

PRs **#7, #8, #9, #10 merged**; `unfk` rebuilt from `:edge` (`52472a0`) and recreated twice — once
for #7/#9 and again for #10, which the first deploy is what found. Backup at
`/root/unfk-backup-pre-hygiene-2026-08-27/` (config + db + **-wal** + -shm + inspect).

**A shallow clone now lives at `/mnt/user/appdata/.unfk-build`** and `/root/recreate-unfk.sh` names
it in the rebuild recipe. It is on the cache pool deliberately: `/root` is a RAM disk and does not
survive a reboot. Rebuild is `git fetch --depth 1 origin main && git reset --hard origin/main`,
then the usual `docker build -f docker/makemkv/Dockerfile -t unfuckarr-makemkv:edge .`. The
makemkv image's only build-context dependency is `docker/makemkv/makemkvcon-wrapper.sh`.

**Counters:** 28 files at `fix_attempts` 2 whose open findings are now fixable
(`multiple_default_audio` / `all_subtitles_forced`) were reset to 0 with the container **stopped**
— hygiene-at-cap went 269 → 241. Do it stopped: `sqlite3` as root on a live DB leaves `-wal` /
`-shm` owned by root and the app then cannot write. `PRAGMA wal_checkpoint(TRUNCATE)` first, and
`chown nobody:users` after if root touched anything. The remaining 241 are genuinely unfixable and
are now *flagged*, so the counter no longer matters for them.

**Emby mapping corrected** to the single row `/mnt/user/media` → `/media`. `not_in_emby` is gone.

Verified on the live instance after deploy, by rechecking real files with `act=false`:

| file | codes | decision |
|---|---|---|
| AVI / mpeg4 ASP | `no_direct_play`, `bad_container`, `bad_video_codec` | transcode |
| X-Files remux | `no_direct_play`, `mixed_audio_support`, `image_subtitles_only` | transcode |
| The Night Manager 2160p | `multiple_default_audio`, `hdr_not_shrunk` | transcode — "stream metadata needs tidying" |
| 8 Mile Remux-2160p | `image_subtitles_only`, `hdr_not_shrunk` | **flag** — "nothing a rewrite can change" |

Both directions, on real media: what can be fixed is fixed, what cannot is said plainly once.

⚠️ **`policy.max_actions_per_scan` is 100000** — effectively unlimited, and it was 50 until
recently. The next scheduled scan is the first to run under both that and the corrected Emby
mapping, so it is also the first that could act on the ~1,300 files whose compat verdict just
changed. `abort_if_failure_ratio_over` (0.5) is the only remaining brake, and Emby's refusals are
**error**-severity, so they count as failures in that ratio. Watch scan 23.

## 2026-08-27: scan 23, and the bug the hygiene fix uncovered

First scan under the new code and the corrected Emby mapping, fired manually with
`max_actions_per_scan` **temporarily capped at 300** (it is otherwise 100000, and this was the one
run that could not be called back — `/api/scan/stop` does not stop work in flight).

**It behaved.** 8,088 files checked, **the abort brake held** at 8,040 findings — which is invariant
4 earning its keep: a worklist denominator would have read 99% and aborted. The library moved the
way the sample predicted: hygiene **3,750 → 3,296**, incompatible **4,116 → 4,489**, as Emby's
error-severity refusals reached files the local table had called untidy.

The fix visible in production, in a plan string and a flag reason:

```
transcoding  38%  remux, tag languages, set default audio   (Knives Out)
FLAG  Austin Powers (1997) / 8 Mile (2002) Remux-2160p
      nothing a rewrite can change — these describe how the file was made, not how it was muxed
```

**`transcode_did_not_fix`: 0 of the first 11 transcodes, against 44% (39 of 88) before.**

### ⚠️ And then one fired — with an EMPTY findings list

`Licence to Kill (1989) WEBDL-2160p.mkv`, `status: unmeasured`, `findings: []`. The transcode had
cleared everything and was recorded as having fixed nothing.

`_confirm_fixed` tested `result.status in ("ok", "hygiene")`. A transcode output is a **brand new
file**, so `checks/efficiency` has not priced it and the re-check comes back **`unmeasured`** —
which is a state, not a claim (invariant 14). So a successful repair incremented `fix_attempts`,
and two of those write off a file that was never broken.

**It stayed invisible because the hygiene bug was masking it.** `hygiene` was in that allowlist, so
while hygiene-triggered remuxes were failing to clear their findings, their outputs came back
`hygiene` and counted as *success*. Two bugs, each hiding the other, in the same four lines.

Success is now asked of the findings, not of `result.status`: `not result.errors and not
still_open`. `result.errors` still decides the negative, so a genuinely broken replacement fails
whatever its status is called — and a status added later cannot silently reopen this.

**Lesson worth keeping, and it is the same one as August:** the unit tests passed on the broken
version *and* on the version before it. Both of these were found by watching real work on real
media — the first by rechecking a file by hand, this one by watching a scan actually run. The
regression test was checked against the old logic (`assert 1 == 0`) before being trusted.

### Scan 23's final tally, and the deploy that ended it

Cut short at 59 actions by the recreate that deployed PR #12 — the restart is the only thing that
stops a scan (`/api/scan/stop` sets `aborted` but `pool.map` has already submitted every future).
What it managed first:

| | |
|---|---|
| checked | 8,088 / 8,088 |
| transcode_done | **29** |
| transcode_did_not_fix | **1** — and that one was the `unmeasured` false negative, now fixed |
| transcode_failed | 1 — `pcm_bluray` into Matroska, pre-existing, see below |
| shrink_done | 13 (shrink total now 700 files / 3.56 TB) |
| shrink_failed | 1 — the VAAPI `-38` again, source untouched |
| destructive actions | **none** |
| abort brake | held |

So **effectively 0 real did-not-fix out of 29**, against 44% (39 of 88) before the work.

`de18d2d` is live and all four fixes verified inside the running container (`_confirm_fixed`'s
status allowlist gone, `apply_hygiene_fixes`, `plan_has_work`, `_verdict_without_reasons`).

**`Licence to Kill` needs no counter repair** — it sits at `fix_attempts` 1, which still has an
attempt left, and the next scan's re-check will now pass. It was the only file that hit the
`unmeasured` false negative.

⚠️ **`max_actions_per_scan` is still 300**, set by hand for the manual run and NOT put back to
100000. Scan 23's remediation was killed at 59, so a full 300-action pass under the corrected Emby
mapping has still never been observed — and that mapping moved ~1,300 files into `incompatible`,
all of which now queue transcodes. Leave it at 300 for at least one scheduled scan.

### Two pre-existing failures, both chipped rather than fixed here

- **VAAPI `Function not implemented` (-38)** on shrink, ~1/day since at least 08-24, on an entirely
  ordinary file (1080p yuv420p 8-bit H.264 High, 25 fps, stereo) — so not a format edge case.
  ⚠️ NOTES records that same string as the signature of a *different*, already-fixed bug; check the
  built command carries neither `hwaccel_output_format` nor `scale_vaapi` before calling it a
  regression. Leading hypothesis: two VAAPI sessions colliding on `/dev/dri/renderD128` — the
  continuous shrink worker and scan-triggered transcodes both encode, and `transcode.max_concurrent`
  is 1 but may not cover the shrink worker.
- **`pcm_bluray` cannot go into Matroska.** `plan` has `_subtitles_to_drop`/`CONTAINER_SUBTITLES` to
  stop it copying an unmuxable *subtitle* codec, and **no equivalent for audio** — so the remux
  copies it through and ffmpeg refuses to write the header. Measured: **11 files** carry
  `pcm_bluray`. 77 carry some `pcm*`, but `pcm_s16le` (31) and `pcm_s24le` (36) are legal in
  Matroska — do not "fix" those. Audio is not optional the way a subtitle track is, so the answer is
  to force `audio_action="encode"`, not to drop the track.

## 2026-08-27: subtitles — Bazarr, and teaching the check to see sidecars

`image_subtitles_only` was written off as unfixable (invariant 22 flags it rather than remuxing).
That was right about *rewriting* and wrong about the problem: the fix is to **get a text subtitle**,
which is Bazarr's job, not unfuckarr's. Measured across the 2,611 flagged files: **2,564 PGS, 23
VOBSUB, 24 DVB**, and **zero** had a sidecar of any kind.

**unfuckarr's half:** `image_subtitles_only` no longer fires when a text sidecar sits beside the
media. Without this the finding would survive every subtitle Bazarr ever downloads — Emby happily
serves an external `.srt` and never burns in the PGS, so the finding simply is not true any more.
Stem-matched, so one subtitled episode does not clear a season; `.sup` correctly does not count.
Verified against Bazarr's real output names (`.en.srt`, `.en.hi.srt`, `.en.forced.srt`, `.srt`)
with `subfolder: current`, which writes beside the media.

The directory listing is cached **thread-locally** — for throughput, not correctness. A global
cache would answer correctly (the `cached[0] != parent` guard rebuilds on a miss) but two pool
threads in different directories would evict each other on every call. ⚠️ Deliberately **not** an
`lru_cache`: a scan runs for hours and Bazarr writes sidecars while it does, so an entry that
outlived its directory would hide a subtitle that had just arrived.

### ⚠️ Bazarr's config file is NOT the place to change Bazarr's config

`bazarr` (lscr.io/linuxserver/bazarr, host port 6767, appdata `/mnt/user/appdata/bazarr`).
Editing `config/config.yaml` **does not work** — Bazarr rewrites it from its own state on startup,
so a stopped-container edit is silently discarded. Use `POST /api/system/settings` with
form keys like `settings-sonarr-ip`; those persist. The API key is under `auth:` in that file.

**And do not read that file with a regex either.** Keys like `ip`, `port` and `ssl` appear in
several sections, so a first-match regex reports another section's values — that is how these
notes nearly recorded "no providers configured" when four were, and `use_sonarr: false` when it
was true. `GET /api/system/settings` is authoritative.

**What was actually broken:** `radarr.ip` was `https://radarr.tail745ddc.ts.net` — the Address
field takes a **hostname**, and Bazarr builds `{scheme}://{ip}:{port}`, so it dialled
`https://https://radarr...:7878`. Both services were also pointed at tailnet hostnames with the
app's own port, and those names are fronted by `tailscale serve` on **443** —
`https://sonarr.tail745ddc.ts.net:8989` answers nothing. Bazarr is on the same Docker host, so it
now uses `sonarr:8989` / `radarr:7878`, ssl off. (Unraid's default bridge *does* resolve container
names — worth knowing, it is not true of a stock Docker bridge.)

**Two API traps, both of which return success while doing nothing:**
- A language profile injected after startup must carry **`audio_only_include`**. `database.py`'s
  migration adds it at boot, so a profile created through the API never gets it, and the subtitle
  indexer then throws `'audio_only_include'` on every assignment — HTTP 500.
- `POST /api/series` takes **`seriesid` and `profileid` as parallel lists**, one profile id per
  series id (movies: `radarrid`). Sending `id=` plus a single `profileid` parses to two empty
  lists, and the loop does nothing — **HTTP 204, zero rows changed**.

Live config: English profile on all 660 items, `ignore_pgs_subs` and `ignore_vobsub_subs` true
(this is what stops Bazarr counting the PGS track as "has subtitles" and skipping the file).
⚠️ There is **no `ignore_dvb_subs`**, so the 24 DVB files stay flagged whatever Bazarr does.

### CORRECTION, same day: both of my cautions above were wrong

**"Wanted is 692 and the gap is unexplained"** — it was simply a partially-completed index. Once
`series_full_scan_subtitles` finished, episodes wanted went to **5,759**. Nothing was suppressing
anything. Do not read a wanted count while an index task is still running.

**"OpenSubtitles' free tier means this drips over weeks"** — wrong, and wrong because I reasoned
from one provider instead of looking at which one was actually serving. Measured over one
afternoon: **2,365 episode subtitles downloaded, and 500 of 500 sampled came from `gestdown`**.

⚠️ **The TV/film asymmetry is structural, not a fault, and it is the thing to understand here:**

| provider | covers | practical yield on this library |
|---|---|---|
| `gestdown` (Addic7ed) | **TV only** | effectively unlimited — 2,365 in an afternoon |
| `yifysubtitles` | **films only** | indexed by *YIFY releases*; this library is Bluray Remux / WEBDL, so it rarely matches |
| `opensubtitlescom` | both | the rate-limited one |
| `subsource` | both | matched little |

So TV clears fast and films crawl: **2,365 episodes against 19 films**. Not fixable by
configuration. Films need either an OpenSubtitles.com VIP account (~1,000/day instead of a
handful) or another film-capable provider. Keep it in proportion — films are 343 files of 18,400
and only 174 still want subtitles.

### What it actually achieved, measured

**706 files cleared in a single scan** — `image_subtitles_only` went **2,611 → 1,905** with
nothing rewritten, nothing transcoded and nothing recycled. The finding stopped being true.

⚠️ **A finding only re-evaluates when the file is re-checked.** Subtitles that land *during* a
scan clear on the **next** one, not the running one — so `image_subtitles_only` sitting still
mid-scan is expected, not stalled. At the time of writing **245 of the 1,905 still-open ones
already have a sidecar on disk** and are waiting for the next pass.

## 2026-09-03: the download queue, and the three good files Cleanuparr was about to bin

**New: `unfuckarr/intake.py`.** Watches the Sonarr/Radarr *queue* for downloads that finished and
that the *arr will not import. Deliberately narrow — no stalled, slow or metadata-stuck handling.

**Why narrow.** Cleanuparr is already running on lilnasx (container up 7 days, queue cleaner
enabled on a 30-minute cron, pointed at Sonarr 4.0 / Radarr / Lidarr and qbt), with stall (3
strikes, reset-on-progress), slow (3 strikes, 500 KB floor) and downloading-metadata (3 strikes)
rules all on. Duplicating those would put two tools on one queue, and the race is not theoretical:
the loser's `DELETE` hits a queue id that no longer exists, or it blocklists the *replacement* the
winner's re-search just grabbed. Allan chose "only what Cleanuparr misses" when asked.

**What it misses, and why it misses it.** `failed_import_max_strikes` is 0 in
`queue_cleaner_configs` and `-1` in `arr_configs` — but Cleanuparr is *still recording*
`failedimport` strikes. At 17:30 and 18:00 that day it struck three items that were
**complete, healthy 41-minute episodes**: `Trains.That.Changed.The.World` S01E01/E02/E04,
1.34–2.00 GB, h264 in mkv, 2437–2526 s, all opening cleanly. Their block was Sonarr saying

> Found matching series via grab history, but release was matched to series by ID. Automatic
> import is not possible. See the FAQ for details.

which is a request for a *manual import*, not a complaint about the file. A third strike would
have deleted ~5 GB of good media and blocklisted three clean releases. **That is the gap**: every
tool in this space tells "bad release" from "the *arr cannot place a good release" by matching the
status message, and that is a guess that is wrong in the expensive direction.

**So: only the files can condemn a release** (now invariant 23). `triage` is pure and has no route
to `bad_release` at all — it decides only whether the files are worth opening. `inspect` opens
them, and it is the only thing that can condemn one. Every path that cannot see the files ends in
`unrecognised`: flag, act on nothing. Verified twice against the live queue from a throwaway
container (`docker run --rm -v /mnt/user/media:/media:ro -v /tmp/uf-new:/app-new:ro --entrypoint
python unfuckarr-makemkv:edge`): once through the normal path, and once with the message table
bypassed so **only** file evidence decided. All three came back `manual` both ways.

### Traps found writing it

- **A metadata-stuck torrent reports `size: 0` AND `sizeleft: 0`.** Any `1 - sizeleft/size`
  progress figure divides by zero or reads as 100% complete. Seen live on three qbt torrents.
- **Sonarr puts a season pack's *total* size on every episode's queue record.** So judging
  completeness by the largest single file makes every episode of a twenty-part pack 5% of "its"
  release — the first version of `looks_like_sample` condemned all twenty. Fixed by measuring
  every video in the directory, and leaving `looks_like_sample` to do names only.
- **An empty directory where the *arr recorded gigabytes is a path problem, not a bad release.**
  Between "the release evaporated" and "the mapping lands somewhere that merely exists", the
  second is far likelier — and this instance already has form (the Emby mapping hid 17,569 of
  17,715 files). It returns `unrecognised`.
- **An unreachable *arr must not read as an empty *arr.** `_mark_gone` only retires items from
  sources that actually answered; otherwise an outage loses `blocked_since` on every item and
  restarts every timer on reconnection.
- **`blocked_since` is keyed on the download client's id, not the queue row's.** The *arr
  renumbers its queue on restart.

### What is NOT verified

**No removal has ever run against a real queue.** `remove_from_queue` is exercised only against an
httpx mock; the `fix` action is assumed. No `bad_release` has been produced from live data either
— the queue held none that day. Every `ARR_SIDE_MARKERS` entry except the by-ID match is from
documentation rather than the wire.

### Deploy note

`intake` needs a **path mapping** on the *arr: it reports `outputPath` as SABnzbd/qbt see it
(`/downloads/...`). On lilnasx the downloads are at `/mnt/user/media/downloads/complete`, already
inside the `/mnt/user/media → /media` bind, so the mapping is
`/downloads` → `/media/downloads/complete` and **no new volume is needed**. Without it every
blocked download reads as `unrecognised`.

Ships `enabled: true, action: "flag"` — it classifies and touches nothing until told otherwise.

### Incidental, worth fixing

`/mnt/user/appdata/Cleanuparr/cleanuparr.db` holds the qBittorrent **username and password in
plaintext** in `download_clients`, and appdata is world-readable (`drwxrwxrwx` on the parent).

## 2026-09-03: intake DEPLOYED, and the lock bug the deploy found

**PRs #16 and #17 merged; `unfk` rebuilt from `:edge` (`3e8930b`) and recreated twice.** Backup at
`/root/unfk-backup-pre-intake-2026-09-03/` (config + db + **-wal** + -shm + inspect), plus
`unfuckarr.db.pre-3e8930b` before the second recreate. Previous image kept as
**`unfuckarr-makemkv:rollback-2026-08-27`** — rollback is `docker tag` it back to
`unfuckarr-makemkv:edge` and re-run `/root/recreate-unfk.sh`.

**Live settings:** `intake.enabled=true`, **`action="flag"`** (classifies, removes nothing),
`poll_minutes=10`, `min_blocked_minutes=30`. Path mappings added to **both** *arrs:
`/downloads` → `/media/downloads/complete`. That works because the downloads already live inside
the existing `/mnt/user/media → /media` bind — no new volume. Container confirmed to see 2,413
entries there.

**The deploy found a real bug, and it was not in the new code.** The first live `intake` pass
fetched the queue, classified it, wrote its verdicts — then died on the *last* statement of the
pass, the activity-log line describing what it had just done: `database is locked`, HTTP 500,
result discarded. Cause: a scan starting up holds the write lock while `sync_inventory` rewrites
the whole library (18,440 rows), so a write arriving in that window waits out `busy_timeout` and
raises. Two fixes in #17 — `db.ex` now **rolls back** (a failed statement was leaving Python's
implicit transaction open on that thread's connection, and FastAPI reuses worker threads, so one
transient lock error poisoned every later request landing on it), and `run_pass` no longer throws
away a completed pass because its summary line could not be written.

**Diagnostic lesson, worth more than the fix.** The first theory was a dangling `q1` cursor
holding a read transaction — which *does* produce an instant `database is locked` with the busy
handler bypassed, and I "confirmed" it with a repro that held the cursor in a variable.
`db.q1` is `connect().execute(...).fetchone()`, whose cursor is a temporary CPython frees at
once, so it was never exposed. What caught it: the regression tests written for that theory
**passed on the unfixed code**. A regression test that does not fail without the fix has not
reproduced anything. Reproduce against the actual helper, not a paraphrase of it.

Measured while chasing it: `busy_timeout` **is** honoured — against a held `BEGIN EXCLUSIVE` a
write fails after exactly the timeout. So a *fast* `database is locked` is not plain contention.

**Still not fixed:** `sync_inventory` should commit in batches instead of holding the write lock
across the whole library. That window is the root cause and it recurs on every scan start.

**Interrupting scans is cheap and the reconciler earns its keep.** Two restarts killed two
in-flight transcodes; startup removed **28 abandoned outputs totalling 9.26 GB** and marked 2
interrupted jobs failed.

**Not yet exercised live:** the queue was empty by the time everything was deployed (the three
`importBlocked` Trains episodes imported on their own), so no `bad_release` and no removal has
run against a real queue. `action` stays `flag` until a week of verdicts has been read.
