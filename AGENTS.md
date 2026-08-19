# AGENTS.md — unfuckarr

Onboarding for an LLM or a newcomer. `README.md` is the user-facing document; this is the *why*.

## What this is

A Docker service that validates a Sonarr/Radarr media library against Emby and repairs it
unattended — transcoding, remuxing, or deleting-and-re-searching. Python 3.12, FastAPI, SQLite,
vanilla-JS web UI (no build step), ffmpeg via subprocess.

## Mental model

```
scanner.collect_library()      → inventory from Sonarr/Radarr APIs + extra paths
  ↓
scanner.check_file()           → integrity → (emby direct-play OR local compat) → hygiene → efficiency
  ↓                              produces CheckResult: a list of Findings with categories
remediation.decide()           → CheckResult + Policy → Decision (none|flag|transcode|repair|shrink|redownload)
  ↓
Remediator.apply()             → transcode.plan() → build_command() → run() → verify → replace
                                 or: quality.search() → encode → quality.verify() → replace
                                 or: recycle.store() → arr.delete_file() → arr.blocklist_last_grab()
```

`Scanner._remediate()` sits between decide and apply and holds the safety brakes.

The watch folder path is the same pipeline with a different trigger:
`watcher.WatchManager` → settle timer → `service._check_arrival()` → `check_file` → `decide` → `apply`.

## Load-bearing invariants

Break any of these and the failure is quiet and expensive.

1. **Deletes go through the *arr API, never `os.unlink`.** `os.unlink` leaves Sonarr believing
   the episode is present until its next rescan. The only exception is a file no *arr owns.
2. **Blocklist before search.** `POST /history/failed/{id}` blocklists *and* searches. A plain
   search re-grabs the same broken release. `blocklist_last_grab` returning `False` (no grab in
   history — a hand-imported file) is normal, and the caller falls back to a plain search.
3. **`Remediator._verify_output` must run before `transcode.replace`.** ffmpeg exits 0 having
   written a file with no video stream often enough to matter.
4. **The two brakes in `Scanner._remediate` are the point of the whole design.** An unmounted
   array makes every file fail every check. `abort_if_failure_ratio_over` catches that;
   `max_actions_per_scan` caps the damage if it does not. Do not "optimise" them away.
   **The abort ratio is measured against the library, not the worklist** — `_remediate` takes a
   `population` (every file the pass knows the state of), because `needs_check` deliberately
   re-queues known-bad files. Divide by the re-probed count and the brake becomes self-locking:
   the pass after a scan that found real problems consists of almost nothing *but* known-bad
   files, so the ratio reads 100% and every scan aborts for ever. That is not theoretical —
   live it ran 8 scans and took **zero** actions, escalating 62% → 100% as the worklist shrank
   to exactly the files it had already flagged.
5. **Hygiene findings can never delete.** `Policy.hygiene_action` is typed `none|flag|transcode`
   — the literal type is the enforcement, and `test_hygiene_never_deletes` asserts it.
6. **`CheckResult.status` priority is deliberate**: corrupt > incompatible > hygiene. A file that
   is both corrupt and incompatible must read as corrupt, because that is the finding that
   decides what happens to it.
7. **Emby's verdict wins over the local codec table.** When `check_direct_play` returns `True`
   (Emby answered), `compat.check` is skipped entirely so the two cannot contradict each other
   in the UI.
8. **Only `/config` is chowned at start.** Recursively chowning a 40 TB media mount on every
   container start is how someone loses an evening.
9. **A transcode must be confirmed to have fixed the finding.** `_confirm_fixed` re-checks the
   replacement, and *every* outcome short of a verified fix goes through
   `Remediator._count_attempt`: a run that fails, an output that fails verification or vanishes
   before the swap, and a re-check that still shows the findings the transcode was meant to
   clear — hygiene included, because a hygiene-triggered remux whose warnings survive has fixed
   nothing. Only a user cancel does not count, and a redownload resets the counter (the
   replacement is a different file). At `MAX_FIX_ATTEMPTS` (2) `_transcode` refuses. Without
   this, a transcode that does not clear the finding is redone on *every* scan, for ever, with
   nothing in the log saying why — live proof: 86 identical remuxes of one film (2026-08),
   because the hygiene path and the failure paths never counted.
