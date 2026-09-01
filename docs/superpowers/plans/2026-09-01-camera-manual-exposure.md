# Camera Manual Exposure Implementation Plan (Phase 1B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the camera actually honour manual exposure and long exposure — the recipe values Phase 1A stores but `reframe.py` currently ignores.

**Architecture:** All the decision logic — auto vs manual, clamping to sensor limits, the `FrameDurationLimits` fix, and autofocus settle timing — moves into a new pure `camera_controls.py` that never imports picamera2. `CameraManager` becomes a thin caller. That split is what makes this phase testable at all: picamera2 cannot be installed on a development machine, so anything left inside `reframe.py` is verifiable only by reading.

**Tech Stack:** Python 3.12 (venv), picamera2 (device only), FastAPI, `unittest`. No new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-08-30-capture-programs-design.md` — "Sensor-reported limits" and "Long exposure vs the capture path" (Collision 3), and Phase 1 of "Implementation phasing".

## Global Constraints

- **`camera_controls.py` must be PURE.** No picamera2, no FastAPI, no file I/O, no hardware access. It must import on a machine with no camera attached. This is the entire testability strategy for this phase — if logic ends up in `reframe.py` instead, it cannot be tested before the hardware exists.
- **Keep `CameraManager`'s public surface compatible.** `docs/hardware-porting.md` promises that `load_settings`, `reload_settings`, `apply_camera_settings`, `configure_camera`, `capture_image`, and `capture_image_with_metadata` keep working for people porting to other hardware. Do not rename or remove any of them.
- **Never let a control failure stop a capture.** The existing code applies controls one at a time inside `try/except` so an unsupported control logs and is skipped. Preserve that: a camera that cannot do manual exposure must still take photos.
- **Log what the sensor actually reported, once, at startup.** Nobody can verify this phase until hardware exists, so first boot must say what limits it found. A silent success and a silent clamp look identical otherwise.
- **Dashboard validation stays a permissive outer envelope.** `validate_settings` keeps its static bounds and does NOT try to reach the camera — the dashboard and camera are separate processes with no startup ordering guarantee. Real sensor limits bound the UI sliders and clamp at capture time.
- **Tests use `unittest`, invoked by explicit module name** from the repo root. `tests/` is NOT a package; `unittest discover` fails with "Start directory is not importable". Never use discover; never add `tests/__init__.py`.
- **Use `./.venv/bin/python`, never bare `python3`.** System Python is 3.9 and lacks FastAPI. The venv is Python 3.12.
- **Full suite command:**
  `./.venv/bin/python -m unittest tests.test_camera_controls tests.test_recipes tests.test_recipe_routes tests.test_dashboard_exports tests.test_dashboard_frontend -v`

---

### Task 1: The pure control builder

**Files:**
- Create: `camera_controls.py`
- Test: `tests/test_camera_controls.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces, importable from `camera_controls`:
  - `DEFAULT_SENSOR_LIMITS: dict`
  - `read_sensor_limits(raw_controls: dict) -> dict`
  - `build_controls(camera: dict, limits: dict) -> dict`
  - `autofocus_settle_seconds(camera: dict, has_captured: bool, fast_mode: bool) -> float`

Task 2 calls all four from `CameraManager`. Task 3 serves `read_sensor_limits`' output over HTTP.

**The bug this task exists to fix.** Setting `ExposureTime: 4000000` alone does NOT give you a 4-second exposure. libcamera caps exposure at the current frame duration, so unless `FrameDurationLimits` is widened to cover it you silently get a normal-looking, wrongly-exposed photo. It fails quietly, which is the worst way for it to fail — hence a pure function with a test that pins it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_camera_controls.py`:

```python
import unittest

import camera_controls

