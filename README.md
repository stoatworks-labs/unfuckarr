# unfuckarr

> **AI-assisted project.** This codebase was created with [Claude Code](https://claude.com/claude-code).
> The check engine, the transcode planner, the policy brakes and the recycle bin are covered by a
> 76-test suite that runs against real files rendered by ffmpeg on every push. What has **not** been
> exercised is the other half: no Sonarr, Radarr or Emby server has ever been connected to this code.
> Every *arr and Emby call is written to the documented v3 / Emby API and tested against mocked HTTP,
> which is not the same thing as working. Treat the first run against a real library as a trial —
> start with the actions set to `flag`, look at what it found, and only then let it act.

Scans a Sonarr/Radarr library, works out which video files are broken or which Emby cannot
direct play, and fixes them — by transcoding, by remuxing, or by deleting the file and asking the
*arr for a better copy.

Runs as a Docker container with a web UI. Packaged for Unraid Community Applications.

---

## What it checks

Four independent classes of problem, each with its own remediation policy.

**Integrity — is the file actually intact?**
ffprobe refuses it, the container has no duration, there is no video or audio stream, the file is
a stub, or a decode pass over the start, middle and end produces errors. Also: the file is
*shorter than the runtime Sonarr or Radarr expects*, which catches a truncated download that
decodes perfectly for every byte it does have.

**Emby direct play — will it play without cooking the server?**
An intact MPEG-2 AVI is not broken; it is just transcoded on every single play, and a weak client
fails outright. When an Emby server is configured, unfuckarr does not guess: it calls
`POST /Items/{id}/PlaybackInfo` with a device profile describing your target client and reads
Emby's own `TranscodeReasons` back. That accounts for your server's version, its ffmpeg build and
its remux rules in a way a local codec table never can. Without Emby, a local profile model is
used instead — `modern`, `conservative` (Chromecast-class), `permissive`, or a custom list.

**Stream hygiene — will it behave?**
Missing audio language tags, no default track among several, image-only subtitles (which force a
video transcode whenever they are switched on), every subtitle flagged forced, odd frame rates.
None of this is breakage, so hygiene findings can never trigger a delete — the config type does
not permit it.

**Emby's activity log.** Real playback failures the server recorded, for files that otherwise
look fine.

## What it does about it

| Finding | Default action |
|---|---|
| Container damage only (index, atom order, bad timestamps) | Remux — seconds, lossless. Falls through to re-download if the remux fails or its output does not verify. |
| Genuinely corrupt | Delete, blocklist the release, trigger a new search |
| Intact but Emby would transcode | Transcode, copying every stream that already passes |
| Hygiene | Flag only |

Everything is configurable per class, including turning it off.

**Transcoding copies what it can.** A compatible H.264 stream in the wrong container is a remux,
not a re-encode — getting that wrong turns twenty seconds of work into six hours. Only the stream
that actually fails gets re-encoded. Output is verified (streams present, duration within 2% of
the source) before it is allowed to replace anything.

**Deleting goes through the *arr, not `os.unlink`.** Removing a file behind Sonarr's back leaves
the episode marked present until its next rescan. unfuckarr deletes via the API, then marks the
grab failed — which blocklists the release *and* queues a search, so the indexer does not hand
back the same broken file within the hour.

## The brakes

Unattended deletion needs to be wrong safely, so there are three layers:

- **Recycle bin.** Deleted files move to `/config/recycle/<date>/` and are recorded. Retention
  defaults to 14 days and any entry can be restored from the web UI. Set it to 0 to unlink
  immediately.
- **Action cap.** No single scan may act on more than `max_actions_per_scan` files (default 50).
- **Failure-ratio abort.** If more than half a library fails one pass, the scan stops and changes
  *nothing*. That is what an unmounted array looks like, and it is the failure mode that costs
  people their library.

## Watch folders — the live import gate

Point a watch folder at your completed-downloads directory and every new video file is checked
the moment it lands, before the *arr imports it. That is the cheapest possible time to reject
something: nothing has been imported, and Sonarr will simply grab another release.

The hard part is knowing when a file has *finished* landing. A copy still in progress and an
unpacking rar are indistinguishable from a truncated download if you probe them too early, so
each event starts a settle timer that resets on every further write; the probe only runs once the
size has held steady (60s by default). `.part`, `.!qb`, `.crdownload` and friends are ignored
outright.

Unraid user shares are SMB/NFS and deliver no inotify events, so unfuckarr detects a network
mount and polls instead. Force it either way with `UNFUCKARR_WATCH_POLL=1` / `=0`.

## Install

### Unraid

Community Applications → search **unfuckarr**. Set the media path to the *same path your *arrs
use*, or fix it afterwards with path mappings in Settings.

Or add the template by hand: `unraid/unfuckarr.xml` in this repo.

### Docker Compose

**This repository is private, so the GHCR package is private too** — an anonymous `docker pull`
gets a 403. Log in first with a token carrying `read:packages`:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u stoatworks-labs --password-stdin
```

On Unraid the same applies: add the registry credentials under Docker → Add Container, or make
the package public from its GHCR settings page. Community Applications cannot install a private
image.

```bash
docker compose up -d
```

See [docker-compose.yml](docker-compose.yml). Then open `http://<host>:6969`.

Image tags:

| Tag | Means | Architectures |
|---|---|---|
| `latest`, `1.0.0`, `1.0` | the newest release | amd64 + arm64 |
| `edge` | current `main` | amd64 only |
| `sha-abc1234` | one specific commit | amd64 (arm64 too, on releases) |

arm64 is emulated at build time and ships no VAAPI drivers, so there is no hardware transcoding
there. `edge` is amd64-only because building arm64 under QEMU on every push is not worth the
minutes — which is also why `latest` never points at a `main` build.

### From source

```bash
pip install -r requirements-dev.txt
UNFUCKARR_CONFIG_DIR=./config python -m unfuckarr
```

Needs `ffmpeg` and `ffprobe` on `PATH`.

## Configuration

Everything is on the settings page and written to `/config/config.json`. Environment variables
(`UNFUCKARR_SONARR_URL`, `UNFUCKARR_SONARR_API_KEY`, `UNFUCKARR_RADARR_*`, `UNFUCKARR_EMBY_*`,
`UNFUCKARR_WATCH_FOLDERS`, `UNFUCKARR_API_KEY`, `UNFUCKARR_PORT`) seed it and **override the saved
file on every start** — set them once to bootstrap, then remove them.

**Path mappings are the thing that goes wrong.** Sonarr reports `/tv/Show/S01E01.mkv`; if this
container mounts the same file at `/media/tv/...`, every lookup misses and the whole library reads
as missing. Either mount media at the same path the *arrs use, or add `/tv = /media/tv` under the
relevant service in Settings.

### Hardware transcoding

Pass `/dev/dri` into the container and set the accelerator in Settings → Transcoding:

| Setting | Needs |
|---|---|
| `qsv` | Intel iGPU, `/dev/dri` passed through |
| `vaapi` | `/dev/dri`, correct render node (`vaapi_device`) |
| `nvenc` | NVIDIA container runtime |
| `videotoolbox` | macOS host, source install only |

The amd64 image ships the Intel and Mesa VAAPI drivers. **The arm64 image ships neither** — there
is no hardware transcoding on ARM.

None of the four accelerator paths has been run on real hardware; they are command-line
construction only. `none` (libx264) is the tested default.

## Development

```bash
python -m pytest tests -q
```

The suite renders real clips with ffmpeg rather than mocking the probe, because the interesting
bugs are in what ffmpeg actually says about a file. Tests that need ffmpeg skip cleanly without it.
The *arr and Emby clients are tested against `httpx.MockTransport`.

## API

The whole UI runs on the JSON API at `/api`, documented at `/api/docs`. Set an API key in Settings
to require `X-API-Key` (or `?apikey=`) on every call; unset means no auth, which is the sensible
default on a LAN.

## Licence

MIT — see [LICENSE](LICENSE).

*Not affiliated with Sonarr, Radarr, or Emby.*
