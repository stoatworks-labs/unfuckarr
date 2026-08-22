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
remediation.decide()           → CheckResult + Policy → Decision (none|flag|transcode|repair|shrink|convert|redownload)
  ↓
Remediator.apply()             → transcode.plan() → build_command() → run() → verify → replace
                                 or: quality.search() → encode → quality.verify() → replace
                                 or: makemkv.titles() → select() → rip() → verify → replace + extras/
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

13. **The shrink worker selects on the *finding*, not the status.** `CheckResult.status` is a
    single word and its precedence puts anything with untidy metadata under `hygiene` — but
    `decide` deliberately prefers a shrink over a hygiene flag, because the re-encode rewrites
    every byte and carries the tag fixes with it. A worker querying `status='unmeasured'`
    therefore disagrees with `decide` about the same file. Found on the live library the hour it
    was deployed: 1,489 candidates were invisible to the worker while `decide` would have shrunk
    every one of them, leaving a backlog of 28 where there were 1,517. `corrupt` and
    `incompatible` stay excluded, which also matches `decide`: something is wrong with those
    files and repairing them comes first.

14. **`unmeasured` is a state, not a claim, and it has to drain.** The finding is `not_measured`
    at info severity: nothing is wrong with the file, it simply has not been priced. Every file
    ends in one of two terminal states on the row — `shrunk` or `shrink_skipped`, both permanent
    — and `checks/efficiency.check` then returns nothing for it. That is what makes the count a
    progress figure rather than a permanent pill on the whole library. `CheckResult.unmeasured`
    is deliberately narrower than `.efficiency` for the same reason: an HDR file being skipped is
    an efficiency finding but a *terminal* one, so it reads as `ok` with an explanation rather
    than sitting in a backlog it will never leave. `needs_check` also treats `unmeasured` like
    `ok` — the backlog is the whole library at the start, and re-probing all of it nightly buys
    nothing.

15. **Efficiency findings are carried separately from everything else, and each separation is
    load-bearing.** They are excluded from `CheckResult`'s hygiene warning set (so
    `hygiene_action` cannot be what decides to re-encode a 40 GB file), they are last in the
    status precedence (a corrupt file that is also large is corrupt), `oversize_action` is typed
    `none|flag|shrink` so it can never delete, and — most importantly — **shrinks are not in
    `_remediate`'s destructive list**. The abort ratio exists to notice a library that has just
    broken. "Most of this library is H.264" is not that; an unmounted array produces integrity
    failures, and a file ffprobe cannot read never reaches the efficiency check. Counting shrinks
    there would disable the scanner permanently on almost every real library. They get
    `max_shrinks_per_scan` instead, and are applied *after* every repair.

16. **Shrinking is paced, not rationed, and the pacing is measured.** It runs on its own
    worker for as long as the service is up, because a library of thousands of candidates is not
    a per-scan job — a nightly batch of five takes years, and a batch big enough to matter is a
    scan that runs all day and blocks everything behind it. What makes that safe to leave on is
    `governor.py`: `drm-engine-enc` in `/proc/<pid>/fdinfo`, sampled over wall clock, is a direct
    percentage of the GPU's *encode* engine for that process, and SIGSTOP/SIGCONT holds it at
    `gpu_encode_percent`. Measured on the target hardware — flat out 958 ms/s, stopped a true 0,
    resumed 966 ms/s, output valid. **Do not swap this for `gpu_busy_percent` or debugfs
    `VCN Load`**: both read 0 through a saturating 4K encode on this GPU, because the first
    tracks the graphics engine and the second is not mounted in a container. The governor must
    also always leave the process running on exit, or a cancelled job sits in state T for ever
    holding a semaphore and looking exactly like a hang; and it must stand down entirely for
    software encodes, which have no engine to share.

17. **"I cannot read this" and "this is broken" are different sentences.** `probe` raises
    `DiscUnreadable` (a `ProbeError` subclass) for a disc image no available route can open, and
    the integrity check turns that into an *info* `disc_not_inspectable` — never `probe_failed`,
    which is in `REPAIRABLE_CODES` and leads to a remux and then a redownload. This is not a
    hypothetical: before disc support existed, every BR-DISK in the live library was queued for
    delete-and-re-search, and three 42 GB images are recoverable only because the recycle bin
    caught them. Any new "cannot open" path must land on the info finding, not the error one.

