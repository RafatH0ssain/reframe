# Capture Programs Implementation Plan (Phase 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One shutter press can produce an exposure bracket instead of a single frame, every photo records what it was shot with, and any photo can be re-developed with a different recipe's look.

**Architecture:** A new pure `capture_programs.py` decides which frames a trigger produces and which one reaches the display — the same testability split `camera_controls.py` uses, because `reframe.py` imports picamera2 and cannot be run on a development machine. The bracket's extra frames are captured AFTER the base frame has been dispatched to the panel, so the existing single-shot path — and its latency — is untouched.

**Tech Stack:** Python 3.12 (venv), picamera2 (device only), FastAPI, `unittest`. No new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-08-30-capture-programs-design.md` — "Architecture: Capture Programs", "Per-photo provenance", "Display throughput" (Collision 2), and Phase 2 of "Implementation phasing".

## Global Constraints

- **`capture_programs.py` must be PURE.** No picamera2, no FastAPI, no file I/O, no hardware. It must import on a machine with no camera. This is the testability strategy for the phase; logic that lands in `reframe.py` instead cannot be tested before the hardware exists.
- **`SingleShot` must be behaviourally identical to today.** Same capture-to-display latency, same async display dispatch, same background save ordering. The spec makes this the regression guard for the whole phase. The safest way to honour it on an untestable file is to leave that code path alone — this plan does.
- **Interval and self-timer are Phase 3.** Do not add them here, even though the spec's program table lists `Interval`. This phase ships `SingleShot` and `Bracket` only.
- **The derived cache keeps its shape.** `settings["camera"]` and `settings["processing"]` keep every key they have, so `reframe.py` goes on reading what it always read.
- **Tests use `unittest`, invoked by explicit module name** from the repo root. `tests/` is NOT a package; `unittest discover` fails with "Start directory is not importable". Never use discover; never add `tests/__init__.py`.
- **Use `./.venv/bin/python`, never bare `python3`.** System Python is 3.9 and lacks FastAPI. The venv is Python 3.12.
- **Security:** recipe ids and names are user-supplied and reach the gallery. The established pattern is `escapeHtml` for text, `escapeAttr` for attribute values, `data-*` attributes with a delegated listener instead of inline handlers, and `encodeURIComponent` only in fetch URLs. Recipe ids are restricted server-side to `^[a-z0-9][a-z0-9-]*$`.
- **Full suite command:**
  `./.venv/bin/python -m unittest tests.test_capture_programs tests.test_camera_controls tests.test_recipes tests.test_recipe_routes tests.test_dashboard_exports tests.test_dashboard_frontend -v`

---

### Task 1: The pure program model

**Files:**
- Create: `capture_programs.py`
- Test: `tests/test_capture_programs.py`

**Interfaces:**
- Consumes: the control dict shape produced by `camera_controls.build_controls` — `{"AeEnable": bool, "ExposureValue": int, "ExposureTime": int, "AnalogueGain": float, "FrameDurationLimits": (int, int), "Sharpness": int, "AfMode": int}`, where the manual-only keys are absent in auto mode.
- Produces, importable from `capture_programs`:
  - `PlannedFrame` (frozen dataclass: `controls: dict`, `wait_before: float`, `label: str`)
  - `SingleShot()` and `Bracket(frames=3, step_ev=1.0)`, each with `id`, `plan(base_controls) -> list[PlannedFrame]`, `select_for_display(frames) -> int`, `is_continuous() -> bool`
  - `program_for(trigger: dict) -> SingleShot | Bracket`
  - `make_group_id(when=None, token=None) -> str`
  - `build_sidecar(...) -> dict`

**Why `plan()` takes the built controls rather than the raw settings.** An exposure bracket means something different in each mode: with auto exposure a stop is a change to `ExposureValue`, the AE compensation control; with manual exposure a stop is a doubling of `ExposureTime`. The program can only tell which it is by looking at `AeEnable` in the controls `camera_controls` already produced. Layering it this way keeps both modules pure and keeps the mode decision in exactly one place.

**The trap this task must avoid.** A brighter bracket frame in manual mode lengthens `ExposureTime`, and libcamera caps exposure at the frame duration — the same silent failure `camera_controls` exists to prevent. Every offset frame that lengthens the exposure must widen `FrameDurationLimits` with it, or the bright end of your bracket comes back identical to the middle.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_capture_programs.py`:

```python
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
```

- [ ] **Step 2: Run to verify the tests fail**

```bash
./.venv/bin/python -m unittest tests.test_capture_programs -v
```

Expected: `ModuleNotFoundError: No module named 'capture_programs'`.

- [ ] **Step 3: Implement `capture_programs.py`**

