# Capture Programs, Recipes, and Manual Exposure — Design

Date: 2026-08-30
Status: Approved design, pending implementation plan

## Overview

reFrame today exposes three camera settings (`exposure_value`, `sharpness`,
`autofocus_mode`) and one capture behavior (press button, take one photo,
dither it, show it). This design adds manual exposure, long exposure, a
self-timer, an intervalometer, and named film recipes — without adding
hardware, without a live viewfinder, and without changing the camera from a
one-button object.

### Goals

- Full manual exposure: shutter time and analogue gain, with auto as default.
- Long exposure / night capture, correctly held rather than silently clamped.
- Self-timer and a bounded intervalometer.
- Named recipes bundling capture and render settings, swappable per shot and
  reapplicable to past photos.
- Exposure bracketing, as the safety net that makes blind manual exposure
  usable on a camera with no preview.

### Non-goals

- No live viewfinder or MJPEG preview. The panel is full-refresh only.
- No new physical controls, no case changes, no soldering.
- No new button gestures. Single press remains the entire input vocabulary.
- No manual white balance. Auto WB is retained; recipes carry the look instead.
- No raw/DNG capture. Explicitly deferred.

## Governing constraints

These three facts drive every decision below and should be re-read before
proposing changes to this design.

1. **The display cannot preview.** `waveshare_epd/epd4in0e.py` is a Spectra 6
   panel with a single `DISPLAY_REFRESH` path and no partial-update LUT. Every
   update is a full ~20 second refresh. No adjust-and-observe loop is possible
   on the device.
2. **There is one button and it is shared with power.** `main()` reads the
   PiSugar 3 over I2C (`0x57`, register `0x02`). Presses of 2s or longer are
   consumed by PiSugar firmware for hardware shutdown and never reach the
   application. Short press is the only available input.
3. **The sensor is far more capable than the settings expose.** The IMX708
   through Picamera2 supports `ExposureTime`, `AnalogueGain`, `AeEnable`,
   `FrameDurationLimits`, `AeMeteringMode`, and `LensPosition`. Adding these is
   an adapter change, not a hardware one.

Consequence: capability is nearly free; **input and feedback are the entire
design problem**. All configuration therefore lives on the web dashboard, and
in-the-field feedback uses the Pi's onboard ACT LED, which is visible through
the translucent PLA enclosure the build guide already specifies.

## Architecture: Capture Programs

A **capture program** declares which frames a single trigger produces, with
which control overrides, over what timing, and which frame reaches the display.

```python
@dataclass(frozen=True)
class PlannedFrame:
    controls: dict       # Picamera2 control overrides for this frame
    wait_before: float   # seconds to wait before capturing this frame
    label: str           # "0EV", "-1EV", "interval_003"

class CaptureProgram(Protocol):
    id: str
    def plan(self, base_controls: dict) -> list[PlannedFrame]: ...
    def is_continuous(self) -> bool: ...

    # Used when is_continuous() is False: all frames exist, pick one.
    def select_for_display(self, frames: list[CapturedFrame]) -> int | None: ...

    # Used when is_continuous() is True: decide per frame, as it arrives.
    def should_display(self, frame_index: int, total_planned: int) -> bool: ...
```

**Display dispatch rule.** A continuous program cannot wait for all its frames
before deciding what to show, and a bracket cannot decide without them. The
executor therefore picks the method by `is_continuous()`: `True` calls
`should_display()` once per frame as it arrives; `False` calls
`select_for_display()` once after the plan completes. A program implements only
the one matching its own `is_continuous()` value.

Two things that look like programs are deliberately *not* programs:

- **Long exposure is a recipe value**, not a capture pattern. It is manual
  exposure with a large `ExposureTime`. Modelling it as a program would
  duplicate the recipe layer.
- **Self-timer is a trigger modifier**, not a program. It is a delay between
  the button press and the plan starting. Modelling it as a program would make
  a self-timed bracket impossible; as a modifier it composes with all three
  programs for free.

That leaves exactly three programs:

| Program | Frames | Display policy | Continuous |
|---|---|---|---|
| `SingleShot` | 1, no overrides | display it | no |
| `Bracket` | N at exposure offsets around the recipe value | display base (0 EV) frame | no |
| `Interval` | `max_frames` spaced `period_seconds` apart | first and final frame only | yes |

### Module placement

Programs live in a new `capture_programs.py`, not in `reframe.py` (already
80KB). `plan()` is a pure function — controls in, frame list out, no hardware
access — so every program is unit-testable on a development machine with no Pi
and no Picamera2 installed. The executor is the only component that touches the
camera.

