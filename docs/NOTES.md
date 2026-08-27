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