Create `capture_programs.py` at the repo root, beside `camera_controls.py` and `recipes.py`:

```python
"""Pure capture-program logic for reFrame.

A capture program decides which frames one shutter press produces, what each
frame overrides, and which one reaches the e-paper panel. Executing the plan is
reframe.py's job; deciding it is this module's.

The split exists so this logic is testable: picamera2 cannot be installed on a
development machine, so anything inside reframe.py can only be verified by
reading it.

No picamera2, no FastAPI, no I/O.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class PlannedFrame:
    """One frame to capture, as control overrides on top of the base controls."""
    controls: dict = field(default_factory=dict)
    wait_before: float = 0.0
    label: str = "0EV"


def _format_offset(offset):
    """Render an EV offset as a label: 0EV, -1EV, +0.5EV."""
    if offset == 0:
        return "0EV"
    text = f"{abs(offset):g}"
    return f"{'+' if offset > 0 else '-'}{text}EV"


class SingleShot:
    """One frame, exactly as the recipe asks. Today's behaviour."""

    id = "single"

    def plan(self, base_controls):
        return [PlannedFrame(controls={}, wait_before=0.0, label="0EV")]

    def select_for_display(self, frames):
        return 0

    def is_continuous(self):
        return False


class Bracket:
    """N frames stepped around the recipe's exposure."""

    id = "bracket"

    def __init__(self, frames=3, step_ev=1.0):
        self.frames = frames
        self.step_ev = step_ev

    def _offsets(self):
        half = self.frames // 2
        return [(index - half) * self.step_ev for index in range(self.frames)]

    def _overrides(self, base_controls, offset):
        if offset == 0:
            return {}

        if base_controls.get("AeEnable"):
            # Auto exposure: a stop is a change to the AE compensation control,
            # relative to whatever the recipe already asked for.
            return {"ExposureValue": base_controls.get("ExposureValue", 0) + offset}

        exposure = base_controls.get("ExposureTime")
        if exposure is None:
            # Manual mode with no exposure time is a misconfiguration handled
            # upstream; bracketing it would only multiply the confusion.
            return {}

        # Manual exposure: a stop is a doubling of exposure time.
        scaled = int(round(exposure * (2 ** offset)))

        # libcamera caps exposure at the frame duration, so a longer frame has
        # to carry a wider duration with it or it silently comes back the same
        # length as the base frame.
        base_low = base_controls.get("FrameDurationLimits", (scaled, scaled))[0]
        frame_us = max(scaled, base_low)
        return {"ExposureTime": scaled, "FrameDurationLimits": (frame_us, frame_us)}

    def plan(self, base_controls):
        base_controls = base_controls or {}
        return [
            PlannedFrame(controls=self._overrides(base_controls, offset),
                         wait_before=0.0,
                         label=_format_offset(offset))
            for offset in self._offsets()
        ]

    def select_for_display(self, frames):
        """Show the frame the recipe actually asked for.

        Auto-picking a "best" exposure by histogram would be a guess dressed as
        a decision; the offset frames wait in the gallery where there is a
        screen to decide on.
        """
        for index, frame in enumerate(frames):
            if frame.label == "0EV":
                return index
        return 0

    def is_continuous(self):
        return False


def program_for(trigger):
    """Pick the program named by the trigger settings."""
    trigger = trigger or {}
    if trigger.get("program") == "bracket":
        bracket = trigger.get("bracket") or {}
        return Bracket(frames=bracket.get("frames", 3),
                       step_ev=bracket.get("step_ev", 1.0))
    return SingleShot()


def make_group_id(when=None, token=None):
    """Identify the frames produced by one shutter press.

    Bracket siblings share this so the gallery can collapse them into one
    entry and delete them as a unit.
    """
    when = when or datetime.now(timezone.utc)
    token = token or uuid.uuid4().hex[:4]
    return f"{when:%Y%m%d-%H%M%S}-{token}"


def build_sidecar(recipe_id, recipe_name, program, group_id, frame_label,
                  resolved_controls, captured_at=None):
    """The provenance record written beside each photo."""
    captured_at = captured_at or datetime.now(timezone.utc)
    return {
        "recipe_id": recipe_id,
        "recipe_name": recipe_name,
        "program": program,
        "group_id": group_id,
        "frame_label": frame_label,
        "resolved_controls": {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in (resolved_controls or {}).items()
        },
        "captured_at": captured_at.isoformat(),
    }
```

- [ ] **Step 4: Run to verify the tests pass**

```bash
./.venv/bin/python -m unittest tests.test_capture_programs -v
```

Expected: 23 tests PASS.

- [ ] **Step 5: Confirm the module is pure**

```bash
grep -nE "^(import|from) " capture_programs.py
```

