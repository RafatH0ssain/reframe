"""Pure camera-control logic for reFrame.

Decides WHICH picamera2 controls to send for a given set of camera settings.
Sending them is reframe.py's job; deciding is this module's.

The split exists so this logic is testable. picamera2 cannot be installed on a
development machine, so anything living inside reframe.py can only be verified
by reading it. Everything here runs under plain unittest with no camera.

No picamera2, no FastAPI, no I/O.
"""

import logging

logger = logging.getLogger(__name__)

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


def _coerce_number(value, default):
    """Best-effort numeric coercion for values that may arrive as strings.

    /api/settings/apply does no validation before calling build_controls, so a
    numeric string (or a stray None) must not raise TypeError -- it should
    either be used as the number it represents, or fall back to a safe
    default.
    """
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
        if low > high:
            # A reversed tuple would otherwise make _clamp() collapse every
            # value to the larger number -- silently pinning every capture to
            # the sensor's maximum. Swap rather than discard: it is still
            # real sensor data, just misordered.
            logger.warning(
                "%s reported as (min=%s, max=%s) with min > max; swapping.",
                picam_key, low, high)
            low, high = high, low
        limits[our_key]["min"] = low
        limits[our_key]["max"] = high
        if len(entry) > 2 and entry[2] is not None and "default" in limits[our_key]:
            limits[our_key]["default"] = entry[2]

    return limits


def build_controls(camera, limits, fast_mode=False, include_autofocus=True):
    """Build the picamera2 control dict for these camera settings.

    fast_mode forces the auto-exposure branch regardless of the active
    recipe -- used for the startup photo, which must never block systemd's
    READY=1 behind a long manual exposure.

    include_autofocus controls whether AfMode is part of the returned dict.
    configure_camera() needs it excluded: AfMode is set separately, in its
    own try/except, so a sensor without autofocus can't make the whole
    configure() call raise and silently drop every other control.
    """
    camera = camera or {}
    controls = {"Sharpness": camera.get("sharpness", 3)}
    if include_autofocus:
        controls["AfMode"] = camera.get("autofocus_mode", 2)

    exposure_us_requested = _coerce_number(camera.get("exposure_time_us", 0), 0)
    manual = (not fast_mode
              and camera.get("exposure_mode") == "manual"
              and exposure_us_requested > 0)

    if not manual:
        if (not fast_mode
                and camera.get("exposure_mode") == "manual"
                and exposure_us_requested <= 0):
            # A recipe marked manual but carrying exposure_time_us 0 is a
            # config error. Clamping to the sensor minimum would give a
            # near-instant exposure -- a black photo. Falling back to auto
            # gives a usable one, but it's a silent surprise unless logged.
            logger.warning(
                "Manual exposure requested with exposure_time_us=%r; "
                "falling back to auto exposure.", camera.get("exposure_time_us"))
        # Auto exposure. ExposureValue is an AE compensation control and means
        # nothing once AE is off, so it only appears on this branch.
        controls["AeEnable"] = True
        controls["ExposureValue"] = camera.get("exposure_value", 0)
        return controls

    exposure_limits = limits["exposure_time_us"]
    exposure_us = _clamp(exposure_us_requested, exposure_limits["min"], exposure_limits["max"])
    if exposure_us != exposure_us_requested:
        logger.warning(
            "Requested exposure_time_us=%s clamped to %s (sensor range %s-%s us)",
            exposure_us_requested, exposure_us, exposure_limits["min"], exposure_limits["max"])
    exposure_us = int(exposure_us)

    gain_limits = limits["analogue_gain"]
    gain_requested = _coerce_number(camera.get("analogue_gain", 1.0), 1.0)
    gain = _clamp(gain_requested, gain_limits["min"], gain_limits["max"])
    if gain != gain_requested:
        logger.warning(
            "Requested analogue_gain=%s clamped to %s (sensor range %s-%s)",
            gain_requested, gain, gain_limits["min"], gain_limits["max"])

    controls["AeEnable"] = False
    controls["ExposureTime"] = exposure_us
    controls["AnalogueGain"] = gain

    # libcamera caps exposure at the frame duration. Without widening this, a
    # 4-second ExposureTime silently yields a normal-length exposure and a
    # plausible-looking, wrong photo. This line is the whole point of manual
    # mode working at all.
    #
    # Critically, the frame duration must never be clamped DOWN to the
    # sensor's reported frame-duration maximum: that maximum was very likely
    # read while the sensor was still in preview mode (see read_sensor_limits
    # callers), and a still configuration can legitimately reach durations
    # preview mode could not. When the reported ceiling is below the
    # requested exposure, we widen past it deliberately and say so.
    frame_limits = limits["frame_duration_us"]
    frame_us = max(exposure_us, frame_limits["min"])
    if frame_us > frame_limits["max"]:
        logger.warning(
            "Exposure %d us exceeds the reported frame-duration ceiling of %d us; "
            "widening FrameDurationLimits past it deliberately so the exposure "
            "is honoured instead of silently capped.",
            exposure_us, frame_limits["max"])
    frame_us = int(frame_us)
    controls["FrameDurationLimits"] = (frame_us, frame_us)

    logger.info(
        "Manual exposure controls: ExposureTime=%d us, FrameDurationLimits=(%d, %d)",
        exposure_us, frame_us, frame_us)

    return controls


def autofocus_settle_seconds(camera, has_captured, fast_mode):
    """How long to wait for focus before capturing.

    Encodes the timing reframe.py already used, plus one addition: a manual
    exposure with autofocus off has nothing to settle, so it waits not at all.

    fast_mode is checked first: the startup photo must always get the short
    startup settle, even when a manual recipe with autofocus off is active --
    otherwise the active recipe defeats fast_mode entirely.
    """
    camera = camera or {}
    autofocus_mode = camera.get("autofocus_mode", 2)

    if fast_mode:
        return 0.1
    if camera.get("exposure_mode") == "manual" and autofocus_mode == 0:
        return 0.0
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
