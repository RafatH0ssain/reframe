# Recipes and Settings Layer Implementation Plan (Phase 1A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make named recipes the source of truth for camera and processing settings, with the existing `camera`/`processing` sections demoted to a derived cache, plus a recipe manager UI.

**Architecture:** A new pure-Python `recipes.py` owns the recipe model, legacy-settings migration, and bidirectional sync between recipes and the derived cache. `dashboard.py` calls into it from `SettingsManager.load_settings()` and `save_settings()`, and exposes recipe CRUD routes. Because the derived cache keeps the exact key names the camera already reads, **`reframe.py` requires no changes in this phase** — the camera goes on reading `camera` and `processing` and never learns recipes exist.

**Tech Stack:** Python 3.12 (venv), FastAPI, Starlette, `unittest`. No new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-08-30-capture-programs-design.md` — the "Recipe model" and "Settings schema" sections, and Phase 1 of "Implementation phasing". This plan covers the settings/UI half of Phase 1 only; camera-side manual exposure is Phase 1B.

## Global Constraints

- **Do not modify `reframe.py`.** If a task seems to require it, stop and report — the derived-cache design exists specifically so this phase does not touch the camera process.
- **The derived cache keeps its exact current shape.** `settings["camera"]` and `settings["processing"]` must keep every key they have today, with the same names and value types, because `reframe.py` reads them directly (`CameraManager.load_settings`, `apply_camera_settings`, `configure_camera`). Adding new keys is allowed; renaming or removing an existing key is not.
- **Write-through precedence is fixed:** if an incoming settings payload contains a `recipes` key, recipes win and the cache is regenerated from the active recipe. Otherwise `camera`/`processing` are written through into the active recipe first, then the cache is regenerated. Never apply both directions as competing writes in one save.
- **Recipe cap: 12.** At least one recipe must always exist and `recipes.active` must always resolve to an existing id. Deleting the last recipe, or the active one, is rejected.
- **Manual-exposure bounds in this phase are PROVISIONAL static values.** Phase 1B replaces them with sensor-reported limits from `picam2.camera_controls`. Never present them in the UI as authoritative sensor limits.
- **Tests use `unittest`, invoked by explicit module name** from the repo root. `tests/` is NOT a package; `unittest discover` fails with "Start directory is not importable". Never use discover; never add `tests/__init__.py`.
- **Use `./.venv/bin/python`, never bare `python3`.** System Python is 3.9 and lacks FastAPI. The venv is Python 3.12.
- **Full suite command** (grows as tasks add modules):
  `./.venv/bin/python -m unittest tests.test_dashboard_exports tests.test_dashboard_frontend tests.test_recipes -v`

---

### Task 1: Retire the Phase 0 migration proof

**Files:**
- Modify: `tests/test_dashboard_frontend.py`
- Delete: `tests/fixtures/dashboard_baseline.html`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: a `tests/test_dashboard_frontend.py` containing ONLY `RouteTests` and the module-level constants it uses (`TEMPLATE`, `CSS`, `JS`, `LINK_TAG`, `SCRIPT_TAG`). Task 6 adds tests to this same file.

**Why this is first:** `ReconstructionTests` asserts the extracted assets still reconstruct a frozen *pre-refactor* snapshot. It was the correct proof that Phase 0 changed nothing, and it is actively harmful now — the first legitimate CSS or JS edit in this phase fails it, and the only available "fix" is regenerating the fixture, at which point it proves nothing while carrying 100KB of duplicated bytes forever. Retire it deliberately rather than letting a later implementer quietly regenerate it.

- [ ] **Step 1: Record the current state before deleting anything**

```bash
./.venv/bin/python -m unittest tests.test_dashboard_frontend -v
```

Write down how many tests ran and how many are `RouteTests`. If any test already fails, STOP and report — never delete a failing test, since the failure may be real.

- [ ] **Step 2: Check what the reconstruction machinery is still used by**

```bash
grep -n "STYLE_OPEN\|STYLE_CLOSE\|SCRIPT_OPEN\|SCRIPT_CLOSE\|reconstruct\|BASELINE" tests/test_dashboard_frontend.py
```

- [ ] **Step 3: Remove it**

In `tests/test_dashboard_frontend.py` delete: the `BASELINE` constant, the `reconstruct()` function, the entire `ReconstructionTests` class, and the `STYLE_OPEN` / `STYLE_CLOSE` / `SCRIPT_OPEN` / `SCRIPT_CLOSE` constants — the last four only if Step 2 showed nothing else references them.

Keep `TEMPLATE`, `CSS`, `JS`, `LINK_TAG`, `SCRIPT_TAG` and the whole `RouteTests` class untouched.

- [ ] **Step 4: Delete the fixture**

```bash
git rm tests/fixtures/dashboard_baseline.html
```

- [ ] **Step 5: Verify only the intended tests disappeared**

```bash
./.venv/bin/python -m unittest tests.test_dashboard_frontend -v
```

Expected: every remaining test PASSES, and the count is exactly 2 fewer than Step 1 recorded. Nothing may error.

- [ ] **Step 6: Commit**

```bash
git add -A tests/
git commit -m "test: retire Phase 0 reconstruction proof and its baseline fixture"
```

---

### Task 2: The recipe model and legacy migration

**Files:**
- Create: `recipes.py`
- Test: `tests/test_recipes.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces, importable from `recipes`: `RECIPE_LIMIT: int`, `CAPTURE_KEYS: tuple`, `RENDER_KEYS: tuple`, `MANUAL_EXPOSURE_DEFAULTS: dict`, `make_standard_recipe(camera: dict, processing: dict) -> dict`, `built_in_recipes(camera: dict, processing: dict) -> list[dict]`, `migrate_settings(settings: dict, default_camera: dict, default_processing: dict) -> dict`, `resolve_active(settings: dict) -> dict`.

Task 3 adds sync functions to this module. Task 4 calls `migrate_settings` from `SettingsManager.load_settings`.

**Design notes:** A recipe is `{"id", "name", "capture", "render"}`. `capture` is consumed at shutter time and gone; `render` is reapplicable to an already-captured photo forever.

`exposure_mode`, `exposure_time_us`, and `analogue_gain` are NEW keys `reframe.py` does not read yet — Phase 1B teaches the camera to honor them. Storing them now means 1B is a pure `reframe.py` change with no second migration. They still get written into the derived `camera` cache; `reframe.py` ignores keys it does not know, so this is safe today.

`make_standard_recipe` must DERIVE values from its arguments rather than hardcoding them, so `SettingsManager.default_settings` stays the single source of truth.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_recipes.py`:

```python
import copy
import unittest

import recipes

LEGACY_CAMERA = {
    "resolution": {"width": 1200, "height": 800},
    "exposure_value": 1,
    "sharpness": 7,
    "autofocus_mode": 1,
}
LEGACY_PROCESSING = {
    "saturation": 0.9,
    "brightness_factor": 1.2,
    "color_factor": 1.5,
    "dithering_method": "ordered",
    "bayer_size": 8,
    "threshold_scale": 1.4,
}
DEFAULT_CAMERA = {
    "resolution": {"width": 1200, "height": 800},
    "exposure_value": 0,
    "sharpness": 3,
    "autofocus_mode": 2,
}
DEFAULT_PROCESSING = {
    "saturation": 0.6,
    "brightness_factor": 1.1,
    "color_factor": 1.4,
    "dithering_method": "floyd_steinberg",
    "bayer_size": 4,
    "threshold_scale": 1.0,
}


