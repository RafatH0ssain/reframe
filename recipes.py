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
