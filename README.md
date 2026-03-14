# Vinyl Scrobbler

Identifies vinyl records playing on a turntable using Shazam audio fingerprinting (via SongRec) and scrobbles the tracks to ListenBrainz.

Designed to run continuously on a Raspberry Pi with a USB microphone positioned near the speakers.

## How it works

1. Listens for sustained audio above a silence threshold
2. Records a sample and fingerprints it against Shazam's database
3. Checks the match against a blocklist of known false positives
4. Submits the identified track to ListenBrainz as a listen
5. Deduplicates consecutive plays of the same track

## Setup

```bash
# Clone the repo
git clone git@github.com:clodpated/vinyl-scrobbler.git
cd vinyl-scrobbler

# Create your environment file
cp .env.example .env

# Add your ListenBrainz API token (https://listenbrainz.org/settings/)
nano .env

# Install dependencies
pip install requests pydub

# Run
python scrobbler.py
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `LISTENBRAINZ_TOKEN` | Yes | — | Your ListenBrainz API token |
| `ALSA_DEVICE` | No | `hw:0,0` | ALSA capture device (`arecord -l` to list) |
| `SAMPLE_RATE` | No | `48000` | Mic sample rate in Hz (`arecord --dump-hw-params` to check) |
| `CHANNELS` | No | `2` | Number of audio channels (1 = mono, 2 = stereo) |
| `SAMPLE_FORMAT` | No | `S24_3LE` | ALSA sample format (`arecord --dump-hw-params` to check) |
| `SILENCE_THRESHOLD` | No | `500` | RMS amplitude threshold for silence detection |
| `SUSTAINED_AUDIO_CHECKS` | No | `3` | Consecutive above-threshold checks before recognition triggers |
| `RMS_STRIDE` | No | `16` | Sample stride for RMS calculation (higher = faster, less precise) |
| `RECOGNITION_COOLDOWN` | No | `10` | Seconds between recognition cycles |
| `BLOCKLIST_FILE` | No | `blocklist.txt` | Path to phantom track blocklist |
| `LOG_LEVEL` | No | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

## Blocklist

Shazam occasionally matches ambient noise or vinyl artifacts to unrelated tracks ("phantoms"). The blocklist prevents these from being scrobbled.

`blocklist.txt` is a tab-separated file with one entry per line:

```
artist<TAB>track
```

Matching is case-insensitive. Lines starting with `#` are comments.

### Managing phantoms manually

1. **Identify** — check your [ListenBrainz profile](https://listenbrainz.org/user/clodpated/) for tracks you didn't play
2. **Add to blocklist** — append a new line to `blocklist.txt` with the artist and track separated by a tab
3. **Delete from ListenBrainz** — use the API to remove the scrobble:
   ```bash
   curl -X POST "https://api.listenbrainz.org/1/delete-listen" \
     -H "Authorization: Token $LISTENBRAINZ_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"listened_at": <unix_timestamp>, "recording_msid": "<msid>"}'
   ```
   You can find `listened_at` and `recording_msid` via the listens API:
   ```bash
   curl -s "https://api.listenbrainz.org/1/user/<username>/listens?count=100" \
     -H "Authorization: Token $LISTENBRAINZ_TOKEN"
   ```
4. **Deploy** — copy the updated blocklist to the Pi and restart the service:
   ```bash
   scp blocklist.txt scrobblepi@scrobblepi.local:~/vinyl-scrobbler/blocklist.txt
   ssh scrobblepi@scrobblepi.local "sudo systemctl restart vinyl-scrobbler"
   ```

### Automated phantom sweep (Claude Code)

A scheduled task (`weekly-phantom-sweep`) runs every Saturday at 10am to detect phantoms automatically. It:

1. Fetches the past week of scrobbles from ListenBrainz
2. Groups them into listening sessions (20-minute gap threshold)
3. Flags isolated one-off tracks surrounded by a different dominant artist — these are likely Shazam false positives
4. Presents suspects for review, categorized as:
   - **Shazam noise** — completely unrelated artists (e.g., spa music in a rock session)
   - **Sample matches** — Shazam matching a sampled song (e.g., a funk track in a hip-hop session)
   - **Credit variants** — same artist with different featuring credits (usually false positives)
5. After confirmation, deletes from ListenBrainz, adds to blocklist, syncs to Pi, restarts the service, and pushes to GitHub

The task is managed in Claude Code under Scheduled > `weekly-phantom-sweep`.

## Requirements

- Python 3.10+
- [SongRec](https://github.com/marin-m/SongRec) Python fingerprinting module (`~/songrec-python/fingerprinting`)
- `arecord` (ALSA utils)
- USB microphone
