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


if __name__ == "__main__":
    unittest.main()
