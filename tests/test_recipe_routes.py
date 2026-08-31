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


if __name__ == "__main__":
    unittest.main()
