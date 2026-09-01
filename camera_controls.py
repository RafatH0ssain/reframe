"""Pure camera-control logic for reFrame.

Decides WHICH picamera2 controls to send for a given set of camera settings.
Sending them is reframe.py's job; deciding is this module's.

The split exists so this logic is testable. picamera2 cannot be installed on a
development machine, so anything living inside reframe.py can only be verified
by reading it. Everything here runs under plain unittest with no camera.

No picamera2, no FastAPI, no I/O.
"""

import logging

# Used when the sensor does not report a control, or reports it in a shape we
# do not recognise. Deliberately permissive -- real limits come from the sensor.
DEFAULT_SENSOR_LIMITS = {
    "exposure_time_us": {"min": 100, "max": 200_000_000, "default": 20_000},
    "analogue_gain": {"min": 1.0, "max": 16.0, "default": 1.0},
    "frame_duration_us": {"min": 100, "max": 200_000_000},
}

_LIMIT_SOURCES = (
    ("exposure_time_us", "ExposureTime"),
    ("analogue_gain", "AnalogueGain"),
    ("frame_duration_us", "FrameDurationLimits"),
)


def _clamp(value, low, high):
    return max(low, min(high, value))


def read_sensor_limits(raw_controls):
    """Convert picamera2's ``camera_controls`` mapping into our own shape.

    picamera2 exposes ``{"ExposureTime": (min, max, default), ...}``. Parsing
    that is pure, so it is tested here even though obtaining it is not.

    Any control that is absent or malformed falls back to its default entry:
    a camera that reports something unexpected must still take photos.
    """
    limits = {key: dict(value) for key, value in DEFAULT_SENSOR_LIMITS.items()}

    if not isinstance(raw_controls, dict):
        return limits

    for our_key, picam_key in _LIMIT_SOURCES:
        entry = raw_controls.get(picam_key)
        if not isinstance(entry, (tuple, list)) or len(entry) < 2:
            continue
        low, high = entry[0], entry[1]
        if low is None or high is None:
            continue
        limits[our_key]["min"] = low
        limits[our_key]["max"] = high
        if len(entry) > 2 and entry[2] is not None and "default" in limits[our_key]:
            limits[our_key]["default"] = entry[2]

    return limits


def build_controls(camera, limits):
    """Build the picamera2 control dict for these camera settings."""
    camera = camera or {}
    controls = {
        "Sharpness": camera.get("sharpness", 3),
        "AfMode": camera.get("autofocus_mode", 2),
    }

    exposure_us = camera.get("exposure_time_us", 0) or 0
    manual = camera.get("exposure_mode") == "manual" and exposure_us > 0

    if not manual:
        # Auto exposure. ExposureValue is an AE compensation control and means
        # nothing once AE is off, so it only appears on this branch.
        controls["AeEnable"] = True
        controls["ExposureValue"] = camera.get("exposure_value", 0)
        return controls

    exposure_us = int(_clamp(exposure_us,
                             limits["exposure_time_us"]["min"],
                             limits["exposure_time_us"]["max"]))

    controls["AeEnable"] = False
    controls["ExposureTime"] = exposure_us
    controls["AnalogueGain"] = _clamp(camera.get("analogue_gain", 1.0),
                                      limits["analogue_gain"]["min"],
                                      limits["analogue_gain"]["max"])

    # libcamera caps exposure at the frame duration. Without widening this, a
    # 4-second ExposureTime silently yields a normal-length exposure and a
    # plausible-looking, wrong photo. This line is the whole point of manual
    # mode working at all.
    frame_us = int(_clamp(max(exposure_us, limits["frame_duration_us"]["min"]),
                          limits["frame_duration_us"]["min"],
                          limits["frame_duration_us"]["max"]))
    controls["FrameDurationLimits"] = (frame_us, frame_us)

    return controls


def autofocus_settle_seconds(camera, has_captured, fast_mode):
    """How long to wait for focus before capturing.

    Encodes the timing reframe.py already used, plus one addition: a manual
    exposure with autofocus off has nothing to settle, so it waits not at all.
    """
    camera = camera or {}
    autofocus_mode = camera.get("autofocus_mode", 2)

    if camera.get("exposure_mode") == "manual" and autofocus_mode == 0:
        return 0.0
    if fast_mode:
        return 0.1
    if has_captured and autofocus_mode == 2:
        return 0.0
    if has_captured:
        return 0.1
    return 0.3


def describe_limits(limits):
    """One-line human summary, for the startup log.

    Nobody can verify this phase until the hardware exists, so first boot has
    to say what the sensor actually reported.
    """
    exposure = limits["exposure_time_us"]
    gain = limits["analogue_gain"]
    return (
        f"exposure {exposure['min']}-{exposure['max']} us, "
        f"gain {gain['min']}-{gain['max']}x"
    )
