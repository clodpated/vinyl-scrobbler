#!/usr/bin/env python3
"""
Vinyl Scrobbler — Silence-aware audio recognition and ListenBrainz scrobbling.

Listens via USB microphone, identifies tracks using SongRec's fingerprinting
(which talks to Shazam's servers), and submits listens to ListenBrainz.
"""
from __future__ import annotations

import subprocess
import time
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Optional

import requests

log = logging.getLogger("vinyl-scrobbler")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class ScrobbleState:
    """Tracks the last scrobbled track to deduplicate consecutive plays."""

    artist: Optional[str] = None
    track: Optional[str] = None
    scrobbled_at: float = 0.0

    def is_duplicate(self, artist: str, track: str) -> bool:
        return self.artist == artist and self.track == track

    def record_scrobble(self, artist: str, track: str) -> None:
        self.artist = artist
        self.track = track
        self.scrobbled_at = time.time()


# ---------------------------------------------------------------------------
# Blocklist
# ---------------------------------------------------------------------------


def load_blocklist(path: str) -> set:
    """
    Load blocked artist/track pairs from a text file.

    Each line should be: artist\ttrack
    Lines starting with # and blank lines are ignored.
    Returns a set of (artist_lower, track_lower) tuples.
    """
    blocked = set()
    if not os.path.exists(path):
        return blocked
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                blocked.add((parts[0].strip().lower(), parts[1].strip().lower()))
    return blocked


def is_blocked(artist: str, track: str, blocklist: set) -> bool:
    """Check if an artist/track pair is in the blocklist."""
    return (artist.lower(), track.lower()) in blocklist


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------


def rms_of_raw_24bit(data: bytes, stride: int = 16) -> float:
    """
    Calculate RMS amplitude of raw 24-bit stereo PCM audio bytes.

    Uses stride sampling for performance — on a Pi at 48kHz stereo,
    full sample processing is ~96k iterations per second of audio.
    With stride=16 this drops to ~6k with negligible accuracy loss
    for silence detection purposes.
    """
    byte_stride = 3 * stride
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