Expected: only `uuid`, `dataclasses`, and `datetime`. No picamera2, no FastAPI, no `os`, no `json`.

- [ ] **Step 6: Commit**

```bash
git add capture_programs.py tests/test_capture_programs.py
git commit -m "feat: add pure capture program model with bracket planning"
```

---

### Task 2: The trigger settings section

**Files:**
- Modify: `dashboard.py` — `validate_settings`, and `SettingsManager.__init__`'s `default_settings`
- Modify: `settings.example.json`
- Test: `tests/test_recipes.py`

**Interfaces:**
- Consumes: `capture_programs` is not needed here — this task only stores and validates.
- Produces: a `trigger` section in settings, shaped
  `{"program": "single"|"bracket", "bracket": {"frames": 3|5, "step_ev": float}}`.
  Task 3 reads it via `capture_programs.program_for(settings.get("trigger"))`.

**Why `trigger` and not `capture`:** a recipe already has a `capture` half, and reusing the word for a top-level section makes every validation path ambiguous. `trigger` says what it is — what one press of the shutter does.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_recipes.py`, in the existing `ValidationTests` class:

```python
    def test_default_trigger_is_a_single_shot(self):
        settings = _valid()
        self.assertEqual(settings["trigger"]["program"], "single")
        dashboard.validate_settings(settings)

    def test_bracket_trigger_is_accepted(self):
        settings = _valid()
        settings["trigger"] = {"program": "bracket",
                               "bracket": {"frames": 5, "step_ev": 0.5}}
        dashboard.validate_settings(settings)

    def test_unknown_program_is_rejected(self):
        settings = _valid()
        settings["trigger"] = {"program": "interval", "bracket": {"frames": 3, "step_ev": 1.0}}
        with self.assertRaises(dashboard.SettingsValidationError):
            dashboard.validate_settings(settings)

    def test_bracket_frame_count_must_be_three_or_five(self):
        for count in (2, 4, 7):
            with self.subTest(count=count):
                settings = _valid()
                settings["trigger"] = {"program": "bracket",
                                       "bracket": {"frames": count, "step_ev": 1.0}}
                with self.assertRaises(dashboard.SettingsValidationError):
                    dashboard.validate_settings(settings)

    def test_bracket_step_must_be_within_range(self):
        for step in (0.1, 3.0):
            with self.subTest(step=step):
                settings = _valid()
                settings["trigger"] = {"program": "bracket",
                                       "bracket": {"frames": 3, "step_ev": step}}
                with self.assertRaises(dashboard.SettingsValidationError):
                    dashboard.validate_settings(settings)
```

Note `_valid()` is the existing helper in that file; it builds a settings dict and runs it through `recipes.migrate_settings`. If it does not yet produce a `trigger` section, the first test fails until Step 3 adds the default — which is the point.

- [ ] **Step 2: Run to verify they fail**

```bash
./.venv/bin/python -m unittest tests.test_recipes -v
```

Expected: the five new tests FAIL; every pre-existing test still passes.

- [ ] **Step 3: Add the default**

In `SettingsManager.__init__`, add to `self.default_settings` after the `recipes` key:

```python
            "trigger": {
                "program": "single",
                "bracket": {"frames": 3, "step_ev": 1.0}
            },
```

- [ ] **Step 4: Add the validation**

In `validate_settings`, after the recipes block, add:

```python
    trigger = section(settings, "trigger", "trigger")
    if trigger.get("program", "single") not in {"single", "bracket"}:
        raise SettingsValidationError("trigger.program must be 'single' or 'bracket'")
    bracket = section(trigger, "bracket", "trigger.bracket")
    if bracket.get("frames", 3) not in {3, 5}:
        raise SettingsValidationError("trigger.bracket.frames must be 3 or 5")
    number(bracket.get("step_ev", 1.0), "trigger.bracket.step_ev", 0.3, 2.0)
```

Every field uses a default in its lookup, so a settings file written before this phase still validates — an existing install must not be bricked by an upgrade.

- [ ] **Step 5: Regenerate the example settings**

```bash
./.venv/bin/python - <<'EOF'
import json
from pathlib import Path
import dashboard