class StandardRecipeTests(unittest.TestCase):
    def test_standard_recipe_derives_values_from_supplied_defaults(self):
        recipe = recipes.make_standard_recipe(LEGACY_CAMERA, LEGACY_PROCESSING)
        self.assertEqual(recipe["id"], "standard")
        self.assertEqual(recipe["capture"]["exposure_value"], 1)
        self.assertEqual(recipe["capture"]["sharpness"], 7)
        self.assertEqual(recipe["capture"]["autofocus_mode"], 1)
        self.assertEqual(recipe["render"]["saturation"], 0.9)
        self.assertEqual(recipe["render"]["dithering_method"], "ordered")
        self.assertEqual(recipe["render"]["bayer_size"], 8)

    def test_standard_recipe_fills_absent_manual_exposure_keys(self):
        recipe = recipes.make_standard_recipe(LEGACY_CAMERA, LEGACY_PROCESSING)
        self.assertEqual(recipe["capture"]["exposure_mode"], "auto")
        self.assertEqual(recipe["capture"]["exposure_time_us"], 0)
        self.assertEqual(recipe["capture"]["analogue_gain"], 1.0)

    def test_standard_recipe_does_not_carry_resolution(self):
        # Resolution is a device property, not a look. It stays in the cache only.
        recipe = recipes.make_standard_recipe(LEGACY_CAMERA, LEGACY_PROCESSING)
        self.assertNotIn("resolution", recipe["capture"])

    def test_built_ins_are_within_cap_and_have_unique_ids(self):
        built = recipes.built_in_recipes(DEFAULT_CAMERA, DEFAULT_PROCESSING)
        ids = [r["id"] for r in built]
        self.assertLessEqual(len(built), recipes.RECIPE_LIMIT)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("standard", ids)
        self.assertIn("night", ids)

    def test_night_built_in_is_a_manual_long_exposure(self):
        built = recipes.built_in_recipes(DEFAULT_CAMERA, DEFAULT_PROCESSING)
        night = next(r for r in built if r["id"] == "night")
        self.assertEqual(night["capture"]["exposure_mode"], "manual")
        self.assertGreaterEqual(night["capture"]["exposure_time_us"], 1_000_000)
        self.assertEqual(night["capture"]["autofocus_mode"], 0)

    def test_built_ins_do_not_share_mutable_state(self):
        built = recipes.built_in_recipes(DEFAULT_CAMERA, DEFAULT_PROCESSING)
        standard = next(r for r in built if r["id"] == "standard")
        night = next(r for r in built if r["id"] == "night")
        standard["render"]["saturation"] = 0.01
        self.assertNotEqual(night["render"]["saturation"], 0.01)


class MigrationTests(unittest.TestCase):
    def test_legacy_settings_gain_a_standard_recipe_built_from_their_own_values(self):
        legacy = {"camera": copy.deepcopy(LEGACY_CAMERA),
                  "processing": copy.deepcopy(LEGACY_PROCESSING)}
        migrated = recipes.migrate_settings(legacy, DEFAULT_CAMERA, DEFAULT_PROCESSING)

        self.assertEqual(migrated["recipes"]["active"], "standard")
        standard = next(r for r in migrated["recipes"]["items"] if r["id"] == "standard")
        # The user's OWN settings become Standard -- not the shipped defaults.
        self.assertEqual(standard["capture"]["sharpness"], 7)
        self.assertEqual(standard["render"]["saturation"], 0.9)

    def test_migration_is_idempotent(self):
        legacy = {"camera": copy.deepcopy(LEGACY_CAMERA),
                  "processing": copy.deepcopy(LEGACY_PROCESSING)}
        once = recipes.migrate_settings(legacy, DEFAULT_CAMERA, DEFAULT_PROCESSING)
        twice = recipes.migrate_settings(copy.deepcopy(once), DEFAULT_CAMERA, DEFAULT_PROCESSING)
        self.assertEqual(once, twice)

    def test_existing_recipes_are_never_overwritten(self):
        existing = {
            "camera": copy.deepcopy(LEGACY_CAMERA),
            "processing": copy.deepcopy(LEGACY_PROCESSING),
            "recipes": {"active": "mine", "items": [
                {"id": "mine", "name": "Mine", "capture": {}, "render": {}}
            ]},
        }
        migrated = recipes.migrate_settings(existing, DEFAULT_CAMERA, DEFAULT_PROCESSING)
        self.assertEqual(migrated["recipes"]["active"], "mine")
        self.assertEqual([r["id"] for r in migrated["recipes"]["items"]], ["mine"])

    def test_empty_settings_still_produce_a_usable_recipe_set(self):
        migrated = recipes.migrate_settings({}, DEFAULT_CAMERA, DEFAULT_PROCESSING)
        self.assertGreaterEqual(len(migrated["recipes"]["items"]), 1)
        standard = next(r for r in migrated["recipes"]["items"] if r["id"] == "standard")
        self.assertEqual(standard["capture"]["sharpness"], 3)

    def test_a_recipes_section_with_an_empty_item_list_is_repaired(self):
        broken = {"camera": copy.deepcopy(LEGACY_CAMERA),
                  "processing": copy.deepcopy(LEGACY_PROCESSING),
                  "recipes": {"active": "standard", "items": []}}
        migrated = recipes.migrate_settings(broken, DEFAULT_CAMERA, DEFAULT_PROCESSING)
        self.assertGreaterEqual(len(migrated["recipes"]["items"]), 1)

    def test_migration_does_not_mutate_its_argument(self):
        legacy = {"camera": copy.deepcopy(LEGACY_CAMERA),
                  "processing": copy.deepcopy(LEGACY_PROCESSING)}
        snapshot = copy.deepcopy(legacy)
        recipes.migrate_settings(legacy, DEFAULT_CAMERA, DEFAULT_PROCESSING)
        self.assertEqual(legacy, snapshot)


class ResolveActiveTests(unittest.TestCase):
    def _migrated(self):
        return recipes.migrate_settings(
            {"camera": copy.deepcopy(LEGACY_CAMERA),
             "processing": copy.deepcopy(LEGACY_PROCESSING)},
            DEFAULT_CAMERA, DEFAULT_PROCESSING)

    def test_resolve_active_returns_the_named_recipe(self):
        settings = self._migrated()
        settings["recipes"]["active"] = "night"
        self.assertEqual(recipes.resolve_active(settings)["id"], "night")

    def test_resolve_active_falls_back_to_first_when_pointer_is_dangling(self):
        settings = self._migrated()
        settings["recipes"]["active"] = "does-not-exist"
        # A dangling pointer must degrade to a working camera, never raise.
        self.assertEqual(recipes.resolve_active(settings)["id"],
                         settings["recipes"]["items"][0]["id"])

    def test_resolve_active_raises_when_there_are_no_recipes_at_all(self):
        with self.assertRaises(ValueError):
            recipes.resolve_active({"recipes": {"active": "x", "items": []}})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify the tests fail**

```bash
./.venv/bin/python -m unittest tests.test_recipes -v
```

Expected: `ModuleNotFoundError: No module named 'recipes'`. That is the correct red.

- [ ] **Step 3: Implement `recipes.py`**

Create `recipes.py` at the repo root, beside `dashboard.py`, so `import recipes` resolves the same way `import dashboard` already does in the test suite:

