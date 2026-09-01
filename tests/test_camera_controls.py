import logging
import unittest

import camera_controls

LIMITS = {
    "exposure_time_us": {"min": 100, "max": 112_000_000, "default": 20_000},
    "analogue_gain": {"min": 1.0, "max": 16.0, "default": 1.0},
    "frame_duration_us": {"min": 100, "max": 112_000_000},
}

# A sensor whose FrameDurationLimits was read while still in preview mode:
# a realistic, narrow ceiling that must never cap a still-mode exposure.
PREVIEW_MODE_LIMITS = {
    "exposure_time_us": {"min": 100, "max": 112_000_000, "default": 20_000},
    "analogue_gain": {"min": 1.0, "max": 16.0, "default": 1.0},
    "frame_duration_us": {"min": 33_333, "max": 120_000},
}

AUTO = {"exposure_mode": "auto", "exposure_value": 1, "sharpness": 4, "autofocus_mode": 2}
MANUAL = {"exposure_mode": "manual", "exposure_time_us": 4_000_000,
          "analogue_gain": 1.5, "sharpness": 3, "autofocus_mode": 0}


class ReadSensorLimitsTests(unittest.TestCase):
    def test_reads_min_max_default_from_picamera2_shape(self):
        raw = {
            "ExposureTime": (75, 112_015_130, 20_000),
            "AnalogueGain": (1.0, 16.0, 1.0),
            "FrameDurationLimits": (33_333, 112_015_400, None),
        }
        limits = camera_controls.read_sensor_limits(raw)
        self.assertEqual(limits["exposure_time_us"]["min"], 75)
        self.assertEqual(limits["exposure_time_us"]["max"], 112_015_130)
        self.assertEqual(limits["analogue_gain"]["max"], 16.0)
        self.assertEqual(limits["frame_duration_us"]["max"], 112_015_400)

    def test_missing_controls_fall_back_per_key(self):
        limits = camera_controls.read_sensor_limits({"AnalogueGain": (1.0, 8.0, 1.0)})
        self.assertEqual(limits["analogue_gain"]["max"], 8.0)
        self.assertEqual(limits["exposure_time_us"],
                         camera_controls.DEFAULT_SENSOR_LIMITS["exposure_time_us"])

    def test_garbage_input_falls_back_instead_of_raising(self):
        # A ported camera may report a shape we do not expect. Never crash the
        # camera process over introspection.
        for raw in ({}, {"ExposureTime": None}, {"ExposureTime": (1,)}, None):
            with self.subTest(raw=raw):
                limits = camera_controls.read_sensor_limits(raw)
                self.assertEqual(limits, camera_controls.DEFAULT_SENSOR_LIMITS)

    def test_reversed_min_max_is_swapped_not_collapsed(self):
        # With low > high, _clamp() would otherwise collapse every value to
        # the larger number -- pinning every capture to the sensor maximum.
        raw = {"ExposureTime": (112_000_000, 75, 20_000)}
        with self.assertLogs("camera_controls", level="WARNING"):
            limits = camera_controls.read_sensor_limits(raw)
        self.assertEqual(limits["exposure_time_us"]["min"], 75)
        self.assertEqual(limits["exposure_time_us"]["max"], 112_000_000)


class BuildControlsAutoTests(unittest.TestCase):
    def test_auto_enables_ae_and_passes_exposure_value(self):
        controls = camera_controls.build_controls(AUTO, LIMITS)
        self.assertIs(controls["AeEnable"], True)
        self.assertEqual(controls["ExposureValue"], 1)
        self.assertEqual(controls["Sharpness"], 4)
        self.assertEqual(controls["AfMode"], 2)

    def test_auto_never_sets_manual_exposure_controls(self):
        controls = camera_controls.build_controls(AUTO, LIMITS)
        for key in ("ExposureTime", "AnalogueGain", "FrameDurationLimits"):
            self.assertNotIn(key, controls)