path = Path("settings.example.json")
settings = json.loads(path.read_text(encoding="utf-8"))
settings["trigger"] = {"program": "single", "bracket": {"frames": 3, "step_ev": 1.0}}
dashboard.validate_settings(settings)
path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
print("trigger:", settings["trigger"])
EOF
```

Expected: `trigger: {'program': 'single', 'bracket': {'frames': 3, 'step_ev': 1.0}}` and no validation error.

- [ ] **Step 6: Run the full suite**

```bash
./.venv/bin/python -m unittest tests.test_capture_programs tests.test_camera_controls tests.test_recipes tests.test_recipe_routes tests.test_dashboard_exports tests.test_dashboard_frontend -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add dashboard.py settings.example.json tests/test_recipes.py
git commit -m "feat: add trigger settings section for capture programs"
```

---

### Task 3: Sidecars and the bracket continuation

**Files:**
- Modify: `reframe.py` — `FileManager` (add sidecar helpers), `CameraSystem.capture_photo_api`

**Interfaces:**
- Consumes: `capture_programs.program_for`, `make_group_id`, `build_sidecar`, `PlannedFrame`; `camera_controls.build_controls`; `recipes.resolve_active`.
- Produces: a `<photo_id>.json` sidecar beside every captured photo, and extra bracket frames sharing its `group_id`. Task 4 reads those sidecars from the dashboard.

**This task has no automated tests, deliberately.** `reframe.py` imports picamera2 and cannot be imported here. Keep the edits to thin delegation — every decision already lives in `capture_programs.py` and `camera_controls.py` with tests. **If you find yourself writing a conditional that decides anything about exposure or frame selection, it belongs in a pure module with a test.**

**The ordering that protects single-shot latency.** The existing body of `capture_photo_api` captures one frame, dithers it, and dispatches it to the panel before saving anything. Do not reorder or rewrite any of that. The bracket's extra frames are captured AFTER the display dispatch, so the shutter-to-panel time is byte-for-byte the same whether or not bracketing is on.

- [ ] **Step 1: Add the import**

At the top of `reframe.py`, beside `import camera_controls`:

```python
import capture_programs
```

- [ ] **Step 2: Add sidecar helpers to `FileManager`**

Add these two methods to `FileManager`, after `save_image`:

```python
    def sidecar_path(self, photo_id):
        """Provenance file for a photo: photos/00042.json beside photos/00042.jpg."""
        return os.path.join(self.save_path, f"{photo_id}.json")

    def write_sidecar(self, photo_id, sidecar):
        """Write a photo's provenance record. Never fails a capture."""
        try:
            with open(self.sidecar_path(photo_id), "w", encoding="utf-8") as handle:
                json.dump(sidecar, handle, indent=2)
        except Exception as e:
            logging.error("Could not write sidecar for %s: %s", photo_id, e)
```

The `.json` extension is deliberately outside the `{".png", ".jpg", ".jpeg"}` set that `_find_next_photo_index` scans and that the gallery filters on, so sidecars cannot be mistaken for photos or disturb ID allocation.

- [ ] **Step 3: Extend `delete_photo` to remove the sidecar**

In `FileManager.delete_photo`, alongside the existing removals of the original and dithered files, add:

```python
        sidecar = self.sidecar_path(photo_id)
        if os.path.exists(sidecar):
            try:
                os.remove(sidecar)
            except OSError as e:
                logging.warning("Could not remove sidecar for %s: %s", photo_id, e)
```

Read the method first and match its existing error-handling style rather than pasting this verbatim if it differs.

- [ ] **Step 4: Resolve the program before anything else in the capture**

Near the top of `capture_photo_api`, before `pipeline_start`, add:

```python
            program = capture_programs.program_for(self.camera_manager.settings.get("trigger"))
            group_id = capture_programs.make_group_id()
            base_controls = camera_controls.build_controls(
                self.camera_manager.settings.get("camera", {}),
                self.camera_manager.sensor_limits,
                fast_mode=fast_mode)
```

These three names are captured by the closure in Step 5 and passed to the
worker in Step 6, so they must be bound before either.

`reframe.py` does not currently import `recipes` — add `import recipes` beside
the other local imports at the top of the file.

- [ ] **Step 5: Write a sidecar for the primary frame**

In `capture_photo_api`, inside the existing `_save_outputs` background function — after the original JPEG and dithered PNG are saved — add:

```python
                        active = recipes.resolve_active(self.camera_manager.settings)
                        self.file_manager.write_sidecar(result["photo_id"],
                            capture_programs.build_sidecar(
                                recipe_id=active.get("id", ""),
                                recipe_name=active.get("name", ""),
                                program=program.id,
                                group_id=group_id,
                                frame_label="0EV",
                                resolved_controls=base_controls))
```

This runs on the existing background save thread, so it cannot delay the panel.

- [ ] **Step 6: Dispatch the bracket after the display, then add the worker**

In `capture_photo_api`, AFTER the `save_thread.start()` line and before `return result`, add:

```python
            # Bracket frames are captured only after the base frame has been
            # dispatched to the panel, so shutter-to-display latency is
            # identical whether or not bracketing is on.
            extra_frames = [f for f in program.plan(base_controls) if f.controls]
            if extra_frames:
                threading.Thread(
                    target=self._capture_bracket_frames,
                    args=(extra_frames, base_controls, program, group_id),
                    daemon=True).start()