LIMITS = {
    "exposure_time_us": {"min": 100, "max": 112_000_000, "default": 20_000},
    "analogue_gain": {"min": 1.0, "max": 16.0, "default": 1.0},
    "frame_duration_us": {"min": 100, "max": 112_000_000},
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify the tests fail**

```bash
./.venv/bin/python -m unittest tests.test_camera_controls -v
```

Expected: `ModuleNotFoundError: No module named 'camera_controls'`.

- [ ] **Step 3: Implement `camera_controls.py`**

Create `camera_controls.py` at the repo root, beside `reframe.py` and `recipes.py`:

```python
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
```

- [ ] **Step 4: Run to verify the tests pass**

```bash
./.venv/bin/python -m unittest tests.test_camera_controls -v
```

Expected: 19 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add camera_controls.py tests/test_camera_controls.py
git commit -m "feat: add pure camera control builder with long-exposure fix"
```

---

### Task 2: Wire the control builder into CameraManager

**Files:**
- Modify: `reframe.py` — `CameraManager.__init__` (line 252), `apply_camera_settings` (line 308), `configure_camera` (line 385), `_settle_autofocus` (line 482)

**Interfaces:**
- Consumes from Task 1: `camera_controls.read_sensor_limits`, `build_controls`, `autofocus_settle_seconds`, `describe_limits`.
- Produces: `CameraManager.sensor_limits` (dict, same shape as `read_sensor_limits` returns). Task 3 serves it over HTTP.

**This task has no automated tests, deliberately.** `reframe.py` imports picamera2, which cannot be installed on a development machine, so nothing here can run until the hardware exists. That is exactly why Task 1 pulled the logic out: keep these edits to thin delegation, so there is very little that can be wrong. **If you find yourself writing a conditional here, it belongs in `camera_controls.py` with a test.**

- [ ] **Step 1: Add the import**

At the top of `reframe.py`, beside the other local imports:

```python
import camera_controls
```

- [ ] **Step 2: Read the sensor's limits once at startup**

In `CameraManager.__init__`, after `self.picam2 = Picamera2()` and BEFORE `self.configure_camera()`:

```python
        # Read the sensor's real control ranges once. Wrapped because a ported
        # camera may not expose camera_controls at all -- falling back to
        # permissive defaults is always better than failing to start.
        try:
            self.sensor_limits = camera_controls.read_sensor_limits(self.picam2.camera_controls)
        except Exception as e:
            logging.warning("Could not read sensor limits, using defaults: %s", e)
            self.sensor_limits = camera_controls.DEFAULT_SENSOR_LIMITS

        logging.info("Sensor limits: %s", camera_controls.describe_limits(self.sensor_limits))
```

That log line matters: this phase cannot be verified before hardware exists, so first boot must state what the sensor reported.

- [ ] **Step 3: Build controls from the shared logic in `apply_camera_settings`**

Replace the body that builds the `controls` dict — currently the block starting `controls = {}` through the three `if "..." in camera_settings:` branches — with:

```python
        controls = camera_controls.build_controls(camera_settings, self.sensor_limits)
```

Leave the loop below it exactly as it is. It applies controls one at a time inside `try/except` so an unsupported control logs and is skipped, and that behaviour must survive: a camera that cannot do manual exposure still has to take photos.

- [ ] **Step 4: Use the same builder in `configure_camera`**

In `configure_camera`, replace:

```python
        controls = {
            "ExposureValue": camera_settings.get("exposure_value", 0),
            "Sharpness": camera_settings.get("sharpness", 3)
        }
```

with:

```python
        controls = camera_controls.build_controls(camera_settings, self.sensor_limits)
```

Leave the rest of the method alone — the `create_still_configuration` call, the fallback to a basic configuration, the separate `AfMode` set, and `self.picam2.start()`.

Note `build_controls` already includes `AfMode`, so the explicit `set_controls({"AfMode": af_mode})` below is now redundant but harmless. Leave it: it is inside its own `try/except` and removing it is a behaviour change this task does not need.

- [ ] **Step 5: Delegate the settle timing**

Replace the body of `_settle_autofocus` with:

```python
    def _settle_autofocus(self, fast_mode=False):
        """Give autofocus a short settle window before capture."""
        self.update_activity_time()
        camera_settings = self.settings.get("camera", {})
        delay = camera_controls.autofocus_settle_seconds(
            camera_settings, self._has_captured, fast_mode)
        if delay:
            sleep(delay)
        self._has_captured = True
```

- [ ] **Step 6: Verify nothing else broke**

`reframe.py` cannot be imported without picamera2, so check it parses and that the wiring is complete:

```bash
./.venv/bin/python -m py_compile reframe.py && echo "reframe.py parses"
grep -n "camera_controls\." reframe.py
grep -c "ExposureValue" reframe.py
```

Expected: it parses; **six** lines matching `camera_controls.` (read_sensor_limits, DEFAULT_SENSOR_LIMITS, describe_limits, build_controls twice, autofocus_settle_seconds — the bare `import` line has no dot); and `ExposureValue` no longer appears in `reframe.py` at all — it now lives only in `camera_controls.py`, since it is an auto-exposure-only control.

- [ ] **Step 7: Confirm the rest of the suite is unaffected**

```bash
./.venv/bin/python -m unittest tests.test_camera_controls tests.test_recipes tests.test_recipe_routes tests.test_dashboard_exports tests.test_dashboard_frontend -v
```

Expected: all pass. None of them import `reframe.py`, so this only proves you did not break the dashboard — which is worth proving.

- [ ] **Step 8: Commit**

```bash
git add reframe.py
git commit -m "feat: honour manual exposure and long exposure in the camera adapter"
```

---

### Task 3: Serve the sensor limits to the dashboard

**Files:**
- Modify: `reframe.py` — add a route inside `_create_fastapi_routes()` (the function begins at line 1552; add beside the existing `@app.get("/api/status")` route)
- Modify: `dashboard.py` — add a proxy route beside the other camera settings routes
- Test: `tests/test_recipe_routes.py`

**Interfaces:**
- Consumes from Task 2: `CameraManager.sensor_limits`.
- Produces: `GET /api/camera/limits` on the dashboard, returning `{"exposure_time_us": {"min", "max", "default"}, "analogue_gain": {...}, "frame_duration_us": {...}}`. Task 4's UI bounds its sliders with it.

**Why the dashboard proxies rather than computing:** the dashboard and camera are separate processes with no startup ordering guarantee, and `validate_settings` must work whether or not the camera is up. So validation keeps its static permissive bounds, and these real limits only bound the UI and clamp at capture time.

- [ ] **Step 1: Add the hardware route**

Inside `_create_fastapi_routes()` in `reframe.py`, beside the existing `@app.get("/api/status")` route:

```python
    @app.get("/api/camera/limits")
    def api_camera_limits():
        if camera_system is None:
            raise HTTPException(status_code=503, detail="Camera not ready")
        return camera_system.camera_manager.sensor_limits
```

- [ ] **Step 2: Write the failing dashboard test**

Add to `tests/test_recipe_routes.py`, inside the existing `RecipeRouteTests` class:

```python
    def test_camera_limits_are_proxied_from_the_hardware_service(self):
        expected = {
            "exposure_time_us": {"min": 75, "max": 112_015_130, "default": 20_000},
            "analogue_gain": {"min": 1.0, "max": 16.0, "default": 1.0},
            "frame_duration_us": {"min": 33_333, "max": 112_015_400},
        }

        async def fake_get(path):
            self.assertEqual(path, "/camera/limits")
            return expected

        with patch.object(dashboard.reframe_client, "get", fake_get):
            response = self.client.get("/api/camera/limits")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)

    def test_camera_limits_surface_an_error_when_the_camera_is_down(self):
        async def failing_get(path):
            raise RuntimeError("camera not running")

        with patch.object(dashboard.reframe_client, "get", failing_get):
            response = self.client.get("/api/camera/limits")

        # The UI must be able to tell "camera is down" from "here are limits",
        # so this may not quietly return defaults with a 200.
        self.assertEqual(response.status_code, 502)