class BuildControlsManualTests(unittest.TestCase):
    def test_manual_disables_ae_and_sets_exposure_and_gain(self):
        controls = camera_controls.build_controls(MANUAL, LIMITS)
        self.assertIs(controls["AeEnable"], False)
        self.assertEqual(controls["ExposureTime"], 4_000_000)
        self.assertEqual(controls["AnalogueGain"], 1.5)

    def test_manual_widens_frame_duration_to_cover_the_exposure(self):
        # THE bug this module exists for: without this, libcamera caps the
        # exposure at the frame duration and silently returns a normal photo.
        controls = camera_controls.build_controls(MANUAL, LIMITS)
        low, high = controls["FrameDurationLimits"]
        self.assertGreaterEqual(low, 4_000_000)
        self.assertGreaterEqual(high, 4_000_000)

    def test_manual_omits_exposure_value_because_ae_is_off(self):
        controls = camera_controls.build_controls(MANUAL, LIMITS)
        self.assertNotIn("ExposureValue", controls)

    def test_exposure_time_is_clamped_to_sensor_maximum(self):
        camera = dict(MANUAL, exposure_time_us=900_000_000)
        controls = camera_controls.build_controls(camera, LIMITS)
        self.assertEqual(controls["ExposureTime"], LIMITS["exposure_time_us"]["max"])

    def test_gain_is_clamped_to_sensor_range(self):
        self.assertEqual(
            camera_controls.build_controls(dict(MANUAL, analogue_gain=99.0), LIMITS)["AnalogueGain"],
            LIMITS["analogue_gain"]["max"])
        self.assertEqual(
            camera_controls.build_controls(dict(MANUAL, analogue_gain=0.1), LIMITS)["AnalogueGain"],
            LIMITS["analogue_gain"]["min"])

    def test_manual_with_no_exposure_time_falls_back_to_auto(self):
        # A recipe marked manual but carrying exposure_time_us 0 is a config
        # error. Clamping to the sensor minimum would give a 1/10000s frame --
        # a black photo. Falling back to auto gives a usable one.
        with self.assertLogs("camera_controls", level="WARNING"):
            controls = camera_controls.build_controls(dict(MANUAL, exposure_time_us=0), LIMITS)
        self.assertIs(controls["AeEnable"], True)
        self.assertNotIn("ExposureTime", controls)

    def test_unknown_exposure_mode_is_treated_as_auto(self):
        controls = camera_controls.build_controls(dict(MANUAL, exposure_mode="bulb"), LIMITS)
        self.assertIs(controls["AeEnable"], True)

    def test_empty_settings_produce_a_working_auto_configuration(self):
        controls = camera_controls.build_controls({}, LIMITS)
        self.assertIs(controls["AeEnable"], True)
        self.assertIn("Sharpness", controls)
        self.assertIn("AfMode", controls)

    def test_frame_duration_is_never_clamped_below_the_exposure(self):
        # THE headline bug: exposure_us is clamped against the EXPOSURE
        # limits, but frame_us must cover it even when the sensor's reported
        # FRAME-DURATION ceiling (read in preview mode, per I7) is far below
        # the requested exposure. Clamping frame_us down to that ceiling is
        # exactly the condition that makes libcamera silently cap the
        # exposure -- turning a 4-second Night recipe into a 120ms photo.
        with self.assertLogs("camera_controls", level="WARNING"):
            controls = camera_controls.build_controls(MANUAL, PREVIEW_MODE_LIMITS)
        low, high = controls["FrameDurationLimits"]
        self.assertGreaterEqual(low, 4_000_000)
        self.assertGreaterEqual(high, 4_000_000)
        self.assertEqual(controls["ExposureTime"], 4_000_000)

    def test_exposure_time_us_numeric_string_is_coerced_not_rejected(self):
        camera = dict(MANUAL, exposure_time_us="4000000")
        controls = camera_controls.build_controls(camera, LIMITS)
        self.assertEqual(controls["ExposureTime"], 4_000_000)

    def test_analogue_gain_numeric_string_is_coerced_not_rejected(self):
        camera = dict(MANUAL, analogue_gain="2.0")
        controls = camera_controls.build_controls(camera, LIMITS)
        self.assertEqual(controls["AnalogueGain"], 2.0)

    def test_fast_mode_forces_auto_regardless_of_recipe(self):
        # The startup photo must never inherit a long manual exposure -- that
        # is what turns a boot into a systemd restart loop (Type=notify,
        # TimeoutStartSec=45, Restart=always).
        controls = camera_controls.build_controls(MANUAL, LIMITS, fast_mode=True)
        self.assertIs(controls["AeEnable"], True)
        self.assertNotIn("ExposureTime", controls)

    def test_include_autofocus_true_by_default(self):
        controls = camera_controls.build_controls(MANUAL, LIMITS)
        self.assertIn("AfMode", controls)

    def test_include_autofocus_false_omits_af_mode(self):
        # configure_camera() needs AfMode excluded from the configure()-time
        # dict: a sensor without autofocus must not make the whole configure()
        # call raise and silently drop ExposureValue/Sharpness too.
        controls = camera_controls.build_controls(AUTO, LIMITS, include_autofocus=False)
        self.assertNotIn("AfMode", controls)


class DescribeLimitsTests(unittest.TestCase):
    def test_summary_names_both_ranges(self):
        # This string is the only evidence a first boot gives about what the
        # sensor actually reported, so it has to carry both numbers.
        summary = camera_controls.describe_limits(LIMITS)
        self.assertIn("112000000", summary.replace(",", ""))
        self.assertIn("16.0", summary)
        self.assertIn("exposure", summary)
        self.assertIn("gain", summary)


class AutofocusSettleTests(unittest.TestCase):
    def test_manual_with_focus_off_skips_the_settle_entirely(self):
        self.assertEqual(
            camera_controls.autofocus_settle_seconds(MANUAL, has_captured=False, fast_mode=False),
            0.0)

    def test_fast_mode_uses_the_short_startup_settle(self):
        self.assertEqual(
            camera_controls.autofocus_settle_seconds(AUTO, has_captured=False, fast_mode=True),
            0.1)

    def test_continuous_autofocus_after_first_capture_needs_no_settle(self):
        self.assertEqual(
            camera_controls.autofocus_settle_seconds(AUTO, has_captured=True, fast_mode=False),
            0.0)

    def test_non_continuous_autofocus_after_first_capture_settles_briefly(self):
        camera = dict(AUTO, autofocus_mode=1)
        self.assertEqual(
            camera_controls.autofocus_settle_seconds(camera, has_captured=True, fast_mode=False),
            0.1)

    def test_first_capture_gets_the_full_settle(self):
        self.assertEqual(
            camera_controls.autofocus_settle_seconds(AUTO, has_captured=False, fast_mode=False),
            0.3)

    def test_fast_mode_wins_even_with_manual_af_off_recipe_active(self):
        # Previously the manual/AF-off check ran before the fast_mode check,
        # so an active manual recipe defeated fast_mode and the startup
        # capture got no settle at all.
        self.assertEqual(
            camera_controls.autofocus_settle_seconds(MANUAL, has_captured=False, fast_mode=True),
            0.1)


if __name__ == "__main__":
    unittest.main()