```python
"""Recipe model for reFrame.

A recipe is a named bundle of camera and processing settings. It has two
halves, split along the line between what can be redone after the shutter
fires and what cannot:

  capture  -- consumed at shutter time and gone
  render   -- reapplicable to any already-captured photo, forever

Recipes are the source of truth. The legacy ``camera`` and ``processing``
settings sections are kept as a derived cache so reframe.py can go on reading
exactly the keys it already reads, without knowing recipes exist.

This module is pure: no file I/O, no FastAPI, no hardware.
"""

import copy

RECIPE_LIMIT = 12

CAPTURE_KEYS = (
    "exposure_mode",
    "exposure_time_us",
    "analogue_gain",
    "exposure_value",
    "sharpness",
    "autofocus_mode",
)

RENDER_KEYS = (
    "saturation",
    "brightness_factor",
    "color_factor",
    "dithering_method",
    "bayer_size",
    "threshold_scale",
)

# Defaults for the manual-exposure keys, which legacy settings files predate.
# reframe.py does not read these yet; Phase 1B teaches the camera to honor
# them. Storing them now avoids a second migration later.
MANUAL_EXPOSURE_DEFAULTS = {
    "exposure_mode": "auto",
    "exposure_time_us": 0,
    "analogue_gain": 1.0,
}


def _capture_half(camera):
    """Project a camera settings dict onto the recipe's capture keys."""
    half = {}
    for key in CAPTURE_KEYS:
        if key in camera:
            half[key] = camera[key]
        elif key in MANUAL_EXPOSURE_DEFAULTS:
            half[key] = MANUAL_EXPOSURE_DEFAULTS[key]
    return half


def _render_half(processing):
    """Project a processing settings dict onto the recipe's render keys."""
    return {key: processing[key] for key in RENDER_KEYS if key in processing}


def make_standard_recipe(camera, processing):
    """Build the Standard recipe from supplied camera/processing values.

    Values are derived from the arguments rather than hardcoded so that
    SettingsManager.default_settings stays the single source of truth.
    ``resolution`` is deliberately excluded: it is a device property, not a
    look, and lives only in the derived cache.
    """
    return {
        "id": "standard",
        "name": "Standard",
        "capture": _capture_half(camera or {}),
        "render": _render_half(processing or {}),
    }


def built_in_recipes(camera, processing):
    """The recipes a fresh install ships with, Standard first."""
    standard = make_standard_recipe(camera, processing)

    night = copy.deepcopy(standard)
    night.update(id="night", name="Night")
    night["capture"].update(
        exposure_mode="manual",
        exposure_time_us=4_000_000,
        analogue_gain=1.5,
        autofocus_mode=0,
    )
    night["render"].update(saturation=0.5, brightness_factor=1.0, color_factor=1.6)

    contrast = copy.deepcopy(standard)
    contrast.update(id="high-contrast", name="High Contrast")
    contrast["capture"].update(sharpness=5)
    contrast["render"].update(saturation=0.7, brightness_factor=1.0,
                              color_factor=1.8, threshold_scale=1.3)

    soft = copy.deepcopy(standard)
    soft.update(id="soft", name="Soft")
    soft["capture"].update(sharpness=1)
    soft["render"].update(saturation=0.5, brightness_factor=1.15,
                          color_factor=1.1, threshold_scale=0.9)

    return [standard, night, contrast, soft]


def migrate_settings(settings, default_camera, default_processing):
    """Add a recipes section to settings that predate one. Idempotent.

    A settings file written before recipes existed has its OWN camera and
    processing values promoted into Standard -- not the shipped defaults --
    so upgrading never silently changes how the camera shoots.

    Returns a new dict; the argument is not mutated.
    """
    migrated = copy.deepcopy(settings) if settings else {}

    existing = migrated.get("recipes")
    if isinstance(existing, dict) and existing.get("items"):
        return migrated

    camera = migrated.get("camera") or default_camera
    processing = migrated.get("processing") or default_processing

    migrated["recipes"] = {
        "active": "standard",
        "items": built_in_recipes(camera, processing),
    }
    return migrated


def resolve_active(settings):
    """Return the active recipe dict.

    A dangling ``active`` pointer degrades to the first recipe rather than
    raising: a camera with a confused pointer must still take photos.
    """
    section = settings.get("recipes") or {}
    items = section.get("items") or []
    if not items:
        raise ValueError("settings contain no recipes")

    active_id = section.get("active")
    for recipe in items:
        if recipe.get("id") == active_id:
            return recipe
    return items[0]
```

- [ ] **Step 4: Run to verify the tests pass**

```bash
./.venv/bin/python -m unittest tests.test_recipes -v
```

Expected: 15 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add recipes.py tests/test_recipes.py
git commit -m "feat: add recipe model and legacy settings migration"
```

---

### Task 3: Bidirectional sync between recipes and the derived cache

**Files:**
- Modify: `recipes.py`
- Modify: `tests/test_recipes.py`

**Interfaces:**
- Consumes from Task 2: `CAPTURE_KEYS`, `RENDER_KEYS`, `resolve_active`.
- Produces, importable from `recipes`: `apply_active_to_cache(settings: dict) -> dict`, `write_cache_into_active(settings: dict) -> dict`, `sync(settings: dict, recipes_changed: bool) -> dict`.

Task 4 calls `sync()` from `SettingsManager.save_settings`.

**The precedence rule — implement exactly this:** `sync(settings, recipes_changed)` picks the direction. `True` means the payload edited recipes, so the recipe wins: run `apply_active_to_cache`. `False` means the payload edited the cache through the existing settings form, so the cache wins: run `write_cache_into_active`, then `apply_active_to_cache` to normalize. The second call in the `False` branch is a normalization, not a competing write.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_recipes.py`, above the `if __name__` block:

```python
class SyncTests(unittest.TestCase):
    def _settings(self):
        return recipes.migrate_settings(
            {"camera": copy.deepcopy(LEGACY_CAMERA),
             "processing": copy.deepcopy(LEGACY_PROCESSING)},
            DEFAULT_CAMERA, DEFAULT_PROCESSING)

    def test_activating_a_recipe_rewrites_the_derived_cache(self):
        settings = self._settings()
        settings["recipes"]["active"] = "night"
        result = recipes.apply_active_to_cache(settings)

        self.assertEqual(result["camera"]["exposure_mode"], "manual")
        self.assertEqual(result["camera"]["exposure_time_us"], 4_000_000)
        self.assertEqual(result["camera"]["autofocus_mode"], 0)
        self.assertEqual(result["processing"]["color_factor"], 1.6)

    def test_applying_to_cache_preserves_non_recipe_camera_keys(self):
        # resolution is not a recipe key and must survive untouched.
        settings = self._settings()
        settings["recipes"]["active"] = "night"
        result = recipes.apply_active_to_cache(settings)
        self.assertEqual(result["camera"]["resolution"], {"width": 1200, "height": 800})

    def test_editing_the_cache_writes_through_to_the_active_recipe(self):
        settings = self._settings()
        settings["processing"]["saturation"] = 0.25
        settings["camera"]["sharpness"] = 9
        result = recipes.write_cache_into_active(settings)

        active = recipes.resolve_active(result)
        self.assertEqual(active["render"]["saturation"], 0.25)
        self.assertEqual(active["capture"]["sharpness"], 9)

    def test_write_through_touches_only_the_active_recipe(self):
        settings = self._settings()
        settings["processing"]["saturation"] = 0.25
        result = recipes.write_cache_into_active(settings)

        night = next(r for r in result["recipes"]["items"] if r["id"] == "night")
        self.assertEqual(night["render"]["saturation"], 0.5)

    def test_sync_with_recipes_changed_lets_the_recipe_win(self):
        settings = self._settings()
        settings["recipes"]["active"] = "night"
        settings["processing"]["saturation"] = 0.99  # stale cache value
        result = recipes.sync(settings, recipes_changed=True)
        self.assertEqual(result["processing"]["saturation"], 0.5)

    def test_sync_without_recipes_changed_lets_the_cache_win(self):
        settings = self._settings()
        settings["processing"]["saturation"] = 0.99
        result = recipes.sync(settings, recipes_changed=False)
        self.assertEqual(result["processing"]["saturation"], 0.99)
        self.assertEqual(recipes.resolve_active(result)["render"]["saturation"], 0.99)

    def test_sync_is_idempotent_in_both_directions(self):
        for changed in (True, False):
            with self.subTest(recipes_changed=changed):
                settings = self._settings()
                once = recipes.sync(settings, recipes_changed=changed)
                twice = recipes.sync(copy.deepcopy(once), recipes_changed=changed)
                self.assertEqual(once, twice)

    def test_sync_does_not_mutate_its_argument(self):
        settings = self._settings()
        snapshot = copy.deepcopy(settings)
        recipes.sync(settings, recipes_changed=True)
        self.assertEqual(settings, snapshot)
```

