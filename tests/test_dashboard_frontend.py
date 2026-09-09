import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "templates" / "dashboard.html"
CSS = REPO_ROOT / "static" / "dashboard.css"
JS = REPO_ROOT / "static" / "dashboard.js"
LINK_TAG = '        <link rel="stylesheet" href="/static/dashboard.css">\n'
SCRIPT_TAG = '        <script src="/static/dashboard.js"></script>\n'


class RouteTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import dashboard
        self.client = TestClient(dashboard.app)

    def test_index_serves_template_without_inline_assets(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn(LINK_TAG.strip(), body)
        self.assertIn(SCRIPT_TAG.strip(), body)
        self.assertNotIn("<style>", body)
        self.assertNotIn("--primary-color", body)

    def test_static_css_is_served_byte_identical(self):
        response = self.client.get("/static/dashboard.css")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/css", response.headers["content-type"])
        self.assertEqual(response.content, CSS.read_bytes())

    def test_static_js_is_served_byte_identical(self):
        response = self.client.get("/static/dashboard.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])
        self.assertEqual(response.content, JS.read_bytes())

    def test_static_mount_returns_404_for_missing_asset(self):
        # A path under /static with no matching file on disk must 404, not
        # fall through to some other handler or raise.
        response = self.client.get("/static/does-not-exist.css")
        self.assertEqual(response.status_code, 404)

    def test_static_assets_are_revalidated_not_blindly_cached(self):
        # A stable /static URL whose body changes each release must never be
        # served from cache without revalidation, or an update leaves new HTML
        # driving old JavaScript.
        for path in ("/static/dashboard.css", "/static/dashboard.js"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("no-cache", response.headers.get("cache-control", ""))

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

    def test_recipe_buttons_use_event_delegation_not_inline_handlers(self):
        # Security fix: recipe buttons must not use inline onclick handlers
        # (which are vulnerable to id-based XSS). They must use event delegation
        # with data attributes instead.
        js = JS.read_text(encoding="utf-8")
        self.assertNotIn('onclick="activateRecipe(', js)
        self.assertNotIn('onclick="deleteRecipe(', js)
        # Verify the structural fix is in place
        self.assertIn("data-recipe-id", js)
        self.assertIn("handleRecipeClick", js)

    def test_recipe_ids_properly_escaped_in_attributes_and_urls(self):
        # Security fix: recipe ids must be escaped in attribute contexts
        # (using escapeAttr which escapes quotes) and encoded in URL paths
        # (using encodeURIComponent).
        js = JS.read_text(encoding="utf-8")
        # Verify escapeAttr is used for attribute values
        self.assertIn("function escapeAttr", js)
        self.assertIn('data-recipe-id="${escapeAttr(recipe.id)}"', js)
        # Verify encodeURIComponent is used in fetch URLs
        self.assertIn("encodeURIComponent(recipeId)", js)
        self.assertIn("/api/recipes/${encodeURIComponent(recipeId)}/activate", js)
        self.assertIn("/api/recipes/${encodeURIComponent(recipeId)}", js)

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

    def test_gallery_has_a_develop_control_wired_by_delegation(self):
        # developPhoto() previously had zero call sites -- the gallery must
        # actually offer a way to trigger it, and it must follow the same
        # data-attribute + delegated-listener pattern as the recipe list
        # rather than an inline onclick carrying interpolated recipe values
        # (this panel shipped a stored XSS earlier in this project).
        js = JS.read_text(encoding="utf-8")
        self.assertIn("photo-develop-btn", js)
        self.assertIn("photo-develop-select", js)
        self.assertIn("function handlePhotoGalleryClick", js)
        self.assertIn("function attachPhotoGalleryListeners", js)
        self.assertIn("developPhoto(photoId, recipeId)", js)
        self.assertNotIn('onclick="developPhoto(', js)

    def test_develop_control_recipe_options_are_escaped(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("function renderRecipeOptions", js)
        self.assertIn("escapeAttr(recipe.id)", js)
        self.assertIn("escapeHtml(recipe.name || recipe.id)", js)

    def test_load_recipes_caches_the_list_for_the_gallery(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("let cachedRecipes", js)
        self.assertIn("cachedRecipes = data.items", js)


if __name__ == "__main__":
    unittest.main()
