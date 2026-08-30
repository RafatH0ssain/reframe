# Dashboard Frontend Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the 2,231-line inline HTML/CSS/JS blob from `dashboard.py` into `templates/` and `static/` files with zero behavior change, proven by byte-exact reconstruction.

**Architecture:** `dashboard()` currently returns a 2,231-line plain string literal (NOT an f-string — every `${...}` inside it is JavaScript, not Python interpolation). The CSS and JS move verbatim into standalone files; the HTML skeleton keeps its exact bytes except that the `<style>` block becomes a `<link>` and the `<script>` block becomes a `<script src>`. FastAPI's `StaticFiles` serves the assets. A committed fixture of the pre-refactor served HTML plus a reconstruction test proves not one byte of CSS or JS was altered.

**Tech Stack:** Python 3, FastAPI, Starlette `StaticFiles`, `unittest` (this repo has no pytest — do not introduce it).

**Spec:** `docs/superpowers/specs/2026-08-30-capture-programs-design.md` (Phase 0 of the Implementation Phasing section)

## Global Constraints

- **No reindentation, ever.** Extracted CSS and JS keep the original's leading whitespace byte-for-byte. The JS contains multi-line template literals whose embedded newlines and indentation become part of the strings they emit; reindenting changes program output, not just formatting.
- **Zero behavior change.** No feature work, no cleanup, no renames, no "while I'm here" fixes. Anything beyond moving bytes belongs in Phase 1.
- **Tests use `unittest`, invoked by explicit module name from the repo root** — e.g. `./.venv/bin/python -m unittest tests.test_dashboard_frontend -v`. `tests/` is NOT a package (no `__init__.py`), so `unittest discover` fails with `Start directory is not importable`; do not use it and do not add an `__init__.py` to make it work. The existing suite is `tests/test_dashboard_exports.py`; match its style. `pytest` is not a dependency.
- **Use the worktree's `./.venv/bin/python`**, never bare `python3` — system Python is 3.9 and lacks FastAPI. The venv is Python 3.12 with fastapi, httpx, starlette, and pillow already installed.
- **Paths derive from `BASE_PATH`**, never from the current working directory. `dashboard.py` already defines `BASE_PATH = os.path.dirname(os.path.realpath(__file__))`.
- **Verified source boundaries** (as of commit `a0f61c8`): `html_content = """` at line 829, `<style>` at 836, `</style>` at 1472, `<script>` at 1735, `</script>` at 3052, closing `"""` at 3055. CSS body is lines 837-1471 (635 lines); JS body is lines 1736-3051 (1,316 lines).
- **No new runtime dependencies.** `StaticFiles` ships with Starlette, already installed via FastAPI.

---

### Task 1: Capture the pre-refactor baseline and write the failing reconstruction test

**Files:**
- Create: `tests/fixtures/dashboard_baseline.html`
- Create: `tests/test_dashboard_frontend.py`

**Interfaces:**
- Consumes: `dashboard.dashboard()` — the existing FastAPI route coroutine, returns `HTMLResponse` whose `.body` is `bytes`.
- Produces: `tests/fixtures/dashboard_baseline.html` (the frozen pre-refactor HTML, byte-exact) and the four module-level marker constants in `tests/test_dashboard_frontend.py` — `STYLE_OPEN`, `STYLE_CLOSE`, `SCRIPT_OPEN`, `SCRIPT_CLOSE`, `LINK_TAG`, `SCRIPT_TAG` — reused by Task 2's extraction.

- [ ] **Step 1: Capture the baseline fixture from the CURRENT, unmodified dashboard.py**

This must run before `dashboard.py` is touched. If `dashboard.py` has already been modified, `git stash` first — a baseline captured after the refactor proves nothing.

Run this with the worktree venv — `dashboard.py` imports `fastapi`, `httpx`,
and `PIL`, none of which exist in system Python 3.9. (On the Pi, the equivalent
is `./scripts/reframe-python`.)

