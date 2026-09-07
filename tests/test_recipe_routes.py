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

        # Photo-related tests write fixture .jpg/.json files. Point PHOTOS_PATH
        # at a scratch directory instead of the repo's real photos/ so those
        # fixtures never land in (or overwrite) the actual photo library.
        self.photos_dir = tempfile.TemporaryDirectory()
        self.photos_path_patch = patch.object(dashboard, "PHOTOS_PATH", self.photos_dir.name)
        self.photos_path_patch.start()

        self.client = TestClient(dashboard.app)

    def tearDown(self):
        self.photos_path_patch.stop()
        self.photos_dir.cleanup()
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

    def test_create_discards_unknown_top_level_keys(self):
        # A large unknown field would otherwise be stored verbatim and
        # re-parsed on every load_settings() call, i.e. every API request.
        recipe = self._new_recipe()
        recipe["junk"] = "x" * 1000
        response = self.client.post("/api/recipes", json=recipe)
        self.assertEqual(response.status_code, 200)
        stored = self.client.get("/api/recipes").json()
        created = next(r for r in stored["items"] if r["id"] == "custom")
        self.assertNotIn("junk", created)

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

    def test_camera_rejection_rolls_back_and_returns_502(self):
        # Override the always-succeeding fake_post from setUp with one that
        # raises, simulating the camera refusing the reload. The rollback
        # branch re-notifies with the same patched post and tolerates that
        # failing too -- what matters is that the previous settings were
        # restored to disk before the 502 was raised.
        async def failing_post(path, json=None):
            raise RuntimeError("camera unreachable")

        original_active = self.client.get("/api/recipes").json()["active"]
        with patch.object(dashboard.reframe_client, "post", failing_post):
            response = self.client.post("/api/recipes/night/activate")
        self.assertEqual(response.status_code, 502)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["recipes"]["active"], original_active)

    def test_failed_save_returns_500_not_200(self):
        # save_settings() swallows non-validation exceptions (disk-write
        # failures, permissions problems) and returns False. The route must
        # not report success when nothing reached disk.
        with patch.object(dashboard.settings_manager, "save_settings", return_value=False):
            response = self.client.post("/api/recipes/night/activate")
        self.assertEqual(response.status_code, 500)

    def test_recipe_with_malicious_id_is_rejected(self):
        # XSS attack vector: if recipe.id is not validated server-side,
        # an attacker can inject JavaScript through the id field.
        response = self.client.post("/api/recipes", json={
            "id": "x')*(alert)('1",
            "name": "Malicious",
            "capture": {
                "exposure_mode": "auto",
                "exposure_time_us": 0,
                "analogue_gain": 1.0,
                "exposure_value": 0,
                "sharpness": 0,
                "autofocus_mode": 0
            },
            "render": {
                "saturation": 0.6,
                "brightness_factor": 1.0,
                "color_factor": 1.1,
                "dithering_method": "floyd_steinberg",
                "bayer_size": 4,
                "threshold_scale": 1.0
            }
        })
        self.assertEqual(response.status_code, 422)
        self.assertIn("id", response.json()["detail"].lower())

    def test_recipe_with_normal_hyphenated_id_works_end_to_end(self):
        # Verify that legitimate hyphenated ids (generated by the frontend
        # slugify) still work after the id format validation is added.
        recipe_data = {
            "id": "my-recipe",
            "name": "My Recipe",
            "capture": {
                "exposure_mode": "auto",
                "exposure_time_us": 0,
                "analogue_gain": 1.0,
                "exposure_value": 0,
                "sharpness": 0,
                "autofocus_mode": 0
            },
            "render": {
                "saturation": 0.6,
                "brightness_factor": 1.0,
                "color_factor": 1.1,
                "dithering_method": "floyd_steinberg",
                "bayer_size": 4,
                "threshold_scale": 1.0
            }
        }
        # Create
        response = self.client.post("/api/recipes", json=recipe_data)
        self.assertEqual(response.status_code, 200)
        # Activate
        response = self.client.post("/api/recipes/my-recipe/activate")
        self.assertEqual(response.status_code, 200)
        # Verify it's active
        response = self.client.get("/api/recipes")
        self.assertEqual(response.json()["active"], "my-recipe")

    def test_reset_restores_built_in_recipes_and_clears_manual_exposure(self):
        # Activate Night, then edit a slider -- which writes through into
        # Night's render values via the cache-wins direction of sync(). A
        # reset must undo both: the recipe set goes back to the shipped
        # built-ins, Standard becomes active again, and the derived camera
        # cache no longer carries Night's manual 4-second exposure.
        self.client.post("/api/recipes/night/activate")
        self.client.post("/api/settings", json={"processing": {"saturation": 0.01}})

        response = self.client.post("/api/settings/reset")
        self.assertEqual(response.status_code, 200)

        body = self.client.get("/api/recipes").json()
        self.assertEqual(body["active"], "standard")

        default_items = self.manager.default_settings["recipes"]["items"]
        self.assertEqual({r["id"] for r in body["items"]},
                          {r["id"] for r in default_items})
        night = next(r for r in body["items"] if r["id"] == "night")
        default_night = next(r for r in default_items if r["id"] == "night")
        self.assertEqual(night["render"], default_night["render"])

        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotEqual(stored["camera"].get("exposure_mode"), "manual")
        self.assertNotEqual(stored["camera"].get("exposure_time_us"), 4_000_000)

    def test_reset_rolls_back_when_the_camera_rejects_it(self):
        async def failing_post(path, json=None):
            raise RuntimeError("camera unreachable")

        original_active = self.client.get("/api/recipes").json()["active"]
        with patch.object(dashboard.reframe_client, "post", failing_post):
            response = self.client.post("/api/settings/reset")
        self.assertEqual(response.status_code, 502)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["recipes"]["active"], original_active)

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

    def test_a_sidecar_holding_valid_json_that_is_not_an_object_is_ignored(self):
        # `{not json` is a decode error; `[]` parses fine and used to reach
        # .get(), taking down the entire gallery rather than one photo.
        from PIL import Image
        photos = Path(dashboard.PHOTOS_PATH)
        photos.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4)).save(photos / "90006.jpg", format="JPEG")
        (photos / "90006.json").write_text("[]", encoding="utf-8")
        listed = dashboard.PhotoManager().get_all_photos()["photos"]
        entry = [p for p in listed if p["id"] == "90006"][0]
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


if __name__ == "__main__":
    unittest.main()