10. **A shrink is measured at both ends, and nothing else in the codebase works this way.**
    Every other action fixes a *fault*; `shrink` re-encodes a file that is perfectly good, so the
    only thing that justifies it is proof. `quality.search` finds the largest CRF still meeting
    the target on sampled windows, and its projected saving must clear `min_saving_pct` before a
    single frame of the real encode runs. Then the **finished file** is checked again: it must be
    that much smaller *and* still score at the target when measured against the original
    (`quality.verify`). Failing either, the output is deleted and the source is untouched. Do not
    "optimise" the second measurement away because the first one passed — the sample search
    genuinely lands over the line and the full encode genuinely misses it (observed: samples
    92.0, full encode 90.3, output correctly discarded).

11. **A file is shrunk at most once, ever.** `files.shrunk` and `files.shrink_skipped` are both
    permanent, and `checks/efficiency.check` returns nothing for a file carrying either. Without
    that, an encode gets re-encoded — a second generation of loss for a fraction of the saving —
    and a file the search has already priced gets priced again, for hours of CPU, to reach the
    same answer. `Decision.force` (the UI's Shrink button) reopens a *skipped* file, because the
    settings may have changed; it never reopens a *shrunk* one, because generation loss does not
    become untrue.

12. **The measurement is the gate — nothing decides in advance which files are "too big".**
    An earlier version of `checks/efficiency.py` selected candidates by video bitrate against a
    target for their resolution. That is a guess about how an encoder will behave on content it
    has not seen, and it is wrong in both directions: it condemns grain-heavy 35mm that will not
    compress and waves through a lazy encode that would halve. `EfficiencyConfig` now decides
    only whether the *search is worth spending* (size floor, duration floor, HDR, a codec skip
    list that is purely a cost optimisation); `min_saving_pct` and the quality target decide
    everything else. `target_mbps` survives only to **order** the backlog fattest-first, because
    the per-scan cap means the order decides which savings land this month and which land next
    year. Do not reintroduce it as a filter.

13. **`unmeasured` is a state, not a claim, and it has to drain.** The finding is `not_measured`
    at info severity: nothing is wrong with the file, it simply has not been priced. Every file
    ends in one of two terminal states on the row — `shrunk` or `shrink_skipped`, both permanent
    — and `checks/efficiency.check` then returns nothing for it. That is what makes the count a
    progress figure rather than a permanent pill on the whole library. `CheckResult.unmeasured`
    is deliberately narrower than `.efficiency` for the same reason: an HDR file being skipped is
    an efficiency finding but a *terminal* one, so it reads as `ok` with an explanation rather
    than sitting in a backlog it will never leave. `needs_check` also treats `unmeasured` like
    `ok` — the backlog is the whole library at the start, and re-probing all of it nightly buys
    nothing.

14. **Efficiency findings are carried separately from everything else, and each separation is
    load-bearing.** They are excluded from `CheckResult`'s hygiene warning set (so
    `hygiene_action` cannot be what decides to re-encode a 40 GB file), they are last in the
    status precedence (a corrupt file that is also large is corrupt), `oversize_action` is typed
    `none|flag|shrink` so it can never delete, and — most importantly — **shrinks are not in
    `_remediate`'s destructive list**. The abort ratio exists to notice a library that has just
    broken. "Most of this library is H.264" is not that; an unmounted array produces integrity
    failures, and a file ffprobe cannot read never reaches the efficiency check. Counting shrinks
    there would disable the scanner permanently on almost every real library. They get
    `max_shrinks_per_scan` instead, and are applied *after* every repair.