```bash
mkdir -p tests/fixtures
./.venv/bin/python - <<'EOF'
import asyncio, pathlib, dashboard
response = asyncio.run(dashboard.dashboard())
out = pathlib.Path("tests/fixtures/dashboard_baseline.html")
out.write_bytes(response.body)
newlines = response.body.count(b"\n")
print(f"baseline: {len(response.body)} bytes, {newlines} newlines")
EOF
```

Expected exactly: `baseline: 100594 bytes, 2226 newlines`. These are measured
from the current source, not estimated — a different byte count means
`dashboard.py` has changed since this plan was written, so re-verify the
boundary line numbers in Global Constraints before continuing.

- [ ] **Step 2: Sanity-check the fixture contains all three regions**

```bash
grep -c "<style>" tests/fixtures/dashboard_baseline.html
grep -c "</script>" tests/fixtures/dashboard_baseline.html
head -c 40 tests/fixtures/dashboard_baseline.html | od -c | head -3
```

Expected: `1` for each grep. The `od` output must begin with `\n` then 4 spaces
then `<!DOCTYPE html>`, confirming the leading newline and indentation survived.

The file also ends with `</html>\n` followed by **four trailing spaces** — those
are part of the original string literal (the indentation before its closing
`"""`). Do not strip them; the reconstruction test compares them.

- [ ] **Step 3: Write the failing reconstruction test**

Create `tests/test_dashboard_frontend.py`:

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Run the test to verify it fails for the right reason**

```bash
./.venv/bin/python -m unittest tests.test_dashboard_frontend -v
```

Expected: FAIL — `FileNotFoundError` on `templates/dashboard.html`. That is the correct failure; the assets do not exist yet. A different error means the marker constants are wrong.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/dashboard_baseline.html tests/test_dashboard_frontend.py
git commit -m "test: baseline fixture and reconstruction test for dashboard extraction"
```

---

### Task 2: Extract the assets into template and static files

**Files:**
- Create: `templates/dashboard.html`
- Create: `static/dashboard.css`
- Create: `static/dashboard.js`

**Interfaces:**
- Consumes: `tests/fixtures/dashboard_baseline.html` from Task 1, and the marker constants defined in `tests/test_dashboard_frontend.py`.
- Produces: the three asset files. Task 3 loads `templates/dashboard.html` at import time and mounts `static/` at the URL prefix `/static`.

- [ ] **Step 1: Split the baseline with a one-shot script**

Do this by script, not by hand — hand-copying 1,951 lines will introduce errors that the reconstruction test will catch but that waste a cycle. Run from the repo root:

```bash
./.venv/bin/python - <<'EOF'
from pathlib import Path

STYLE_OPEN = "        <style>\n"
STYLE_CLOSE = "        </style>\n"
SCRIPT_OPEN = "        <script>\n"
SCRIPT_CLOSE = "        </script>\n"
LINK_TAG = '        <link rel="stylesheet" href="/static/dashboard.css">\n'
SCRIPT_TAG = '        <script src="/static/dashboard.js"></script>\n'

html = Path("tests/fixtures/dashboard_baseline.html").read_text(encoding="utf-8")

pre, rest = html.split(STYLE_OPEN, 1)
css, rest = rest.split(STYLE_CLOSE, 1)
mid, rest = rest.split(SCRIPT_OPEN, 1)
js, post = rest.split(SCRIPT_CLOSE, 1)

template = pre + LINK_TAG + mid + SCRIPT_TAG + post

Path("templates").mkdir(exist_ok=True)
Path("static").mkdir(exist_ok=True)
Path("static/dashboard.css").write_text(css, encoding="utf-8")
Path("static/dashboard.js").write_text(js, encoding="utf-8")
Path("templates/dashboard.html").write_text(template, encoding="utf-8")