- [ ] **Step 2: Run to verify the new tests fail**

```bash
./.venv/bin/python -m unittest tests.test_recipes -v
```

Expected: the 15 Task 2 tests still PASS; the 8 new `SyncTests` FAIL with `AttributeError: module 'recipes' has no attribute 'apply_active_to_cache'`.

- [ ] **Step 3: Implement the sync functions**

Append to `recipes.py`:

```python
def apply_active_to_cache(settings):
    """Regenerate the derived camera/processing cache from the active recipe.

    Only recipe keys are overwritten. Keys the recipe does not own -- notably
    camera.resolution -- are left exactly as they were, because reframe.py
    reads them and they are device properties rather than part of a look.

    Returns a new dict; the argument is not mutated.
    """
    result = copy.deepcopy(settings)
    active = resolve_active(result)

    camera = result.setdefault("camera", {})
    for key in CAPTURE_KEYS:
        if key in active.get("capture", {}):
            camera[key] = active["capture"][key]

    processing = result.setdefault("processing", {})
    for key in RENDER_KEYS:
        if key in active.get("render", {}):
            processing[key] = active["render"][key]

    return result


def write_cache_into_active(settings):
    """Fold direct edits of camera/processing back into the active recipe.

    The existing settings form edits the cache directly. Without this, the
    selected recipe would silently drift out of agreement with what the
    camera is actually doing.

    Returns a new dict; the argument is not mutated.
    """
    result = copy.deepcopy(settings)
    active = resolve_active(result)

    camera = result.get("camera", {})
    capture = active.setdefault("capture", {})
    for key in CAPTURE_KEYS:
        if key in camera:
            capture[key] = camera[key]

    processing = result.get("processing", {})
    render = active.setdefault("render", {})
    for key in RENDER_KEYS:
        if key in processing:
            render[key] = processing[key]

    return result


def sync(settings, recipes_changed):
    """Reconcile recipes and the derived cache in one fixed direction.

    ``recipes_changed`` says which side the incoming payload edited. Recipes
    win when they were the thing edited; otherwise the cache wins and is
    folded back into the active recipe. Applying both directions as competing
    writes would make the result depend on ordering, so it never happens.
    """
    if recipes_changed:
        return apply_active_to_cache(settings)
    return apply_active_to_cache(write_cache_into_active(settings))
```

- [ ] **Step 4: Run to verify all tests pass**

```bash
./.venv/bin/python -m unittest tests.test_recipes -v
```

Expected: 23 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add recipes.py tests/test_recipes.py
git commit -m "feat: sync recipes with the derived camera and processing cache"
```

---

### Task 4: Validation and SettingsManager integration

**Files:**
- Modify: `dashboard.py` — `validate_settings()` (starts around line 62), `SettingsManager.default_settings` (around line 143), `SettingsManager.load_settings()` (around line 185), `SettingsManager.save_settings()` (around line 204)
- Modify: `settings.example.json`
- Test: `tests/test_recipes.py`

**Interfaces:**
- Consumes from Tasks 2-3: `recipes.migrate_settings`, `recipes.sync`, `recipes.built_in_recipes`, `recipes.RECIPE_LIMIT`, `recipes.CAPTURE_KEYS`, `recipes.RENDER_KEYS`.
- Produces: a `SettingsManager` whose `load_settings()` returns migrated settings with a `recipes` section, and whose `save_settings()` keeps recipes and the cache in agreement. Task 5's routes rely on both.

**Provisional bounds** (Phase 1B replaces these with sensor-reported values):
- `exposure_time_us`: integer, 0 to 200000000 (200s). 0 means "unused", valid only when `exposure_mode` is `"auto"`.
- `analogue_gain`: number, 1.0 to 16.0.

- [ ] **Step 1: Write the failing tests**

Add these three imports to the TOP of `tests/test_recipes.py`, beside the existing `import copy` / `import unittest` / `import recipes` lines — not mid-file:

```python
import json
import tempfile
from pathlib import Path

import dashboard
```

Then append the test classes above the `if __name__` block:

```python
class ValidationTests(unittest.TestCase):
    def _valid(self):
        base = {
            "camera": copy.deepcopy(DEFAULT_CAMERA),
            "processing": copy.deepcopy(DEFAULT_PROCESSING),
            "display": {"auto_display": True, "display_timeout": 0},
            "system": {"auto_refresh_interval": 30, "auto_timeout_minutes": 10,
                       "auto_timeout_enabled": True,
                       "show_dashboard_qr_on_wifi_connect": True, "camera_name": ""},
            "exports": {"upscale_dithered_2x": False},
            "extensions": {"arena": {"enabled": False, "channel": "", "access_token": ""}},
        }
        return recipes.migrate_settings(base, DEFAULT_CAMERA, DEFAULT_PROCESSING)

    def test_migrated_settings_validate(self):
        dashboard.validate_settings(self._valid())

    def test_active_must_name_an_existing_recipe(self):
        settings = self._valid()
        settings["recipes"]["active"] = "ghost"
        with self.assertRaises(dashboard.SettingsValidationError):
            dashboard.validate_settings(settings)

    def test_empty_recipe_list_is_rejected(self):
        settings = self._valid()
        settings["recipes"]["items"] = []
        with self.assertRaises(dashboard.SettingsValidationError):
            dashboard.validate_settings(settings)

    def test_duplicate_recipe_ids_are_rejected(self):
        settings = self._valid()
        clone = copy.deepcopy(settings["recipes"]["items"][0])
        settings["recipes"]["items"].append(clone)
        with self.assertRaises(dashboard.SettingsValidationError):
            dashboard.validate_settings(settings)

    def test_exceeding_the_recipe_cap_is_rejected(self):
        settings = self._valid()
        base = settings["recipes"]["items"][0]
        while len(settings["recipes"]["items"]) <= recipes.RECIPE_LIMIT:
            extra = copy.deepcopy(base)
            extra["id"] = f"extra-{len(settings['recipes']['items'])}"
            settings["recipes"]["items"].append(extra)
        with self.assertRaises(dashboard.SettingsValidationError):
            dashboard.validate_settings(settings)

    def test_bad_exposure_mode_is_rejected(self):
        settings = self._valid()
        settings["recipes"]["items"][0]["capture"]["exposure_mode"] = "aperture-priority"
        with self.assertRaises(dashboard.SettingsValidationError):
            dashboard.validate_settings(settings)

    def test_out_of_range_exposure_time_is_rejected(self):
        settings = self._valid()
        settings["recipes"]["items"][0]["capture"]["exposure_time_us"] = 999_000_000
        with self.assertRaises(dashboard.SettingsValidationError):
            dashboard.validate_settings(settings)

    def test_out_of_range_analogue_gain_is_rejected(self):
        settings = self._valid()
        settings["recipes"]["items"][0]["capture"]["analogue_gain"] = 99.0
        with self.assertRaises(dashboard.SettingsValidationError):
            dashboard.validate_settings(settings)

    def test_render_half_is_validated_like_processing(self):
        settings = self._valid()
        settings["recipes"]["items"][0]["render"]["dithering_method"] = "halftone"
        with self.assertRaises(dashboard.SettingsValidationError):
            dashboard.validate_settings(settings)


class SettingsManagerRecipeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "settings.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _manager_with(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        return dashboard.SettingsManager(str(self.path))

    def test_loading_a_legacy_file_migrates_it_in_memory(self):
        manager = self._manager_with({
            "camera": copy.deepcopy(LEGACY_CAMERA),
            "processing": copy.deepcopy(LEGACY_PROCESSING),
        })
        loaded = manager.load_settings()
        self.assertIn("recipes", loaded)
        standard = next(r for r in loaded["recipes"]["items"] if r["id"] == "standard")
        self.assertEqual(standard["capture"]["sharpness"], 7)

    def test_saving_an_activation_rewrites_the_derived_cache(self):
        manager = self._manager_with({
            "camera": copy.deepcopy(DEFAULT_CAMERA),
            "processing": copy.deepcopy(DEFAULT_PROCESSING),
        })
        loaded = manager.load_settings()
        loaded["recipes"]["active"] = "night"
        manager.save_settings({"recipes": loaded["recipes"]})

        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["camera"]["exposure_mode"], "manual")
        self.assertEqual(stored["processing"]["color_factor"], 1.6)

    def test_saving_a_slider_edit_writes_through_to_the_active_recipe(self):
        manager = self._manager_with({
            "camera": copy.deepcopy(DEFAULT_CAMERA),
            "processing": copy.deepcopy(DEFAULT_PROCESSING),
        })
        manager.load_settings()
        manager.save_settings({"processing": {"saturation": 0.33}})

        stored = json.loads(self.path.read_text(encoding="utf-8"))
        active = next(r for r in stored["recipes"]["items"]
                      if r["id"] == stored["recipes"]["active"])
        self.assertEqual(active["render"]["saturation"], 0.33)
        self.assertEqual(stored["processing"]["saturation"], 0.33)
```

- [ ] **Step 2: Run to verify the new tests fail**

```bash
./.venv/bin/python -m unittest tests.test_recipes -v
```

Expected: the 23 earlier tests PASS; the 12 new tests FAIL (validation does not know about recipes, and `SettingsManager` does not migrate).

- [ ] **Step 3: Add the recipe validation rules**

In `dashboard.py`, add `import recipes` beside the existing imports. Then, inside `validate_settings()`, after the `extensions` block and before the function ends, add:

```python
    recipes_section = section(settings, "recipes", "recipes")
    items = recipes_section.get("items")
    if not isinstance(items, list) or not items:
        raise SettingsValidationError("recipes.items must be a non-empty list")
    if len(items) > recipes.RECIPE_LIMIT:
        raise SettingsValidationError(
            f"recipes.items must contain {recipes.RECIPE_LIMIT} recipes or fewer")

    seen_ids = set()
    for index, recipe in enumerate(items):
        path = f"recipes.items[{index}]"
        if not isinstance(recipe, dict):
            raise SettingsValidationError(f"{path} must be an object")
        text(recipe.get("id"), f"{path}.id", 64)
        text(recipe.get("name"), f"{path}.name", 80)
        if not recipe.get("id"):
            raise SettingsValidationError(f"{path}.id must not be empty")
        if recipe["id"] in seen_ids:
            raise SettingsValidationError(f"{path}.id duplicates an earlier recipe id")
        seen_ids.add(recipe["id"])

        capture = section(recipe, "capture", f"{path}.capture")
        if capture.get("exposure_mode") not in {"auto", "manual"}:
            raise SettingsValidationError(f"{path}.capture.exposure_mode must be auto or manual")
        # Provisional bounds. Phase 1B replaces these with sensor-reported
        # limits read from picam2.camera_controls.
        integer(capture.get("exposure_time_us"), f"{path}.capture.exposure_time_us", 0, 200000000)
        number(capture.get("analogue_gain"), f"{path}.capture.analogue_gain", 1.0, 16.0)
        number(capture.get("exposure_value"), f"{path}.capture.exposure_value", -2, 2)
        number(capture.get("sharpness"), f"{path}.capture.sharpness", 0, 10)
        if capture.get("autofocus_mode") not in {0, 1, 2}:
            raise SettingsValidationError(f"{path}.capture.autofocus_mode must be 0, 1, or 2")

        render = section(recipe, "render", f"{path}.render")
        number(render.get("saturation"), f"{path}.render.saturation", 0, 2)
        number(render.get("brightness_factor"), f"{path}.render.brightness_factor", 0.1, 3)
        number(render.get("color_factor"), f"{path}.render.color_factor", 0.1, 3)
        if render.get("dithering_method") not in {"floyd_steinberg", "ordered"}:
            raise SettingsValidationError(f"{path}.render.dithering_method is unsupported")
        if render.get("bayer_size") not in {2, 4, 8}:
            raise SettingsValidationError(f"{path}.render.bayer_size must be 2, 4, or 8")
        number(render.get("threshold_scale"), f"{path}.render.threshold_scale", 0.1, 2)

    if recipes_section.get("active") not in seen_ids:
        raise SettingsValidationError("recipes.active must name an existing recipe id")
```

Also extend the existing `camera` validation so the derived cache's new keys are accepted. Immediately after the existing `camera.autofocus_mode` check, add:

```python
    if camera.get("exposure_mode", "auto") not in {"auto", "manual"}:
        raise SettingsValidationError("camera.exposure_mode must be auto or manual")
    integer(camera.get("exposure_time_us", 0), "camera.exposure_time_us", 0, 200000000)
    number(camera.get("analogue_gain", 1.0), "camera.analogue_gain", 1.0, 16.0)
```

- [ ] **Step 4: Wire the migration and sync into SettingsManager**

In `SettingsManager.__init__`, add a `recipes` key to `self.default_settings`, placed after `processing`:

```python
            "recipes": {
                "active": "standard",
                "items": []
            },
```

The empty list is deliberate: `_deep_merge` replaces lists wholesale, so leaving it empty means a real file's recipes always win, and `migrate_settings` fills it for files that have none.

Replace the body of `load_settings()` with:

```python
    def load_settings(self) -> Dict[str, Any]:
        """Load settings from JSON file, migrating pre-recipe files in memory."""
        try:
            with open(self.settings_path, 'r') as f:
                settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            settings = copy.deepcopy(self.default_settings)

        # Migrate BEFORE merging with defaults: the migration needs to see
        # whether the file itself had recipes, and must promote the user's own
        # camera/processing values into Standard rather than the shipped ones.
        settings = recipes.migrate_settings(
            settings,
            self.default_settings["camera"],
            self.default_settings["processing"],
        )
        return self._merge_with_defaults(settings)
```

Add `import copy` to `dashboard.py`'s imports if it is not already present.

In `save_settings()`, replace the line `merged_settings = self._deep_merge(current_settings, settings)` with:

```python
            merged_settings = self._deep_merge(current_settings, settings)
            merged_settings = recipes.sync(
                merged_settings,
                recipes_changed="recipes" in settings,
            )
```

- [ ] **Step 5: Update `settings.example.json`**

Add a `recipes` key after `processing`, so fresh installs ship with the four built-ins rather than relying on migration. Generate it rather than hand-writing it, to guarantee it matches `built_in_recipes` exactly:

```bash
./.venv/bin/python - <<'EOF'
import json
from pathlib import Path
import recipes

