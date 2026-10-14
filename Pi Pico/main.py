# main.py -- runs automatically on power-up (MicroPython convention: any file
# named main.py on the device root is run after boot).
#
# What this does: polls the LSM6DSOX as fast as I2C allows, and runs a
# classic seismology-style STA/LTA (short-term-average / long-term-average)
# trigger on the deviation of the acceleration vector's magnitude from its
# resting value. STA/LTA is what real seismometer software (e.g. ObsPy's
# recursive_sta_lta) uses too: a short EMA tracks "what's happening right
# now," a long EMA tracks "the ambient noise floor," and a trigger fires
# when the ratio between them spikes -- which adapts to your specific
# mounting location's normal vibration level instead of using one fixed
# threshold that might be too twitchy in a noisy spot or too dead in a
# quiet one.
#
# Using the acceleration vector's *magnitude* (not a single axis) means the
# sensor doesn't need to be mounted perfectly level or in any particular
# orientation -- at rest, |x,y,z| is always ~1g regardless of which way is
# "up" for the board.

import time
import network
import urequests
from machine import I2C, Pin
from lsm6dsox import LSM6DSOX
import config

# ---- Tunables ---------------------------------------------------------
I2C_SDA_PIN = 4
I2C_SCL_PIN = 5

STA_TAU_S = 0.5  # short-term average time constant (seconds)
LTA_TAU_S = 30.0  # long-term average time constant (seconds)
BASELINE_TAU_S = 120.0  # very slow EMA tracking the resting ~1g offset
WARMUP_S = 90  # ignore triggers for this long after boot, while STA/LTA settle

TRIGGER_RATIO = 4.0  # STA/LTA ratio that starts an event. Measured idle
# ratio on this board (after the baseline-calibration and lta-guard fixes)
# settles around 1.0-1.5 at rest; 3.0 caught real shakes fine but was also
# tripped by just tapping the table, so 4.0 is the confirmed-good working
# value -- comfortable headroom above idle noise, still sensitive to a
# genuine small tremor. Watch DEBUG_STATUS if you ever want to retune.
DETRIGGER_RATIO = 1.3  # STA/LTA ratio that ends an event -- comfortably
# below TRIGGER_RATIO and above the ~1.0-1.5 idle range, so a real event
# fully de-triggers once it settles back to normal instead of flapping.
TRIGGER_CONFIRM_SAMPLES = 3  # consecutive over-threshold samples required to fire
MIN_EVENT_MS = 150  # basic floor against instant one-sample blips; note that STA
# smoothing itself stretches a brief tap into a longer-looking event (a single
# knock can still end up reported as ~1s long), so this alone won't cleanly
# separate "someone bumped the table" from a real quake -- amplitude is the
# more reliable signal for that: a tap is a low peak-g event, a real quake is not.
AMPLITUDE_SCALE = 3000  # peak g-deviation * this = reported amplitude (0-1000, clipped)
DEBUG_STATUS = True  # print the live STA/LTA ratio every few seconds -- turn
# this on while tuning so you can see how close ambient noise sits to
# TRIGGER_RATIO, then turn it off (or just ignore the extra print) once happy.
DEBUG_STATUS_INTERVAL_S = 3

# ---- Impulsive-event rejection (door slams, taps, footsteps) ------------
# STA/LTA amplitude alone can't tell a sharp mechanical knock from a real
# tremor -- a knock can produce just as high (often higher) peak-g as a
# gentle real shake. The real physical difference is duration/shape: a
# knock is a single brief impulse that the board's structure absorbs and
# stops ringing from almost immediately, while real ground shaking keeps
# the raw signal meaningfully elevated for a sustained stretch (multiple
# oscillation cycles), not just one spike. STA smoothing itself can't be
# used to tell these apart -- it stretches a single sharp knock into a
# ~1s-long-looking event on its own -- so this checks the *raw* deviation
# signal directly, before STA/LTA smoothing blurs the two together.
RAW_ELEVATED_MULT = 2.0  # a raw sample counts as "elevated" once dev
# exceeds this multiple of the (frozen, pre-event) lta noise floor
SUSTAINED_MS = 200  # raw dev must stay elevated for at least this many
# consecutive milliseconds somewhere during the event to be treated as
# real shaking rather than a single impulsive knock. This is the main
# knob to retune here -- if real small tremors are getting rejected as
# "impulsive," lower it; if taps/door slams still slip through, raise it.
# -------------------------------------------------------------------------

# IMPORTANT while testing: WARMUP_S below only blocks *triggering* -- the
# LTA (ambient noise floor) and baseline keep updating the entire time,
# warmup included. If you shake the board hard during those first 90s to see
# if it "works," that shake gets absorbed into LTA as if it were normal
# background noise, which raises your effective threshold for the next
# ~30-90s (LTA's own time constant) even after warmup ends -- so a second
# test shake right after can look like it "doesn't trigger" when really the
# first shake poisoned the baseline. Power-cycle, leave it completely still
# for the full warmup, *then* do your test shake.


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("Connecting to WiFi...")
        wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
        for _ in range(30):
            if wlan.isconnected():
                break
            time.sleep(1)
    if wlan.isconnected():
        print("WiFi connected:", wlan.ifconfig()[0])
    else:
        print("WiFi did not connect -- check config.py. Will retry before the next report.")
    return wlan


REPORT_RETRIES = 3  # first HTTPS request after boot (or after a while idle) can
# fail once with a low-level socket error even when WiFi/DNS/TLS are all fine
# -- this has been observed in practice and isn't specific to this endpoint,
# so a real trigger gets a couple of quick retries rather than being dropped.
REPORT_RETRY_DELAY_S = 1.5