print(f"css   {css.count(chr(10))} lines")
print(f"js    {js.count(chr(10))} lines")
print(f"html  {template.count(chr(10))} lines")
EOF
```

Expected exactly: `css 635 lines`, `js 1316 lines`, `html 273 lines`. If `.split()` raises `ValueError: not enough values to unpack`, a marker string does not match the file byte-for-byte — print `repr()` around the region and correct the constant rather than loosening the split.

- [ ] **Step 2: Run the reconstruction test to verify it now passes**

```bash
./.venv/bin/python -m unittest tests.test_dashboard_frontend -v
```

Expected: 2 tests PASS. This is the proof that every CSS and JS byte survived unaltered. If `test_extracted_assets_reconstruct_original_html_exactly` fails, the extraction changed bytes — do not "fix" the test.

- [ ] **Step 3: Confirm no reindentation crept in**

```bash
head -3 static/dashboard.css | od -c | head -4
grep -c "^            " static/dashboard.js
```

Expected: the CSS begins with 12 spaces before `:root {`, and the JS has thousands of deeply indented lines. Both files should look over-indented — that is correct and required.

- [ ] **Step 4: Commit**

```bash
git add templates/dashboard.html static/dashboard.css static/dashboard.js
git commit -m "refactor: extract dashboard frontend into template and static files"
```

---

### Task 3: Serve the extracted assets from dashboard.py

**Files:**
- Modify: `dashboard.py:826-3056` (replace the `dashboard()` route body), plus the import block at `dashboard.py:18` and the constants block at `dashboard.py:26-32`
- Modify: `tests/test_dashboard_frontend.py` (add route tests)

**Interfaces:**
- Consumes: `templates/dashboard.html`, `static/dashboard.css`, `static/dashboard.js` from Task 2.
- Produces: module-level `DASHBOARD_HTML: str` in `dashboard.py`, a `StaticFiles` mount at `/static`, and a `dashboard()` route returning `HTMLResponse(content=DASHBOARD_HTML)`.

- [ ] **Step 1: Write the failing route tests**

Append to `tests/test_dashboard_frontend.py`, above the `if __name__` block:

```python
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
```

- [ ] **Step 2: Run to verify the new tests fail**

```bash
./.venv/bin/python -m unittest tests.test_dashboard_frontend -v
```

Expected: `ReconstructionTests` still PASS; all four `RouteTests` FAIL — the index still contains `--primary-color`, and `/static/...` returns 404 because nothing is mounted.

- [ ] **Step 3: Add the StaticFiles import**

In `dashboard.py`, directly below the existing `from fastapi.responses import ...` line (currently line 18):

```python
from fastapi.staticfiles import StaticFiles
```

- [ ] **Step 4: Add path constants and load the template at import time**

In `dashboard.py`, after the existing `UPDATE_PENDING_PATH` constant (line 32):

```python
TEMPLATES_PATH = os.path.join(BASE_PATH, "templates")
STATIC_PATH = os.path.join(BASE_PATH, "static")

# Read once at import, matching the previous behavior of returning a constant
# string. Editing the template requires a service restart, exactly as editing
# dashboard.py did before.
with open(os.path.join(TEMPLATES_PATH, "dashboard.html"), "r", encoding="utf-8") as _f:
    DASHBOARD_HTML = _f.read()
```

- [ ] **Step 5: Mount the static directory**

In `dashboard.py`, immediately after the `app = FastAPI(...)` line (line 37):

```python
app.mount("/static", StaticFiles(directory=STATIC_PATH), name="static")
```

- [ ] **Step 6: Replace the 2,231-line route body**

Delete `dashboard.py` lines 829 through 3056 inclusive — that is everything from `    html_content = """` through `    return HTMLResponse(content=html_content)` — and replace with:

```python
    return HTMLResponse(content=DASHBOARD_HTML)
```

Leave the decorator, `async def dashboard():`, and the docstring on lines 826-828 exactly as they are.

- [ ] **Step 7: Run the full test suite**

```bash
./.venv/bin/python -m unittest tests.test_dashboard_exports tests.test_dashboard_frontend -v
```

Expected: all tests PASS — the 6 tests in `test_dashboard_frontend.py` and the 4 pre-existing tests in `test_dashboard_exports.py`. The export tests must be unaffected; if they broke, something outside the route body was edited.

- [ ] **Step 8: Confirm the file actually shrank**

```bash
wc -l dashboard.py
git diff --stat HEAD
```

Expected: `dashboard.py` drops from 3,788 lines to roughly 1,560. If it did not shrink by ~2,230 lines, the deletion in Step 6 was incomplete.

- [ ] **Step 9: Commit**

```bash
git add dashboard.py tests/test_dashboard_frontend.py
git commit -m "refactor: serve dashboard frontend from template and static mount"
```

---

### Task 4: Verify on the Pi

**Files:** None modified. This task is verification only.

**Interfaces:**
- Consumes: the deployed result of Tasks 1-3.
- Produces: confirmation that the browser and the port-80 proxy behave identically to before. Nothing downstream depends on this task's output, but Phase 1 must not start until it passes.

The reconstruction test proves the bytes are intact. It cannot prove a browser loads them, that MIME types are right in the real server, or that the port-80 proxy forwards `/static/`. Only the device can.

`dashboard_proxy.py` forwards `self.path` verbatim to the backend, so `/static/*` requires no proxy change — but confirm rather than assume.

- [ ] **Step 1: Deploy and restart**

```bash
ssh cam@reframe.local 'cd ~/reframe && git pull && sudo systemctl restart reframe-dashboard.service'
```

- [ ] **Step 2: Confirm the service came up clean**

```bash
ssh cam@reframe.local 'systemctl is-active reframe-dashboard.service && journalctl -u reframe-dashboard.service -n 30 --no-pager'
```

Expected: `active`, and no traceback. A `FileNotFoundError` on `templates/dashboard.html` means `WorkingDirectory` differs from `BASE_PATH` — the constants in Step 4 of Task 3 must use `BASE_PATH`, not a relative path.

- [ ] **Step 3: Confirm assets serve through the port-80 proxy, not just the backend**

```bash
ssh cam@reframe.local 'curl -s -o /dev/null -w "%{http_code} %{content_type}\n" http://127.0.0.1/static/dashboard.css http://127.0.0.1/static/dashboard.js http://127.0.0.1/'
```

Expected: `200 text/css`, `200 text/javascript` (or `application/javascript`), `200 text/html`. A 404 here with a 200 on port 8000 means the proxy is mangling the path.

- [ ] **Step 4: Exercise the real UI from a phone**

Open `http://reframe.local` and confirm, by hand:

- Gallery thumbnails load and paginate.
- Opening a photo works; download original and download dithered both work.
- The settings panel opens, and changing a dither slider persists after reload.
- "Display on screen" for an existing photo triggers a panel refresh.
- Battery percentage renders.
- The browser console shows **zero** errors (a stale cached asset shows up here first — hard-reload before concluding).

- [ ] **Step 5: Record the result**

If every check passed, Phase 0 is complete and Phase 1 may begin. If anything failed, fix it before starting Phase 1 — feature work on a broken extraction will make the cause impossible to isolate.

---

## Notes for the implementer

**This extraction was verified end-to-end before the plan was written.** The
split-and-reconstruct round-trip in Tasks 1-2 was executed against the current
`dashboard.py` and reproduced the original string byte-for-byte. Every
"Expected exactly" figure in this plan is a measured value, not an estimate. If
one of them does not match, treat it as a signal that the source has changed —
not as a rounding difference to shrug off.

**Why byte-exactness matters more than tidiness here.** The temptation is to reindent the extracted CSS and JS, since they carry pointless 8-to-12-space indentation inherited from being inside a Python string. Resist it. The JavaScript builds HTML with multi-line template literals; their embedded newlines and leading spaces are part of the emitted strings. Reindenting would change program output while looking purely cosmetic in review, and it forfeits the one property that makes this refactor provably safe. Tidy formatting is worth nothing next to a proof that behavior did not change.

**If the reconstruction test fails, the extraction is wrong — not the test.** Its entire job is to fail when bytes change. Never adjust the assertion to make it pass.

**Scope discipline.** No feature work in Phase 0. Recipes, manual exposure, capture programs, and the LED all belong to later phases. A pure refactor that also changed behavior is unreviewable, and the point of doing this first is that Phase 1 lands on a clean base.