```

- [ ] **Step 3: Run to verify the tests fail**

```bash
./.venv/bin/python -m unittest tests.test_recipe_routes -v
```

Expected: both new tests FAIL with 404 — the dashboard route does not exist yet.

- [ ] **Step 4: Add the dashboard proxy route**

In `dashboard.py`, beside the existing `@app.get("/api/settings/camera")` route:

```python
@app.get("/api/camera/limits")
async def get_camera_limits():
    """Proxy the sensor's real control ranges from the hardware service.

    Not merged into validate_settings: the dashboard and camera are separate
    processes with no startup ordering guarantee, so settings validation must
    work whether or not the camera is up. These bound the UI instead.
    """
    try:
        return await reframe_client.get("/camera/limits")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not read camera limits: {e}")
```

- [ ] **Step 5: Run to verify the tests pass**

```bash
./.venv/bin/python -m unittest tests.test_recipe_routes -v
```

Expected: all pass, including the two new tests.

- [ ] **Step 6: Commit**

```bash
git add reframe.py dashboard.py tests/test_recipe_routes.py
git commit -m "feat: expose sensor-reported exposure and gain limits"
```

---

### Task 4: Manual exposure controls in the recipe editor

**Files:**
- Modify: `templates/dashboard.html` — add fields to the recipe section built in Phase 1A
- Modify: `static/dashboard.css`
- Modify: `static/dashboard.js`
- Test: `tests/test_dashboard_frontend.py`

**Interfaces:**
- Consumes from Task 3: `GET /api/camera/limits`.
- Produces: nothing later tasks depend on. This is the last task of Phase 1B.

**Scope discipline:** this adds three fields to the existing recipe panel. It is not a redesign. Read the surrounding markup and JS first and match the conventions already there — including the deliberate deep indentation of `static/dashboard.css` and `static/dashboard.js`, which were extracted from a Python string literal and whose multi-line template literals emit their own leading whitespace. Never reindent existing lines.

**Security:** recipe ids and names are user-supplied. Phase 1A established `escapeHtml` for text content and `escapeAttr` for attribute values, and restricted ids server-side to `^[a-z0-9][a-z0-9-]*$`. If you interpolate any user value, use those helpers — never build an inline event handler from one.

- [ ] **Step 1: Write the failing tests**

Add to `RouteTests` in `tests/test_dashboard_frontend.py`:

```python
    def test_recipe_editor_exposes_manual_exposure_fields(self):
        body = self.client.get("/").text
        self.assertIn('id="recipe-exposure-mode"', body)
        self.assertIn('id="recipe-exposure-time"', body)
        self.assertIn('id="recipe-analogue-gain"', body)

    def test_frontend_bounds_exposure_inputs_from_reported_sensor_limits(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("/api/camera/limits", js)
        self.assertIn("function loadCameraLimits", js)
        self.assertIn("function applyCameraLimits", js)

    def test_manual_fields_are_hidden_in_auto_mode(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("function toggleManualExposureFields", js)
```

- [ ] **Step 2: Run to verify they fail**

```bash
./.venv/bin/python -m unittest tests.test_dashboard_frontend -v
```

Expected: the three new tests FAIL; every pre-existing test still passes.

- [ ] **Step 3: Add the markup**

In `templates/dashboard.html`, inside the recipe section added in Phase 1A and directly above the recipe-name field, matching the surrounding `setting-group` / `setting-label` conventions:

```html
                    <div class="setting-group">
                        <div>
                            <span class="setting-label">exposure</span>
                            <select id="recipe-exposure-mode" class="setting-input" onchange="toggleManualExposureFields()">
                                <option value="auto">auto</option>
                                <option value="manual">manual</option>
                            </select>
                        </div>
                    </div>
                    <div class="setting-group manual-exposure-field">
                        <div>
                            <span class="setting-label">shutter (seconds)</span>
                            <input type="number" id="recipe-exposure-time" class="setting-input" step="0.1" min="0" value="0">
                            <div class="setting-help" id="recipe-exposure-time-range"></div>
                        </div>
                    </div>
                    <div class="setting-group manual-exposure-field">
                        <div>
                            <span class="setting-label">ISO gain</span>
                            <input type="number" id="recipe-analogue-gain" class="setting-input" step="0.1" min="1" value="1">
                            <div class="setting-help" id="recipe-analogue-gain-range"></div>
                        </div>
                    </div>
```

- [ ] **Step 4: Add the style**

Append to `static/dashboard.css`, matching the file's existing indentation:

```css
            .manual-exposure-field {
                display: none;
            }

            .manual-exposure-field.is-visible {
                display: block;
            }
```

- [ ] **Step 5: Add the JavaScript**

Append to `static/dashboard.js`, matching the file's existing indentation:

```javascript
            let cameraLimits = null;

            async function loadCameraLimits() {
                try {
                    const response = await fetch('/api/camera/limits');
                    if (!response.ok) {
                        // Camera process may be down; leave the inputs unbounded
                        // rather than inventing limits we cannot verify.
                        return;
                    }
                    cameraLimits = await response.json();
                    applyCameraLimits();
                } catch (error) {
                    console.error('Could not load camera limits:', error);
                }
            }

            function applyCameraLimits() {
                if (!cameraLimits) {
                    return;
                }
                const exposure = cameraLimits.exposure_time_us;
                const gain = cameraLimits.analogue_gain;

                const exposureInput = document.getElementById('recipe-exposure-time');
                const minSeconds = exposure.min / 1000000;
                const maxSeconds = exposure.max / 1000000;
                exposureInput.min = minSeconds.toFixed(4);
                exposureInput.max = maxSeconds.toFixed(1);
                document.getElementById('recipe-exposure-time-range').textContent =
                    `sensor supports ${minSeconds.toFixed(4)}s to ${maxSeconds.toFixed(1)}s`;

                const gainInput = document.getElementById('recipe-analogue-gain');
                gainInput.min = gain.min;
                gainInput.max = gain.max;
                document.getElementById('recipe-analogue-gain-range').textContent =
                    `sensor supports ${gain.min}x to ${gain.max}x`;
            }

            function toggleManualExposureFields() {
                const manual = document.getElementById('recipe-exposure-mode').value === 'manual';
                document.querySelectorAll('.manual-exposure-field').forEach(field => {
                    field.classList.toggle('is-visible', manual);
                });
            }
```

- [ ] **Step 6: Feed the fields into the saved recipe**

In `saveRecipe()`, replace the three `capture` fields that currently read from `settings.camera` with values from the new inputs. The seconds-to-microseconds conversion happens here:

```javascript
                        exposure_mode: document.getElementById('recipe-exposure-mode').value,
                        exposure_time_us: Math.round(
                            Number(document.getElementById('recipe-exposure-time').value) * 1000000),
                        analogue_gain: Number(document.getElementById('recipe-analogue-gain').value),
```

- [ ] **Step 7: Call the new functions when the modal opens**

In `openSettings()`, after the existing `loadRecipes()` call:

```javascript
                    await loadCameraLimits();
                    toggleManualExposureFields();
```

- [ ] **Step 8: Run the full suite**

```bash
./.venv/bin/python -m unittest tests.test_camera_controls tests.test_recipes tests.test_recipe_routes tests.test_dashboard_exports tests.test_dashboard_frontend -v
```

Expected: every test passes.

- [ ] **Step 9: Commit**

```bash
git add templates/dashboard.html static/dashboard.css static/dashboard.js tests/test_dashboard_frontend.py
git commit -m "feat: add manual exposure controls to the recipe editor"
```

---

## Notes for the implementer

**Why the logic lives outside `reframe.py`.** picamera2 cannot be installed on a development machine, so any conditional inside `reframe.py` is verifiable only by reading it — and this phase is being written before the hardware exists. Everything that makes a decision belongs in `camera_controls.py` with a test. `reframe.py`'s job in this phase is to fetch limits, call the builder, and send the result. If a task pushes you toward branching inside `CameraManager`, move that branch into `camera_controls.py` instead.

**The one bug that will waste your day if it is ever dropped.** `FrameDurationLimits` must be widened to cover a long `ExposureTime`. Without it libcamera silently caps the exposure at the current frame duration, and you get a photo that looks plausible and is wrong — no error, no warning. `test_manual_widens_frame_duration_to_cover_the_exposure` is the guard. Never weaken it.

**Nothing in this phase has run on a camera.** Neither has Phase 0 or 1A beneath it. When hardware exists, the first thing to check is the startup log line added in Task 2 Step 2: it prints the sensor's actual reported ranges. If those differ wildly from `DEFAULT_SENSOR_LIMITS`, the defaults were wrong and the UI was showing guesses — which is precisely why the log exists. Then select the built-in Night recipe, press the shutter, and confirm the capture visibly takes about four seconds rather than returning instantly.