path = Path("settings.example.json")
settings = json.loads(path.read_text(encoding="utf-8"))
settings = recipes.migrate_settings(settings, settings["camera"], settings["processing"])
path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
print("recipes:", [r["id"] for r in settings["recipes"]["items"]])
EOF
```

The `recipes` key lands at the end of the file rather than beside `processing`. That is fine — JSON object order is not significant and no code depends on it.

Expected: `recipes: ['standard', 'night', 'high-contrast', 'soft']`.

Then confirm the file is still valid and validates:

```bash
./.venv/bin/python -c "import json, dashboard; dashboard.validate_settings(json.load(open('settings.example.json'))); print('settings.example.json validates')"
```

- [ ] **Step 6: Run the full suite**

```bash
./.venv/bin/python -m unittest tests.test_dashboard_exports tests.test_dashboard_frontend tests.test_recipes -v
```

Expected: every test passes, including the 4 pre-existing `test_dashboard_exports` tests. Those exercise settings loading, so a regression there means the migration broke something.

- [ ] **Step 7: Commit**

```bash
git add dashboard.py settings.example.json tests/test_recipes.py
git commit -m "feat: validate recipes and make them the settings source of truth"
```

---

### Task 5: Recipe CRUD routes

**Files:**
- Modify: `dashboard.py` — add routes immediately after the existing `update_settings` route. LOCATE IT BY CONTENT, not by line number: find the `@app.post("/api/settings")` decorator and insert after that function's final `return` statement. Task 4 inserts code above this point, so any line number stated here would already be stale by the time you read it.
- Test: `tests/test_recipe_routes.py`

**Interfaces:**
- Consumes from Tasks 2-4: `recipes.RECIPE_LIMIT`, `settings_manager.load_settings()`, `settings_manager.save_settings()`, `SettingsValidationError`.
- Produces these routes: `GET /api/recipes`, `POST /api/recipes`, `PUT /api/recipes/{recipe_id}`, `DELETE /api/recipes/{recipe_id}`, `POST /api/recipes/{recipe_id}/activate`. Task 6's UI calls all five.

**Behavior contract:**
- `GET /api/recipes` returns `{"active": str, "items": [...]}`.
- `POST /api/recipes` takes a full recipe object, rejects a duplicate id with 409, rejects exceeding the cap with 409.
- `PUT` replaces a recipe by id, 404 if absent.
- `DELETE` removes by id; 409 if it is the active recipe or the last remaining one.
- `POST .../activate` sets `recipes.active` and regenerates the cache; 404 if the id is absent.
- Every mutation goes through `settings_manager.save_settings()` so validation and sync run, and so the camera gets a `/settings/reload` — reuse the existing notify-and-rollback pattern from `update_settings()`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_recipe_routes.py`:

```python
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import dashboard
import recipes


class RecipeRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "settings.json"
        base = json.loads(Path("settings.example.json").read_text(encoding="utf-8"))
        self.path.write_text(json.dumps(base), encoding="utf-8")

        self.manager = dashboard.SettingsManager(str(self.path))
        self.manager_patch = patch.object(dashboard, "settings_manager", self.manager)
        self.manager_patch.start()

        # The camera process is not running in tests; make the reload a no-op.
        async def fake_post(path, json=None):
            return {"status": "ok"}
        self.client_patch = patch.object(dashboard.reframe_client, "post", fake_post)
        self.client_patch.start()

        self.client = TestClient(dashboard.app)

    def tearDown(self):
        self.client_patch.stop()
        self.manager_patch.stop()
        self.temp_dir.cleanup()

    def _new_recipe(self, recipe_id="custom"):
        template = self.manager.load_settings()["recipes"]["items"][0]
        recipe = copy.deepcopy(template)
        recipe["id"] = recipe_id
        recipe["name"] = recipe_id.title()
        return recipe

    def test_list_returns_active_and_items(self):
        response = self.client.get("/api/recipes")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("active", body)
        self.assertGreaterEqual(len(body["items"]), 1)

    def test_create_adds_a_recipe(self):
        response = self.client.post("/api/recipes", json=self._new_recipe())
        self.assertEqual(response.status_code, 200)
        ids = [r["id"] for r in self.client.get("/api/recipes").json()["items"]]
        self.assertIn("custom", ids)

    def test_create_rejects_a_duplicate_id(self):
        self.client.post("/api/recipes", json=self._new_recipe())
        response = self.client.post("/api/recipes", json=self._new_recipe())
        self.assertEqual(response.status_code, 409)

    def test_create_rejects_exceeding_the_cap(self):
        for index in range(recipes.RECIPE_LIMIT):
            self.client.post("/api/recipes", json=self._new_recipe(f"extra-{index}"))
        response = self.client.post("/api/recipes", json=self._new_recipe("one-too-many"))
        self.assertEqual(response.status_code, 409)

    def test_create_rejects_an_invalid_recipe(self):
        bad = self._new_recipe("bad")
        bad["capture"]["exposure_mode"] = "bulb"
        response = self.client.post("/api/recipes", json=bad)
        self.assertEqual(response.status_code, 422)

    def test_update_replaces_an_existing_recipe(self):
        self.client.post("/api/recipes", json=self._new_recipe())
        updated = self._new_recipe()
        updated["name"] = "Renamed"
        response = self.client.put("/api/recipes/custom", json=updated)
        self.assertEqual(response.status_code, 200)
        items = self.client.get("/api/recipes").json()["items"]
        self.assertEqual(next(r for r in items if r["id"] == "custom")["name"], "Renamed")

    def test_update_404s_for_an_unknown_id(self):
        response = self.client.put("/api/recipes/ghost", json=self._new_recipe("ghost"))
        self.assertEqual(response.status_code, 404)

    def test_activate_switches_the_active_recipe_and_cache(self):
        response = self.client.post("/api/recipes/night/activate")
        self.assertEqual(response.status_code, 200)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["recipes"]["active"], "night")
        self.assertEqual(stored["camera"]["exposure_mode"], "manual")

    def test_activate_404s_for_an_unknown_id(self):
        response = self.client.post("/api/recipes/ghost/activate")
        self.assertEqual(response.status_code, 404)

    def test_delete_removes_an_inactive_recipe(self):
        self.client.post("/api/recipes", json=self._new_recipe())
        response = self.client.delete("/api/recipes/custom")
        self.assertEqual(response.status_code, 200)
        ids = [r["id"] for r in self.client.get("/api/recipes").json()["items"]]
        self.assertNotIn("custom", ids)

    def test_delete_refuses_the_active_recipe(self):
        active = self.client.get("/api/recipes").json()["active"]
        response = self.client.delete(f"/api/recipes/{active}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("active", response.json()["detail"].lower())

    def test_delete_refuses_the_last_remaining_recipe(self):
        # The active-recipe guard fires before the last-recipe guard, so
        # reaching the latter requires a settings file whose `active` pointer
        # is dangling -- exactly the corrupted state the guard exists for.
        # Deleting every other recipe first and then deleting the survivor
        # would only re-test the active guard under a misleading name.
        settings = self.manager.load_settings()
        survivor = settings["recipes"]["items"][0]
        settings["recipes"]["items"] = [survivor]
        settings["recipes"]["active"] = survivor["id"]
        self.manager.replace_settings(settings)

        # Now dangle the pointer directly on disk, bypassing validation.
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["recipes"]["active"] = "ghost"
        self.path.write_text(json.dumps(raw), encoding="utf-8")

        response = self.client.delete(f"/api/recipes/{survivor['id']}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("last", response.json()["detail"].lower())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify the tests fail**

```bash
./.venv/bin/python -m unittest tests.test_recipe_routes -v
```

Expected: all 12 FAIL with 404s, because the routes do not exist.

- [ ] **Step 3: Implement the routes**

In `dashboard.py`, immediately after the existing `update_settings` route, add:

```python
async def _save_recipes_section(section: Dict[str, Any]) -> None:
    """Persist a recipes section and tell the camera to reload.

    Mirrors update_settings()'s rollback contract: if the camera rejects the
    new settings, the previous ones are restored so the device is never left
    running configuration the dashboard has already forgotten.
    """
    previous_settings = settings_manager.load_settings()
    try:
        settings_manager.save_settings({"recipes": section})
    except SettingsValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        await reframe_client.post("/settings/reload")
    except Exception as apply_error:
        try:
            settings_manager.replace_settings(previous_settings)
            await reframe_client.post("/settings/reload")
        except Exception as rollback_error:
            logging.error(f"Recipe rollback failed: {rollback_error}")
        raise HTTPException(
            status_code=502,
            detail=f"Camera rejected the recipe; previous settings were restored: {apply_error}"
        )


