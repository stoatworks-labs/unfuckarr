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
  hours. **Only a container restart actually stops it.** *(Corrected 2026-09-14 — the symptom
  was real, the explanation was not; see "2026-09-14: the scan that read 100% for six days".)*
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

**What was actually broken:** `radarr.ip` was `https://radarr.<tailnet>.ts.net` — the Address
field takes a **hostname**, and Bazarr builds `{scheme}://{ip}:{port}`, so it dialled
`https://https://radarr...:7878`. Both services were also pointed at tailnet hostnames with the
app's own port, and those names are fronted by `tailscale serve` on **443** —
`https://sonarr.<tailnet>.ts.net:8989` answers nothing. Bazarr is on the same Docker host, so it
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

## 2026-09-07: the 18-hour Xena shrink — Mesa spins on closed-caption SEIs

Xena S06E19 (1080p WEB-DL, 44 min, 1.37 GB) sat at 22% after 4.7 h with an ETA of 16 h. The
ETA was honest: the encode ran at **0.7 fps**. Not the box (load 9 on 24 cores, Emby idle), not
the GPU (VCN engine at 0.1% via `drm-engine-enc`, fence ring advancing twice a second), not the
governor (inert, correctly). The `enc0:0:hevc_vaapi` thread was at 100% of one core inside
`libgallium-25.3.3.so`; `eu-stack` on lilnasx plus `llvm-nm` on the library put every sample in
`vlVaHandleVAEncPackedHeaderDataBufferTypeHEVC`, Mesa's parser for the packed headers ffmpeg
hands the driver each frame.

**Cause.** The Xena WEB-DLs carry EIA-608 captions in an SEI on every frame. ffmpeg's h264
decoder lifts that onto each frame as A/53 side data and `hevc_vaapi` (default
`-sei hdr+a53_cc`) re-emits it as an 80-byte packed SEI per frame — visible in `-v trace` as a
"Packed header buffer (4)" the caption-free files never show. Mesa's `parseEncSeiH265` then
spins ~0.7 s on each one.

| test on lilnasx, unfuckarr's exact command | speed |
|---|---|
| Young Sheldon S06E22 remux | 15.3x |
| Xena S06E19 | 0.028x |
| Xena S06E18 | 0.026x |
| Xena S06E19 with `-hwaccel vaapi` decode | 0.023x |
| Xena S06E19, software x265 ultrafast | 5.8x |
| **Xena S06E19 with `-sei hdr`** | **19.1x** |

**Fix.** `video_encode_args` now passes `-sei hdr` to `hevc_vaapi` and `-sei -a53_cc` to
`h264_vaapi`. The embedded captions are the only loss; the files this bites have a text subtitle
track beside them. Software encoders are untouched. Regression test
`test_vaapi_never_reemits_closed_captions`. No upstream Mesa report found. Shrinkray runs the
same ffmpeg build on the same GPU and is exposed to the same thing.

**Why nothing caught it.** The stall detector keys off *any* progress line and `out_time` kept
advancing, so a crawl is not a stall. The quality search before the encode took 1.5 h for the
same reason (15 sample encodes at 0.7 fps) and nothing times a search. Worth a ceiling on
sample-encode speed one day: a 15 s sample that takes over a minute on a GPU is a symptom.

## 2026-09-08: progress froze at 1.5%, and the stall detector had never been able to fire

"Pirates of the Caribbean - On Stranger Tides (2011)" (20 streams, a UHD remux) sat at **1.5%
with a 47,000 s ETA** while ffmpeg was 86% through the file. `out_time_ms` in the `-progress`
output was stuck at 121,997,000. **ffmpeg 8's `out_time` is the *minimum* last-muxed timestamp
across all output streams** (7.x reported the maximum); PGS stream 18 has 2 packets in the first
300 s, so the counter stops at the last forced subtitle and stays there. `-sn` makes it advance;
the `-fflags` are irrelevant. Every BD/UHD remux with a sparse forced-subtitle track does this.

**Fix.** `transcode.run` no longer derives progress from `out_time`. It reads the kernel's read
position on the source — `/proc/<pid>/fdinfo/<fd>` for the fd whose `readlink` is the source
path — over the source size. Works for a remux and an encode alike, costs one small file read a
second, needs nothing from ffmpeg. `out_time` is only the fallback where `/proc` is absent (a
Mac). `SourcePosition` in `transcode.py`; the two call sites pass `source=path`, and the default
is the argument after `-i`, which for a disc image is a `bluray:` URL and so would not match.

**The second bug was worse and older.** The stall check sat inside `if not line:` after a
blocking `readline()`, which returns `''` only at EOF. A hung ffmpeg never trips it; a live one
printing a frozen counter every `-stats_period` looked healthy. `cancel` had the same blind
spot. The loop now ticks on a timer (stdout through a reader thread and a queue with a
`poll_interval`) and **the stall clock resets only when the read position moves** — or, without
`/proc`, when `out_time` moves. Lines arriving is not progress. The 2026-09-07 note that "the
stall detector keys off any progress line" described what was intended, not what ran.

Tests in `tests/test_transcode_run.py`, none needing ffmpeg: a Python child prints the stuck
counter and writes its own fake `/proc/<pid>/{fd,fdinfo}` tree, standing in for the kernel.

## 2026-09-14: the scan that read 100% for six days, and a stop that now stops

Reported as "scan progress gets stuck on completion and does not release, so a new scan cannot
be triggered, and there is no way to stop/cancel/restart one". All three were true, and none of
them was a hang.

**What the live instance was doing.** Scan 30 (`scheduled`, started 2026-09-07 14:51, thirty
seconds after a recreate) read `7,533 of 7,533 checked` with the bar full — and was still
running on the 13th, at **495 actions**, applying a transcode every 5–15 minutes and sharing
the single transcode slot with the continuous shrink worker (its next job sat `queued` behind a
shrink). The probes had finished on day one; everything since was the *repairing* pass, which
runs on the scan thread, one job at a time, for as long as the pending list takes —
`max_actions_per_scan` is 10,000 and the pending list was thousands long, so weeks. Nothing on
the dashboard described that pass: `checked/total` was the only figure, `current` was the last
file *probed*, and the `scan` task still said `probing 7530/7533`. The scan lock is held for the
whole of it, so `Scan now` was refused and the 24-hour schedule was skipped every tick. Every
scan since Aug 26 (23–30) had `finished NULL` and zeroed counters — each killed by a deploy
restart before its repairs ended — so `last_scan_finished` read Aug 26, every restart found the
schedule overdue, and started a new week-long scan thirty seconds in. Pausing (tried at 15:07)
does not touch a running scan either.

**The August note blaming `pool.map` was wrong.** Measured on CPython 3.14: breaking out of
`for … in pool.map(…)` cancels the queued futures (6 of 50 ran, `with` exited in 0.6 s) — the
map generator's `finally` does it when the iterator is dropped. The stop check has been in the
probe loop since 1.0.0. What actually happened on Aug 27 was a stop *during repairing*: the
loop checks `_stop` only between jobs, never reaches the job under way, and a job waiting for
the transcode slot behind the shrink worker's encode blocks on the semaphore for the length of
that encode before it can even look at a cancel event. So a stop appeared to do nothing, and a
restart really was the only thing that ended it. Reproduce against the actual helper — the same
lesson as the `q1` cursor theory.

**Found while wiring the stop in, and worse than the complaint:** `_transcode`'s failure path
falls through to `_redownload_row` when a *repair* fails and `corrupt_action` is `redownload`,
and a cancelled run took that path too. A scan stopped mid-remux would have deleted and
re-searched the file being repaired. `test_a_cancelled_repair_is_not_a_failed_repair` pins it;
it fails on the old code with the file gone and a recycle row.

