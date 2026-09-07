import copy
import json
import tempfile
import unittest
from pathlib import Path

import dashboard
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
            "trigger": {"program": "single", "bracket": {"frames": 3, "step_ev": 1.0}},
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

    def test_manual_recipe_with_zero_exposure_time_is_rejected(self):
        # Number("") === 0 in the frontend, so clearing the shutter field
        # yields exactly this: a recipe saved as manual with exposure_time_us
        # 0, which then shoots auto forever with no visible error.
        settings = self._valid()
        settings["recipes"]["items"][0]["capture"]["exposure_mode"] = "manual"
        settings["recipes"]["items"][0]["capture"]["exposure_time_us"] = 0
        with self.assertRaises(dashboard.SettingsValidationError):
            dashboard.validate_settings(settings)

    def test_manual_recipe_with_positive_exposure_time_is_accepted(self):
        settings = self._valid()
        settings["recipes"]["items"][0]["capture"]["exposure_mode"] = "manual"
        settings["recipes"]["items"][0]["capture"]["exposure_time_us"] = 4_000_000
        dashboard.validate_settings(settings)

    def test_default_trigger_is_a_single_shot(self):
        settings = self._valid()
        self.assertEqual(settings["trigger"]["program"], "single")
        dashboard.validate_settings(settings)

    def test_bracket_trigger_is_accepted(self):
        settings = self._valid()
        settings["trigger"] = {"program": "bracket",
                               "bracket": {"frames": 5, "step_ev": 0.5}}
        dashboard.validate_settings(settings)

    def test_unknown_program_is_rejected(self):
        settings = self._valid()
        settings["trigger"] = {"program": "interval", "bracket": {"frames": 3, "step_ev": 1.0}}
        with self.assertRaises(dashboard.SettingsValidationError):
            dashboard.validate_settings(settings)

    def test_bracket_frame_count_must_be_three_or_five(self):
        for count in (2, 4, 7):
            with self.subTest(count=count):
                settings = self._valid()
                settings["trigger"] = {"program": "bracket",
                                       "bracket": {"frames": count, "step_ev": 1.0}}
                with self.assertRaises(dashboard.SettingsValidationError):
                    dashboard.validate_settings(settings)

    def test_bracket_step_must_be_within_range(self):
        for step in (0.1, 3.0):
            with self.subTest(step=step):
                settings = self._valid()
                settings["trigger"] = {"program": "bracket",
                                       "bracket": {"frames": 3, "step_ev": step}}
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

    def test_constructing_against_a_missing_file_writes_usable_settings(self):
        # Every other test writes a settings file first, so the fresh-install
        # bootstrap path had no coverage: an empty default recipe list made
        # _ensure_settings_file() fail silently and never create settings.json.
        missing = Path(self.temp_dir.name) / "brand-new.json"
        self.assertFalse(missing.exists())

        manager = dashboard.SettingsManager(str(missing))

        self.assertTrue(missing.exists(), "fresh install did not write settings.json")
        stored = json.loads(missing.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(stored["recipes"]["items"]), 1)
        dashboard.validate_settings(stored)

    def test_saving_an_empty_recipe_list_raises_a_validation_error(self):
        # recipes.sync() runs before the second validate_settings() call and
        # would otherwise raise a raw ValueError from resolve_active() for
        # this exact payload, bypassing the friendly message written for it.
        manager = self._manager_with({
            "camera": copy.deepcopy(DEFAULT_CAMERA),
            "processing": copy.deepcopy(DEFAULT_PROCESSING),
        })
        manager.load_settings()
        with self.assertRaises(dashboard.SettingsValidationError) as ctx:
            manager.save_settings({"recipes": {"active": "standard", "items": []}})
        self.assertIn("recipes.items", str(ctx.exception))

    def test_saving_a_valid_activation_still_succeeds(self):
        # Guards against the earlier validate_settings() call rejecting a
        # legitimate payload before recipes.sync() has a chance to run.
        manager = self._manager_with({
            "camera": copy.deepcopy(DEFAULT_CAMERA),
            "processing": copy.deepcopy(DEFAULT_PROCESSING),
        })
        loaded = manager.load_settings()
        loaded["recipes"]["active"] = "night"
        self.assertTrue(manager.save_settings({"recipes": loaded["recipes"]}))

        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["recipes"]["active"], "night")
        self.assertEqual(stored["camera"]["exposure_mode"], "manual")
        self.assertEqual(stored["processing"]["color_factor"], 1.6)

    def test_processing_missing_a_key_promotes_to_standard_filled_from_defaults(self):
        # Regression test: a settings.json whose "processing" section exists
        # but predates a key (e.g. threshold_scale) must not build a Standard
        # recipe that omits it -- that used to 422 every settings save and
        # every recipe route forever, naming a key the user never wrote.
        processing = copy.deepcopy(LEGACY_PROCESSING)
        del processing["threshold_scale"]
        manager = self._manager_with({
            "camera": copy.deepcopy(LEGACY_CAMERA),
            "processing": processing,
        })
        loaded = manager.load_settings()
        standard = next(r for r in loaded["recipes"]["items"] if r["id"] == "standard")
        # The missing key is filled from the shipped default...
        self.assertEqual(standard["render"]["threshold_scale"], DEFAULT_PROCESSING["threshold_scale"])
        # ...but every key the user DID supply keeps the user's own value.
        self.assertEqual(standard["render"]["saturation"], LEGACY_PROCESSING["saturation"])
        self.assertEqual(standard["render"]["dithering_method"], LEGACY_PROCESSING["dithering_method"])
        dashboard.validate_settings(loaded)

    def test_camera_missing_a_key_promotes_to_standard_filled_from_defaults(self):
        camera = copy.deepcopy(LEGACY_CAMERA)
        del camera["sharpness"]
        manager = self._manager_with({
            "camera": camera,
            "processing": copy.deepcopy(LEGACY_PROCESSING),
        })
        loaded = manager.load_settings()
        standard = next(r for r in loaded["recipes"]["items"] if r["id"] == "standard")
        self.assertEqual(standard["capture"]["sharpness"], DEFAULT_CAMERA["sharpness"])
        self.assertEqual(standard["capture"]["exposure_value"], LEGACY_CAMERA["exposure_value"])
        self.assertEqual(standard["capture"]["autofocus_mode"], LEGACY_CAMERA["autofocus_mode"])
        dashboard.validate_settings(loaded)


if __name__ == "__main__":
    unittest.main()