18. **The tailnet-only stance is policy, not decoration.** The README warning (private
    network/tailnet only, never the public internet, no independent human security review) stays,
    because the service deletes media unattended and has no auth until a key is set. In the same
    spirit, `__main__.resolve_host` **fails the start** when `UNFUCKARR_BIND_INTERFACE` names an
    interface that never gets an address — falling back to `0.0.0.0` would silently expose the
    service exactly when the VPN is down. Do not "fix" that into a fallback.

19. **A conversion is the only action that produces more than one file, and only
    one of them is a library item.** The feature replaces the image; the bonus
    features go to `extras/`, and `walk_video_files`, the watcher and
    `transcode.is_extras_path` all refuse to follow them. That is not tidiness.
    A deleted scene checked as if it were a film is short, often has no usable
    audio track, and reads as corrupt or incompatible — so it gets *repaired*,
    or redownloaded, and a redownload on a file carrying its parent's *arr
    identity deletes the film. `EXTRAS_DIRS` deliberately does not contain
    `specials`: that is Sonarr's season-zero folder and a real part of the
    library.

20. **A failure of the MakeMKV installation is never recorded against a disc.**
    `files.convert_skipped` and `convert_attempts` are permanent, and the
    failures that earn them are properties of the image — no title matching the
    runtime, an image MakeMKV cannot open. An expired beta key is not one of
    those: it arrives one morning with nothing else changed, a new key fixes
    it, and writing it to every disc it touched would silently retire the whole
    feature. Hence `makemkv.KeyExpired`, which is an `Unavailable`, and the
    availability check that runs before every conversion.

21. **The tool is never bundled, and that is a licence fact, not a preference.**
    MakeMKV's binary half is not redistributable, so a public image cannot ship
    it. `MakeMKVConfig.command` is a whole command line rather than a path so
    that a container shim is expressible without this codebase knowing anything
    about containers — with the trap that the paths must mean the same thing on
    both sides of that mount, and MakeMKV's error when they do not says only
    that it cannot open the disc.

## Traps found the hard way

- **A Blu-ray image is pure UDF, not ISO9660.** Measured across the live library: of 104 disc
  images, 98 were pure UDF (`BEA01`/`NSR03`/`TEA01` at sector 16), 5 carried an ISO9660 bridge as
  well, and exactly one was ISO9660 with a BDMV directory. An identifier that looks for `CD001`
  recognises none of the 98. `disc.identify` reads the Volume Recognition Sequence and only walks
  an ISO9660 directory when one is actually there — which is the DVD case, and the case where
  telling a DVD from a Blu-ray needs the directory.
- **A Blu-ray's file metadata is usually not where its blocks appear to say.** UDF 2.50 puts file
  entries and directory contents inside a *metadata file*, addressed through a second, virtual
  partition; every disc measured here does that, so a reader resolving logical blocks directly
  finds the file set descriptor missing. Behind that sits the opposite trap: the metadata
  partition holds the metadata, but **file data stays in the physical partition underneath**.
  Resolving data extents through the metadata file lands past its end, and the symptom is
  exquisitely misleading — every directory reads perfectly and every single `.m2ts` fails.
- **A raw MPEG-TS read through `subfile` reports a duration that is simply wrong** — 117 seconds
  for a 137-minute film. That number decides whether the integrity check calls a file truncated,
  so `disc.stream_duration` measures it from the first and last video presentation timestamps
  instead, each found by probing a small window rather than reading 77 GiB. Cross-checked against
  libbluray on a disc both can read: 8201.7 seconds against libbluray's 8201.4.
- **The main title is the largest `.m2ts`, and that is a heuristic, not a fact.** On a disc using
  seamless branching the feature is split, and the largest stream is one segment: measured across
  ten real discs, two read short for that reason, one by 85%. So a *disc* that reads short of its
  expected runtime raises `duration_below_expected` (info) and never `duration_mismatch` — on an
  ordinary file that shortfall means truncation, on a disc it almost always means the heuristic
  picked a segment. Reading `BDMV/PLAYLIST/*.mpls` for the longest playlist is the real fix.
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
- **`verify` has to cut both sides out too, not just the search.** Seeking two different files to
  the same timestamp does not reliably reach the same frame, and while that was survivable when
  both were ordinary Matroska with the same timebase, a disc image is read as a raw MPEG-TS byte
  range whose timestamps start wherever the stream does. Live, every disc encode scored between
  0.0 and 32 against a target of 92 — not quality loss, a comparison of two different films. The
  guards held and nothing was damaged, but each verdict cost a full encode.