**What changed** (`claude/scan-stop-and-phases`):

- `ScanProgress` gained `phase` (`enumerating | checking | repairing | finishing`), `pending`,
  `position`, `phase_started` and `stopping`. The dashboard draws the two passes as two
  different things — *Repairing — 1,204 of 3,812 files with something to do · about 3 weeks
  left at this rate* — with the job in flight beneath, and the `scan` task is cleared when the
  checking pass ends.
- `Scanner.request_stop` sets the in-flight job's cancel event (the scanner makes one per
  repair and hands it to `Remediator.apply(…, cancel=)`), and the loop re-checks `_stop` after
  the event is in place so a stop cannot land in the gap. `Remediator._slot` polls the
  semaphore against the event instead of blocking on it. The probe pool is explicit
  `submit` + `shutdown(cancel_futures=True)` rather than a contract inferred from garbage
  collection. A stop in either pass records `stopped by user` and logs `scan_stopped` with
  where it got to; the old repairing-pass stop logged `scan_finished`.
- A cancelled transcode is `cancelled`, not `failed`: no attempt counted, no redownload.
- **Stop scan** / **Restart** on the dashboard, and the header button reads *Stop scan* while
  a scan runs and *Stopping…* while it stops. `POST /api/scan/restart` stops and starts the
  moment the lock is free; a plain stop arriving in between withdraws the restart.
- Scans with `finished NULL` are closed at startup as `interrupted by a restart`, before the
  schedule is restored — so **the next scheduled scan after a restart that killed one is a
  full interval later, not thirty seconds in**. `scan_at_startup` is the switch for that.

**Verified on the running app** (a scratch library of ffmpeg-rendered AVIs, an `ffmpeg` shim
that sleeps 25 s on transcode outputs only): the repairing panel and the job beneath it agree;
Stop shows *Stopping* at once and lets go in ~4 s with the job `cancelled`, the source intact,
no temp output and nothing recycled; Restart cancels and has a fresh scan repairing six seconds
later; a restart of the server mid-repair closes the scan as interrupted; a scan left alone
finishes with `aborted NULL`. First attempt tripped the abort brake — five of six demo files are
"broken", which is exactly what an unmounted array looks like — and that is invariant 4 doing
its job, not a bug in the demo.

**Still structural, not done here:** the repairs are a *phase of the scan*, so a scan holds the
library for as long as its backlog takes and the schedule means nothing while it does. The real
shape is the shrink worker's — a persisted queue of decisions drained by a worker, with the scan
reduced to the checking pass and the brakes evaluated when the queue is built. Until then, the
figures are honest and the stop works; the lock is still held for the duration.

**Deploy note:** the live scan 30 will be closed as `interrupted by a restart` by the recreate,
`last_scan_finished` becomes the recreate time, and the next scheduled scan is 24 h later. Press
*Scan now* to start the first one under the new code and watch the repairing figures — the
pending count is the number nobody has seen yet.

## 2026-09-14: deployed — scan phases and stop, and the image that was never recreated

PR **#23 merged** (`de62cad`), `:edge` published; `unfk` rebuilt from it and recreated at 17:36.
Backup at **`/mnt/user/appdata/.unfk-backups/pre-scanstop-2026-09-14/`** — on the cache pool this
time, not `/root`, which is a RAM disk and has quietly held every previous backup. It is an
online `sqlite3` `.backup()` taken from inside the container as uid 99 rather than a copy of the
live db + wal + shm, so it is consistent by construction (`integrity_check ok`, 18,547 files).
Rollback tag **`unfuckarr-makemkv:rollback-2026-09-14`** = the image that was actually running.

**⚠️ The 2026-09-08 image was built and never deployed.** `unfk` had been running the image
started 09-07 13:50Z (`be584e4c92f5`, since re-tagged `rollback-2026-09-08`) while
`unfuckarr-makemkv:edge` pointed at the 09-08 build (`0a0dcca3f82b`, PR #22's read-position
progress and the stall detector that can fire). Checked with
`docker exec unfk python -c "import unfuckarr.transcode as t; print(hasattr(t, 'SourcePosition'))"`
→ `False`. So the 09-08 notes above describe code that reached the box today, not on the 8th.
`docker ps` shows an image *ID* rather than a tag when the tag has moved on; that is the tell.
Check the running container's code, not the tag, before believing a deploy happened.

**What the first start under the new code did:** `scans_reconciled` closed **19** open scan rows
(1–7, 10, 11, 13, 14, 23–30 — it was never eight), `jobs_reconciled` failed 4 interrupted jobs,
and the leftover sweep removed 190 `*.unfuckarr*` files totalling 1.47 GB — all Emby artwork
and `.nfo` sidecars written against earlier temp outputs, not media. One survivor:
`The Hunger Games (2012)/…BR-DISK.unfuckarr.convert/` is an **empty, root-owned** directory from
the 08-24 hand-run conversion; harmless. `last_scan_finished` is now 17:36 on the 14th and the
next scheduled scan is 17:36 on the 15th — the schedule change from #23, working as intended.
The shrink worker resumed within the minute. Services green; recycle bin on `/media/.recycle`,
writable.

**Nothing has been scanned under the new code yet.** The first scan — manual, or the scheduled
one tomorrow — is the one to watch: `max_actions_per_scan` is still 10,000, so the repairing
pass will again run for weeks, but this time it will say so, and it can be stopped.

## 2026-10-12: "Emby did not say why" on 3,348 files, and three reasons none of them moved

Reported as files sitting at *Emby would transcode this: Emby did not say why* and never being
resolved. The tag itself is innocent — Emby 4.9.5 gives no `TranscodeReasons` on any refusal
(2026-08-27), so it is on **every** incompatible file, 3,348 of 3,348. What kept them there was
three separate things, all measured on the live box (read-only: the API, the DB opened
`mode=ro` as the app user, and the two September backups through a throwaway container).

**1. A full disk wrote off 3,815 files in a day.** 3,254 of the incompatible files sat at
`fix_attempts` 2, and their mtimes were months or years old — nothing had ever replaced them. The
22 September backup's activity log holds 2,403 `transcode_failed` events between 09-21 18:07 and
09-22 18:23, every one `Error opening output …: No space left on device`. Scan 32 started 4,059
repairs and finished in **20 minutes**; scan 33 did the same in **8**. Each instant failure
counted, so two scans took everything from 0 to 2. Comparing against the 09-14 backup: **3,032
incompatible and 783 hygiene files had 0 attempts on 09-14, are byte-identical today, and are at
2.** Every scan since refused them — the ~4,040 nightly "actions" were all flags.
`free_space_ok` could not have caught it: `disk_usage` on `/media` (shfs) reports 32 TB free across
the array, but the `media` share is `shareUseCache="yes"` and new files land on the 7.3 TB NVMe
pool first. Re-running a capped file's exact ffmpeg command on 10-12 worked (DTS → 6-channel AAC,
video copied), so nothing about the files was wrong.

**2. Emby opens a file on its default audio track.** 480 more files carried only
`mixed_audio_support` — a DTS or PCM default with a playable AC3 track beside it — and the local
check's comment said "one playable track is enough: Emby picks it". It does not. Battlestar
Galactica S01E01 (DTS 5.1 default, AC3 2.0): `PlaybackInfo` refuses; the same call with
`AudioStreamIndex` on the AC3 says `SupportsDirectPlay: True`. Con Air (PCM default, five AC3)
the same. `plan` only re-encoded audio when *nothing* was playable, so the plan for every one of
these was a stream copy.

