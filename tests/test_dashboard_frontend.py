import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE = REPO_ROOT / "tests" / "fixtures" / "dashboard_baseline.html"
TEMPLATE = REPO_ROOT / "templates" / "dashboard.html"
CSS = REPO_ROOT / "static" / "dashboard.css"
JS = REPO_ROOT / "static" / "dashboard.js"

# Exact byte markers from the original string literal. The leading spaces are
# part of the original indentation and must not be altered.
STYLE_OPEN = "        <style>\n"
STYLE_CLOSE = "        </style>\n"
SCRIPT_OPEN = "        <script>\n"
SCRIPT_CLOSE = "        </script>\n"
LINK_TAG = '        <link rel="stylesheet" href="/static/dashboard.css">\n'
SCRIPT_TAG = '        <script src="/static/dashboard.js"></script>\n'


def reconstruct() -> str:
    """Re-inline the extracted assets, reproducing the original HTML."""
    template = TEMPLATE.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")
    html = template.replace(LINK_TAG, STYLE_OPEN + css + STYLE_CLOSE)
    html = html.replace(SCRIPT_TAG, SCRIPT_OPEN + js + SCRIPT_CLOSE)
    return html


class ReconstructionTests(unittest.TestCase):
    def test_extracted_assets_reconstruct_original_html_exactly(self):
        expected = BASELINE.read_text(encoding="utf-8")
        actual = reconstruct()
        self.assertEqual(
            actual,
            expected,
            "Reconstructed HTML differs from the pre-refactor baseline. "
            "Some CSS or JS bytes were altered during extraction.",
        )

    def test_template_contains_no_inline_assets(self):
        template = TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn(STYLE_OPEN, template, "template still has an inline <style> block")
        self.assertNotIn(SCRIPT_OPEN, template, "template still has an inline <script> block")
        self.assertIn(LINK_TAG, template)
        self.assertIn(SCRIPT_TAG, template)


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

    def test_existing_api_routes_are_not_shadowed_by_static_mount(self):
        # /static must not swallow the photo-serving routes.
        response = self.client.get("/static/does-not-exist.css")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