- **Never compare a sample against a re-seeked source.** Two seeks to the same timestamp in the
  same file do not reliably land on the same frame, and when they do not the metric reports
  quality loss that is not there. It fails *plausibly*, which is why it survived a whole
  calibration run: lossy encodes still produced believable numbers, and only the one case with a
  known answer gave it away — a **lossless** encode scored **VMAF 62**, where it must score ~100.
  `quality.extract_window` cuts each window out once, losslessly, and every candidate encode and
  every comparison then works from exactly those frames. Same media, after the fix: 99.93.
  `test_a_lossless_encode_scores_as_lossless` is the guard; if it ever fails again, every number
  the search produces is wrong by an unknown amount, in the direction of encoding harder than
  necessary.
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
- **`hevc_vaapi` on Mesa/AMD emits 1088 rows for a 1080p source, and hides it.** It pads to the
  CTB boundary and does not signal a conformance window, so the output *decodes* eight rows
  taller than the source with padding at the bottom — while the container metadata still says
  1080, so `ffprobe` reports the right number and everything looks fine. The only way to see it
  is to decode a frame and count bytes: 3,133,440 against the source's 3,110,400 for yuv420p
  1920x1080. Consequences, both found on 2026-08-19:
  - **The quality search cannot run at all on VAAPI output.** Every comparison dies with
    "Width and height of input videos must be same" — libvmaf *and* ssim, so it is not a metric
    problem.
  - **A VAAPI re-encode silently changes the picture.** `_verify_output` did not look at
    dimensions, so a shrink or a compat transcode would have replaced a 1080p file with a padded
    1088p one. It checks now, and that check must stay: nothing in this application ever asks for
    a resolution change, so one is always a defect.

  `scale_vaapi` does not fix it — it breaks the pipeline outright, consistent with the trap
  below. **The fix is a newer ffmpeg**, established by testing the same command and source
  against four builds on the same hardware:

  | ffmpeg | VAAPI geometry | libvmaf |
  |---|---|---|
  | Debian bookworm 5.1.9 (what the image ships) | **1088, padded** | no |
  | Debian trixie 7.1.5 | **1088, padded** | no |
  | BtbN static (master) | aborts — bundled libva cannot reach the host driver | yes |
  | **linuxserver.io 8.0.1** | **1080, correct** | **yes** |

  So ffmpeg 8.x fixes it, and the linuxserver.io build has libvmaf as well — one binary instead
  of Debian's plus a separate scorer. That is exactly what Shrinkray does (it is built *from*
  `lscr.io/linuxserver/ffmpeg`), which is why it can do VAAPI encodes when this cannot.

  **The image is now built the same way** — Ubuntu 24.04 plus a multi-stage copy of that
  ffmpeg's `/usr/local`, verified by building it and running it on the target hardware: correct
  1080p geometry, VMAF 91.65 on a QP 26 encode against a calibration that predicted 91.96, and
  the PUID/PGID drop still working.

  **What that build does not have is libbluray**, so Blu-ray images cannot be opened and are
  reported as `disc_not_inspectable`. DVD images are unaffected, because that path parses ISO9660
  here and reads the VOB through `subfile`, which is a builtin. BtbN's static build *does* carry
  all three flags, but it needs a libva newer than Ubuntu ships (it aborts on `vaMapBuffer2`
  against 2.20), and once given linuxserver's libva 2.23 its VAAPI and libvmaf worked while
  reading a disc still did not. The durable fix is to stop depending on the ffmpeg build for this
  at all: parse UDF here, the way the DVD path already parses ISO9660, and hand ffmpeg a
  `subfile` range.

  Grafting that binary onto a Debian Python base does **not** work as-is: its `/usr/local/lib`
  carries its own libva (2.23), which shadows the distro's and then cannot load Mesa's
  `radeonsi_drv_video.so`, so device creation fails with "unknown libva error". Either leave that
  libva behind and use the distro's, or take Mesa from the same image too. Seventeen ordinary
  system libraries also have to be installed alongside (libxcb*, libX11, libglib, libgomp,
  libv4l2, libxml2, libasound, libbrotli, libexpat, libOpenCL).

  Until the image ships an ffmpeg 8.x, **hardware encoding is unusable for anything that has to
  be compared with its source**, and `_verify_output`'s dimension check is what stops it doing
  damage. On ffmpeg 8 it works and measures well — see the calibration table below, where VAAPI
  reached the quality target at a slightly *lower* bitrate than x265 `medium`.
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
- **ffmpeg exits 0 having written a header and no frames** when the region it was asked for is
  damaged: it complains on stderr and returns success. Checking the exit code and that the output
  exists — the obvious pair — catches neither, so `extract_window` checks the size as well. Live,
  a Matroska with EBML damage produced a 576-byte "window", and every failure downstream then
  pointed at the sample encode instead of at the source.
