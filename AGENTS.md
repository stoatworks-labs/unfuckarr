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
scanner.check_file()           → integrity → (emby direct-play OR local compat) → hygiene
  ↓                              produces CheckResult: a list of Findings with categories
remediation.decide()           → CheckResult + Policy → Decision (none|flag|transcode|repair|redownload)
  ↓
Remediator.apply()             → transcode.plan() → build_command() → run() → verify → replace
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
   replacement, and increments `files.fix_attempts` when it still fails. At `MAX_FIX_ATTEMPTS`
   (2) `_transcode` refuses. Without this, a transcode that does not clear the finding is redone
   on *every* scan, for ever, with nothing in the log saying why — an unattended service burning
   a CPU indefinitely on one file.

## Traps found the hard way

- **`-nostdin` on every ffmpeg call.** Without it a stalled ffmpeg consumes the parent's stdin
  and hangs the worker. Same trap as the rest of this fleet's ffmpeg tooling.
- **A size floor cannot detect a "sample instead of the movie".** `TINY_FILE_BYTES` is 128 KB and
  only catches stubs; the real detection is `duration_mismatch` against the *arr's runtime, and
  `too_short`. A floor high enough to catch a 20 MB fake would condemn every legitimate short extra.
- **`ffprobe` reports format *families*.** `format_name` is `"matroska,webm"`, not `"mkv"`.
  `_container_from` reconciles that against the extension.
- **PGS subtitles cannot be copied into MP4** — it fails the whole job. `_subtitles_to_drop`
  handles it per container.
- **`-ss` before `-i`** so ffmpeg seeks rather than decoding up to the point. Sampling the middle
  of a 40 GB file takes seconds this way and minutes the other way.
- **Unraid user shares are SMB/NFS and produce no inotify events.** `WatchManager._needs_polling`
  reads `/proc/mounts` and falls back to `PollingObserver`. When it cannot tell, it polls, because
  a missed event is silent and polling only costs a stat.
- **SQLite objects are per-thread.** `db.connect()` uses `threading.local`. Scan workers, transcode
  workers and the async web layer are all different threads.
- **The event bus crosses the thread/async boundary** via `loop.call_soon_threadsafe`, with the
  loop captured in the FastAPI lifespan. A worker thread has no loop of its own.
- **The scan is single-flight** via `Service._scan_lock`, and `__main__.py` pins uvicorn to one
  worker. Two workers would mean two scanners fighting over the same library.

## Verified vs assumed

**Verified** — 76 tests, green, run against real ffmpeg output in CI:

- The whole check engine against files ffmpeg actually renders: good, garbage, truncated,
  MPEG-2/AVI, HEVC, dual-audio, faststart vs not.
- The transcode planner's copy-vs-encode decisions, and one full end-to-end run
  (MPEG-2 AVI → detected → H.264/MKV → output verified → original recycled → re-check passes).
- Both safety brakes, the action cap, that flag-only findings do not consume it, and that a
  transcode which fails to fix the file is not repeated for ever.
- The `fix_attempts` schema migration against a database created in the 1.0.0 shape.
- Recycle store/restore/sweep, including two files with the same basename.
- Path mapping, including that `/tv` does not rewrite `/tvshows`.
- The full API surface, settings round-trip, API-key gating, and the settle timer.
- The web UI: dashboard, files list, file drawer, and a settings round-trip driven through the
  real page in a browser, persisted to disk.

**Assumed — no real hardware or service has ever been connected:**

- **No Sonarr, Radarr or Emby server has been touched.** Every call is written to the documented
  API and tested against `httpx.MockTransport`. Response shapes, especially Sonarr's
  `history/failed` semantics and Emby's `PlaybackInfo` `TranscodeReasons`, are from documentation,
  not observation.
- **The Docker image has never been built.** There is no container runtime on the development
  machine; CI is the only proof. The `RUN test -f /app/web/index.html` line and the smoke-test job
  exist so a broken image fails there rather than on a server.
- **No Unraid server has installed the template.** It is written against the CA conventions used
  elsewhere in this fleet (root `<Container version="2">`, no trailing colon on subcategory tokens,
  per-image `<Registry>` URL).
- **Hardware transcoding is untested.** All four accelerator paths (qsv/nvenc/vaapi/videotoolbox)
  are command-line construction only.
- **No large library has been scanned.** Performance on 10,000 files is unknown; `sample` depth
  should be a few seconds per file, but that is arithmetic, not measurement.

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
  clients/
    arr.py           Sonarr + Radarr v3
    emby.py          PlaybackInfo, item index, activity log
  transcode.py       Plan builder, ffmpeg command, progress runner
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