```

Then add the worker itself

Add this method to `CameraSystem`, after `capture_photo_api`:

```python
    def _capture_bracket_frames(self, frames, base_controls, program, group_id):
        """Capture a bracket's offset frames after the base frame is on screen.

        Runs on a background thread. Every failure is contained: a bracket that
        cannot finish must still leave the photo the user actually saw.
        """
        active = recipes.resolve_active(self.camera_manager.settings)
        for frame in frames:
            try:
                merged = dict(base_controls)
                merged.update(frame.controls)
                self.camera_manager.picam2.set_controls(merged)

                photo_path = self.file_manager.get_new_file_path(
                    SAVE_PATH, ORIGINAL_CAPTURE_EXTENSION)
                photo_id = os.path.splitext(os.path.basename(photo_path))[0]
                image = self.camera_manager.capture_image()
                image.save(photo_path, format="JPEG")

                self.file_manager.write_sidecar(photo_id,
                    capture_programs.build_sidecar(
                        recipe_id=active.get("id", ""),
                        recipe_name=active.get("name", ""),
                        program=program.id,
                        group_id=group_id,
                        frame_label=frame.label,
                        resolved_controls=merged))
                logging.info("Bracket frame %s saved as %s", frame.label, photo_id)
            except Exception as e:
                logging.error("Bracket frame %s failed: %s", frame.label, e)

        try:
            self.camera_manager.apply_camera_settings()
        except Exception as e:
            logging.error("Could not restore controls after bracket: %s", e)
```

The restore at the end matters: the offset frames leave the sensor holding a shifted exposure, and the next single press must not inherit it.

- [ ] **Step 7: Verify what you can**

```bash
./.venv/bin/python -m py_compile reframe.py && echo parses
grep -n "capture_programs\.\|write_sidecar\|_capture_bracket_frames" reframe.py
./.venv/bin/python -m unittest tests.test_capture_programs tests.test_camera_controls tests.test_recipes tests.test_recipe_routes tests.test_dashboard_exports tests.test_dashboard_frontend -v
```

Expected: it parses; the greps show the new call sites; the suite still passes. None of those tests import `reframe.py`, so this only proves the dashboard is undisturbed — which is still worth proving.

- [ ] **Step 8: Confirm the single-shot path is untouched**

```bash
git diff -U3 -- reframe.py | grep "^-" | grep -v "^---"
```

Expected: only removals that are part of the additions above (e.g. a line you replaced to insert before it). If this shows deletions inside the dither, display-dispatch, or `save_thread` region, you have altered the single-shot path and must put it back — the spec makes its latency the regression guard for this phase.

- [ ] **Step 9: Commit**

```bash
git add reframe.py
git commit -m "feat: write photo provenance sidecars and capture bracket frames"
```

---

### Task 4: Gallery provenance, grouping, and re-develop

**Files:**
- Modify: `dashboard.py` — `PhotoManager.get_all_photos`, and a new develop route beside the existing `/api/photos/{photo_id}/reprocess`
- Test: `tests/test_recipe_routes.py`

**Interfaces:**
- Consumes: the `<photo_id>.json` sidecars Task 3 writes; `recipes` for looking up a recipe's render half.
- Produces: `group_id`, `frame_label`, `recipe_name` on each gallery entry, and `POST /api/photos/{photo_id}/develop` taking `{"recipe_id": "..."}`. Task 5's UI consumes both.

**Why re-develop is nearly free.** A recipe's `render` half is exactly the set of settings `ImageProcessor.reprocess_photo_by_id` already takes. The split that made recipes worth having — capture is gone once the shutter fires, render is reapplicable forever — is what this route cashes in.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_recipe_routes.py`, inside the existing `RecipeRouteTests` class:

```python
    def _write_photo_with_sidecar(self, photo_id, group_id, frame_label):
        from PIL import Image
        photos = Path(dashboard.PHOTOS_PATH)
        photos.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4)).save(photos / f"{photo_id}.jpg", format="JPEG")
        (photos / f"{photo_id}.json").write_text(json.dumps({
            "recipe_id": "night", "recipe_name": "Night", "program": "bracket",
            "group_id": group_id, "frame_label": frame_label,
            "resolved_controls": {}, "captured_at": "2026-09-06T14:22:11+00:00",
        }), encoding="utf-8")

    def test_gallery_entries_carry_their_provenance(self):
        self._write_photo_with_sidecar("90001", "grp-1", "0EV")
        photos = dashboard.PhotoManager().get_all_photos()["photos"]
        entry = [p for p in photos if p["id"] == "90001"][0]
        self.assertEqual(entry["group_id"], "grp-1")
        self.assertEqual(entry["frame_label"], "0EV")
        self.assertEqual(entry["recipe_name"], "Night")

    def test_a_photo_without_a_sidecar_still_lists(self):
        # Every photo taken before this phase has no sidecar. The gallery must
        # not hide them or crash on them.
        from PIL import Image
        photos = Path(dashboard.PHOTOS_PATH)
        photos.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4)).save(photos / "90002.jpg", format="JPEG")
        listed = dashboard.PhotoManager().get_all_photos()["photos"]
        entry = [p for p in listed if p["id"] == "90002"][0]
        self.assertIsNone(entry["group_id"])
        self.assertIsNone(entry["frame_label"])

    def test_a_corrupt_sidecar_does_not_break_the_gallery(self):
        from PIL import Image
        photos = Path(dashboard.PHOTOS_PATH)
        photos.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4)).save(photos / "90003.jpg", format="JPEG")
        (photos / "90003.json").write_text("{not json", encoding="utf-8")
        listed = dashboard.PhotoManager().get_all_photos()["photos"]
        entry = [p for p in listed if p["id"] == "90003"][0]
        self.assertIsNone(entry["group_id"])

    def test_develop_applies_a_recipes_render_settings(self):
        self._write_photo_with_sidecar("90004", "grp-2", "0EV")
        sent = {}

        async def fake_post(path, json=None):
            sent["path"] = path
            sent["json"] = json
            return {"status": "ok"}

        with patch.object(dashboard.reframe_client, "post", fake_post):
            response = self.client.post("/api/photos/90004/develop",
                                        json={"recipe_id": "high-contrast"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(sent["path"], "/reprocess/90004")
        expected = [r for r in dashboard.settings_manager.load_settings()["recipes"]["items"]
                    if r["id"] == "high-contrast"][0]["render"]
        self.assertEqual(sent["json"]["processing_settings"], expected)

    def test_develop_rejects_an_unknown_recipe(self):
        self._write_photo_with_sidecar("90005", "grp-3", "0EV")
        response = self.client.post("/api/photos/90005/develop",
                                    json={"recipe_id": "nope"})
        self.assertEqual(response.status_code, 404)

    def test_develop_rejects_a_missing_photo(self):
        response = self.client.post("/api/photos/does-not-exist/develop",
                                    json={"recipe_id": "standard"})
        self.assertEqual(response.status_code, 404)
```

`tests/test_recipe_routes.py` already imports `json`, `Path`, `patch`, `dashboard`, and `recipes` at the top, so these tests need no new imports. Read the existing test file first — it already has fixtures for a temporary settings file and a `TestClient`, and it may already provide an async stub you should reuse rather than duplicate.

- [ ] **Step 2: Run to verify they fail**

```bash
./.venv/bin/python -m unittest tests.test_recipe_routes -v
```

Expected: the new tests FAIL — `get_all_photos` returns no `group_id` key, and the develop route 404s because it does not exist.

- [ ] **Step 3: Read sidecars in the gallery**

In `PhotoManager.get_all_photos`, inside the loop that builds `photo_info`, after the existing keys:

```python
                    sidecar = self._read_sidecar(photo_file)
                    photo_info["group_id"] = sidecar.get("group_id")
                    photo_info["frame_label"] = sidecar.get("frame_label")
                    photo_info["recipe_name"] = sidecar.get("recipe_name")
                    photo_info["program"] = sidecar.get("program")
```

And add this method to `PhotoManager`:

```python
    def _read_sidecar(self, photo_file):
        """Provenance for a photo, or an empty dict.

        Photos taken before sidecars existed have none, and a half-written file
        must not take the gallery down, so every failure returns empty.
        """
        try:
            sidecar_file = photo_file.with_suffix(".json")
            if not sidecar_file.exists():
                return {}
            return json.loads(sidecar_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
```

- [ ] **Step 4: Add the develop route**

Add beside the existing `@app.post("/api/photos/{photo_id}/reprocess")` route. LOCATE IT BY CONTENT, not by line number — earlier tasks in this plan insert code above it.

```python
@app.post("/api/photos/{photo_id}/develop")
async def develop_photo_with_recipe(photo_id: str, request: Request):
    """Re-render an existing photo with another recipe's look.

    Only the render half is reapplicable — the capture settings are gone the
    moment the shutter fired. That asymmetry is why recipes are split in two.
    """
    body = await request.json()
    recipe_id = body.get("recipe_id")

    settings = settings_manager.load_settings()
    match = [r for r in settings["recipes"]["items"] if r.get("id") == recipe_id]
    if not match:
        raise HTTPException(status_code=404, detail=f"No recipe '{recipe_id}'")

    original = Path(PHOTOS_PATH) / f"{photo_id}.jpg"
    if not original.exists():
        raise HTTPException(status_code=404, detail=f"No photo '{photo_id}'")

    try:
        result = await reframe_client.post(
            f"/reprocess/{photo_id}",
            json={"processing_settings": match[0]["render"]})
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not develop photo: {e}")

    return {"status": "success", "photo_id": photo_id, "recipe_id": recipe_id,
            "result": result}
```