- **Report the *tail* of ffmpeg's stderr, never the head.** It narrates as it goes, so the cause
  is the last thing it says; `[:300]` from the front reliably returned the warnings and discarded
  the error. Worse, with no writable home Mesa prints a shader-cache complaint on *every*
  invocation, which sat at the front of every captured message. The entrypoint now gives the app
  user a real cache directory, and `quality.ffmpeg_error` filters what is left.
- **Anything the continuous worker can fail at, it will fail at in a tight loop.** It picks the
  fattest candidate every time it looks, so a deterministic failure is an infinite loop on one
  file — 330 identical failures on one damaged Matroska, one every 55 seconds, while the rest of
  the backlog waited. Every path out of `_shrink` that is not a success must therefore either
  record a verdict (`shrink_skipped`) or count an attempt. A search that *could not run* counts
  the attempt without recording a verdict: fixing the cause and forcing from the UI clears both.
- **A watch folder covering the library sees a shrink's own output as an arrival.** The new file
  lands, the settle timer fires about a minute later, and `_check_arrival` re-checks it — so
  every path that calls `check_file` has to pass `already_shrunk`, not just the scanner. Missing
  it there does not re-shrink anything (the `shrunk` marker still refuses), but it re-raises
  `not_measured` and overwrites the post-shrink state, and the file then sits in the backlog for
  ever. Observed on the live library within two minutes of the very first real shrink.
- **`persist_result` has to write `size` and `mtime`, not just the signature.** Nothing else
  updates that column until the next full enumeration, so a file rewritten in place keeps its old
  size on record — and the reclaimed total is `shrunk_from - size`, so the first live shrink
  reported a saving of zero while the file on disk had gone from 10.71 GiB to 3.45.
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

### Disc conversion

- **Menus cannot come across, and nothing in this stack ever played them.** No
  Matroska file has ever held a DVD VM program or a BD-J title; MakeMKV does not
  preserve them and never has. Emby picks a title and plays it, from an image
  as much as from a file. So what a conversion actually costs is the
  navigation, and what it keeps — every audio track including the lossless
  ones, every subtitle track, the chapters, and the bonus features as files in
  `extras/` — is what anyone was opening the menu to reach. Do not accept a
  feature request to "keep the menus": the honest answer is that the format
  cannot and the player would not.
- **MakeMKV's own reported duration is the free verification, and it is the
  better one.** `_verify_conversion` compares the finished file against the
  length MakeMKV said the title was moments before writing it. That reference
  needs nothing to have been readable, which matters: the five images
  libudfread refuses have no `MediaInfo` at all, and they are exactly the ones
  worth handing to MakeMKV. The *arr's nominal runtime is the second reference
  and gets a much wider tolerance, for the same reason
  `duration_below_expected` exists.
- **The work directory is the leftover, not a file.** A rip goes into
  `<name>.unfuckarr.convert/`, because MakeMKV names the files it writes and
  the only thing marking them as ours is the folder. `is_temp_output` therefore
  checks every path component rather than the filename — and the startup sweep
  removes directories as well as files, because a conversion killed by a
  restart is the 199 GB trap again with a partial Blu-ray inside it.
- **Nothing is re-encoded by a conversion.** MakeMKV stream-copies, so a UHD
  disc arrives already in HEVC and a conversion is minutes of I/O rather than
  hours of encode. Making it smaller stays the shrink path's job, on the
  ordinary Matroska file the conversion leaves behind, where it is already
  measured at both ends. Two proven steps beat one step doing two unproven
  things.