def _post_event(amplitude, duration_ms):
    resp = urequests.post(
        config.INGEST_URL,
        headers={"Content-Type": "application/json", "X-Api-Key": config.STATION_API_KEY},
        json={"amplitude": round(amplitude, 2), "durationMs": int(duration_ms)},
    )
    text = resp.text
    resp.close()
    return text


def report_event(wlan, amplitude, duration_ms):
    if not wlan.isconnected():
        wlan = connect_wifi()
    for attempt in range(1, REPORT_RETRIES + 1):
        try:
            print("Reported event:", _post_event(amplitude, duration_ms))
            return wlan
        except Exception as e:
            print("Report attempt %d/%d failed: %s" % (attempt, REPORT_RETRIES, e))
            if attempt < REPORT_RETRIES:
                time.sleep(REPORT_RETRY_DELAY_S)
    print("Giving up on this report after %d attempts." % REPORT_RETRIES)
    return wlan


def ema_alpha(dt, tau):
    return dt / (tau + dt)


def main():
    wlan = connect_wifi()

    i2c = I2C(0, sda=Pin(I2C_SDA_PIN), scl=Pin(I2C_SCL_PIN), freq=400000)
    print("I2C devices found:", [hex(a) for a in i2c.scan()])
    sensor = LSM6DSOX(i2c)

    # Calibrate the resting baseline from real readings instead of assuming
    # exactly 1.000g. Board tilt and per-unit sensor offset error mean the
    # true resting |acceleration| is almost never exactly 1g -- starting
    # from a wrong guess creates a large fake "dev" that BASELINE_TAU_S (120s)
    # takes a long time to settle out of, which swamps the real ambient noise
    # floor for minutes and makes low-ratio tuning meaningless during that
    # window. Don't touch the board during this 1s calibration.
    print("Calibrating resting baseline (keep the board still)...")
    calib_sum = 0.0
    calib_n = 0
    calib_start = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), calib_start) < 1000:
        cx, cy, cz = sensor.acceleration()
        calib_sum += (cx * cx + cy * cy + cz * cz) ** 0.5
        calib_n += 1
    baseline = calib_sum / calib_n
    print("Baseline calibrated: %.5fg (%d samples). Warming up for %ds..." % (
        baseline, calib_n, WARMUP_S
    ))

    sta = 0.0
    lta = 0.0
    triggered = False
    trigger_start = 0
    peak_dev = 0.0
    confirm_count = 0
    elevated_run_ms = 0.0
    max_elevated_run_ms = 0.0

    boot_time = time.ticks_ms()
    last_t = boot_time
    last_debug_print = boot_time

    while True:
        x, y, z = sensor.acceleration()
        mag = (x * x + y * y + z * z) ** 0.5

        now = time.ticks_ms()
        dt = time.ticks_diff(now, last_t) / 1000.0
        last_t = now
        if dt <= 0:
            continue

        dev = abs(mag - baseline)

        sta += (dev - sta) * ema_alpha(dt, STA_TAU_S)
        if not triggered:
            # Freeze LTA/baseline while an event is in progress, so a real
            # earthquake doesn't get absorbed into "the new normal" mid-shake.
            lta += (dev - lta) * ema_alpha(dt, LTA_TAU_S)
            baseline += (mag - baseline) * ema_alpha(dt, BASELINE_TAU_S)

        ratio = (sta / lta) if lta > 0.00002 else 0.0
        # NOTE: this guard used to sit at 0.0005 -- fine back when a baseline
        # seeding bug made lta run 10-20x too high, but with baseline now
        # calibrated correctly, real quiet-room lta legitimately settles well
        # below the old 0.0005 floor, which was forcing ratio to a permanent
        # 0.00 and made triggering impossible. This value only needs to be
        # small enough to dodge a literal divide-by-near-zero at boot.
        warmed_up = time.ticks_diff(now, boot_time) > WARMUP_S * 1000

        if DEBUG_STATUS and not triggered:
            if time.ticks_diff(now, last_debug_print) >= DEBUG_STATUS_INTERVAL_S * 1000:
                last_debug_print = now
                print("status: ratio=%.2f sta=%.5f lta=%.5f dev=%.5f%s" % (
                    ratio, sta, lta, dev, "" if warmed_up else " (warming up)"
                ))

        if not triggered:
            if warmed_up and ratio > TRIGGER_RATIO:
                confirm_count += 1
                if confirm_count >= TRIGGER_CONFIRM_SAMPLES:
                    triggered = True
                    trigger_start = now
                    peak_dev = dev
                    elevated_run_ms = 0.0
                    max_elevated_run_ms = 0.0
                    print("TRIGGER (ratio=%.2f)" % ratio)
            else:
                confirm_count = 0
        else:
            peak_dev = max(peak_dev, dev)

            if dev > lta * RAW_ELEVATED_MULT:
                elevated_run_ms += dt * 1000.0
            else:
                elevated_run_ms = 0.0
            max_elevated_run_ms = max(max_elevated_run_ms, elevated_run_ms)

            if ratio < DETRIGGER_RATIO:
                duration_ms = time.ticks_diff(now, trigger_start)
                triggered = False
                confirm_count = 0
                is_impulsive = max_elevated_run_ms < SUSTAINED_MS
                print("de-trigger: peak=%.4fg duration=%dms sustained=%dms%s" % (
                    peak_dev, duration_ms, max_elevated_run_ms,
                    " -> IMPULSIVE, not reporting (looks like a knock/door/tap)" if is_impulsive else ""
                ))
                if duration_ms >= MIN_EVENT_MS and not is_impulsive:
                    amplitude = min(1000.0, peak_dev * AMPLITUDE_SCALE)
                    wlan = report_event(wlan, amplitude, duration_ms)


try:
    main()
except KeyboardInterrupt:
    print("Stopped.")
