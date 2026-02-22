#!/usr/bin/env python3
"""
Vinyl Scrobbler — Silence-aware audio recognition and ListenBrainz scrobbling.

Listens via USB microphone, identifies tracks using SongRec's fingerprinting
(which talks to Shazam's servers), and submits listens to ListenBrainz.

v5.1 — Performance, privacy, and storage hardening for always-on Pi use.

Changes from v5.0:
- Removed hardcoded ListenBrainz token; requires env var
- Optimized RMS silence detection with stride sampling (~16x faster)
- Added sustained audio check to reduce false triggers
- Added cooldown + exponential backoff after recognition failures
- Added temp file cleanup on exit and between cycles
- Added rate limiting between Shazam recognition attempts
- Removed unused struct import
- Replaced mutable dict state with ScrobbleState dataclass
"""

import subprocess
import time
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Optional

import requests
from pydub import AudioSegment

# ---------------------------------------------------------------------------
# SongRec fingerprinting imports
# ---------------------------------------------------------------------------

SONGREC_DIR = os.path.expanduser("~/songrec-python/fingerprinting")
sys.path.insert(0, SONGREC_DIR)

from algorithm import SignatureGenerator
from communication import recognize_song_from_signature

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LISTENBRAINZ_TOKEN = os.environ.get("LISTENBRAINZ_TOKEN")
if not LISTENBRAINZ_TOKEN:
    sys.exit(
        "Error: LISTENBRAINZ_TOKEN environment variable is required.\n"
        "Export it before running: export LISTENBRAINZ_TOKEN='your-token-here'"
    )

# ALSA device — EPOS B20 mic
ALSA_DEVICE = os.environ.get("ALSA_DEVICE", "hw:0,0")

# Audio recording settings — must match mic hardware
SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_FORMAT = "S24_3LE"

# Silence detection — uses short raw recordings
SILENCE_THRESHOLD = int(os.environ.get("SILENCE_THRESHOLD", "500"))
SILENCE_CHECK_SECONDS = 1

# Sustained audio — require audio above threshold for this many consecutive
# checks before triggering recognition. Reduces false triggers from transient
# sounds (conversations, bumps, doorbell, etc.)
SUSTAINED_AUDIO_CHECKS = int(os.environ.get("SUSTAINED_AUDIO_CHECKS", "3"))

# RMS stride — check every Nth sample during silence detection.
# At 48kHz stereo, stride=16 means ~6000 checks instead of ~96000.
RMS_STRIDE = int(os.environ.get("RMS_STRIDE", "16"))

# Recognition — tiered durations (seconds)
RECOGNIZE_DURATIONS = [20, 30, 45]

# Cooldown between recognition cycles (seconds)
RECOGNITION_COOLDOWN = int(os.environ.get("RECOGNITION_COOLDOWN", "10"))

# Backoff after a full recognition failure (all tiers exhausted)
FAILURE_BACKOFF_BASE = int(os.environ.get("FAILURE_BACKOFF_BASE", "30"))
FAILURE_BACKOFF_MAX = int(os.environ.get("FAILURE_BACKOFF_MAX", "300"))

# ListenBrainz API
LISTENBRAINZ_API_URL = "https://api.listenbrainz.org/1/submit-listens"

# Logging
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# ---------------------------------------------------------------------------
# Setup logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("vinyl-scrobbler")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class ScrobbleState:
    """Tracks the last scrobbled track and failure backoff."""

    artist: Optional[str] = None
    track: Optional[str] = None
    scrobbled_at: float = 0.0
    consecutive_failures: int = 0

    def is_duplicate(self, artist: str, track: str) -> bool:
        return self.artist == artist and self.track == track

    def record_scrobble(self, artist: str, track: str) -> None:
        self.artist = artist
        self.track = track
        self.scrobbled_at = time.time()
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1

    def backoff_seconds(self) -> int:
        """Exponential backoff capped at FAILURE_BACKOFF_MAX."""
        if self.consecutive_failures == 0:
            return 0
        delay = FAILURE_BACKOFF_BASE * (2 ** (self.consecutive_failures - 1))
        return min(delay, FAILURE_BACKOFF_MAX)


state = ScrobbleState()


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------