- **A conversion supersedes compat, hygiene and efficiency, so it decides
  before all three.** An image is not direct-playable, its tracks carry no
  tags, and it cannot be shrunk from where it sits; one conversion clears all
  of it. It is also strictly better than what the compat path would otherwise
  do to a disc, which is the byte-range encode that came out short live.

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

**Calibrated on real media, 2026-08-20**, with the fixed comparison — one 6-second 1080p window
cut losslessly out of a 28.9 Mbps H.264 Blu-ray remux, every candidate encoded from that window
and scored against it:

| target | hevc_vaapi | libx265 |
|---|---|---|
| control (near-lossless) | QP 1 → **99.87** | CRF 0 → **99.73** |
| excellent (95) | QP 22 → 95.55, 9.3 Mbps | CRF 18 → 95.34, 8.1 Mbps |
| **good (92)** | **QP 26 → 91.96, 2.2 Mbps (92% smaller)** | **CRF 22 → 92.46, 2.7 Mbps (91% smaller)** |
| acceptable (85) | QP 31 ≈ 85, ~1 Mbps | CRF 29 ≈ 85, ~0.7 Mbps |

Two things follow. **The default range 18–34 brackets all three tiers for both encoders**, with
the "good" target landing mid-range where a bisection can actually find it — so it needs no
change. And **VAAPI is not the poor relation here**: at the 92 target it reached a slightly lower
bitrate than x265 `medium` for the same score. That is one window of one file and should not be
read as a general claim about the encoders, but it does mean there is no quality argument for
preferring software once the geometry bug is fixed.

The control rows are the point of the table. Before `extract_window`, those same two encodes
scored 76.7 and 62.

**Verified on the GPU, 2026-08-19** — `governor.py` driving a real 4K HEVC VAAPI encode in the
shipped container, measured through `drm-engine-enc`:

| asked for | settled at | on-fraction |
|---|---|---|
| 50% | **50.5%** (range 49–56%) | 0.51 |
| 25% | **27.0%** (range 24–61%) | 0.26 |

The first two or three samples of any run read 96–97%: the controller deliberately runs the first
period flat out to find out whether the job touches the encode engine at all, and averaging those
in is what makes a correct governor look like it overshoots. Judge it on the settled figure.

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
- **The continuous worker has never run against the live library.** Its parts are covered — the
  candidate query, the idle conditions, the governor against a real GPU — but "a worker that runs
  for weeks" is a claim only time can support, and the failure mode to watch for is the backlog
  not draining because every candidate fails for the same reason.
- **Encoding *out of* a disc image does not work yet, and is off by default**
  (`shrink_disc_images`). Reading and measuring them is verified against the real library; the
  full encode is not — on the live run it came out short, 1620s of a 6604s film, on a stream read
  through `concat:`/`subfile`. Every one was caught by the duration check in `_verify_output` and
  discarded, so the failure mode is wasted CPU rather than damaged media, but it is wasted a
  whole encode at a time. The likely culprit is timestamp handling on a raw MPEG-TS byte range;
  `-fflags +genpts+igndts` is already set, so it needs a proper look rather than another guess.
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
- **No disc has ever been converted.** Every part of the path is exercised — the
  robot-output parsing, the title selection, the duration checks, the extras
  placement, the recycle and the swap — but against a stand-in that speaks
  MakeMKV's protocol and copies files ffmpeg rendered, because MakeMKV cannot
  be installed in CI. What that cannot prove is the only thing MakeMKV is here
  for: that it picks the right playlist off a real Blu-ray with seamless
  branching. Convert one disc by hand, with `keep_disc_image` on, and compare
  the result against the image before trusting it with a library.
- **The extras heuristic is a length heuristic and nothing more.** "The longest
  title is the feature" was right on every disc measured, and the discs where
  it will not be — an anthology, a TV season on one disc, a concert film with
  a longer bonus concert — are real and are not represented in any test here.
  `max_extras` and the extras floor limit the damage; nothing prevents it.
- **Nothing has been converted on the live library**, and the live instance's
  `disc_action` default (`flag`) means nothing will be until someone sets it.
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
  governor.py        Per-process GPU encode share from fdinfo, held by SIGSTOP/SIGCONT
  disc.py            Reading .iso/.img without mounting: a UDF reader (metadata
                     partitions included), ISO9660 for DVDs, subfile/concat URLs
  makemkv.py         Converting a disc image to Matroska through an external
                     MakeMKV: robot-output parsing, title selection, the rip
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