def wait_for_audio(*, alsa_device, sample_format, sample_rate, channels,
                   silence_threshold, silence_check_seconds,
                   sustained_audio_checks, rms_stride) -> None:
    """
    Block until sustained audio above the silence threshold is detected.

    Requires sustained_audio_checks consecutive above-threshold readings
    to trigger, reducing false positives from transient sounds.
    """
    log.debug("Listening for audio...")
    consecutive_hits = 0

    while True:
        try:
            proc = subprocess.Popen(
                [
                    "arecord",
                    "-D", alsa_device,
                    "-f", sample_format,
                    "-r", str(sample_rate),
                    "-c", str(channels),
                    "-t", "raw",
                    "-d", str(silence_check_seconds),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            data = proc.stdout.read()
            rc = proc.wait()

            if rc != 0:
                log.debug("arecord exited with code %d", rc)

            if data:
                rms = rms_of_raw_24bit(data, stride=rms_stride)
                if rms > silence_threshold:
                    consecutive_hits += 1
                    log.debug(
                        "Audio above threshold (RMS: %.0f, %d/%d)",
                        rms,
                        consecutive_hits,
                        sustained_audio_checks,
                    )
                    if consecutive_hits >= sustained_audio_checks:
                        log.debug(
                            "Sustained audio detected (RMS: %.0f, %d consecutive checks)",
                            rms,
                            consecutive_hits,
                        )
                        return
                else:
                    if consecutive_hits > 0:
                        log.debug("Audio dropped below threshold, resetting")
                    consecutive_hits = 0

        except (subprocess.SubprocessError, OSError) as e:
            log.error("Error in silence detection: %s", e)
            consecutive_hits = 0
            time.sleep(1)

        time.sleep(0.1)


def record_audio(duration: int, filepath: str, *, alsa_device, sample_format,
                 sample_rate, channels) -> bool:
    """Record audio to a WAV file. Returns True on success."""
    log.debug("Recording %ds of audio to %s", duration, filepath)
    try:
        result = subprocess.run(
            [
                "arecord",
                "-D", alsa_device,
                "-f", sample_format,
                "-r", str(sample_rate),
                "-c", str(channels),
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
    except OSError as e:
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


def recognize_track(filepath: str, *, SignatureGenerator, recognize_song_from_signature) -> dict | None:
    """
    Recognize a track using SongRec's fingerprinting + Shazam API.
    Returns dict with artist, track or None.
    """
    from pydub import AudioSegment

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
                log.debug("Match: %s - %s", artist, title)
                return {"artist": artist, "track": title}

            # Try next chunk
            processed_seconds = sig_gen.samples_processed / 16000
            log.debug("No match at %.0fs, trying next chunk...", processed_seconds)

    except (FileNotFoundError, OSError, ValueError) as e:
        log.error("Recognition error: %s", e)
        return None
    except Exception as e:
        log.error("Unexpected recognition error (%s): %s", type(e).__name__, e)
        return None


# ---------------------------------------------------------------------------
# Scrobbling
# ---------------------------------------------------------------------------


LISTENBRAINZ_API_URL = "https://api.listenbrainz.org/1/submit-listens"


def submit_to_listenbrainz(match: dict, *, token: str, state: ScrobbleState,
                           blocklist: set | None = None) -> bool:
    """Submit a listen to ListenBrainz with retry."""
    artist = match["artist"]
    track = match["track"]

    if blocklist and is_blocked(artist, track, blocklist):
        log.info("Blocked phantom: %s - %s", artist, track)
        return False

    if state.is_duplicate(artist, track):
        log.debug("Skipping duplicate: %s - %s", artist, track)
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
                        "submission_client_version": "1.0.0",
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
                    "Authorization": f"Token {token}",
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


def attempt_recognition(tmp_file: str, *, record_durations, record_fn,
                        recognize_fn) -> dict | None:
    """
    Try to recognize a track using tiered durations.
    Tries shorter samples first, then longer ones before giving up.
    Cleans up the temp file between attempts.
    """
    for i, duration in enumerate(record_durations):
        if not record_fn(duration, tmp_file):
            log.debug("Recording failed at %ds", duration)
            cleanup_temp_file(tmp_file)
            time.sleep(2)
            continue

        match = recognize_fn(tmp_file)
        cleanup_temp_file(tmp_file)

        if match:
            return match

        log.debug("No match at %ds", duration)

    log.info("No match (%s)", "/".join(f"{d}s" for d in record_durations))
    return None


# ---------------------------------------------------------------------------
# Configuration and main loop
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """Load configuration from environment variables."""
    token = os.environ.get("LISTENBRAINZ_TOKEN")
    if not token:
        sys.exit(
            "Error: LISTENBRAINZ_TOKEN environment variable is required.\n"
            "Export it before running: export LISTENBRAINZ_TOKEN='your-token-here'"
        )

    return {
        "token": token,
        "alsa_device": os.environ.get("ALSA_DEVICE", "hw:0,0"),
        "sample_rate": int(os.environ.get("SAMPLE_RATE", "48000")),
        "channels": int(os.environ.get("CHANNELS", "2")),
        "sample_format": os.environ.get("SAMPLE_FORMAT", "S24_3LE"),
        "silence_threshold": int(os.environ.get("SILENCE_THRESHOLD", "500")),
        "silence_check_seconds": 1,
        "sustained_audio_checks": int(os.environ.get("SUSTAINED_AUDIO_CHECKS", "3")),
        "rms_stride": int(os.environ.get("RMS_STRIDE", "16")),
        "recognize_durations": [20, 40],
        "recognition_cooldown": int(os.environ.get("RECOGNITION_COOLDOWN", "10")),
        "log_level": os.environ.get("LOG_LEVEL", "INFO"),
    }


def main_loop():
    """Main scrobbler loop with backoff and cleanup."""
    cfg = load_config()

    logging.basicConfig(
        level=getattr(logging, cfg["log_level"]),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Import SongRec at runtime — only available on the Pi
    songrec_dir = os.path.expanduser("~/songrec-python/fingerprinting")
    sys.path.insert(0, songrec_dir)
    from algorithm import SignatureGenerator
    from communication import recognize_song_from_signature

    state = ScrobbleState()

    blocklist_path = os.environ.get(
        "BLOCKLIST_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "blocklist.txt"),
    )
    blocklist = load_blocklist(blocklist_path)
    if blocklist:
        log.info("Blocklist loaded: %d entries from %s", len(blocklist), blocklist_path)

    audio_kwargs = {
        "alsa_device": cfg["alsa_device"],
        "sample_format": cfg["sample_format"],
        "sample_rate": cfg["sample_rate"],
        "channels": cfg["channels"],
    }

    log.info("Vinyl Scrobbler v1.0 (SongRec engine)")
    log.info("ALSA device: %s", cfg["alsa_device"])
    log.info("Silence threshold: %d", cfg["silence_threshold"])
    log.info("Sustained audio checks: %d", cfg["sustained_audio_checks"])
    log.info("RMS stride: %d (~%d samples checked per second of audio)",
             cfg["rms_stride"],
             (cfg["sample_rate"] * cfg["channels"]) // cfg["rms_stride"])
    log.info(
        "Recognition durations: %s seconds",
        "/".join(str(d) for d in cfg["recognize_durations"]),
    )
    log.info("Recognition cooldown: %ds", cfg["recognition_cooldown"])
    log.info(
        "Audio format: %dHz, %dch, %s",
        cfg["sample_rate"], cfg["channels"], cfg["sample_format"],
    )

    # Use a predictable temp path so we can always clean up
    tmp_file = os.path.join(tempfile.gettempdir(), "vinyl_capture.wav")

    def _record(duration, filepath):
        return record_audio(duration, filepath, **audio_kwargs)

    def _recognize(filepath):
        return recognize_track(
            filepath,
            SignatureGenerator=SignatureGenerator,
            recognize_song_from_signature=recognize_song_from_signature,
        )

    try:
        while True:
            try:
                wait_for_audio(
                    silence_threshold=cfg["silence_threshold"],
                    silence_check_seconds=cfg["silence_check_seconds"],
                    sustained_audio_checks=cfg["sustained_audio_checks"],
                    rms_stride=cfg["rms_stride"],
                    **audio_kwargs,
                )

                match = attempt_recognition(
                    tmp_file,
                    record_durations=cfg["recognize_durations"],
                    record_fn=_record,
                    recognize_fn=_recognize,
                )

                if match:
                    submit_to_listenbrainz(match, token=cfg["token"], state=state,
                                           blocklist=blocklist)

                # Cooldown between recognition cycles to rate-limit Shazam calls
                log.debug("Cooldown: %ds before next cycle", cfg["recognition_cooldown"])
                time.sleep(cfg["recognition_cooldown"])

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