### Integration point

`CameraSystem.capture_photo_api()` (`reframe.py:1398`) becomes a thin
orchestrator: resolve active program and recipe, request a plan, execute
frames through `CameraManager`, route results to display and background save.

**Bracket selection is deliberately not automatic in v1.** `Bracket` displays
the base (0 EV) frame — the one your recipe asked for — and the offset frames
go to the gallery as `group_id` siblings for you to swap in later. Automatic
best-exposure picking by histogram analysis is explicitly out of scope; it is
a guess dressed as a decision, and the whole point of shoot-then-decide is that
the decision happens where there is a screen.

**Regression guard:** `SingleShot` must be behaviorally identical to the
current path — same capture-to-display latency, same async display dispatch,
same background save ordering. If measured latency regresses, the refactor is
wrong.

## Recipe model

A recipe has two halves, split along the line between what can be redone after
the shutter fires and what cannot.

```jsonc
{
  "id": "night",
  "name": "Night",
  "capture": {
    "exposure_mode": "manual",      // "auto" | "manual"
    "exposure_time_us": 4000000,
    "analogue_gain": 1.5,
    "exposure_value": 0,
    "sharpness": 3,
    "autofocus_mode": 0
  },
  "render": {
    "saturation": 0.5,
    "brightness_factor": 1.0,
    "color_factor": 1.6,
    "dithering_method": "floyd_steinberg",
    "bayer_size": 2,
    "threshold_scale": 1.0
  }
}
```

`capture` is consumed at shutter time and gone. `render` can be reapplied to
any photo forever, which makes "re-develop this shot with a different recipe" a
consequence of the split rather than a separate feature —
`ImageProcessor.reprocess_photo_by_id()` (`reframe.py:915`) already does the work.

### Recipes are the source of truth

`recipes.active` names the live recipe. The existing `camera` and `processing`
settings sections are **demoted to a derived cache**, rewritten on every recipe
change and every recipe edit.

This keeps the compatibility cost near zero. Everything currently reading those
sections — `CameraManager.reload_settings()`, `apply_camera_settings()`,
`configure_camera()`, `SettingsManager.get_camera_settings()`,
`get_processing_settings()`, and the existing dashboard sliders — continues
reading the same keys and never learns that recipes exist.

**Migration:** on settings load, if no `recipes` key is present, synthesize a
`standard` recipe from the current `camera` and `processing` values and mark it
active. Existing installations and `settings.example.json` are unaffected.

**Editing the derived cache directly** (via the existing dashboard sliders)
writes through to the active recipe, so the two never drift apart.

### Built-in recipes

Four ship by default: `Standard` (current values verbatim), `Night`,
`High Contrast`, `Soft`. Users may author more. Hard cap: 12 recipes.

Built-ins carry no special status: they are editable, renameable, and
deletable like any other recipe. The only invariant is that **at least one
recipe must always exist** and `recipes.active` must always resolve to it —
deleting the last recipe, or the active one, is rejected by validation.

### Per-photo provenance

Each captured photo gets a sidecar JSON alongside it recording:

```jsonc
{
  "recipe_id": "night",
  "recipe_name": "Night",
  "program": "bracket",
  "group_id": "20260830-142211-a3f1",   // shared by bracket/interval siblings
  "frame_label": "-1EV",
  "resolved_controls": { "ExposureTime": 4000000, "AnalogueGain": 1.5 },
  "captured_at": "2026-08-30T14:22:11Z"
}
```

`group_id` is what lets the gallery collapse bracket siblings into one entry
and lets an interval run be browsed or deleted as a unit. Sidecars are plain
files; no database is introduced.

## Settings schema

Added sections. `camera` and `processing` are unchanged in shape.

```jsonc
"recipes": {
  "active": "standard",
  "items": [ /* recipe objects, max 12 */ ]
},
"trigger": {
  "program": "single",              // "single" | "bracket" | "interval"
  "self_timer_seconds": 0,          // 0 = off; composes with any program
  "bracket":  { "frames": 3, "step_ev": 1.0 },
  "interval": { "period_seconds": 300, "max_frames": 24 }
}
```

### Validation

Extends `validate_settings()` in `dashboard.py:62`, following its existing
`section` / `number` / `integer` / `boolean` / `text` helper style.

