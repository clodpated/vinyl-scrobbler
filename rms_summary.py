#!/usr/bin/env python3
"""
RMS Summary — Sample audio and report 30-second window summaries.

Usage:
    python3 rms_summary.py [duration_seconds] [window_seconds]

Defaults to 120s duration with 30s windows. Samples 1 second at a time,
then prints a compact summary per window.

Uses the same RMS calculation as scrobbler.py so values are directly comparable.
"""

import subprocess
import sys
import os
import tempfile
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


def bar(val: float, max_val: float = 1_200_000, width: int = 30) -> str:
    """Create a visual bar."""
    filled = int(min(val / max_val, 1.0) * width)
    return "█" * filled + "░" * (width - filled)


def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    window = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    alsa_device = os.environ.get("ALSA_DEVICE", "hw:0,0")
    sample_format = os.environ.get("SAMPLE_FORMAT", "S24_3LE")
    sample_rate = int(os.environ.get("SAMPLE_RATE", "48000"))
    channels = int(os.environ.get("CHANNELS", "2"))
    threshold = int(os.environ.get("SILENCE_THRESHOLD", "250000"))
    tmpfile = os.path.join(tempfile.gettempdir(), "rms_summary_capture.raw")

    windows = duration // window
    print(f"RMS Summary — {duration}s in {window}s windows ({windows} windows)")
    print(f"Threshold: {threshold:,}")
    print(f"Sampling...", flush=True)

    all_readings = []
    window_readings = []
    summaries = []

    for sec in range(duration):
        try:
            subprocess.run(
                [
                    "arecord",
                    "-D", alsa_device,
                    "-f", sample_format,
                    "-r", str(sample_rate),
                    "-c", str(channels),
                    "-t", "raw",
                    "-d", "1",
                    tmpfile,
                ],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )

            if os.path.exists(tmpfile) and os.path.getsize(tmpfile) > 0:
                with open(tmpfile, "rb") as f:
                    data = f.read()
                os.remove(tmpfile)

                rms = rms_of_raw_24bit(data)
                all_readings.append(rms)
                window_readings.append(rms)

        except KeyboardInterrupt:
            print("\nStopped early.", flush=True)
            break
        except subprocess.TimeoutExpired:
            subprocess.run(["pkill", "-f", "arecord"], stderr=subprocess.DEVNULL)
        except Exception:
            pass

        # End of window?
        if (sec + 1) % window == 0 and window_readings:
            summaries.append({
                "start": sec + 1 - window,
                "end": sec + 1,
                "min": min(window_readings),
                "max": max(window_readings),
                "mean": sum(window_readings) / len(window_readings),
                "above": sum(1 for r in window_readings if r >= threshold),
                "total": len(window_readings),
            })
            window_readings = []

    # Catch partial final window
    if window_readings:
        start = (len(summaries)) * window
        summaries.append({
            "start": start,
            "end": start + len(window_readings),
            "min": min(window_readings),
            "max": max(window_readings),
            "mean": sum(window_readings) / len(window_readings),
            "above": sum(1 for r in window_readings if r >= threshold),
            "total": len(window_readings),
        })

    # Clean up
    if os.path.exists(tmpfile):
        os.remove(tmpfile)

    # Print results
    print(f"\n{'─' * 78}")
    print(f"{'Window':>10}  {'Min':>10}  {'Mean':>10}  {'Max':>10}  {'Bar (mean)':^30}  Trig")
    print(f"{'─' * 78}")

    for s in summaries:
        label = f"{s['start']}–{s['end']}s"
        trig = f"{s['above']}/{s['total']}"
        print(
            f"{label:>10}  {s['min']:>10,.0f}  {s['mean']:>10,.0f}  {s['max']:>10,.0f}  {bar(s['mean'])}  {trig}"
        )

    print(f"{'─' * 78}")

    if all_readings:
        all_readings.sort()
        above = sum(1 for r in all_readings if r >= threshold)
        print(f"\nOverall ({len(all_readings)} samples):")
        print(f"  Min:      {min(all_readings):>12,.0f}")
        print(f"  Max:      {max(all_readings):>12,.0f}")
        print(f"  Mean:     {sum(all_readings)/len(all_readings):>12,.0f}")
        print(f"  Triggers: {above}/{len(all_readings)} ({100*above/len(all_readings):.0f}%)")


if __name__ == "__main__":
    main()