Read the existing `reprocess_single_photo` route first and match how it talks to `reframe_client` — reuse its call shape rather than inventing a second convention.

- [ ] **Step 5: Run to verify they pass**

```bash
./.venv/bin/python -m unittest tests.test_recipe_routes -v
```

Expected: all pass.

- [ ] **Step 6: Run the full suite**

```bash
./.venv/bin/python -m unittest tests.test_capture_programs tests.test_camera_controls tests.test_recipes tests.test_recipe_routes tests.test_dashboard_exports tests.test_dashboard_frontend -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add dashboard.py tests/test_recipe_routes.py
git commit -m "feat: surface photo provenance in the gallery and add re-develop"
```

---

### Task 5: Program picker and gallery grouping in the UI

**Files:**
- Modify: `templates/dashboard.html`, `static/dashboard.css`, `static/dashboard.js`
- Test: `tests/test_dashboard_frontend.py`

**Interfaces:**
- Consumes: `trigger` from `/api/settings`, the `group_id` / `frame_label` / `recipe_name` fields on gallery entries, and `POST /api/photos/{id}/develop`.
- Produces: nothing later tasks depend on. This is the last task of Phase 2.

**Scope discipline:** a program picker in the settings modal, a provenance line on gallery cards, and a develop action. Not a redesign. Match the conventions already in these files.

**Indentation is load-bearing.** `static/dashboard.css` and `static/dashboard.js` were mechanically extracted from a Python string literal and are deliberately over-indented. The JS contains multi-line template literals whose embedded whitespace becomes part of the strings they emit, so reindenting an existing line changes program output. Match surrounding indentation for new lines; never touch existing ones.

**Security:** `recipe_name` and `frame_label` come from sidecar files and reach the gallery markup. Use `escapeHtml` for text and `escapeAttr` for attribute values — never build an inline handler from either.

- [ ] **Step 1: Write the failing tests**

Add to `RouteTests` in `tests/test_dashboard_frontend.py`:

```python
    def test_settings_modal_exposes_the_program_picker(self):
        body = self.client.get("/").text
        self.assertIn('id="trigger-program"', body)
        self.assertIn('id="trigger-bracket-frames"', body)
        self.assertIn('id="trigger-bracket-step"', body)

    def test_gallery_renders_provenance_safely(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("frame_label", js)
        self.assertIn("recipe_name", js)
        # Provenance comes from sidecar files on disk; it must be escaped like
        # every other user-supplied string in this file.
        self.assertNotIn("${photo.recipe_name}", js)
        self.assertNotIn("${photo.frame_label}", js)

    def test_develop_action_posts_to_the_develop_route(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("function developPhoto", js)
        self.assertIn("/develop", js)
```

- [ ] **Step 2: Run to verify they fail**

```bash
./.venv/bin/python -m unittest tests.test_dashboard_frontend -v
```

Expected: the three new tests FAIL; all pre-existing tests pass.

- [ ] **Step 3: Add the program picker markup**

In `templates/dashboard.html`, inside the settings modal and after the recipe section, matching the surrounding `settings-section` / `setting-group` / `setting-label` conventions:

```html
                <div class="settings-section settings-trigger">
                    <h3>shutter</h3>
                    <div class="setting-group">
                        <div>
                            <span class="setting-label">what one press does</span>
                            <select id="trigger-program" class="setting-input" onchange="toggleBracketFields()">
                                <option value="single">one photo</option>
                                <option value="bracket">exposure bracket</option>
                            </select>
                        </div>
                    </div>
                    <div class="setting-group bracket-field">
                        <div>
                            <span class="setting-label">frames</span>
                            <select id="trigger-bracket-frames" class="setting-input">
                                <option value="3">3</option>
                                <option value="5">5</option>
                            </select>
                        </div>
                    </div>
                    <div class="setting-group bracket-field">
                        <div>
                            <span class="setting-label">stops between frames</span>
                            <input type="number" id="trigger-bracket-step" class="setting-input" step="0.1" min="0.3" max="2.0" value="1.0">
                            <div class="setting-help">the extra frames are taken after the photo appears on screen</div>
                        </div>
                    </div>
                </div>
```

- [ ] **Step 4: Add the style**

Append to `static/dashboard.css`, matching the file's existing indentation:

```css
            .bracket-field {
                display: none;
            }

            .bracket-field.is-visible {
                display: flex;
            }

            .photo-provenance {
                font-size: 11px;
                opacity: 0.65;
                margin-top: 2px;
            }
```

`is-visible` uses `flex` because `.setting-group` is itself a flex container; `block` would silently drop its gap and alignment.

- [ ] **Step 5: Add the JavaScript**

Append to `static/dashboard.js`, matching the file's existing indentation:

```javascript
            function toggleBracketFields() {
                const bracketing = document.getElementById('trigger-program').value === 'bracket';
                document.querySelectorAll('.bracket-field').forEach(field => {
                    field.classList.toggle('is-visible', bracketing);
                });
            }

            function renderProvenance(photo) {
                if (!photo.recipe_name && !photo.frame_label) {
                    return '';
                }
                const parts = [];
                if (photo.recipe_name) {
                    parts.push(escapeHtml(photo.recipe_name));
                }
                if (photo.frame_label && photo.frame_label !== '0EV') {
                    parts.push(escapeHtml(photo.frame_label));
                }
                return `<div class="photo-provenance">${parts.join(' · ')}</div>`;
            }

            async function developPhoto(photoId, recipeId) {
                try {
                    const response = await fetch(`/api/photos/${encodeURIComponent(photoId)}/develop`, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({recipe_id: recipeId})
                    });
                    const data = await response.json();
                    if (!response.ok) {
                        alert(data.detail || 'Could not develop photo.');
                        return false;
                    }
                    await loadPhotos();
                    return true;
                } catch (error) {
                    console.error('Develop failed:', error);
                    return false;
                }
            }
```

- [ ] **Step 6: Show provenance on the gallery card**

In the photo-card template literal inside `renderPhotos` (or whichever function builds the card markup — read it first), add `${renderProvenance(photo)}` immediately after the element showing the photo id. Do not restructure the card.

- [ ] **Step 7: Wire the trigger settings into the form**

In `populateSettingsForm`, beside the other settings and ABOVE the line where `settingsFormSnapshot` is captured:

```javascript
                const trigger = settings.trigger || {};
                const bracket = trigger.bracket || {};
                document.getElementById('trigger-program').value = trigger.program || 'single';
                document.getElementById('trigger-bracket-frames').value = String(bracket.frames || 3);
                document.getElementById('trigger-bracket-step').value = bracket.step_ev || 1.0;
                toggleBracketFields();
```

Placing `toggleBracketFields()` inside `populateSettingsForm` — not only at the call sites — matters for the same reason it did for the exposure fields: this function has multiple callers and only one of them toggles afterwards.

In `saveSettings`, add to the payload it builds:

```javascript
                    trigger: {
                        program: document.getElementById('trigger-program').value,
                        bracket: {
                            frames: Number(document.getElementById('trigger-bracket-frames').value),
                            step_ev: Number(document.getElementById('trigger-bracket-step').value)
                        }
                    },
```

- [ ] **Step 8: Run the full suite**

```bash
./.venv/bin/python -m unittest tests.test_capture_programs tests.test_camera_controls tests.test_recipes tests.test_recipe_routes tests.test_dashboard_exports tests.test_dashboard_frontend -v
```

Expected: every test passes.

- [ ] **Step 9: Commit**

```bash
git add templates/dashboard.html static/dashboard.css static/dashboard.js tests/test_dashboard_frontend.py
git commit -m "feat: add shutter program picker and photo provenance to the dashboard"
```

---

## Notes for the implementer

**Why the bracket runs after the display dispatch.** The spec makes single-shot latency the regression guard for this whole phase, and `reframe.py` cannot be tested before the hardware exists — so the only reliable way to guarantee no regression is to leave that code path alone entirely. Capturing the offset frames on a background thread after `save_thread.start()` achieves that: the base frame reaches the panel on exactly the path it does today, and bracketing costs nothing until the user is already looking at the photo.

**The bracket must restore the controls when it finishes.** Offset frames leave the sensor holding a shifted exposure. Without the `apply_camera_settings()` call at the end of `_capture_bracket_frames`, the next ordinary press inherits the last bracket frame's exposure — a wrong photo with no error, which is the failure mode this project keeps having to design against.

**Sidecars must never break anything.** Every read returns `{}` on failure and every write logs and continues. A photo with no sidecar is normal — every photo taken before this phase has none — and a half-written one must not take the gallery down.

**Do not add Interval or the self-timer.** They are Phase 3. The spec's program table lists `Interval` alongside the two here, and `is_continuous()` exists for it, but a continuous program needs the timeout-monitor suspension and the battery guard that Phase 3 brings. Shipping it without those is how you get a camera that flattens its battery in a field.