| Field | Rule |
|---|---|
| `recipes.active` | must match an existing `items[].id` |
| `recipes.items` | 1 to 12 entries; unique ids; id and name are text |
| `recipes.items[].capture.exposure_mode` | `"auto"` or `"manual"` |
| `recipes.items[].capture.exposure_time_us` | integer, within sensor-reported range; required when mode is `manual` |
| `recipes.items[].capture.analogue_gain` | number, within sensor-reported range; required when mode is `manual` |
| `trigger.program` | `"single"`, `"bracket"`, or `"interval"` |
| `trigger.self_timer_seconds` | integer 0-30 |
| `trigger.bracket.frames` | 3 or 5 |
| `trigger.bracket.step_ev` | number 0.3-2.0 |
| `trigger.interval.period_seconds` | integer 5-3600 |
| `trigger.interval.max_frames` | integer 1-500 |

Existing ranges (`exposure_value` -2..2, `sharpness` 0..10, etc.) are
unchanged and continue to apply within a recipe's `capture` half.

### Sensor-reported limits

Exposure and gain bounds are **not hardcoded**. At startup, `CameraManager`
reads `picam2.camera_controls["ExposureTime"]` and `["AnalogueGain"]` for their
real `(min, max, default)` and caches them. `GET /api/camera/limits` serves
these so dashboard sliders bound themselves to the attached sensor.

This avoids baking in an IMX708-specific figure and preserves the promise in
`docs/hardware-porting.md`: a different sensor reports different limits rather
than requiring a code change.

## Dashboard surface

### Step 0: extract the frontend (prerequisite)

`dashboard()` (`dashboard.py:827`) is a single function containing 2,231 lines
— 636 lines of CSS and 1,317 lines of JavaScript inline in a Python string,
with no linting or syntax highlighting on any of it. This design adds roughly
600 lines of new UI.

Before any feature work, extract to `templates/dashboard.html`,
`static/dashboard.css`, and `static/dashboard.js`, served via FastAPI's
`StaticFiles`. This is a **pure no-behavior-change refactor**, verified by
capturing the served HTML before and after and diffing it byte-for-byte.

### New UI

- **Camera panel** — Auto/Manual toggle; when Manual, shutter and gain sliders
  bounded by `/api/camera/limits`, with a derived EV readout.
- **Recipe manager** — list, activate, edit both halves, duplicate, delete.
- **Program picker** — Single / Bracket / Interval with their parameters, plus
  the self-timer field (which applies to all three).
- **Interval status** — armed/running state, frames captured, next frame
  countdown, and a stop control.
- **Gallery additions** — show sidecar provenance, group siblings by
  `group_id`, and a "re-develop with recipe" action.

### New API routes

Following the existing `dashboard.py` -> `reframe.py:8077` proxy pattern.

| Route | Purpose |
|---|---|
| `GET/POST /api/recipes` | list / create |
| `GET/PUT/DELETE /api/recipes/{id}` | read / update / delete |
| `POST /api/recipes/{id}/activate` | set active recipe |
| `GET /api/camera/limits` | sensor-reported control ranges |
| `POST /api/trigger` | set active program and its parameters |
| `POST /api/interval/start` | begin a bounded interval run |
| `POST /api/interval/stop` | abort a run in progress |
| `GET /api/interval/status` | run state, frames done, next frame time |
| `POST /api/photos/{id}/develop` | apply a recipe's render half; body `{"recipe_id": "..."}` |

## Collision resolutions

### 1. Auto-shutdown vs. intervalometer

`_timeout_monitor_loop` (`reframe.py:1297`) shuts the Pi down after
`auto_timeout_minutes` (default 10) of inactivity. Any interval period longer
than that terminates its own run.

**Resolution:** interval runs are always bounded by `max_frames`, so every run
has a guaranteed end. Suspend the timeout monitor for the duration of a run and
restore it on completion or abort. This is only safe because unbounded runs do
not exist; that bound is load-bearing, not cosmetic.

**Replacement guard:** suspending the timeout removes the only protection
against a flat battery. Poll `pisugar-server` on TCP 8423 (the mechanism
`/api/battery`, `dashboard.py:3392`, already uses) once per interval frame and
**abort the run below 15%**, restoring normal timeout shutdown. Without this,
a long run ends in power loss with the SD card mounted — the only failure mode
here that costs the whole camera rather than one photo.

### 2. Display throughput

At ~20s per refresh a 24-frame interval would spend 8 minutes refreshing frames
nobody is present to watch, at real cost in battery and panel cycles.

| Program | Policy |
|---|---|
| `SingleShot` | display the frame (unchanged behavior) |
| `Bracket` | display the base (0 EV) frame only; never the offset frames |
| `Interval` | display the first frame and the final frame only |

