#!/usr/bin/env python3
"""
Example LiveQuakes community-station client.

This is a STARTING POINT, not a finished seismometer integration -- it shows
the API contract (what to POST, how auth works) and a bare-bones trigger
detector you'd replace with something that actually reads your hardware.

Real usage sketch:
  - Raspberry Shake: read the local SeedLink stream with ObsPy
    (`obspy.clients.seedlink`) and run ObsPy's built-in recursive STA/LTA
    trigger (`obspy.signal.trigger.recursive_sta_lta` + `trigger_onset`) on
    the incoming trace instead of the fake `read_sensor()` below.
  - A cheap accelerometer (e.g. MPU6050 on a Pi/Arduino via I2C/serial):
    poll it in a loop, keep a rolling short-term/long-term average of the
    magnitude, and call `post_event()` when the ratio crosses a threshold.

Either way, the contract to LiveQuakes is the same: only POST when you
detect a trigger, not a continuous stream -- see ingestEvent in
functions/index.js for exactly what's validated server-side.
"""

import time
import random
import urllib.request
import json

# From the "Register station" flow on livequakes.com -- shown once, save it.
API_KEY = "PASTE_YOUR_STATION_API_KEY_HERE"
INGEST_URL = "https://us-central1-livequakes.cloudfunctions.net/ingestEvent"

# How often to check the sensor for a trigger, in seconds.
POLL_INTERVAL_SECONDS = 1.0

# Trigger threshold on the (placeholder) 0-100 raw sensor reading below.
# Replace this whole scheme with a real STA/LTA detector for real hardware.
TRIGGER_THRESHOLD = 90


def read_sensor():
    """
    STUB: replace with a real read from your hardware.
    Returns a single instantaneous "shake intensity" sample.
    """
    return random.uniform(0, 20)  # mostly quiet, for demonstration


def post_event(amplitude, duration_ms, note=""):
    """POST one trigger event to LiveQuakes. Raises on network/HTTP errors."""
    body = json.dumps({
        "amplitude": round(amplitude, 2),   # unitless 0-1000, relative trigger strength
        "durationMs": int(duration_ms),
        "note": note,
    }).encode("utf-8")

    req = urllib.request.Request(
        INGEST_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": API_KEY,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        print("Reported event:", result)
        return result


def main():
    if API_KEY == "PASTE_YOUR_STATION_API_KEY_HERE":
        raise SystemExit("Set API_KEY to the key you got when registering your station.")

    print("Watching for triggers... (Ctrl+C to stop)")
    trigger_start = None
    while True:
        sample = read_sensor()

        if sample >= TRIGGER_THRESHOLD:
            if trigger_start is None:
                trigger_start = time.time()
            # keep tracking peak amplitude while the trigger is ongoing
        elif trigger_start is not None:
            duration_ms = (time.time() - trigger_start) * 1000
            try:
                post_event(amplitude=sample, duration_ms=duration_ms)
            except Exception as exc:  # noqa: BLE001 -- example code, keep it simple
                print("Failed to report event:", exc)
            trigger_start = None

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