15. **"I cannot read this" and "this is broken" are different sentences.** `probe` raises
    `DiscUnreadable` (a `ProbeError` subclass) for a disc image no available route can open, and
    the integrity check turns that into an *info* `disc_not_inspectable` — never `probe_failed`,
    which is in `REPAIRABLE_CODES` and leads to a remux and then a redownload. This is not a
    hypothetical: before disc support existed, every BR-DISK in the live library was queued for
    delete-and-re-search, and three 42 GB images are recoverable only because the recycle bin
    caught them. Any new "cannot open" path must land on the info finding, not the error one.

16. **The tailnet-only stance is policy, not decoration.** The README warning (private
    network/tailnet only, never the public internet, no independent human security review) stays,
    because the service deletes media unattended and has no auth until a key is set. In the same
    spirit, `__main__.resolve_host` **fails the start** when `UNFUCKARR_BIND_INTERFACE` names an
    interface that never gets an address — falling back to `0.0.0.0` would silently expose the
    service exactly when the VPN is down. Do not "fix" that into a fallback.

## Traps found the hard way

- **A Blu-ray image is pure UDF, not ISO9660.** Measured across the live library: of 104 disc
  images, 98 were pure UDF (`BEA01`/`NSR03`/`TEA01` at sector 16), 5 carried an ISO9660 bridge as
  well, and exactly one was ISO9660 with a BDMV directory. An identifier that looks for `CD001`
  recognises none of the 98. `disc.identify` reads the Volume Recognition Sequence and only walks
  an ISO9660 directory when one is actually there — which is the DVD case, and the case where
  telling a DVD from a Blu-ray needs the directory.
- **Never fall back to the ISO9660 bridge to find a Blu-ray's streams.** It is tempting when
  libudfread cannot read the UDF (4 of 104 images), and it is wrong: ISO9660 cannot describe a
  file over 4 GB in one extent, so the bridge lists the main feature in fragments. On a real 96 GB
  disc the largest entry it offered was 1.1 GiB — a trailer. The fallback would not fail, it
  would silently pick the wrong title. Report "not inspectable" instead. (The same limit does not
  apply to DVD: the spec caps a VOB at 1 GB precisely so it fits one extent.)
- **ffmpeg's `subfile` URL needs doubled commas**: `subfile,,start,X,end,Y,,:/path`. Drop either
  one and it fails with "Error parsing options string", which points nowhere near the cause.
- **libbluray narrates every open** — BD-J menus it will not run, a playlist whose first clip has
  no timestamp — and seeking into a GOP always reports a missing first slice. `disc.is_noise`
  filters all of it out of the decode-error count. Without that, every disc image reads as
  damaged, which is the failure the module exists to undo.
- **No distribution ffmpeg has libvmaf.** Not Debian bookworm (5.1.9), not Debian trixie (7.1.5),
  not jellyfin-ffmpeg — check `debian/rules` in any of them and `--enable-libvmaf` is simply not
  there. The image therefore installs a second, static BtbN build as `ffmpeg-vmaf`, used *only*
  for scoring, and the Dockerfile greps its filter list so a download that changes shape fails
  the build instead of silently downgrading every shrink to SSIM. Encoding stays on Debian's
  ffmpeg because that is the binary the VAAPI work was verified against.
- **The two inputs to a quality comparison must be seeked identically.** `quality.score_pair`
  takes a `distorted_window` for exactly this. Cutting the window out of a finished file with
  `-c copy` first does *not* work: a stream copy cannot start mid-GOP, so it silently begins at
  the previous keyframe and every frame is then compared against the wrong one. The resulting
  score looks precisely like quality loss and is not — a byte-identical copy scores in the 50s.
  There is a test for this (`test_a_misaligned_comparison_is_not_mistaken_for_quality_loss`).
