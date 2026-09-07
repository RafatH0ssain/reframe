import unittest
from datetime import datetime, timezone

import capture_programs

AUTO_CONTROLS = {
    "AeEnable": True, "ExposureValue": 0, "Sharpness": 3, "AfMode": 2,
}
MANUAL_CONTROLS = {
    "AeEnable": False, "ExposureTime": 1_000_000, "AnalogueGain": 1.5,
    "FrameDurationLimits": (1_000_000, 1_000_000), "Sharpness": 3, "AfMode": 0,
}


class SingleShotTests(unittest.TestCase):
    def test_plans_exactly_one_frame_with_no_overrides(self):
        frames = capture_programs.SingleShot().plan(AUTO_CONTROLS)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].controls, {})
        self.assertEqual(frames[0].wait_before, 0.0)

    def test_displays_its_only_frame(self):
        program = capture_programs.SingleShot()
        self.assertEqual(program.select_for_display(program.plan(AUTO_CONTROLS)), 0)

    def test_is_not_continuous(self):
        self.assertFalse(capture_programs.SingleShot().is_continuous())


class BracketPlanTests(unittest.TestCase):
    def test_three_frames_are_symmetric_around_zero(self):
        frames = capture_programs.Bracket(frames=3, step_ev=1.0).plan(AUTO_CONTROLS)
        self.assertEqual([f.label for f in frames], ["-1EV", "0EV", "+1EV"])

    def test_five_frames_widen_the_spread(self):
        frames = capture_programs.Bracket(frames=5, step_ev=1.0).plan(AUTO_CONTROLS)
        self.assertEqual([f.label for f in frames],
                         ["-2EV", "-1EV", "0EV", "+1EV", "+2EV"])

    def test_step_size_scales_the_offsets(self):
        frames = capture_programs.Bracket(frames=3, step_ev=0.5).plan(AUTO_CONTROLS)
        self.assertEqual([f.label for f in frames], ["-0.5EV", "0EV", "+0.5EV"])

    def test_base_frame_carries_no_overrides(self):
        frames = capture_programs.Bracket(frames=3, step_ev=1.0).plan(AUTO_CONTROLS)
        base = [f for f in frames if f.label == "0EV"][0]
        self.assertEqual(base.controls, {})

    def test_the_base_frame_is_the_one_displayed(self):
        # Shoot-then-decide: the panel shows what the recipe asked for, and the
        # offset frames wait in the gallery. Picking a "best" frame by
        # histogram would be a guess dressed as a decision.
        program = capture_programs.Bracket(frames=3, step_ev=1.0)
        frames = program.plan(AUTO_CONTROLS)
        self.assertEqual(frames[program.select_for_display(frames)].label, "0EV")


class BracketAutoModeTests(unittest.TestCase):
    def test_offsets_exposure_value_when_ae_is_on(self):
        frames = capture_programs.Bracket(frames=3, step_ev=1.0).plan(AUTO_CONTROLS)
        by_label = {f.label: f.controls for f in frames}
        self.assertEqual(by_label["-1EV"]["ExposureValue"], -1.0)
        self.assertEqual(by_label["+1EV"]["ExposureValue"], 1.0)

    def test_offsets_are_relative_to_the_recipe_not_absolute(self):
        controls = dict(AUTO_CONTROLS, ExposureValue=1)
        frames = capture_programs.Bracket(frames=3, step_ev=1.0).plan(controls)
        by_label = {f.label: f.controls for f in frames}
        self.assertEqual(by_label["-1EV"]["ExposureValue"], 0.0)
        self.assertEqual(by_label["+1EV"]["ExposureValue"], 2.0)

    def test_auto_bracket_never_touches_exposure_time(self):
        frames = capture_programs.Bracket(frames=3, step_ev=1.0).plan(AUTO_CONTROLS)
        for frame in frames:
            self.assertNotIn("ExposureTime", frame.controls)
            self.assertNotIn("FrameDurationLimits", frame.controls)