def _recipes_section() -> Dict[str, Any]:
    return settings_manager.load_settings()["recipes"]


@app.get("/api/recipes")
async def list_recipes():
    """List all recipes and which one is active."""
    return _recipes_section()


@app.post("/api/recipes")
async def create_recipe(request: Request):
    """Add a new recipe."""
    try:
        recipe = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Recipe body must be valid JSON")
    if not isinstance(recipe, dict) or not recipe.get("id"):
        raise HTTPException(status_code=400, detail="Recipe must be an object with an id")

    section = _recipes_section()
    if any(existing["id"] == recipe["id"] for existing in section["items"]):
        raise HTTPException(status_code=409, detail=f"Recipe '{recipe['id']}' already exists")
    if len(section["items"]) >= recipes.RECIPE_LIMIT:
        raise HTTPException(
            status_code=409,
            detail=f"Recipe limit of {recipes.RECIPE_LIMIT} reached; delete one first")

    section["items"].append(recipe)
    await _save_recipes_section(section)
    return {"status": "success", "id": recipe["id"]}


@app.put("/api/recipes/{recipe_id}")
async def update_recipe(recipe_id: str, request: Request):
    """Replace an existing recipe."""
    try:
        recipe = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Recipe body must be valid JSON")
    if not isinstance(recipe, dict):
        raise HTTPException(status_code=400, detail="Recipe must be an object")

    section = _recipes_section()
    for index, existing in enumerate(section["items"]):
        if existing["id"] == recipe_id:
            recipe["id"] = recipe_id  # the URL owns identity, not the body
            section["items"][index] = recipe
            await _save_recipes_section(section)
            return {"status": "success", "id": recipe_id}
    raise HTTPException(status_code=404, detail=f"No recipe '{recipe_id}'")


@app.delete("/api/recipes/{recipe_id}")
async def delete_recipe(recipe_id: str):
    """Delete a recipe that is neither active nor the last one."""
    section = _recipes_section()
    if not any(existing["id"] == recipe_id for existing in section["items"]):
        raise HTTPException(status_code=404, detail=f"No recipe '{recipe_id}'")
    if section["active"] == recipe_id:
        raise HTTPException(
            status_code=409, detail="Cannot delete the active recipe; activate another first")
    if len(section["items"]) <= 1:
        raise HTTPException(status_code=409, detail="Cannot delete the last remaining recipe")

    section["items"] = [r for r in section["items"] if r["id"] != recipe_id]
    await _save_recipes_section(section)
    return {"status": "success", "id": recipe_id}


@app.post("/api/recipes/{recipe_id}/activate")
async def activate_recipe(recipe_id: str):
    """Make a recipe active, regenerating the derived camera/processing cache."""
    section = _recipes_section()
    if not any(existing["id"] == recipe_id for existing in section["items"]):
        raise HTTPException(status_code=404, detail=f"No recipe '{recipe_id}'")

    section["active"] = recipe_id
    await _save_recipes_section(section)
    return {"status": "success", "active": recipe_id}
```

- [ ] **Step 4: Run to verify the tests pass**

```bash
./.venv/bin/python -m unittest tests.test_recipe_routes -v
```

Expected: 12 tests PASS.

- [ ] **Step 5: Run the full suite**

```bash
./.venv/bin/python -m unittest tests.test_dashboard_exports tests.test_dashboard_frontend tests.test_recipes tests.test_recipe_routes -v
```

Expected: everything passes.

- [ ] **Step 6: Commit**

```bash
git add dashboard.py tests/test_recipe_routes.py
git commit -m "feat: add recipe CRUD and activation routes"
```

---

### Task 6: Recipe manager UI

**Files:**
- Modify: `templates/dashboard.html` — add a recipe section inside the settings modal
- Modify: `static/dashboard.css` — styles for the recipe list
- Modify: `static/dashboard.js` — load, activate, save, and delete recipes
- Modify: `tests/test_dashboard_frontend.py`

**Interfaces:**
- Consumes from Task 5: `GET /api/recipes`, `POST /api/recipes`, `PUT /api/recipes/{id}`, `DELETE /api/recipes/{id}`, `POST /api/recipes/{id}/activate`.
- Produces: no interface later tasks depend on. This is the last task of Phase 1A.

**Scope discipline:** this is a functional recipe manager, not a redesign. Match the existing settings-modal markup, class names, and JS style exactly — read the surrounding code before writing. Do not restyle anything that already exists.

- [ ] **Step 1: Read the existing settings modal markup and JS conventions**

```bash
grep -n "settings-modal\|setting-input\|setting-group\|settings-section" templates/dashboard.html | head -30
grep -n "function openSettings\|function saveSettings\|function populateSettingsForm" static/dashboard.js
```

Note the exact class names and the shape of `populateSettingsForm` / `saveSettings`. The new UI must use the same conventions.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_dashboard_frontend.py`, inside the existing `RouteTests` class:

```python
    def test_settings_page_exposes_the_recipe_manager(self):
        body = self.client.get("/").text
        self.assertIn('id="recipe-list"', body)
        self.assertIn('id="recipe-name"', body)
        self.assertIn('id="recipe-save-btn"', body)

    def test_frontend_javascript_talks_to_the_recipe_api(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("/api/recipes", js)
        self.assertIn("function loadRecipes", js)
        self.assertIn("function activateRecipe", js)
        self.assertIn("function saveRecipe", js)
        self.assertIn("function deleteRecipe", js)

    def test_recipe_styles_are_present(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".recipe-list", css)
        self.assertIn(".recipe-item", css)
```

- [ ] **Step 3: Run to verify the tests fail**

```bash
./.venv/bin/python -m unittest tests.test_dashboard_frontend -v
```

Expected: the three new tests FAIL; every pre-existing test still passes.

- [ ] **Step 4: Add the markup**

In `templates/dashboard.html`, inside the settings modal and directly above the existing camera settings group, add a recipe section. Use the same wrapper classes the neighbouring groups use (read them in Step 1 and match exactly; the structure below shows required ids, not required classes):

```html
<div class="settings-section">
    <h3>Recipes</h3>
    <p class="settings-hint">A recipe bundles capture and render settings. The active recipe drives every photo.</p>
    <div id="recipe-list" class="recipe-list"></div>
    <div class="setting-group">
        <label for="recipe-name">Recipe name</label>
        <input type="text" id="recipe-name" class="setting-input" maxlength="80" placeholder="My recipe">
    </div>
    <div class="setting-group">
        <button type="button" id="recipe-save-btn" class="action-btn btn-primary" onclick="saveRecipe()">Save current settings as recipe</button>
    </div>
    <p id="recipe-status" class="settings-hint"></p>
</div>
```

- [ ] **Step 5: Add the styles**

Append to `static/dashboard.css`, matching the file's existing indentation (it is deeply indented — follow the surrounding lines exactly):

