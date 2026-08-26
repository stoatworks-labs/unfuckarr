
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
  short of a redownload. The 26 `multiple_default_audio` ones are now genuinely fixable and want
  their counter cleared by hand before they will be retried.