class BracketManualModeTests(unittest.TestCase):
    def test_one_stop_doubles_and_halves_the_exposure_time(self):
        frames = capture_programs.Bracket(frames=3, step_ev=1.0).plan(MANUAL_CONTROLS)
        by_label = {f.label: f.controls for f in frames}
        self.assertEqual(by_label["-1EV"]["ExposureTime"], 500_000)
        self.assertEqual(by_label["+1EV"]["ExposureTime"], 2_000_000)

    def test_a_longer_frame_widens_frame_duration_with_it(self):
        # The same trap camera_controls exists for: libcamera caps exposure at
        # the frame duration, so without this the bright end of the bracket
        # comes back identical to the middle.
        frames = capture_programs.Bracket(frames=3, step_ev=1.0).plan(MANUAL_CONTROLS)
        by_label = {f.label: f.controls for f in frames}
        low, high = by_label["+1EV"]["FrameDurationLimits"]
        self.assertGreaterEqual(low, 2_000_000)
        self.assertGreaterEqual(high, 2_000_000)

    def test_a_shorter_frame_never_narrows_frame_duration_below_the_base(self):
        frames = capture_programs.Bracket(frames=3, step_ev=1.0).plan(MANUAL_CONTROLS)
        by_label = {f.label: f.controls for f in frames}
        low, _ = by_label["-1EV"]["FrameDurationLimits"]
        self.assertGreaterEqual(low, 500_000)

    def test_manual_bracket_never_touches_exposure_value(self):
        frames = capture_programs.Bracket(frames=3, step_ev=1.0).plan(MANUAL_CONTROLS)
        for frame in frames:
            self.assertNotIn("ExposureValue", frame.controls)

    def test_manual_without_an_exposure_time_degrades_to_no_override(self):
        controls = {"AeEnable": False, "Sharpness": 3, "AfMode": 0}
        frames = capture_programs.Bracket(frames=3, step_ev=1.0).plan(controls)
        for frame in frames:
            self.assertNotIn("ExposureTime", frame.controls)


class ProgramSelectionTests(unittest.TestCase):
    def test_missing_trigger_gives_single_shot(self):
        self.assertIsInstance(capture_programs.program_for({}), capture_programs.SingleShot)
        self.assertIsInstance(capture_programs.program_for(None), capture_programs.SingleShot)

    def test_bracket_is_built_with_its_configured_parameters(self):
        program = capture_programs.program_for(
            {"program": "bracket", "bracket": {"frames": 5, "step_ev": 0.5}})
        self.assertIsInstance(program, capture_programs.Bracket)
        self.assertEqual(len(program.plan(AUTO_CONTROLS)), 5)

    def test_an_unknown_program_name_falls_back_to_single_shot(self):
        program = capture_programs.program_for({"program": "interval"})
        self.assertIsInstance(program, capture_programs.SingleShot)

    def test_fast_mode_forces_a_single_shot(self):
        # The startup photo is a "something on screen" shot; bracketing it
        # competes with boot and delays the first frame the user sees.
        program = capture_programs.program_for(
            {"program": "bracket", "bracket": {"frames": 5, "step_ev": 1.0}},
            fast_mode=True)
        self.assertIsInstance(program, capture_programs.SingleShot)

    def test_normal_mode_still_brackets(self):
        program = capture_programs.program_for(
            {"program": "bracket", "bracket": {"frames": 5, "step_ev": 1.0}})
        self.assertIsInstance(program, capture_programs.Bracket)


class GroupIdTests(unittest.TestCase):
    def test_group_id_is_stable_for_given_inputs(self):
        when = datetime(2026, 9, 6, 14, 22, 11, tzinfo=timezone.utc)
        self.assertEqual(capture_programs.make_group_id(when, "a3f1"), "20260906-142211-a3f1")

    def test_generated_ids_differ(self):
        self.assertNotEqual(capture_programs.make_group_id(), capture_programs.make_group_id())


class SidecarTests(unittest.TestCase):
    def test_sidecar_records_what_the_photo_was_shot_with(self):
        sidecar = capture_programs.build_sidecar(
            recipe_id="night", recipe_name="Night", program="bracket",
            group_id="20260906-142211-a3f1", frame_label="-1EV",
            resolved_controls={"ExposureTime": 500_000},
            captured_at=datetime(2026, 9, 6, 14, 22, 11, tzinfo=timezone.utc))
        self.assertEqual(sidecar["recipe_id"], "night")
        self.assertEqual(sidecar["recipe_name"], "Night")
        self.assertEqual(sidecar["program"], "bracket")
        self.assertEqual(sidecar["group_id"], "20260906-142211-a3f1")
        self.assertEqual(sidecar["frame_label"], "-1EV")
        self.assertEqual(sidecar["resolved_controls"], {"ExposureTime": 500_000})
        self.assertEqual(sidecar["captured_at"], "2026-09-06T14:22:11+00:00")

    def test_sidecar_is_json_serialisable(self):
        # FrameDurationLimits is a tuple; json turns tuples into lists, but a
        # set or a datetime would raise. Pin that the shape stays writable.
        import json
        sidecar = capture_programs.build_sidecar(
            recipe_id="standard", recipe_name="Standard", program="single",
            group_id="g", frame_label="0EV",
            resolved_controls={"FrameDurationLimits": (1, 2)})
        json.dumps(sidecar)


if __name__ == "__main__":
    unittest.main()