def rms_of_raw_24bit(data: bytes) -> float:
    """
    Calculate RMS amplitude of raw 24-bit stereo PCM audio bytes.

    Uses stride sampling for performance — on a Pi at 48kHz stereo,
    full sample processing is ~96k iterations per second of audio.
    With stride=16 this drops to ~6k with negligible accuracy loss
    for silence detection purposes.
    """
    byte_stride = 3 * RMS_STRIDE
    total_bytes = len(data)

    if total_bytes < 3:
        return 0.0

    sum_sq = 0
    checked = 0

    for i in range(0, total_bytes - 2, byte_stride):
        val = int.from_bytes(data[i : i + 3], "little", signed=True)
        sum_sq += val * val
        checked += 1

    if checked == 0:
        return 0.0

    return (sum_sq / checked) ** 0.5


def wait_for_audio() -> None:
    """
    Block until sustained audio above the silence threshold is detected.

    Requires SUSTAINED_AUDIO_CHECKS consecutive above-threshold readings
    to trigger, reducing false positives from transient sounds.
    """
    log.info("Listening for audio...")
    consecutive_hits = 0

    while True:
        try:
            proc = subprocess.Popen(
                [
                    "arecord",
                    "-D", ALSA_DEVICE,
                    "-f", SAMPLE_FORMAT,
                    "-r", str(SAMPLE_RATE),
                    "-c", str(CHANNELS),
                    "-t", "raw",
                    "-d", str(SILENCE_CHECK_SECONDS),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            data = proc.stdout.read()
            rc = proc.wait()

            if rc != 0:
                log.debug("arecord exited with code %d", rc)

            if data:
                rms = rms_of_raw_24bit(data)
                if rms > SILENCE_THRESHOLD:
                    consecutive_hits += 1
                    log.debug(
                        "Audio above threshold (RMS: %.0f, %d/%d)",
                        rms,
                        consecutive_hits,
                        SUSTAINED_AUDIO_CHECKS,
                    )
                    if consecutive_hits >= SUSTAINED_AUDIO_CHECKS:
                        log.info(
                            "Sustained audio detected (RMS: %.0f, %d consecutive checks)",
                            rms,
                            consecutive_hits,
                        )
                        return
                else:
                    if consecutive_hits > 0:
                        log.debug("Audio dropped below threshold, resetting")
                    consecutive_hits = 0

        except Exception as e:
            log.error("Error in silence detection: %s", e)
            consecutive_hits = 0
            time.sleep(1)

        time.sleep(0.1)


def record_audio(duration: int, filepath: str) -> bool:
    """Record audio to a WAV file. Returns True on success."""
    log.info("Recording %ds of audio to %s", duration, filepath)
    try:
        result = subprocess.run(
            [
                "arecord",
                "-D", ALSA_DEVICE,
                "-f", SAMPLE_FORMAT,
                "-r", str(SAMPLE_RATE),
                "-c", str(CHANNELS),
                "-d", str(duration),
                filepath,
            ],
            capture_output=True,
            timeout=duration + 10,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.warning("Recording timed out")
        return False
    except Exception as e:
        log.error("Recording error: %s", e)
        return False


def cleanup_temp_file(filepath: str) -> None:
    """Remove a temp file if it exists. Logs errors but doesn't raise."""
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            log.debug("Cleaned up temp file: %s", filepath)
    except OSError as e:
        log.warning("Failed to clean up %s: %s", filepath, e)


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------


def recognize_track(filepath: str) -> dict | None:
    """
    Recognize a track using SongRec's fingerprinting + Shazam API.
    Returns dict with artist, track or None.
    """
    try:
        audio = AudioSegment.from_file(filepath)
        audio = audio.set_sample_width(2)
        audio = audio.set_frame_rate(16000)
        audio = audio.set_channels(1)

        sig_gen = SignatureGenerator()
        sig_gen.feed_input(audio.get_array_of_samples())
        sig_gen.MAX_TIME_SECONDS = 12

        # Start from the middle of the recording if long enough
        if audio.duration_seconds > 12 * 3:
            sig_gen.samples_processed += 16000 * (
                int(audio.duration_seconds / 2) - 6
            )

        while True:
            signature = sig_gen.get_next_signature()
            if not signature:
                return None

            result = recognize_song_from_signature(signature)

            if result.get("matches"):
                track = result.get("track", {})
                artist = track.get("subtitle", "Unknown Artist")
                title = track.get("title", "Unknown Track")
                log.info("Match: %s - %s", artist, title)
                return {"artist": artist, "track": title}

            # Try next chunk
            processed_seconds = sig_gen.samples_processed / 16000
            log.debug("No match at %.0fs, trying next chunk...", processed_seconds)

    except Exception as e:
        log.error("Recognition error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Scrobbling
# ---------------------------------------------------------------------------


def submit_to_listenbrainz(match: dict) -> bool:
    """Submit a listen to ListenBrainz with retry."""
    artist = match["artist"]
    track = match["track"]

    if state.is_duplicate(artist, track):
        log.info("Skipping duplicate: %s - %s", artist, track)
        return False

    payload = {
        "listen_type": "single",
        "payload": [
            {
                "listened_at": int(time.time()),
                "track_metadata": {
                    "artist_name": artist,
                    "track_name": track,
                    "additional_info": {
                        "submission_client": "vinyl-scrobbler",
                        "submission_client_version": "5.1.0",
                        "music_service": "vinyl",
                    },
                },
            }
        ],
    }

    for attempt in range(3):
        try:
            resp = requests.post(
                LISTENBRAINZ_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Token {LISTENBRAINZ_TOKEN}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )

            if resp.status_code == 200:
                log.info("✓ Scrobbled: %s - %s", artist, track)
                state.record_scrobble(artist, track)
                return True
            else:
                log.error(
                    "ListenBrainz API error (%d): %s",
                    resp.status_code,
                    resp.text,
                )
                return False

        except requests.RequestException as e:
            if attempt < 2:
                log.warning("ListenBrainz request failed, retrying: %s", e)
                time.sleep(2)
            else:
                log.error(
                    "ListenBrainz request failed after 3 attempts: %s", e
                )
                return False


def attempt_recognition(tmp_file: str) -> dict | None:
    """
    Try to recognize a track using tiered durations.
    Tries 20s first, then 30s, then 45s before giving up.
    Cleans up the temp file between attempts.
    """
    for i, duration in enumerate(RECOGNIZE_DURATIONS):
        attempt = i + 1
        total = len(RECOGNIZE_DURATIONS)

        if not record_audio(duration, tmp_file):
            log.warning("Recording failed on attempt %d/%d", attempt, total)
            cleanup_temp_file(tmp_file)
            time.sleep(2)
            continue

        match = recognize_track(tmp_file)
        cleanup_temp_file(tmp_file)

        if match:
            if attempt > 1:
                log.info(
                    "Matched on attempt %d/%d (%ds sample)",
                    attempt, total, duration,
                )
            return match

        if attempt < total:
            log.info(
                "No match at %ds, trying %ds (attempt %d/%d)",
                duration,
                RECOGNIZE_DURATIONS[i + 1],
                attempt,
                total,
            )
        else:
            log.info(
                "No match after all %d attempts (tried %s second samples)",
                total,
                "/".join(str(d) for d in RECOGNIZE_DURATIONS),
            )

    return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main_loop():
    """Main scrobbler loop with backoff and cleanup."""
    log.info("Vinyl Scrobbler v5.1 (SongRec engine)")
    log.info("ALSA device: %s", ALSA_DEVICE)
    log.info("Silence threshold: %d", SILENCE_THRESHOLD)
    log.info("Sustained audio checks: %d", SUSTAINED_AUDIO_CHECKS)
    log.info("RMS stride: %d (~%d samples checked per second of audio)",
             RMS_STRIDE, (SAMPLE_RATE * CHANNELS) // RMS_STRIDE)
    log.info(
        "Recognition durations: %s seconds",
        "/".join(str(d) for d in RECOGNIZE_DURATIONS),
    )
    log.info("Recognition cooldown: %ds", RECOGNITION_COOLDOWN)
    log.info(
        "Audio format: %dHz, %dch, %s", SAMPLE_RATE, CHANNELS, SAMPLE_FORMAT
    )

    # Use a predictable temp path so we can always clean up
    tmp_file = os.path.join(tempfile.gettempdir(), "vinyl_capture.wav")

    try:
        while True:
            try:
                # Apply backoff if we've had consecutive failures
                backoff = state.backoff_seconds()
                if backoff > 0:
                    log.info(
                        "Backing off %ds after %d consecutive failure(s)",
                        backoff,
                        state.consecutive_failures,
                    )
                    time.sleep(backoff)

                wait_for_audio()

                match = attempt_recognition(tmp_file)

                if match:
                    submit_to_listenbrainz(match)
                    state.consecutive_failures = 0
                else:
                    state.record_failure()

                # Cooldown between recognition cycles to rate-limit Shazam calls
                log.debug("Cooldown: %ds before next cycle", RECOGNITION_COOLDOWN)
                time.sleep(RECOGNITION_COOLDOWN)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.error("Unexpected error in main loop: %s", e)
                time.sleep(5)

    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        cleanup_temp_file(tmp_file)
        log.info("Cleanup complete. Goodbye.")


if __name__ == "__main__":
    main_loop()