- **libvmaf wants the distorted input first**, reference second. Getting it backwards does not
  error; it returns a different, wrong number.
- **A quality search must use the encoder the real job will use.** `transcode.video_encode_args`
  is split out for this. A CRF found with libx265 does not transfer to `hevc_vaapi`, which has no
  CRF at all (`-qp`), and a sample encoded with different settings produces a number that means
  nothing about the file that actually gets written.
- **`-nostdin` on every ffmpeg call.** Without it a stalled ffmpeg consumes the parent's stdin
  and hangs the worker. Same trap as the rest of this fleet's ffmpeg tooling.
- **A size floor cannot detect a "sample instead of the movie".** `TINY_FILE_BYTES` is 128 KB and
  only catches stubs; the real detection is `duration_mismatch` against the *arr's runtime, and
  `too_short`. A floor high enough to catch a 20 MB fake would condemn every legitimate short extra.
- **The *arr's expected runtime is nominal, not measured.** For TV it is the broadcast slot from
  TVDB, ad breaks included, so a healthy 22 min sitcom reads 12% short of its 25 min slot and a
  healthy 44 min US drama reads 27% short of its 60 min one. Treating that gap as truncation
  flagged **2,924 of 3,153** TV files on the live library as corrupt — Blu-ray remuxes queued for
  delete-and-re-search. Hence two thresholds: `duration_tolerance_pct` only produces an *info*
  `duration_below_expected`, and `duration_truncated_pct` (50%) is what raises the *error*
  `duration_mismatch`. Do not lower the second one to catch "more" — below half its runtime is the
  only shortfall a nominal runtime cannot explain.
- **`ffprobe` reports format *families*.** `format_name` is `"matroska,webm"`, not `"mkv"`.
  `_container_from` reconciles that against the extension.
- **PGS subtitles cannot be copied into MP4** — it fails the whole job. `_subtitles_to_drop`
  handles it per container.
- **Never ask VAAPI to filter frames it did not decode.** `-hwaccel vaapi
  -hwaccel_output_format vaapi` + `scale_vaapi` only works when the *decoder* also ran on the
  GPU. Hardware decode is per-codec, and the sources this tool exists to fix are exactly the ones
  GPUs have dropped — no recent AMD part decodes MPEG-2 at all. The decode silently falls back to
  software and the filter then dies **partway through the file** with "Failed to inject frame
  into filter network: Function not implemented", which reads like a driver fault rather than a
  command-line one. Decode in software, `format=nv12,hwupload`, encode on the GPU: the encode is
  the expensive half, and it works whatever the decoder did.
- **`-ss` before `-i`** so ffmpeg seeks rather than decoding up to the point. Sampling the middle
  of a 40 GB file takes seconds this way and minutes the other way.
- **unfuckarr's own `*.unfuckarr.*` temp outputs look exactly like media files.** A watch folder
  covering the library, or a scan running during a long remux, would pick up the half-written
  output, check it (it carries the same findings as its source — it is a copy of the streams),
  and start a second job that loses the race with the first one's rename into place. Live, that
  paired every remux with a spurious `transcode_failed: No such file or directory` on the temp
  path. `transcode.is_temp_output` is the single test; the walker, the watcher and
  `Remediator.apply` all use it.
- **Unraid user shares are SMB/NFS and produce no inotify events.** `WatchManager._needs_polling`
  reads `/proc/mounts` and falls back to `PollingObserver`. When it cannot tell, it polls, because
  a missed event is silent and polling only costs a stat.
- **A trailing `-- comment` on a column line breaks `ALTER TABLE ... DROP COLUMN` on older
  SQLite.** DROP COLUMN rewrites the stored `CREATE TABLE` text, and the comment then swallows
  the line after the removed one: *"error in table files after drop column: incomplete input"*.
  Newer SQLite handles it, so this passes locally and fails on the CI runner. Production never
  drops a column — the migration only ever ADDs — so it is only ever a problem for tests that
  synthesise an old schema, which strip the comments first.