**3. That stream copy looped through the watch folder.** `_confirm_fixed` re-checks without
Emby, so the unchanged copy read as a fix (no counter, `files_fixed` bumped). The swap looks like
an arrival; the arrival check asks Emby, gets the same *no*, and goes again. Since 10-07 (as far
back as the 5,000-row activity log reaches): **~15 TB rewritten across 20 films** — Blade II
162 times, a 53 GB Starship Troopers 84, a 58 GB Fast & Furious 68 — each pass recycling the
original, all of it written to the cache SSD. Whether this is what filled the pool on 09-21 is
not proven; it is the obvious suspect.

A tail of 8 Dark Angel episodes is refused for a default+forced DVDSUB subtitle (direct play with
subtitles off) — not handled here. 84 uncapped files are disc images waiting for the 2-a-night
conversion cap.

### What changed (`claude/default-audio`)

- `transcode.out_of_space` + `Remediator._out_of_space`: ENOSPC/EDQUOT is logged `no_space`,
  counted against nothing (transcode, shrink or convert), and returns `halt`;
  `Scanner._remediate` stops the pass (`repairs_halted`) and the shrink worker backs off 30 min.
  Invariant 26.
- `compat.default_audio_unplayable` (error) replaces the bad half of `mixed_audio_support`, which
  is now information only and out of `COMPAT_ACTIONABLE`. `_fix_default_audio` promotes a
  stand-in (same language, not commentary/description, channels ≥ min(default, 6)) or adds an AC3
  5.1 copy of the default at 640k as output audio 0 and keeps the original — Allan chose this
  hybrid over always-promote (Battlestar would go stereo) and always-add. Invariant 27.
- **A file with no playable audio takes the same path.** About 1,900 of the files the reset
  releases are DTS-only, and `bad_audio_codec` used to re-encode every track to the configured
  AAC at **192k** — six channels at a stereo bitrate, with the DTS discarded. Asked before the
  reset, Allan chose the hybrid here too: an AC3 5.1 640k default in front, originals kept. Only
  an MP4 target (which cannot carry DTS/TrueHD) still re-encodes everything.
- `transcode.changes_nothing`: `_transcode` refuses a non-repair plan that would write the file
  back unchanged and logs `transcode_no_op` as a **warning**. Seeing one means `decide` and `plan`
  disagree again.
- 17 new tests, each confirmed to fail on the old source first. One existing test pinned the bug
  (`mixed_audio_support` earning a transcode) and now uses `unusual_pixel_format` for the same
  point — a warning-severity code that `plan` really does act on.

### Deployed 2026-10-13, and the counters put back

`unfk` runs **`5a935be`** (v1.3.0) since 00:46. Backup at
`/mnt/user/appdata/.unfk-backups/pre-default-audio-2026-10-12/` (online `.backup()` as uid 99,
`integrity_check ok`, 18,789 files, + config + inspect). Rollback tag
**`unfuckarr-makemkv:rollback-2026-10-12`** = the image that was running (`7a88cdfdad7c`).

**⚠️ The rebuild recipe is broken: the MakeMKV PPA has moved to 2.0.0.** `docker build -f
docker/makemkv/Dockerfile` fails with `Version '1.18.4-1~noble' for 'makemkv-bin' was not found`
— `~heyarje/makemkv-beta` carries only `2.0.0-1~noble` now and keeps no old versions. A major
version under disc conversion is not something to slip in with an unrelated fix, so the deployed
`unfuckarr-makemkv:edge` (`1a04e237107b`) is an **overlay**: `FROM rollback-2026-10-12`,
`COPY unfuckarr/ /app/unfuckarr/`, plus the `import unfuckarr.api` check. That is exact for this
change — between `61b00de` and `5a935be` only the Python package changed (no `web/`,
requirements, entrypoint or base-image change) — and it keeps MakeMKV 1.18.4. Labelled
`com.stoatworks.overlay-of=rollback-2026-10-12`. **The next real rebuild has to bump
`MAKEMKV_VERSION` and verify a conversion first.** → **It cannot: 2.0.0 segfaults on every
Blu-ray image.** See the next section; the overlay is now `docker/makemkv/Dockerfile.overlay`.

**Counters.** With the container stopped, as uid 99 from a throwaway container:

- **3,816** reset (3,032 incompatible, 783 hygiene, 1 ok): 0 attempts in the 09-14 backup and
  byte-identical (size + mtime) since. List in `/config/reset-2026-10-12.json`.
- **210** more (`/config/reset-2026-10-13.json`), found when Battlestar S01E01 was still refused
  at the cap: 0 on 09-14, 2 on 09-22, unchanged since — they *had* changed in between (shrunk, or
  no-op-remuxed in scan 31), which the first rule excluded. 206 of them are DTS/TrueHD defaults
  beside AC3, the very files the old planner could only copy, and that copy never counted an
  attempt — so the two attempts can only be the full-disk scans.
- Left capped on purpose: 12 incompatible (10 `.ts` capped since August, 2 changed since 09-22),
  64 hygiene (most capped since August), 102 ok.

Both resets logged as `fix_attempts_reset` in the activity log.

**Startup** removed **230 abandoned outputs, 94.8 GB** — the loop's half-written copies — and
failed 13 interrupted jobs.

**Verified on real media, through the app.** Before the swap, from a throwaway container on the
new image with `/media` read-only, 30 s of each real plan into `/tmp`: Battlestar S01E01 → AC3 5.1
added as default, DTS-HD MA and the director's commentary kept (the AC3 2.0 *was* the
commentary); Con Air → English AC3 5.1 promoted, PCM kept; X-Files S09E01 (DTS only) → AC3 5.1
added, DTS kept; Blade II → English AC3 5.1 promoted. No compat errors left on any output. After
the swap, `POST /api/files/recheck?act=true` on Battlestar S01E01: `add ac3 5.1 as the default
audio`, confirmed fixed, `fix_attempts` 0 — and after an item refresh **Emby's `PlaybackInfo`
says `SupportsDirectPlay: True`** with the unfuckarr profile, on the file it refused an hour
earlier.

Not yet seen: the watcher's arrival check on that swapped file. It was queued behind a Scott
Pilgrim shrink search holding the arrival pool (`_watch_pool` is a semaphore of 2 shared with
whatever the arrivals go on to do), which is old behaviour. Expect `arrival_checked … "action":
"none"`; a `transcode_no_op` warning anywhere means `decide` and `plan` disagree again. The
first scheduled scan under all of this is **18:01 on 10-13**, with ~4,000 released files — about
1,900 of them audio-only adds, 667 of them AVI → HEVC VAAPI encodes. It will run for days.

## 2026-10-13: MakeMKV 2.0.0 cannot open a Blu-ray image — the bump that was not made

The job was "bump `MAKEMKV_VERSION` to whatever the PPA has, check the parser against it, verify
a conversion, merge". The verification failed at the first disc, so **the pin was not bumped**.
What was found instead, all measured on lilnasx from throwaway containers with `/media`
mounted read-only (`unfk`, its config and the library were not touched):

**The PPA.** `~heyarje/makemkv-beta` carries only `2.0.0-1~noble` (amd64 + arm64, published
09-26). The 1.18.4 noble publications were superseded on 09-27 and Launchpad has **already
purged their files** (`binaryFileUrls` is empty) — so there is no 1.18.4 for noble anywhere
except the local images on lilnasx. **Keep `unfuckarr-makemkv:rollback-2026-10-12`** (and the
older rollback tags): they are now the only copies. www.makemkv.com is up again and lists
2.0.0 (20.9.2026) as current, with 1.18.4 still under `/download/old/`.