```css
.recipe-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-bottom: 12px;
}

.recipe-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 10px;
    border: 1px dotted var(--panel-border-color);
    background: var(--tertiary-color);
}

.recipe-item.is-active {
    border-style: solid;
}

.recipe-item-name {
    flex: 1;
    text-align: left;
}

.recipe-item-actions {
    display: flex;
    gap: 6px;
}
```

- [ ] **Step 6: Add the JavaScript**

Append to `static/dashboard.js`, before the closing of the existing script content, matching the file's indentation:

```javascript
            async function loadRecipes() {
                try {
                    const response = await fetch('/api/recipes');
                    if (!response.ok) {
                        throw new Error('Could not load recipes');
                    }
                    renderRecipes(await response.json());
                } catch (error) {
                    console.error('Error loading recipes:', error);
                    document.getElementById('recipe-status').textContent = 'Could not load recipes.';
                }
            }

            function renderRecipes(data) {
                const list = document.getElementById('recipe-list');
                list.innerHTML = data.items.map(recipe => {
                    const isActive = recipe.id === data.active;
                    return `
                        <div class="recipe-item ${isActive ? 'is-active' : ''}">
                            <span class="recipe-item-name">${escapeHtml(recipe.name || recipe.id)}</span>
                            <span class="recipe-item-actions">
                                <button type="button" class="action-btn btn-secondary"
                                    onclick="activateRecipe('${encodeURIComponent(recipe.id)}')"
                                    ${isActive ? 'disabled' : ''}>${isActive ? 'Active' : 'Use'}</button>
                                <button type="button" class="action-btn btn-secondary"
                                    onclick="deleteRecipe('${encodeURIComponent(recipe.id)}')"
                                    ${isActive || data.items.length <= 1 ? 'disabled' : ''}>Delete</button>
                            </span>
                        </div>`;
                }).join('');
            }

            function escapeHtml(value) {
                const div = document.createElement('div');
                div.textContent = value;
                return div.innerHTML;
            }

            async function activateRecipe(recipeId) {
                await sendRecipeRequest(`/api/recipes/${recipeId}/activate`, 'POST',
                    null, 'Recipe activated.');
                // Activation rewrites the derived cache, so the form is now stale.
                const settings = await (await fetch('/api/settings')).json();
                populateSettingsForm(settings);
            }

            async function saveRecipe() {
                const name = document.getElementById('recipe-name').value.trim();
                if (!name) {
                    document.getElementById('recipe-status').textContent = 'Give the recipe a name first.';
                    return;
                }
                const settings = await (await fetch('/api/settings')).json();
                const recipe = {
                    id: name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''),
                    name: name,
                    capture: {
                        exposure_mode: settings.camera.exposure_mode || 'auto',
                        exposure_time_us: settings.camera.exposure_time_us || 0,
                        analogue_gain: settings.camera.analogue_gain || 1.0,
                        exposure_value: Number(document.getElementById('exposure-value').value),
                        sharpness: Number(document.getElementById('sharpness').value),
                        autofocus_mode: Number(document.getElementById('autofocus-mode').value)
                    },
                    render: {
                        saturation: Number(document.getElementById('saturation').value),
                        brightness_factor: Number(document.getElementById('brightness-factor').value),
                        color_factor: Number(document.getElementById('color-factor').value),
                        dithering_method: document.getElementById('dithering-method').value,
                        bayer_size: Number(document.getElementById('bayer-size').value),
                        threshold_scale: Number(document.getElementById('threshold-scale').value)
                    }
                };
                if (!recipe.id) {
                    document.getElementById('recipe-status').textContent = 'That name has no usable characters.';
                    return;
                }
                await sendRecipeRequest('/api/recipes', 'POST', recipe, 'Recipe saved.');
                document.getElementById('recipe-name').value = '';
            }

            async function deleteRecipe(recipeId) {
                if (!confirm('Delete this recipe?')) {
                    return;
                }
                await sendRecipeRequest(`/api/recipes/${recipeId}`, 'DELETE', null, 'Recipe deleted.');
            }

            async function sendRecipeRequest(url, method, body, successMessage) {
                const status = document.getElementById('recipe-status');
                try {
                    const options = { method: method };
                    if (body) {
                        options.headers = { 'Content-Type': 'application/json' };
                        options.body = JSON.stringify(body);
                    }
                    const response = await fetch(url, options);
                    const data = await response.json().catch(() => ({}));
                    if (!response.ok) {
                        status.textContent = data.detail || 'Recipe request failed.';
                        return;
                    }
                    status.textContent = successMessage;
                    await loadRecipes();
                } catch (error) {
                    console.error('Recipe request failed:', error);
                    status.textContent = 'Recipe request failed.';
                }
            }
```

Then call `loadRecipes()` from `openSettings()`, immediately after `populateSettingsForm(settings);`, so the list is populated whenever the modal opens.

- [ ] **Step 7: Run the full suite**

```bash
./.venv/bin/python -m unittest tests.test_dashboard_exports tests.test_dashboard_frontend tests.test_recipes tests.test_recipe_routes -v
```

Expected: every test passes.

- [ ] **Step 8: Commit**

```bash
git add templates/dashboard.html static/dashboard.css static/dashboard.js tests/test_dashboard_frontend.py
git commit -m "feat: add recipe manager to the dashboard settings panel"
```

---

## Notes for the implementer

**Why `reframe.py` is untouched.** The whole point of the derived cache is that the camera process keeps reading `settings["camera"]` and `settings["processing"]` exactly as it does today. If a task pushes you toward editing `reframe.py`, something has gone wrong with the design rather than with your implementation — stop and report it instead of making the edit.

**The three new camera keys do nothing yet.** `exposure_mode`, `exposure_time_us`, and `analogue_gain` are stored, validated, and written into the derived cache, but `reframe.py` ignores keys it does not recognise, so selecting the Night recipe will NOT produce a 4-second exposure until Phase 1B lands. This is expected, not a bug. Phase 1B is where `AeEnable`, `FrameDurationLimits`, and the autofocus-settle skip are implemented.

**Two things the spec mentions that this plan deliberately excludes.** Neither is an oversight; both are recorded here so a reviewer can confirm the gap is intentional.

*No manual-exposure controls in the UI.* The recipe manager saves whatever `exposure_mode` / `exposure_time_us` / `analogue_gain` are already in the derived cache, but adds no widgets for editing them. Bounded sliders need real sensor limits from `GET /api/camera/limits`, which is Phase 1B. So in Phase 1A you can *use* the built-in Night recipe, but you cannot *author* your own manual-exposure recipe. Shipping unbounded shutter and gain inputs against provisional guesses would teach the user wrong numbers and then change them.

*No `POST /api/photos/{id}/develop`.* Re-developing a past photo with a different recipe's render half is genuinely possible now — `ImageProcessor.reprocess_photo_by_id()` already exists — but it belongs with the per-photo sidecars and gallery grouping the spec assigns to Phase 2. Splitting it away from the gallery work it pairs with would ship a route with nowhere sensible to call it from.

**Provisional bounds are marked as such for a reason.** The `0..200000000` exposure range and `1.0..16.0` gain range are placeholders chosen to be permissive. Phase 1B reads the real limits from `picam2.camera_controls["ExposureTime"]` and `["AnalogueGain"]` and narrows them. Do not present these numbers to the user as sensor capabilities.

**Nothing in this phase has run on the camera.** The Pi was unreachable when this plan was written, so Phase 0's on-device verification never happened either. Everything here is verified by unit and route tests only. When the hardware exists, check that the settings modal still opens, that activating a recipe changes the sliders, and that `settings.json` on the device migrates cleanly on first load rather than being replaced.