- **SQLite objects are per-thread.** `db.connect()` uses `threading.local`. Scan workers, transcode
  workers and the async web layer are all different threads.
- **The event bus crosses the thread/async boundary** via `loop.call_soon_threadsafe`, with the
  loop captured in the FastAPI lifespan. A worker thread has no loop of its own.
- **The scan is single-flight** via `Service._scan_lock`, and `__main__.py` pins uvicorn to one
  worker. Two workers would mean two scanners fighting over the same library.

## Verified vs assumed

**Verified on real hardware — `vaapi`:** an AMD Radeon 880M (gfx1150) encodes 1080p MPEG-2 to
HEVC at ~12x realtime through `plan` → `build_command` → `run`, output probed as hevc/yuv420p
with the audio and duration intact. Two things had to be fixed to get there, and both are the
kind of thing only hardware finds: Debian stable's Mesa cannot initialise any AMD GPU newer than
RDNA2 (hence bookworm-backports in the Dockerfile), and the command asked the GPU to filter
frames it had never decoded (see the trap below).

**Verified in live use (since 1.0.0), and the limits of it:** a real Sonarr, Radarr and Emby
setup has been connected and used against a ~17,000-file library. Genuinely exercised: library
enumeration from both *arrs, the settle timer on real arrivals, and real repairs — remuxes
completed and verified, originals recycled, delete-and-re-search triggered.

What that does **not** cover, found by auditing the live instance on 2026-08-15 rather than by
any test:

- **No scan has ever applied an action.** Eight scans, `actions=0` on every one, all stopped by
  the abort ratio (invariant 4). Every repair in the live activity log came from the watch
  folder, and 415 of its 505 action rows were one film looping against its own temp output
  (fixed in 5427095). The distinct-file count is 22.
- **Emby's `PlaybackInfo` is barely exercised.** `emby/not_in_emby` covers 17,569 of 17,715
  files — the path mapping does not resolve, so invariant 7 almost never fires and compat
  decisions come from the local codec table. The *connection* works; the lookup does not.
- Still untested: hardware *encode* against the live library (every observed repair was a
  stream-copy remux), the CA template install path, and large-library scan performance.

Keep this section honest. "It is running in production" is not the same claim as "the code path
has run", and this project has now been wrong about that once.

**Verified in CI** — the test suite, green, run against real ffmpeg output on every push:

- That the measurement decides, not a bitrate guess: a modest HEVC file and a fat MPEG-2 one
  both queue for measurement, the fattest is ordered first, a skip-listed codec is excluded only
  as a cost optimisation and included again when the list is emptied, and an HDR skip reads as a
  terminal `ok` rather than sitting in a backlog it will never leave.
- The whole check engine against files ffmpeg actually renders: good, garbage, truncated,
  MPEG-2/AVI, HEVC, dual-audio, faststart vs not.
- The transcode planner's copy-vs-encode decisions, and one full end-to-end run
  (MPEG-2 AVI → detected → H.264/MKV → output verified → original recycled → re-check passes).
- Both safety brakes — including that the abort ratio is measured against the library rather
  than the worklist, and that it still trips when the whole library fails — the action cap,
  that flag-only findings do not consume it, and that a
  transcode which fails to fix the file is not repeated for ever — including a run whose output
  is missing at verify, and a hygiene-triggered remux that leaves its warnings in place.
- That `*.unfuckarr.*` temp outputs are invisible to the walker and the watcher, and that the
  remediator refuses to act on one.
- The `fix_attempts` schema migration against a database created in the 1.0.0 shape.
- Recycle store/restore/sweep, including two files with the same basename.
- Path mapping, including that `/tv` does not rewrite `/tvshows`.
- The full API surface, settings round-trip, API-key gating, and the settle timer.
- **The quality search runs in CI**, not just locally: the runner installs the same static
  `ffmpeg-vmaf` the image ships, so the real-media shrink and the VMAF discrimination tests
  execute there rather than skipping. Before that, the part of the application that decides
  whether a re-encode may replace someone's file was the one thing CI could not exercise.