**2.0.0 builds, and segfaults on every image.** `docker build --build-arg
MAKEMKV_VERSION=2.0.0-1~noble` → `unfuckarr-makemkv:makemkv2-test` (`c3505691f10b`) is clean,
and `info disc:9999` is line-for-line the shape 1.18.4 prints (`MSG:1005` banner,
`MakeMKV v2.0.0 linux(x64-release)`, 5074, 5042, 16× `DRV`, 5010, `TCOUNT:0`). But
`info iso:` on Mortal Kombat: Annihilation and Jurassic World Rebirth exits **139** after
`MSG:3007 Using direct disc access mode` and `MSG:3305 AACS directory not present, assuming
unencrypted disc`; the kernel logs `segfault at 0 … error 4 in makemkvcon[5a6422,…]` — a NULL
read in makemkvcon itself, same offset every run. Same through the real entrypoint (uid 99
*with* a passwd entry), as root, with `LD_LIBRARY_PATH` cleared, with 1.18.4's downloaded SDF
copied in, and in a **bare `ubuntu:24.04`** with only the PPA — so it is not this image.
1.18.4 with the same key reads both in 3–5 s (`TCOUNT:3`, 436 lines; 6,357 lines). Upstream
has the identical report, same last two messages:
[flathub/com.makemkv.MakeMKV#123](https://github.com/flathub/com.makemkv.MakeMKV/issues/123),
open since 10-04, no response.

So the parser check could only cover what 2.0.0 emits before it dies — the banner, the `MSG`
record shape and `DRV` — and those are unchanged. **No CINFO/TINFO has ever been seen from
2.0**; the attribute ids remain verified on 1.18.4 only. Re-check them the day a 2.0.x opens an
image.

**What deploying 2.0.0 would have done**, which is the part worth fixing regardless: a crash
printed no TINFO, `titles` raised "no titles reported", and `_convert` wrote **`convert_skipped`
permanently** — two discs a scan, for a bug in the tool. `makemkv.available` would not have
caught it (2.0.0 answers `disc:9999` normally). Fixed: `makemkv.Crashed` (an `Unavailable`)
for a signal death, negative or 128+N through a shim, from `available`, `titles` and `rip`;
the rip handler now lets an `Unavailable` through before it counts the disc's one attempt —
which also means a key lapsing mid-copy no longer spends it. Verified against the real crash:
unfuckarr's own `check_file` → `decide` → `apply` in the 2.0.0 container gave `makemkv_unavailable:
makemkvcon crashed (SIGSEGV) after: AACS directory not present…`, `convert_skipped` NULL,
`convert_attempts` 0.

**Live conversions were already stopped, by the key — and the wrapper could not see it.** The
live `settings.conf` has had **no `app_Key` line since 10-05 13:05**: MakeMKV removed the expired
key itself and rewrote the file — which made it look *freshly written* to the wrapper's
`find -mtime +7`, so nothing would refetch until the file was 8 days old (~10-13 13:05). 1.18.4
still converted Fast & Furious keyless on 10-08; by today `docker exec … /usr/bin/makemkvcon`
answers `MSG:5021 This application version is too old`. With the forum's key the same 1.18.4
reads discs normally. Reproduced from a copy of the live file: old wrapper → no fetch → 5021.
Fixed: the wrapper refetches whenever the file has no `app_Key`. The **unfixed live wrapper
should heal itself at ~13:05 today**, before the 18:01 scan, if the forum fetch works.

**Then something else stops them anyway** — fixed since, in "two refused discs had been spending
the whole conversion cap" below: the two conversion slots per scan have gone to **F9 and The Blues Brothers** every night since 10-09.
Both carry `convert_attempts=1` and `_convert` refuses them, but the scanner spends the slot
before it asks (`convert_cap_reached` against Jurassic World Rebirth on 10-08…10-12). Same
trap invariant 25 fixed for `research`. And the Fast & Furious `convert_done` logged
`"saved": -58232878315`: the image's row had no size, so the conversion took 58 GB *off*
`bytes_saved` — fixed in "every disc that arrived was counted as minus the whole film" below.

**What did pass: a full conversion on 1.18.4, from the live key state.** What an overlay rebuild
would ship (rollback-2026-10-12's MakeMKV + this branch's code and wrapper), started
from a copy of the live keyless `settings.conf`, ran unfuckarr's own `check_file` → `decide` →
`Remediator.apply` on **Jurassic World Rebirth** — the disc the live scanner reaches next —
bind-mounted read-only into a scratch folder, `keep_disc_image` on. The wrapper fetched a key on
the first call; MakeMKV listed **77 titles**; `select` took title 0, **8020 s, 20 chapters**;
the whole apply, feature and extras, took **1,307 s** (≈ 80 MB/s off the array, sharing it
with the live shrink worker).
Output **76.3 GB HEVC 3840x2160 10-bit, `smpte2084`** — HDR untouched — TrueHD 7.1 and seven
more audio tracks with their languages, five PGS tracks, 20 chapters, **8020.01 s against the
8020 s promised** and 8019.97 s measured off the disc by `disc.py`. `_verify_conversion`
passed, the row moved to the `.mkv` with `converted_from` set, 24 extras placed.

**And it found the extras heuristic's real failure** — fixed since in `select`, see "Jurassic World
Rebirth — extras that repeat one another" below: those 24 extras are
**31.6 GB**, so feature + extras = **107.9 GB from a 94.5 GB image**. The playlists are
*nested*: title 7 (56 min, "play all") is segments 482–499, title 19 is 497–499, 13 is 498–499,
71 is 498 alone — `select` dedupes only an *identical* map, so segment 498 was written four
times, and 24 is `max_extras` (6 more were turned away). On the live box (`keep_disc_image`
off) converting this disc would grow the library by 13 GB.

**The rebuild recipe, until a 2.0.x is verified:** `docker/makemkv/Dockerfile.overlay`
(new) — `FROM` a local image whose MakeMKV works, `COPY` `unfuckarr/`, `web/` and the key
wrapper, `import unfuckarr.api`. Built on lilnasx from this branch as
`unfuckarr-makemkv:overlay-test` (`e53ca6cd27a0`): 1.18.4 kept, fixed wrapper at mode 755,
`Crashed` present. Exact only while `requirements.txt`, `docker/entrypoint.sh` and the
`Dockerfile` are what they were for the image it sits on — none has changed since `61b00de`,
which `rollback-2026-10-12` was built from. `docker/makemkv/Dockerfile` stays pinned to
1.18.4 and fails at `apt-get install` on purpose: a build that fails is better than one that
produces an image converting nothing.

Options for later, none taken: wait for a 2.0.x; or build 1.18.4 from makemkv.com's
`/download/old/` tarballs (sha256-pinned), which would make the recipe reproducible again —
but 1.18.4 now depends on the beta key alone, and how long the forum's keys keep accepting an
old version is unknown.

Left on lilnasx for inspection, safe to delete: images `unfuckarr-makemkv:makemkv2-test` and
`unfuckarr-makemkv:overlay-test`; scratch at `/mnt/user/appdata/.makemkv2-test/` (the raw
`info` captures for both versions are in `out/`; the 108 GB of conversion output is deleted).

## 2026-10-13: two refused discs had been spending the whole conversion cap

The last real conversion was Fast & Furious on 10-08. Every scan since logged
`convert_cap_reached` against Jurassic World Rebirth, the next disc in line, and the only two
`convert` jobs in the 10-12 17:01 scan were `F9 (2021) BR-DISK.iso` and `The Blues Brothers (1980)
BR-DISK.iso` — both `convert_attempts` 1, both refused inside `apply` with *a previous conversion
of this disc failed — not trying again*. (Read-only queries against the live DB.)

Why: `decide` cannot see the file row, so it keeps deciding `convert` for a disc `_convert` will
refuse (converted, `convert_skipped`, or attempts ≥ `MAX_CONVERT_ATTEMPTS`). The scanner counted
the slot whatever `apply` returned, and its biggest-first order put the same two written-off discs
at the head of every pass, so with `max_conversions_per_scan` 2 nothing behind them could ever be
reached. It is exactly the trap invariant 25 had already fixed for `research`'s cooldown, one
branch further down the same loop.

Fix: `Remediator.convert_refused(row, force=)` holds the three refusals; `Scanner._remediate`
asks it before the cap and `continue`s without spending a slot, and `_convert` asks it again as
the backstop (the watch folder and the UI go straight to `apply`). `force` reopens a skipped or
failed disc and never a converted one, as before. Three tests beside the cap tests; the two
scanner ones were run against the old `scanner.py` first and failed there — the first with
`applied == [F9, Blues Brothers]`, i.e. the live shape. AGENTS.md invariant 20.

**Not deployed.** The live box is still on `5a935be`. The 84 discs waiting on the cap stay
waiting until it is — and the overlay recipe from the entry above (the PPA has moved to MakeMKV
2.0.0) is still the only way to ship it without a MakeMKV version change.

## 2026-10-13: every disc that arrived was counted as minus the whole film

The header's `bytes_saved` went **down** by the size of a film each time a disc image arriving
through the watch folder was converted. `Remediator._convert_inner` reckoned the saving as
`file_row["size"] - (feature + extras)`, and the row a watch-folder arrival inserts has no size —
`_check_arrival` builds `record` from the `INSERT OR IGNORE` before `persist_result` writes one.
Fast & Furious (2009) logged `"saved": -58232878315` on 10-08: exactly minus its feature and three
extras.

**Fixed:** `_convert` measures the image with `os.stat` before anything moves it (row and
`MediaInfo` only as fallbacks) and the saving uses that. A conversion also never debits the total
now: a negative figure logs `convert_output_larger` (warn, with `image` and `replaced`) and counts
nothing. `convert_done` carries `image` and `replaced` too, so the next audit does not need
Radarr to reconstruct them.

**Why "never negative" and not just "measure properly"** — four live discs genuinely came out
*larger* than their image, with correct sizes on the row: The Hunger Games (+21.9 GB), Once Upon
a Time in Hollywood (+12.8), Dragonslayer (+13.9), Ratatouille (+7.7). Their `extras/` folders
repeat themselves: near-identical pairs (2,483,713,036 vs 2,483,378,542 bytes) and a long
"play all" title beside its parts (Hunger Games' "Extra 01 (122 min)", 28 GB). `select` drops
exact segment-map duplicates and other cuts of the feature, but not those. That is a selection
fault to fix in `makemkv.select`, not a saving to subtract — and the warning is where it now shows.

**Audit of the live counter** (read-only: live DB as uid 99 with `mode=ro`, the three backups
`immutable=1` from a throwaway container, Radarr's history API). `discs_converted` = 22, all
identified: 6 by 09-14, 16 between 09-22 and 10-12. (Mortal Kombat, the 08-23 kept-image test, is
the one before the counter existed; it changed nothing either way.)

- **Image sizes come from Radarr**, not from us: every `.iso` has a `downloadFolderImported` and a
  `movieFileDeleted … MissingFromDisk` carrying its exact bytes. The recycle bin had already been
  swept and the rows moved to the `.mkv`.
- **Arrival vs scan** from timing: a conversion with no scan started between Radarr's import and
  the conversion ran on a row the arrival created, so with no size. That is 14 of 22, and the
  pre-09-14 backup's activity confirms 5 of them as `arrival_checked → convert` with the
  `-replaced` figure.
- 4 conversions have lost their `converted` marker (Fast & Furious, Fantastic Beasts, The Hunger
  Games, Mad God); placed in time by their extras' mtimes. Radarr's history shows it picking up a
  `*.unfuckarr.mkv` temp output as the movie file mid-remux — the likeliest way the row was dropped
  and re-created.

Counted so far for conversions: **−583.4 GB**. The fixed code would have counted **+97.4 GB**.
One-off correction: **+680,813,383,056 bytes** — +646.1 GB for the 14 arrivals (each exactly the
image's size wherever the image outweighed the output) and +34.7 GB for the two scan-driven
negatives. Three figures (Hunger Games, Once Upon a Time, Dragonslayer) are reconstructed from
`shrunk_from`/current size + extras on disk rather than read from a log, so they could be off by
the few MB a no-op remux changes a file by. Not applied yet: waiting on Allan.

⚠️ **The counter is wrong in the other direction too.** Since 10-07, 25 files that arrived
through the watch folder were **shrunk twice within minutes**: the arrival path and a second
worker both searched the same original, the first swapped its output in, and the second then
encoded *that* output (generation loss — invariant 11) and counted a saving against the
original's stale size. Red Dwarf S03E01: 1.75 GB file, `saved` 1.16 GB then 1.26 GB. Not fixed
here, and the conversion correction alone does not make `bytes_saved` right. (Fixed and measured
in "two jobs shrank the same file" below: it is 48 files back to 08-26, not 25.)

## 2026-10-13: Jurassic World Rebirth — extras that repeat one another, fixed in `select`

Measured on lilnasx in a throwaway container (not the live `unfk`): the real `_convert` path with
MakeMKV 1.18.4 on `Jurassic World Rebirth (2025) BR-DISK.iso` (94,465,884,160 bytes, 77 titles)
chose the right feature — title 0, 8020 s, 20 chapters, 76.3 GB written — and then **24 extras,
31.6 GB**, hitting `max_extras` with 6 more turned away. Feature + extras = 107.9 GB, 13 GB more
than the image. On the live setting (`keep_disc_image` off) that conversion grows the library.

The disc offers each of six featurettes four ways. From the robot output
(`/mnt/user/appdata/.makemkv2-test/out/jw-1184.txt` on lilnasx, `--minlength=60`):

| title | length | segments | what it is |
|---|---|---|---|
| 7 | 56:20 | 482,483,485,…,497,498,499 | "play all" of the six |
| 19 | 11:56 | 497,498,499 | title card + featurette + tail |
| 13 | 11:53 | 498,499 | featurette + tail |
| 71 | 11:49 | 498 | the raw clip, added by MakeMKV |

Segments 482, 485 … 497 and 484, 487 … 499 are 3–4 s cards — MakeMKV logs each one as under the
minimum (`MSG:3025 … has length of 3 seconds`). `select` deduped only on an *identical* map, so 498
was ripped four times; the extras came out in near-identical groups (1,457,198,695 /
1,456,907,560 / 1,449,320,915 bytes at 12 min).

**Fix, in `makemkv.select`** (docstring rules 5–7, AGENTS.md disc-conversion traps):

- An extra is kept for what it **adds**: at least `extras_min_seconds` not already in the feature
  or an extra kept so far. The shared part is measured from below, out of titles MakeMKV timed
  that fit inside it without sharing a segment (`_overlap`) — so only MakeMKV's own durations are
  used, and an overlap nothing timed (clips under `--minlength`) counts as new. Of the 75 segments
  Rebirth's titles use, 57 are titles in their own right and the other 18 are logged short.
- **The pieces beat the play-all**: a title waits until every candidate *playlist* inside it has
  been weighed. Not raw clips (`Title.is_clip`, source `.m2ts`) — a documentary authored as four
  clips would otherwise become four fragments.
- **A budget**: extras that would take MakeMKV's size estimate past the image are refused
  (`_convert` passes the measured image size). Belt and braces; it does not fire on Rebirth.

Replayed against the real output: **14 extras, 13.0 GB** by MakeMKV's estimate (was 24 / 33.5 GB),
feature + extras 93.0 GB against 94.5. Every featurette once, as the disc's own playlist (8–13)
rather than the bare clip, so each keeps its 3–4 s tail card and loses its opening one. The play-all
goes with *all but 22s of it is in titles 8, 9, 10, 11, 12, 13*. Mortal Kombat's 3-title output
selects exactly as before. One loss, deliberate: title 2 strings together a 77 s piece and a 169 s
one; the 169 s one is kept as its own playlist, the 77 s one was already under the 90 s floor on its
own and goes with the play-all (lower the floor to 60 and it comes back as title 3).

Tests in `tests/test_makemkv.py` carry the real segment maps (the 42 one-minute clips that overlap
nothing are left out). Run against the old `select` with only a `budget` parameter shimmed in, the
six selection tests failed there; the shared-bumper test passes on both, which is its point —
it guards against the fix over-reaching.

**Not verified:** no rip has been done with the new selection, so 13.0 GB is an estimate (it ran
~4.6% high on the feature). The four live discs that already came out larger than their image
(Hunger Games, Once Upon a Time in Hollywood, Dragonslayer, Ratatouille) have no captured robot
output; that this catches them is an inference from their `extras/` folders. **Not deployed.**

## 2026-10-13: two jobs shrank the same file, and the counter counted both

**What happened** (read-only: live DB as uid 99 `mode=ro`, the three backups `immutable=1` from a
throwaway container, `config.json`). Live runs `shrink.continuous` on, `max_concurrent` 1, and
watch folders on `/media/tv` and `/media/movies`, so the scanner skips shrinks entirely and only
two callers ever start one: the arrival check (`_check_arrival` → `apply`) and the continuous
worker (`_shrink_one` → `apply`). Both read the row and probe the file *before* `_shrink` waits
for the one slot, and nothing looked again afterwards. Nothing claimed a path either: `_cancel`
is keyed by path but registered only at the slot and popped as soon as the encode ends, before
verification and the swap, which is exactly the window the second job came through.

Red Dwarf S03E01, BST: `arrival_checked → shrink` 23:38; first search 23:48:11; first encode
23:50:59. The encode ends and frees the slot; the second job takes it at **23:53:02** and searches
with `size` 1,748,079,760, the original's; the first job verifies and swaps at **23:53:48**. The
second search's CRF ladder is identical to the first's to four decimals (92.0746 / 91.5604 /
89.9326 / 87.5444), so it cut its windows out of the original. Its encode at 23:55:42 read the
path, which by then held the first output. VMAF **93.5** is gen 2 against gen 1, not against the
original. `saved` was then reckoned from the stale size, and `shrunk_from` got the original's
size from both jobs.

**Scale: 48 files, not 25.** Merging the activity from all four databases: 48 paths with two
`shrink_done` 4–38 minutes apart. The first was **2026-08-26** (Jeeves and Wooster S01E01); 25
are since 10-07 and 23 sit in the 09-14 and 09-22 backups. Every one has exactly one
`arrival_checked` and two `shrink_search_started` at the same (original) size. None before
08-24, which is when the counters arrived (seeded honestly from `shrunk_from − size`).

- Red Dwarf 10 (S01E04, S02E01, S02E02, S03E01–03, S04E03, S04E05, S05E02, S05E03)
- Have I Got a Bit More News for You 10 (S57E02/03/05/07, S60E10, S65E01/02/07/08/09)
- Jeeves and Wooster 9 (S01E01, S01E02, S02E02, S02E03, S03E03, S03E04, S03E06, S04E04, S04E05)
- It's Always Sunny S18E04, S18E05; Taskmaster S22E02, S22E06
- 1 each: Fantastic Beasts: The Crimes of Grindelwald, Argo, Bad Boys (1995), Bad Boys: Ride or
  Die, Silo S03E09, Paul Merton in India S01E03, Stuart Fails to Save the Universe S01E07,
  Dispatches S2026E07, Cornwall: This Fishing Life S02E03, Deadliest Catch S21E06, Teachers
  S01E01, Scrubs (2026) S02E03, Have I Got News for You S72E02, Mad Men S07E04, Last Week
  Tonight S13E26

46 of these files are still the gen-2 encode on disk. Jeeves S03E03 and Scrubs S02E03 have been
replaced since. **The originals are gone**: the bin holds one file from today. Each file passed
VMAF 92 against the original *once*. The second generation was never measured against it, so
these files are probably under the target. Re-grabbing them through the *arr is the only way back.

**Fixed:**

- `Remediator.apply` allows **one job per file** (`_busy`, `busy_paths()`). A second caller for a
  path that already has a job logs `already_in_flight` and returns `busy` at once, rather than
  queueing on the slot. This applies to every acting decision, not just shrink: two transcodes or
  a transcode and a conversion on one file race the same way.
- `Service._next_shrink_candidate(busy)` skips claimed paths. Without that, the worker would pick
  the arrival's file every time it looked and spin on the refusal.
- `MediaInfo.mtime` is recorded by `probe` (stat'd *before* ffprobe runs). `_shrink` calls
  `_superseded` at four points: when it gets the slot, after the search (the check the live race
  needed), after the encode (before any verdict is recorded), and before the recycle/swap. It
  re-reads `shrunk`/`shrink_skipped` and compares the file's size and mtime with the probe. If
  anything moved, it deletes any output, logs `shrink_superseded`, and returns `superseded`: no
  attempt, no verdict, no counter. The worker waits 60 s after a superseded outcome so that a
  file still being written is not re-probed in a tight loop.
- **Verified the size half cannot misfire:** `probe().size == os.stat().st_size` on 15 live files
  across mkv, mp4, avi, ts and mpg (read-only, the container's own `probe`). Disc images already
  take `os.path.getsize`.
- 5 new tests in `test_shrink.py`, "one job per file", all run against the old source first.
  They failed there with the live shapes: the second job queued behind the first; a job that
  waited re-shrank the file (`'shrink' != 'shrink'`, two encodes); an encode ran after the file
  changed during the search, both for a replacement and for an mtime-only rewrite; and an *arr
  upgrade was recycled and replaced after the file changed during the encode. Full suite: 436 pass (with main's `select` fix merged).

**The over-count is the *first* `saved` of each pair, not the second.** The second job measured
against the original's stale size, so its `saved` alone is already the whole original → gen 2
reduction. It equals `shrunk_from − size` on the row **exactly** for 46 of 48 (the other two
files have been replaced since). The counter got `s1 + s2`, the disk changed by `s2`, so `s1` is
spurious. Subtracting `s2` instead would leave the counter `s2 − s1` short on every file (gen 2 is
smaller than gen 1).

| window | pairs | over-count (Σ first `saved`) |
|---|---|---|
| 10-07 → 10-12 (live log) | 25 | 32,245,577,801 |
| 08-26 → 09-22 (backups) | 23 | 95,977,704,858 |
| **logged total** | **48** | **128,223,282,659 (128.2 GB)** |

(Σ second `saved` would be 139,090,486,980.) `files_shrunk` is over by the same 48.

**Unlogged gaps.** The activity log keeps ~5,000 rows. 09-14 17:34 → 09-21 19:07 and 09-22 20:24 →
10-07 20:10 exist in no database, the bin has been emptied, and `jobs` is pruned. Estimated by
reconciling the counters against the rows shrunk in each window. Validated on 10-07 → 10-12,
where it gives 26 files / 32.65 GB against the known 25 / 32.25 GB, and on 09-21/22 (1 against 1):

- **Gap 1: ~4 files, ~5.4 GB.** No conversions in that window, so this is reasonably firm.
- **Gap 2: ~51–68 files, bytes not measurable.** 68 is the `files_shrunk` excess, an upper bound
  inflated by rows later replaced. 51–54 comes from the recorded VMAF: a double's score is gen 2
  against gen 1 and reads high, 73% ≥ 93.5 against 10% of singles. The bytes residual mixes in
  15 conversions, and two of the six counted by 09-14 ran between the counters landing
  (08-24 01:53) and the log starting (06:19), so their figures are unknown. The
  double count comes out somewhere around **100–240 GB** depending on those two. Pinning it
  needs the per-disc conversion figures from the audit above.

**Not deployed, and nothing written to the live DB.** Corrections waiting on Allan, best applied
together with the conversion one in the entry above:

- `bytes_saved` **+680,813,383,056** (conversions) **− 128,223,282,659** (logged doubles) =
  **+552,590,100,397**; `files_shrunk` **− 48**.
- Optionally the gap estimates: about −5.4 GB / −4 for gap 1, and an unknown −100 to −240 GB /
  −51 to −68 for gap 2.

## 2026-10-13 15:46: deployed — everything above, on the overlay

`unfk` runs **`1ba9dad834dc`** = `docker/makemkv/Dockerfile.overlay` on
`rollback-2026-10-12` (MakeMKV 1.18.4) with the app at **`451d8ef`** — so the crash guard, the
key wrapper fix, `convert_refused`, the arrival saving fix, the overlap extras selection and
one-job-per-file all went live together. Allan's call, asked first. Rollback tag
**`unfuckarr-makemkv:rollback-2026-10-13`** = what was running (`1a04e237107b`, app `5a935be`);
**keep `rollback-2026-10-12`**, it is the only local 1.18.4 the overlay can sit on. Backup at
`/mnt/user/appdata/.unfk-backups/pre-makemkv-crash-2026-10-13/` (online `.backup()` as uid 99,
`integrity_check ok`, 18,798 files, + `config.json`, inspect, and the keyless MakeMKV
`settings.conf`). `/root/recreate-unfk.sh`'s recipe comment now gives the overlay build
(backup `.bak-2026-10-13`). Nothing schema-side: no `db.py` change since `61b00de`.

**Verified in the live container:** healthy, bound to the tailnet address, Sonarr/Radarr/Emby
answering. `docker exec -u 99:100 unfk makemkvcon -r info disc:9999` — the new wrapper found the
keyless file and fetched a key into `/config/.home/.MakeMKV/settings.conf` (`nobody:users`); the
banner came back with no `MSG:5021`. Then, read-only, `makemkv.titles` + `select` on the real
Jurassic World Rebirth image from inside `unfk`: 77 titles, title 0 / 8020 s / 20 chapters,
**14 extras, 13.0 GB estimated** — the overlap selection, against the 24 / 31.6 GB the old one
wrote in the throwaway run.

**What the restart interrupted:** scan **55**, a *manual* scan started 01:16 BST (not by this
session — its containers all used a scratch `/config`). It was 14.5 h into the repair backlog
the 10-12 deploy released: **421 transcodes done**, 2 failed, 3 did not fix, alongside 107
continuous-worker shrinks. Those stand; the restart only closed the row ("interrupted by a
restart") and failed 4 in-flight jobs, which count no attempts. Remember the row's `total` and
`checked` are written only at the end — the zeros it showed beforehand were not a hang. The
18:01 scheduled scan picks the backlog up again.

Startup also removed 71 `*.unfuckarr-*` artwork files (150 MB) Emby/Radarr had generated beside
old temp outputs, and logged that it **could not** remove `The Hunger Games (2012) BR-DISK.unfuckarr.convert/`
— an empty, root-owned work dir from the 08-24 conversion test, which uid 99 cannot delete. It
holds nothing; removing it needs root.

**Not done:** the `bytes_saved` / `files_shrunk` corrections in the two sections above are still
waiting on Allan — this deploy wrote nothing to the live DB beyond what the app itself does.

## 2026-10-13: a transcode or a conversion acted on the file it was handed, not the one there

**The gap ee4bac2 left.** The claim in `apply` stops two jobs on one file *overlapping*; it
does not stop one running after the other. Every caller reads the row and probes the file before
`apply`, and `_transcode` and `_convert` then built their plan from that and never looked again,
while `_shrink` now looks four times. Found by reading the code after the shrink fix, not by a
live incident. **Not looked for on the live box** (this change did not touch it). The query that
found the 48 shrink doubles finds these too: two `transcode_done`, or two `convert_done`, for one
path minutes apart. Scan 55 (entry above: 421 transcodes beside 107 worker shrinks and the watch
folders) ran with this gap open, so that is the place to look first.

What the old code did, each one a new test that failed on it with exactly this:

- **Transcode, second caller after the first finished** (arrival and scan both decide one):
  the old plan ran on the first job's output, and `_confirm_fixed` judged a file the job never
  measured (`2 == 1` runs).
- **Repair queued on the slot while an *arr upgrade lands:** the remux ran on the upgrade,
  recycled it, and put a remux of it in its place.
- **Changed during the remux, or during verification:** swapped in over the change.
- **Repair fails because the file is being replaced:** counted, then fell through to the
  **redownload** and recycled the upgrade. The most expensive one: the replacement is gone, and
  if an *arr owns the row, a delete through the *arr follows it.
- **Stale `fix_attempts`:** one failure on record, two callers. Both ran, and both wrote 2, so
  the cap took three runs to reach.
- **Conversion, second caller after the first finished.** With the image recycled, MakeMKV was
  asked about a file that was not there. The stand-in copied it anyway; a real one fails, and
  that failure is a permanent `convert_skipped`. With `keep_disc_image` on, the image is
  untouched and only the row moved, so the whole disc was copied out again, swapped over the
  first output, and `discs_converted` was counted twice.
- **Disc replaced after its probe:** copied out a disc nobody measured, against the old
  reference runtime.
- **Disc replaced while MakeMKV read it:** `convert_skipped` was written for both verdicts
  (unreadable, no title matching the runtime).
- **Disc replaced during the feature or an extra:** upgrade recycled, old copy swapped in.
- **Copy fails on a replaced disc:** `convert_attempts` 1, which is `MAX_CONVERT_ATTEMPTS`, so
  the upgrade was written off for good.

**Fixed** (`remediation.py`):

- `_changed(path, size, mtime)` is the stat half of the old `_superseded`. `_stand_down`
  deletes any output, logs `<action>_superseded`, and returns `superseded` with nothing
  recorded. All three actions use both. The job message is now *"stood down, nothing recorded
  — <reason>"*, because the old *"the file changed after it was measured"* is wrong when only
  the row moved.
- `_superseded` (shrink) keeps `shrunk`/`shrink_skipped`, and only the shrink path asks them. A
  shrink has no bearing on whether a repair or a conversion should run.
- `_transcode_superseded`: re-stat against the probe, then re-read `fix_attempts` from the row
  (refreshing `file_row`, and standing down at the cap). It runs **on getting the slot**,
  **after the run** (before any verdict, including the redownload fall-through; skipped on a
  cancel, which already counts nothing) and **before the recycle**.
- `_convert_superseded`: first a row elsewhere with `converted_from` = this path and
  `converted > mtime`, which is the kept-image case. Then `convert_refused` on the row as it is
  now. Then re-stat. It runs **on entry** (before `force` clears anything), **after MakeMKV
  reads the structure** (before either skip verdict and before the rip), **after the copy**
  (before the attempt count and verification) and **before the swap**. The baseline is the
  probe's `size`/`mtime`. A disc that did not probe (the libudfread refusals) is stat'd on entry,
  so it is guarded from there but cannot catch a change made before the job began; for those
  the row check is the only defence against the second-caller case.

**Edges, deliberately in the safe direction:**

- `converted > mtime` is what keeps an *old* conversion from blocking a *new* image at the same
  path. An *arr set to change file dates (Radarr "Change File Date") can backdate the new image
  below that time. That disc then stands down until the next scan's sync drops the Matroska row
  the *arr no longer lists. Nothing is recorded meanwhile.
- A stand-down takes a slot from the scan's cap, exactly as `already_in_flight` does. The file
  is probed afresh on the next pass.

**Tests:** 14 new cases: 6 in `test_remediation.py` and 8 in `test_makemkv.py`, five functions
each.
Run against `HEAD` first, in a scratch export with only the test files swapped in: those 14
failed and the other 102 in both files passed. Two existing tests changed, and both had been
leaning on the bug. `test_a_transcode_that_does_not_fix_it_is_not_repeated_for_ever` and
`test_hygiene_remux_that_leaves_its_warning_counts_attempts` replayed the first probe after
their own transcode had replaced the file. With an honest probe, the real remux tags the
languages, round two's plan is a `transcode_no_op`, and the cap is never reached. They now probe
every round and fake a remux that leaves the file as it found it, which is what "did not fix
it" means. Both still pass on `HEAD`, and both fail when `_confirm_fixed` stops counting
(checked by mutation in the scratch copy). Full suite: **450 pass**, and **463** with main's
MakeMKV crash fix (`b2a5039`) merged, whose `Unavailable` paths record nothing and so need no
re-check.

**Not deployed.**

## 2026-10-14: MakeMKV 1.18.4 built from source — the real build works again

Allan asked to adopt MakeMKV 2 and deploy. 2.0.0 was not adopted, and nothing has changed
upstream since 10-13: the PPA still carries only `2.0.0-1~*` (published 09-25) and makemkv.com
lists 2.0.0 as current. Its changelog says "almost no changes, just a version bump".

**Why 2.0.0 crashes, now that more people have hit it.** The forum thread "Version 2.0.0 won't
open the disk structure" (09-25 to 10-10, no reply from the developer) narrows it down. 2.0.0 dies
straight after `AACS directory not present, assuming unencrypted disc` on any Blu-ray **without
an `AACS` folder**: never-encrypted ISOs, BDMV folders, and images decrypted by
AnyDVD HD or Xreveal. Encrypted physical discs work. Windows 11 does the same, so it is not a
Linux packaging fault. Our library is all one kind of disc: unfuckarr's own `disc.Udf` read every
one of the 95 `.iso` rows read-only inside `unfk`. **93 have no `AACS` folder, and the other 2
raise `DiscError`** (not UDF). So 2.0.0 would convert nothing here. There is no workaround that
fits the no-mount design: an ISO only gets an `AACS` folder by being mounted. 2.0.0 is not worth
trying again until a 2.0.x changelog mentions it, and even then only after a real
`info iso:` reaches `TCOUNT`.

**What was done instead, Allan's choice:** `docker/makemkv/Dockerfile` builds 1.18.4 from
makemkv.com's tarballs, so the real build works again and the image no longer depends on
`rollback-2026-10-12` being the last copy of a working MakeMKV.

- **Provenance:** `/download/old/makemkv-sha-1.18.4.txt` is a PGP clearsigned list. The
  signature checked good against `2ECF23305F1FC0B32001673394E3083A18042697`, "MakeMKV (signature)
  <support@makemkv.com>", fetched from keyserver.ubuntu.com by fingerprint. Its hashes for
  `makemkv-oss-1.18.4.tar.gz` (`8590…a453`) and `makemkv-bin-1.18.4.tar.gz` (`cee5…1608`) are the
  build args, and the tarballs downloaded on 10-14 match them.
- **The EULA:** makemkv-bin's `make install` runs `ask_eula.sh` (a `less` and a `read`) unless
  `tmp/eula_accepted` exists. The build refuses unless it is given `MAKEMKV_EULA=accepted`, and
  that refusal was checked. Allan said to pass it for the lilnasx builds.
- **Shape:** an `ubuntu:24.04` build stage runs `./configure --prefix=/usr --disable-gui`
  against noble's `libavcodec-dev` (`libavcodec60`, the same one the PPA linked). The final
  stage installs only the runtime libraries and `curl`, copies `/out/usr`, and fails on any
  `not found` from `ldd`. Libraries land in `/usr/lib`, where the PPA used
  `/usr/lib/x86_64-linux-gnu`. Without Qt the image is 910 MB, against 1.16 GB.
- **Not byte-identical to the PPA:** the tarball's `bin/amd64/makemkvcon` has a different sha256
  from the live `/usr/bin/makemkvcon` (`c6c1…` against `7214…`), presumably from the packaging's
  strip. So it was compared by what it does, from throwaway containers with `/media` read-only,
  scratch `HOME`, and a copy of the live `settings.conf`, at
  `/mnt/user/appdata/.makemkv-src-test/`:
  - `available`: `MakeMKV v1.18.4 linux(x64-release)` from both.
  - `titles` → `select`: identical on both images. Mortal Kombat: Annihilation gave 1 title,
    5685 s, 17 chapters. Jurassic World Rebirth gave 30 titles ≥ 120 s, feature title 0 at
    8020 s / 20 chapters / 79.98 GB, and 13 extras at 13.1 GB.
  - `rip` of Rebirth title 3 (173 s): 339,061,527 bytes from each image, and `ffmpeg -f
    streamhash -hash sha256` is **identical on all five streams** (HEVC, audio, three PGS). The
    files differ from byte 1,349 on, which is container metadata.
  - Key wrapper: started from a `settings.conf` with its `app_Key` removed, the first
    `info disc:9999` fetched a key, with no `MSG:5021`.
  - `ffmpeg -version` is still 8.0.1, and `import unfuckarr.api` works.

`Dockerfile.overlay` stays as the fallback for when makemkv.com cannot be reached. Keep
`rollback-2026-10-12` for that too.

### 2026-10-14 00:42: deployed — the real build, MakeMKV from source, the app at `c747ba4`

`unfk` runs **`7f86f6202`** = `docker/makemkv/Dockerfile` (`MAKEMKV_EULA=accepted`), on base
`ghcr.io/stoatworks-labs/unfuckarr:edge` at `b309a8e`, so `ee862af` (re-checking the file before
acting) is live too. Labels: `makemkv-version=1.18.4`, `app-revision=c747ba4` (docs only since
`b309a8e`). Rollback tag **`unfuckarr-makemkv:rollback-2026-10-14`** = what was running
(`1ba9dad834dc`, the overlay at `451d8ef`). Backup at
`/mnt/user/appdata/.unfk-backups/pre-makemkv-source-2026-10-14/`: an online `.backup()` as uid 99,
`integrity_check ok`, 18,799 files, plus `config.json`, inspect and the old recreate script.
`/root/recreate-unfk.sh`'s comment now gives this build (`.bak-2026-10-14` beside it).

**Verified live:** healthy, the web UI answers 200 on the tailnet, and Sonarr, Radarr and Emby
answered at startup. The code in `/app` is identical to `origin/main`. As uid 99 with the live
`HOME`, `makemkvcon -r info disc:9999` printed `MakeMKV v1.18.4 linux(x64-release)` with no
`MSG:5021`.

**What the restart interrupted:** scan **56** (manual, started 00:24) was closed as "interrupted
by a restart", and one queued transcode (Back to the Future Part III) failed the same way. That
counts no attempts. The shrink that was running had finished as `done`.
