#!/usr/bin/env python3
"""
RMS Profiler — Sample audio for a set duration and report RMS values over time.

Usage:
    python3 rms_profile.py [duration_seconds]

Defaults to 120 seconds (2 minutes). Samples 1 second of audio at a time
and prints a live RMS reading with a visual bar chart.

Uses the same RMS calculation as scrobbler.py so values are directly comparable.
"""

import subprocess
import sys
import os
import time


def rms_of_raw_24bit(data: bytes, stride: int = 16) -> float:
    """Identical to scrobbler.py's RMS calculation."""
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


def bar(rms: float, max_rms: float = 1_200_000, width: int = 50) -> str:
    """Create a visual bar for the RMS value."""
    filled = int(min(rms / max_rms, 1.0) * width)
    return "█" * filled + "░" * (width - filled)


def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    alsa_device = os.environ.get("ALSA_DEVICE", "hw:0,0")
    sample_format = os.environ.get("SAMPLE_FORMAT", "S24_3LE")
    sample_rate = int(os.environ.get("SAMPLE_RATE", "48000"))
    channels = int(os.environ.get("CHANNELS", "2"))
    threshold = int(os.environ.get("SILENCE_THRESHOLD", "300000"))

    print(f"RMS Profiler — sampling for {duration}s")
    print(f"Device: {alsa_device} | Format: {sample_format} | Rate: {sample_rate} | Channels: {channels}")
    print(f"Current threshold: {threshold:,}")
    print(f"{'─' * 78}")
    print(f"{'Time':>6}  {'RMS':>10}  {'Bar':<50}  Status")
    print(f"{'─' * 78}")

    readings = []
    start = time.time()

    for sec in range(duration):
        try:
            proc = subprocess.Popen(
                [
                    "arecord",
                    "-D", alsa_device,
                    "-f", sample_format,
                    "-r", str(sample_rate),
                    "-c", str(channels),
                    "-t", "raw",
                    "-d", "1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            data = proc.stdout.read()
            proc.wait()

            if data:
                rms = rms_of_raw_24bit(data)
                readings.append(rms)

                if rms >= threshold:
                    status = "▲ TRIGGER"
                else:
                    status = "  silent"

                elapsed = sec + 1
                print(f"{elapsed:>5}s  {rms:>10,.0f}  {bar(rms)}  {status}")
            else:
                print(f"{sec+1:>5}s  {'NO DATA':>10}")

        except KeyboardInterrupt:
            print("\n\nStopped early.")
            break
        except Exception as e:
            print(f"{sec+1:>5}s  ERROR: {e}")

    print(f"{'─' * 78}")

    if readings:
        readings.sort()
        print(f"\nSummary ({len(readings)} samples):")
        print(f"  Min:      {min(readings):>12,.0f}")
        print(f"  Max:      {max(readings):>12,.0f}")
        print(f"  Mean:     {sum(readings)/len(readings):>12,.0f}")
        print(f"  Median:   {readings[len(readings)//2]:>12,.0f}")
        print(f"  P10:      {readings[len(readings)//10]:>12,.0f}")
        print(f"  P90:      {readings[int(len(readings)*0.9)]:>12,.0f}")
        print(f"  Threshold:{threshold:>12,}")

        above = sum(1 for r in readings if r >= threshold)
        print(f"  Triggers: {above}/{len(readings)} ({100*above/len(readings):.0f}%)")
    else:
        print("\nNo readings collected.")


if __name__ == "__main__":
    main()