- The web UI: dashboard, files list, file drawer, and a settings round-trip driven through the
  real page in a browser, persisted to disk.
- **The Docker image builds and runs, with both ffmpegs.** CI boots it and asserts `/health`,
  `/api/status`, the served UI, and — since 2026-08-19 — that the image really carries both
  halves of the transcoding stack: Debian's ffmpeg 5.1.9 for encoding, the static `ffmpeg-vmaf`
  with a working `libvmaf` filter for scoring, and the `bluray` and `subfile` protocols the disc
  reader needs. Build-time greps prove the layer; the smoke test proves the image. Also
  `ffmpeg -version`; the log shows the PUID/PGID drop working
  (`unfuckarr starting as unfuckarr:unfuckarr (1000:1000)`). Multi-arch amd64 + arm64 on tags.
  This is the only container proof available — there is no runtime on the dev machine.
- That the scheduled-scan time survives a restart.
- **The shrink path, including a full pass against real media** — search, encode, structural
  verification, realised-saving check, VMAF verification of the finished file, recycle, replace,
  and the permanent marker. Plus every brake individually: never twice, never reassessed, a
  forced request reopening a skipped file but not a shrunk one, a projection under the floor
  never starting an encode, an output that is not smaller being discarded, an output that
  measures below target being discarded, one bad sample failing a passing mean, an unverifiable
  output being discarded, HDR left alone, a missing metric not writing the file off, the shrink
  window wrapping past midnight, shrinks staying out of the abort ratio, their own cap, repairs
  ordered ahead of them, and the six new columns migrating onto a 1.0.0-shaped database.
- That VMAF actually discriminates: a near-lossless re-encode scores above 90 and CRF 51 below
  80, measured against media ffmpeg rendered.
- **Disc images**, against ISO9660 volumes the tests assemble byte by byte (so no fixture and no
  `xorriso` on the runner): pure-UDF recognised as Blu-ray, a DVD recognised from its VIDEO_TS
  with the menu VOB correctly excluded, an ISO9660 bridge still read as Blu-ray, a truncated or
  non-disc image refused, libbluray chatter not counted as damage, an unopenable image producing
  an info finding and a `none` decision, and the DVD `subfile` route reading a real MPEG-PS
  stream back out of a real image through the byte range the parser computed.

**Verified against the live library on 2026-08-19** — 104 real disc images, read with the
container's own ffmpeg:

- `disc.identify` classified all 104 correctly (98 pure UDF, 5 UDF+ISO9660, 1 ISO9660/BDMV).
- 99 of 104 opened through `bluray:` and gave full stream information — an 88 GiB image reporting
  2h17m of HEVC 4K HDR with six audio tracks, at 89 Mbps, which is the best shrink candidate in
  the library.