For `Interval`, "final frame" means the last frame actually captured — so an
aborted or battery-terminated run still displays where it stopped, rather than
leaving the panel on the first frame.

A non-zero `self_timer_seconds` delays the *start of the plan* once. It does
not delay each frame of a bracket or interval.

Frames not displayed are still captured, saved, and appear in the gallery.

"Is it still running?" is answered by the ACT LED (below) rather than by the
panel. Skipping work while `eink_display.is_busy()` is already the established
idiom in `main()`; this stays consistent with it.

### 3. Long exposure vs. the capture path

Three distinct problems:

**a. Silent clamping.** Setting `ExposureTime: 4000000` alone yields a ~100ms
exposure, because `FrameDurationLimits` still caps the frame duration. Manual
exposure must set `AeEnable: False`, `ExposureTime`, `AnalogueGain`, **and**
widen `FrameDurationLimits` to cover the requested exposure. This fails
silently otherwise, which is the worst available failure mode: a photo that
looks plausible and is wrong.

**b. Autofocus settle is wrong for manual.** `_settle_autofocus`
(`reframe.py:482`) sleeps and assumes autofocus is active. Under a recipe with
`autofocus_mode: 0` it must be skipped entirely.

**c. Blocking lock produces phantom captures.** The button loop
(`reframe.py:1844`) acquires `_operation_lock` with a blocking `with`
statement. During a multi-second exposure, an impatient second press blocks on
the lock and then fires the moment it releases, producing an unwanted photo.

**Fix:** use a non-blocking acquire in the button loop and drop the press if a
capture is already in flight. This is a latent bug today; long exposure makes
it reachable in ordinary use.

## ACT LED feedback

Output only. No new input, no gestures.

| State | Pattern |
|---|---|
| Idle | off (default kernel behavior restored) |
| Exposure in progress | fast blink |
| Self-timer counting down | one blink per second |
| Interval run armed | slow blink |
| Capture failed | three rapid blinks, then off |

Written via `/sys/class/leds/`. The build guide already specifies translucent
PLA so onboard status lights are visible; nothing in the repo currently uses
this channel. LED control must degrade silently to a no-op if the path is
unwritable, so the application never fails because of a status indicator.

## Implementation phasing

This design is too large for one implementation plan. It decomposes into four
phases, each independently shippable and each leaving the camera in a working
state. A phase is only started when the previous one is verified on device.

**Phase 0 — Frontend extraction.** No behavior change, no features. Extract
`dashboard()` into templates and static files; verify by byte-for-byte diff of
the served HTML. Everything after this depends on it.

**Phase 1 — Recipes and manual exposure.** The recipe model, settings
migration, derived-cache write-through, sensor-reported limits, the manual
exposure panel, and the recipe manager UI. Long exposure works at the end of
this phase, since it is only a recipe value plus the `FrameDurationLimits` fix.
No new capture patterns yet; the camera still takes one photo per press.

**Phase 2 — Capture programs.** `capture_programs.py`, the `SingleShot`
re-expression with its latency regression check, `Bracket`, per-photo sidecars,
`group_id` grouping in the gallery, and re-develop.

**Phase 3 — Time-based capture.** Self-timer, `Interval`, the timeout-monitor
suspension, the battery abort guard, interval status UI, and ACT LED feedback.
This phase carries all the concurrency risk and is deliberately last.

## Testing strategy

- **Pure unit tests, no hardware** — every `plan()` implementation, the recipe
  resolver, the settings migration, and `validate_settings()` extensions. These
  run on a development machine with no Picamera2 installed.
- **Display-policy tests** — `select_for_display()` per program, and the
  skip-when-busy behavior, against a fake display.
- **Refactor verification** — served dashboard HTML diffed byte-for-byte before
  and after the Step 0 extraction.
- **Regression** — `SingleShot` capture-to-display latency measured against the
  current implementation on device.
- **On-device only** — actual long-exposure timing, sensor-reported limits, and
  the interval/timeout interaction. These cannot be faked and must be verified
  on the Pi.

## Requires hardware verification

Items deliberately not asserted in this design, to be confirmed on device
during implementation:

1. The IMX708's actual `ExposureTime` and `AnalogueGain` ranges as reported by
   Picamera2 (the design reads them rather than assuming them).
2. Whether the sensor needs discarded frames after switching to manual exposure
   before `ExposureTime` takes effect, and how many.
3. Real capture-to-display latency for `SingleShot` before and after the
   refactor.
4. Battery drain per interval frame, which determines whether the 15% abort
   threshold is correctly placed.
