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


if __name__ == "__main__":
    unittest.main()