- 5 could not be opened by libudfread (UDF 2.50 metadata partitions: *"read metadata file 0:
  unexpected tag 261"*, *"ECMA 167 Volume Recognition failed"*). Those now produce
  `disc_not_inspectable` and no action, where previously they produced `probe_failed` and a
  redownload.
- Seeking an hour into a 4K disc and encoding two seconds took 5 seconds, so the quality search
  is usable on a disc rather than merely possible.

**Still assumed:**

- **`allow_hdr` is better than it was and still not proven.** `transcode.colour_args` now carries
  the transfer function, primaries, matrix and range onto the encode — ffmpeg does not do that on
  its own, and without them an HDR re-encode plays grey and washed out with nothing reporting it.
  What is *not* carried is mastering-display and MaxCLL side data, which needs per-encoder
  plumbing (`-x265-params master-display=...`) and there is no HDR source on the dev machine to
  verify against. That is why the default stays off, and why most 4K discs report
  `hdr_not_shrunk` rather than being touched. Do not flip the default without an HDR file to
  check the output against.
- **No disc has actually been shrunk.** The read path is verified against the real library; the
  write path — a multi-hour 4K HEVC encode out of a disc image, replacing a 90 GB `.iso` with an
  `.mkv` — has not been run. Note also that most 4K discs are HDR, and `allow_hdr` is off by
  default, so out of the box the majority of them will report `hdr_not_shrunk` and be left alone.
- **Nothing has been shrunk on the live library.** The whole path is exercised end to end in CI
  against real media, and the search has been run by hand against a 400 MB 720p clip (83 Mbps →
  CRF 29 at VMAF 92.7). What has *not* happened is a shrink of a real 40 GB remux, on the array,
  with the hardware encoder — so the timings are arithmetic, and `hevc_vaapi`'s `-qp` scale has
  never been through the search at all. Expect the first live run to want `crf_min`/`crf_max`
  adjusting for VAAPI, whose QP numbers do not mean what libx265's CRF numbers mean.
- **No Unraid server has installed the CA template.** It is written against the CA conventions
  used elsewhere in this fleet (root `<Container version="2">`, no trailing colon on subcategory
  tokens, per-image `<Registry>` URL) and `scripts/validate_template.py` checks those in CI, but
  "valid XML" and "installs cleanly" are different claims.
- **`qsv`, `nvenc` and `videotoolbox` are untested** — command-line construction only, no Intel,
  NVIDIA or macOS hardware has ever run them. (`vaapi` is now verified; see below.)
- **Performance on a very large library is unmeasured.** `sample` depth should be a few seconds
  per file, but 10,000-file behaviour is still arithmetic, not measurement.
- ~~Interface binding not yet observed against a real `tailscale0`~~ — it now is: the live
  container runs `UNFUCKARR_BIND_INTERFACE=tailscale0` under Unraid's per-container Tailscale,
  bound to the tailnet address with the LAN refusing connections. That first live run also found
  the healthcheck bug (a hard-coded 127.0.0.1 probe reports a healthy tailnet-bound server as
  dead) — hence `unfuckarr/healthcheck.py` probes the address `resolve_host` returns.

## Layout

```
unfuckarr/
  config.py          Settings model, JSON persistence, env overrides, path mapping
  db.py              SQLite schema + per-thread connections
  probe.py           ffprobe/ffmpeg wrappers, MediaInfo, decode passes, faststart
  checks/
    __init__.py      Finding, CheckResult, status precedence
    integrity.py     Is it intact? + looks_repairable()
    compat.py        Local Emby direct-play model + the three presets
    hygiene.py       Stream metadata
    efficiency.py    Is it far bigger than it needs to be? + the bitrate arithmetic
  clients/
    arr.py           Sonarr + Radarr v3
    emby.py          PlaybackInfo, item index, activity log
  transcode.py       Plan builder, ffmpeg command, progress runner
  quality.py         Metric discovery, sampling, VMAF/SSIM scoring, the CRF search
  disc.py            Reading .iso/.img without mounting: UDF/ISO9660 identification,
                     bluray: and subfile URLs, libbluray noise filtering
  recycle.py         Recycle bin + retention
  remediation.py     decide() and Remediator
  scanner.py         Enumeration, check_file, the scan loop, the brakes
  watcher.py         Watch folders + settle timer
  service.py         Scheduler, single-flight scan, watch wiring
  state.py           Live state + SSE event bus
  api.py             FastAPI app
web/                 index.html + app.js + style.css, no build step
unraid/              CA template (must be copied into the templates repo to list)
```

## Sibling projects

Packaging follows the conventions in `stoatworks-unraid` — but note that repo's generators
**must not** be pointed at this one: unfuckarr has a hand-written Dockerfile (`hasOwnDocker`),
because a generated static-site image would serve nothing.
