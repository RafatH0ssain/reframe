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
