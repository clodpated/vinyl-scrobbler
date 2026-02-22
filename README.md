# Vinyl Scrobbler

Identifies vinyl records playing on a turntable using Shazam audio fingerprinting (via SongRec) and scrobbles the tracks to ListenBrainz.

Designed to run continuously on a Raspberry Pi with a USB microphone positioned near the speakers.

## How it works

1. Listens for sustained audio above a silence threshold
2. Records a sample and fingerprints it against Shazam's database
3. Submits the identified track to ListenBrainz as a listen
4. Deduplicates consecutive plays and backs off after failed recognitions

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
python scrobbler_v5.py
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
| `FAILURE_BACKOFF_BASE` | No | `30` | Base backoff seconds after failed recognition |
| `FAILURE_BACKOFF_MAX` | No | `300` | Maximum backoff cap in seconds |
| `LOG_LEVEL` | No | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

## Requirements

- Python 3.10+
- [SongRec](https://github.com/marin-m/SongRec) Python fingerprinting module (`~/songrec-python/fingerprinting`)
- `arecord` (ALSA utils)
- USB microphone
