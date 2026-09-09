#!/usr/bin/env python3

import os
import json
import sys
import logging
import asyncio
import copy
import time
import shutil
import math
import tempfile
import threading
import re
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
import httpx
from PIL import Image

import recipes

CAMERA_AVAILABLE = False

# Constants
BASE_PATH = os.path.dirname(os.path.realpath(__file__))
PHOTOS_PATH = os.path.join(BASE_PATH, "photos")
DITHERED_PHOTOS_PATH = os.path.join(BASE_PATH, "dithered_photos")
SETTINGS_PATH = os.path.join(BASE_PATH, "settings.json")
USER_DATA_PATHS = ["settings.json", "photos", "dithered_photos"]
UPDATE_HELPER_PATH = "/usr/local/sbin/reframe-apply-update"
UPDATE_PENDING_PATH = os.path.join(BASE_PATH, ".runtime", "update_pending")

TEMPLATES_PATH = os.path.join(BASE_PATH, "templates")
STATIC_PATH = os.path.join(BASE_PATH, "static")

# Read once at import, matching the previous behavior of returning a constant
# string. Editing the template requires a service restart, exactly as editing
# dashboard.py did before.
with open(os.path.join(TEMPLATES_PATH, "dashboard.html"), "r", encoding="utf-8") as _f:
    DASHBOARD_HTML = _f.read()

os.makedirs(PHOTOS_PATH, exist_ok=True)
os.makedirs(DITHERED_PHOTOS_PATH, exist_ok=True)


class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles that forces revalidation instead of heuristic caching.

    /static/dashboard.js is a stable URL whose body changes on every
    software update. Without an explicit Cache-Control, a browser can cache
    it heuristically and keep running pre-update JavaScript against
    post-update HTML after the camera updates, with no visible error.
    ETag/Last-Modified still let a revalidation collapse to a cheap 304.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(title="Reframe Dashboard", description="Control & Gallery Interface for Reframe Camera")
app.mount("/static", RevalidatingStaticFiles(directory=STATIC_PATH), name="static")


def prepare_dithered_export(image_path: str, upscale_2x: bool) -> tuple[bytes, str, str]:
    """Read a dithered image, optionally returning a lossless 2x PNG export."""
    source_path = Path(image_path)
    if not upscale_2x:
        return source_path.read_bytes(), source_path.name, "image/png"

    with Image.open(source_path) as image:
        resampling = getattr(Image, "Resampling", Image)
        enlarged = image.resize(
            (image.width * 2, image.height * 2),
            resampling.NEAREST
        )
        output = BytesIO()
        enlarged.save(output, format="PNG")

    return output.getvalue(), f"{source_path.stem}.png", "image/png"


class SettingsValidationError(ValueError):
    pass


def validate_settings(settings: Dict[str, Any]) -> None:
    """Validate persisted settings against the hardware and dashboard contract."""
    if not isinstance(settings, dict):
        raise SettingsValidationError("Settings must be a JSON object")

    def section(parent: Dict[str, Any], key: str, path: str) -> Dict[str, Any]:
        value = parent.get(key, {})
        if not isinstance(value, dict):
            raise SettingsValidationError(f"{path} must be an object")
        return value

    def number(value: Any, path: str, minimum: float, maximum: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SettingsValidationError(f"{path} must be a number")
        if value < minimum or value > maximum:
            raise SettingsValidationError(f"{path} must be between {minimum} and {maximum}")

    def integer(value: Any, path: str, minimum: int, maximum: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsValidationError(f"{path} must be an integer")
        if value < minimum or value > maximum:
            raise SettingsValidationError(f"{path} must be between {minimum} and {maximum}")

    def boolean(value: Any, path: str) -> None:
        if not isinstance(value, bool):
            raise SettingsValidationError(f"{path} must be true or false")

    def text(value: Any, path: str, maximum: int, allow_controls: bool = False) -> None:
        if not isinstance(value, str):
            raise SettingsValidationError(f"{path} must be text")
        if len(value) > maximum:
            raise SettingsValidationError(f"{path} must be {maximum} characters or fewer")
        if not allow_controls and any(ord(char) < 32 for char in value):
            raise SettingsValidationError(f"{path} cannot contain control characters")

    camera = section(settings, "camera", "camera")
    resolution = section(camera, "resolution", "camera.resolution")
    integer(resolution.get("width"), "camera.resolution.width", 100, 4000)
    integer(resolution.get("height"), "camera.resolution.height", 100, 4000)
    number(camera.get("exposure_value"), "camera.exposure_value", -2, 2)
    number(camera.get("sharpness"), "camera.sharpness", 0, 10)
    if camera.get("autofocus_mode") not in {0, 1, 2}:
        raise SettingsValidationError("camera.autofocus_mode must be 0, 1, or 2")
    if camera.get("exposure_mode", "auto") not in {"auto", "manual"}:
        raise SettingsValidationError("camera.exposure_mode must be auto or manual")
    integer(camera.get("exposure_time_us", 0), "camera.exposure_time_us", 0, 200000000)
    number(camera.get("analogue_gain", 1.0), "camera.analogue_gain", 1.0, 16.0)

    processing = section(settings, "processing", "processing")
    number(processing.get("saturation"), "processing.saturation", 0, 2)
    number(processing.get("brightness_factor"), "processing.brightness_factor", 0.1, 3)
    number(processing.get("color_factor"), "processing.color_factor", 0.1, 3)
    if processing.get("dithering_method") not in {"floyd_steinberg", "ordered"}:
        raise SettingsValidationError("processing.dithering_method is unsupported")
    if processing.get("bayer_size") not in {2, 4, 8}:
        raise SettingsValidationError("processing.bayer_size must be 2, 4, or 8")
    number(processing.get("threshold_scale"), "processing.threshold_scale", 0.1, 2)

    display = section(settings, "display", "display")
    boolean(display.get("auto_display"), "display.auto_display")
    number(display.get("display_timeout"), "display.display_timeout", 0, 3600)

    system = section(settings, "system", "system")
    integer(system.get("auto_refresh_interval"), "system.auto_refresh_interval", 5, 300)
    integer(system.get("auto_timeout_minutes"), "system.auto_timeout_minutes", 1, 60)
    boolean(system.get("auto_timeout_enabled"), "system.auto_timeout_enabled")
    boolean(system.get("show_dashboard_qr_on_wifi_connect"), "system.show_dashboard_qr_on_wifi_connect")
    text(system.get("camera_name", ""), "system.camera_name", 80)

    exports = section(settings, "exports", "exports")
    boolean(exports.get("upscale_dithered_2x"), "exports.upscale_dithered_2x")

    extensions = section(settings, "extensions", "extensions")
    arena = section(extensions, "arena", "extensions.arena")
    boolean(arena.get("enabled"), "extensions.arena.enabled")
    text(arena.get("channel", ""), "extensions.arena.channel", 200)
    text(arena.get("access_token", ""), "extensions.arena.access_token", 4096)

    recipes_section = section(settings, "recipes", "recipes")
    items = recipes_section.get("items")
    if not isinstance(items, list) or not items:
        raise SettingsValidationError("recipes.items must be a non-empty list")
    if len(items) > recipes.RECIPE_LIMIT:
        raise SettingsValidationError(
            f"recipes.items must contain {recipes.RECIPE_LIMIT} recipes or fewer")

    seen_ids = set()
    for index, recipe in enumerate(items):
        path = f"recipes.items[{index}]"
        if not isinstance(recipe, dict):
            raise SettingsValidationError(f"{path} must be an object")
        text(recipe.get("id"), f"{path}.id", 64)
        text(recipe.get("name"), f"{path}.name", 80)
        if not recipe.get("id"):
            raise SettingsValidationError(f"{path}.id must not be empty")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", recipe["id"]):
            raise SettingsValidationError(f"{path}.id must contain only lowercase letters, digits, and hyphens, and must start with a letter or digit")
        if recipe["id"] in seen_ids:
            raise SettingsValidationError(f"{path}.id duplicates an earlier recipe id")
        seen_ids.add(recipe["id"])

        capture = section(recipe, "capture", f"{path}.capture")
        if capture.get("exposure_mode") not in {"auto", "manual"}:
            raise SettingsValidationError(f"{path}.capture.exposure_mode must be auto or manual")
        # These bounds are deliberately generous, not sensor-reported: the
        # dashboard and camera are separate processes with no startup
        # ordering guarantee, so validation here cannot depend on the camera
        # being up. The /api/camera/limits route bounds the UI's inputs
        # instead, once the camera process is actually reachable.
        integer(capture.get("exposure_time_us"), f"{path}.capture.exposure_time_us", 0, 200000000)
        if capture.get("exposure_mode") == "manual" and not capture.get("exposure_time_us"):
            raise SettingsValidationError(
                f"{path}.capture.exposure_time_us must be greater than 0 when "
                f"{path}.capture.exposure_mode is manual")
        number(capture.get("analogue_gain"), f"{path}.capture.analogue_gain", 1.0, 16.0)
        number(capture.get("exposure_value"), f"{path}.capture.exposure_value", -2, 2)
        number(capture.get("sharpness"), f"{path}.capture.sharpness", 0, 10)
        if capture.get("autofocus_mode") not in {0, 1, 2}:
            raise SettingsValidationError(f"{path}.capture.autofocus_mode must be 0, 1, or 2")

        render = section(recipe, "render", f"{path}.render")
        number(render.get("saturation"), f"{path}.render.saturation", 0, 2)
        number(render.get("brightness_factor"), f"{path}.render.brightness_factor", 0.1, 3)
        number(render.get("color_factor"), f"{path}.render.color_factor", 0.1, 3)
        if render.get("dithering_method") not in {"floyd_steinberg", "ordered"}:
            raise SettingsValidationError(f"{path}.render.dithering_method is unsupported")
        if render.get("bayer_size") not in {2, 4, 8}:
            raise SettingsValidationError(f"{path}.render.bayer_size must be 2, 4, or 8")
        number(render.get("threshold_scale"), f"{path}.render.threshold_scale", 0.1, 2)

    if recipes_section.get("active") not in seen_ids:
        raise SettingsValidationError("recipes.active must name an existing recipe id")

    trigger = section(settings, "trigger", "trigger")
    if trigger.get("program", "single") not in {"single", "bracket"}:
        raise SettingsValidationError("trigger.program must be 'single' or 'bracket'")
    bracket = section(trigger, "bracket", "trigger.bracket")
    if bracket.get("frames", 3) not in {3, 5}:
        raise SettingsValidationError("trigger.bracket.frames must be 3 or 5")
    number(bracket.get("step_ev", 1.0), "trigger.bracket.step_ev", 0.3, 2.0)

class SettingsManager:
    """Manages settings operations for the dashboard."""
    
    def __init__(self, settings_path: str = SETTINGS_PATH):
        self.settings_path = Path(settings_path)
        self.default_settings = {
            "camera": {
                "resolution": {"width": 1200, "height": 800},
                "exposure_value": 0,
                "sharpness": 3,
                "autofocus_mode": 2
            },
            "processing": {
                "saturation": 0.6,
                "brightness_factor": 1.1,
                "color_factor": 1.4,
                "dithering_method": "floyd_steinberg",
                "bayer_size": 4,
                "threshold_scale": 1.0
            },
            "recipes": {
                "active": "standard",
                "items": []
            },
            "trigger": {
                "program": "single",
                "bracket": {"frames": 3, "step_ev": 1.0}
            },
            "display": {
                "auto_display": True,
                "display_timeout": 0
            },
            "system": {
                "auto_refresh_interval": 30,
                "auto_timeout_minutes": 10,
                "auto_timeout_enabled": True,
                "show_dashboard_qr_on_wifi_connect": True,
                "camera_name": ""
            },
            "exports": {
                "upscale_dithered_2x": False
            },
            "extensions": {
                "arena": {
                    "enabled": False,
                    "channel": "",
                    "access_token": ""
                }
            }
        }
        self.default_settings["recipes"]["items"] = recipes.built_in_recipes(
            self.default_settings["camera"],
            self.default_settings["processing"],
        )
        self._ensure_settings_file()
    
    def _ensure_settings_file(self):
        """Ensure settings file exists with default values."""
        if not self.settings_path.exists():
            self.save_settings(self.default_settings)
    
    def load_settings(self) -> Dict[str, Any]:
        """Load settings from JSON file, migrating pre-recipe files in memory."""
        try:
            with open(self.settings_path, 'r') as f:
                settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            settings = copy.deepcopy(self.default_settings)

        # Migrate BEFORE merging with defaults: the migration needs to see
        # whether the file itself had recipes, and must promote the user's own
        # camera/processing values into Standard rather than the shipped ones.
        settings = recipes.migrate_settings(
            settings,
            self.default_settings["camera"],
            self.default_settings["processing"],
        )
        return self._merge_with_defaults(settings)

    def load_public_settings(self) -> Dict[str, Any]:
        """Load settings safe to return to the browser."""
        settings = self.load_settings()
        arena_settings = settings.get("extensions", {}).get("arena", {})
        access_token = arena_settings.get("access_token", "")
        arena_settings["access_token"] = ""
        arena_settings["access_token_configured"] = bool(access_token)
        return settings
    
    def save_settings(self, settings: Dict[str, Any]) -> bool:
        """Save settings to JSON file."""
        try:
            if not isinstance(settings, dict):
                raise SettingsValidationError("Settings must be a JSON object")
            # Merge with existing settings to preserve structure
            current_settings = self.load_settings()
            settings = self._prepare_settings_for_save(current_settings, settings)
            merged_settings = self._deep_merge(current_settings, settings)
            validate_settings(merged_settings)
            merged_settings = recipes.sync(
                merged_settings,
                recipes_changed="recipes" in settings,
            )
            validate_settings(merged_settings)
            self._write_settings(merged_settings)
            return True
        except SettingsValidationError:
            raise
        except Exception as e:
            print(f"Error saving settings: {e}")
            return False

    def replace_settings(self, settings: Dict[str, Any]) -> None:
        """Atomically replace settings with a previously validated snapshot."""
        validate_settings(settings)
        self._write_settings(settings)

    def _write_settings(self, settings: Dict[str, Any]) -> None:
        """Write JSON beside the target and atomically move it into place."""
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{self.settings_path.name}.",
            dir=str(self.settings_path.parent)
        )
        try:
            with os.fdopen(fd, "w") as temp_file:
                json.dump(settings, temp_file, indent=2)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, self.settings_path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
    
    def _merge_with_defaults(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Merge loaded settings with defaults to ensure all keys exist."""
        system_settings = settings.get("system")
        if isinstance(system_settings, dict):
            legacy_value = system_settings.pop("show_dashboard_qr_on_first_network", None)
            if "show_dashboard_qr_on_wifi_connect" not in system_settings and legacy_value is not None:
                system_settings["show_dashboard_qr_on_wifi_connect"] = legacy_value
        return self._deep_merge(self.default_settings.copy(), settings)
    
    def _deep_merge(self, default: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """Deep merge two dictionaries."""
        result = default.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _prepare_settings_for_save(self, current_settings: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
        """Apply write-only extension secret semantics before merging settings."""
        extensions = settings.get("extensions")
        if not isinstance(extensions, dict):
            return settings

        arena_settings = extensions.get("arena")
        if not isinstance(arena_settings, dict):
            return settings

        current_token = (
            current_settings
            .get("extensions", {})
            .get("arena", {})
            .get("access_token", "")
        )
        incoming_token = arena_settings.get("access_token")
        clear_token = arena_settings.pop("access_token_clear", False)
        if not isinstance(clear_token, bool):
            raise SettingsValidationError("extensions.arena.access_token_clear must be true or false")

        if clear_token:
            arena_settings["access_token"] = ""
        elif incoming_token is None or incoming_token == "":
            arena_settings["access_token"] = current_token

        arena_settings.pop("access_token_configured", None)
        return settings

    def clear_extension_secret(self, extension_id: str, secret_key: str) -> bool:
        """Clear one stored extension secret."""
        settings = self.load_settings()
        extension_settings = settings.get("extensions", {}).get(extension_id)
        if not isinstance(extension_settings, dict) or secret_key not in extension_settings:
            return False
        extension_settings[secret_key] = ""
        try:
            validate_settings(settings)
            self._write_settings(settings)
            return True
        except Exception as e:
            print(f"Error clearing extension secret: {e}")
            return False
    
    def get_camera_settings(self) -> Dict[str, Any]:
        """Get camera-specific settings."""
        return self.load_settings().get("camera", {})
    
    def get_processing_settings(self) -> Dict[str, Any]:
        """Get processing-specific settings."""
        return self.load_settings().get("processing", {})


class ReframeClient:
    """HTTP client to talk to the main reframe hardware service."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(30.0)

    async def get(self, path: str):
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    async def post(self, path: str, json: Optional[Dict[str, Any]] = None):
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=json)
            resp.raise_for_status()
            return resp.json()

    async def delete(self, path: str):
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.delete(url)
            resp.raise_for_status()
            return resp.json()


async def run_repo_command(args: List[str], timeout: int = 30) -> Dict[str, Any]:
    """Run a command in the repo and return captured output."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=BASE_PATH,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {
            "returncode": 124,
            "stdout": "",
            "stderr": f"Command timed out: {' '.join(args)}"
        }
    except Exception as e:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": str(e)
        }

    return {
        "returncode": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace").strip(),
        "stderr": stderr.decode("utf-8", errors="replace").strip()
    }


async def run_git(args: List[str], timeout: int = 30) -> Dict[str, Any]:
    return await run_repo_command(["git", *args], timeout=timeout)


def git_error_detail(result: Dict[str, Any], fallback: str) -> str:
    return result.get("stderr") or result.get("stdout") or fallback


async def get_update_status(fetch: bool = True) -> Dict[str, Any]:
    """Fetch and compare this checkout with its upstream branch."""
    if not os.path.isdir(os.path.join(BASE_PATH, ".git")):
        raise HTTPException(status_code=501, detail="Software updates require a git checkout")

    if fetch:
        fetch_result = await run_git(["fetch", "--quiet", "origin"], timeout=60)
        if fetch_result["returncode"] != 0:
            detail = git_error_detail(fetch_result, "git fetch failed")
            raise HTTPException(status_code=502, detail=f"Could not check for updates: {detail}")

    branch_result = await run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_result["stdout"] if branch_result["returncode"] == 0 else "unknown"

    upstream_result = await run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    upstream = upstream_result["stdout"] if upstream_result["returncode"] == 0 else "origin/main"

    local_result = await run_git(["rev-parse", "HEAD"])
    remote_result = await run_git(["rev-parse", upstream])
    if local_result["returncode"] != 0 or remote_result["returncode"] != 0:
        raise HTTPException(status_code=500, detail="Could not determine current software version")

    local_revision = local_result["stdout"]
    remote_revision = remote_result["stdout"]
    base_result = await run_git(["merge-base", "HEAD", upstream])
    merge_base = base_result["stdout"] if base_result["returncode"] == 0 else ""

    counts_result = await run_git(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"])
    ahead = 0
    behind = 0
    if counts_result["returncode"] == 0:
        parts = counts_result["stdout"].split()
        if len(parts) == 2:
            ahead = int(parts[0])
            behind = int(parts[1])

    dirty_result = await run_git(["status", "--porcelain", "--untracked-files=no"])
    has_tracked_changes = bool(dirty_result["stdout"]) if dirty_result["returncode"] == 0 else True

    update_available = local_revision != remote_revision and merge_base == local_revision
    up_to_date = local_revision == remote_revision
    diverged = local_revision != remote_revision and merge_base != local_revision
    post_install_pending = os.path.exists(UPDATE_PENDING_PATH)
    can_update = (update_available or post_install_pending) and not has_tracked_changes

    if post_install_pending:
        message = "Code is up to date, but installation steps still need to finish."
    elif up_to_date:
        message = "Software is up to date."
    elif update_available:
        message = f"Update available: {behind} commit{'s' if behind != 1 else ''} behind {upstream}."
    elif diverged:
        message = "This checkout has diverged from upstream and cannot be updated automatically."
    else:
        message = "Could not determine update state."

    if update_available and has_tracked_changes:
        message = f"{message} Local code changes must be committed or discarded before updating."

    return {
        "branch": branch,
        "upstream": upstream,
        "local_revision": local_revision,
        "remote_revision": remote_revision,
        "local_short": local_revision[:7],
        "remote_short": remote_revision[:7],
        "ahead": ahead,
        "behind": behind,
        "update_available": update_available,
        "up_to_date": up_to_date,
        "diverged": diverged,
        "has_tracked_changes": has_tracked_changes,
        "post_install_pending": post_install_pending,
        "can_update": can_update,
        "message": message
    }


def backup_settings_for_update() -> Optional[str]:
    """Back up settings before pulling code; ignored photo dirs are left untouched."""
    settings_path = Path(SETTINGS_PATH)
    if not settings_path.exists():
        return None

    backup_dir = Path(BASE_PATH) / ".update_backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(settings_path, backup_dir / "settings.json")
    return str(backup_dir)


def ignored_user_data_paths() -> List[str]:
    return [path for path in USER_DATA_PATHS if os.path.exists(os.path.join(BASE_PATH, path))]


class PhotoManager:
    """Manages photo operations for the dashboard."""
    
    def __init__(self):
        self.photos_path = Path(PHOTOS_PATH)
        self.dithered_path = Path(DITHERED_PHOTOS_PATH)
        
    def get_all_photos(self, page: int = 1, limit: int = 20) -> Dict[str, Any]:
        """Get paginated list of photos with metadata."""
        all_photos = []
        
        # Get all files from photos directory
        if self.photos_path.exists():
            for photo_file in sorted(self.photos_path.iterdir(), reverse=True):
                if photo_file.is_file() and photo_file.suffix.lower() in ['.jpg', '.jpeg', '.png']:
                    # Look for corresponding dithered version
                    dithered_file = self.dithered_path / f"{photo_file.stem}_dithered.png"
                    if not dithered_file.exists():
                        dithered_file = self.dithered_path / f"{photo_file.stem}_dithered{photo_file.suffix}"
                    if not dithered_file.exists():
                        # Try without _dithered suffix for exact matches
                        dithered_file = self.dithered_path / photo_file.name
                    
                    photo_info = {
                        "id": photo_file.stem,
                        "filename": photo_file.name,
                        "original_path": f"/photos/{photo_file.name}",
                        "dithered_path": f"/dithered/{dithered_file.name}" if dithered_file.exists() else None,
                        "has_dithered": dithered_file.exists(),
                        "size": photo_file.stat().st_size,
                        "created": datetime.fromtimestamp(photo_file.stat().st_mtime).isoformat()
                    }
                    sidecar = self._read_sidecar(photo_file)
                    photo_info["group_id"] = sidecar.get("group_id")
                    photo_info["frame_label"] = sidecar.get("frame_label")
                    photo_info["recipe_name"] = sidecar.get("recipe_name")
                    photo_info["program"] = sidecar.get("program")
                    all_photos.append(photo_info)
        
        # Calculate pagination
        total_photos = len(all_photos)
        total_pages = (total_photos + limit - 1) // limit  # Ceiling division
        start_index = (page - 1) * limit
        end_index = start_index + limit
        
        # Get photos for current page
        photos_page = all_photos[start_index:end_index]
        
        return {
            "photos": photos_page,
            "pagination": {
                "current_page": page,
                "total_pages": total_pages,
                "total_photos": total_photos,
                "photos_per_page": limit,
                "has_next": page < total_pages,
                "has_prev": page > 1
            }
        }
    
    def _read_sidecar(self, photo_file):
        """Provenance for a photo, or an empty dict.

        Photos taken before sidecars existed have none, and a half-written or
        malformed file must not take the gallery down, so every failure
        returns empty. This includes syntactically valid JSON that isn't an
        object (e.g. `[]`, `"text"`, `42`, `null`) — callers use `.get(...)`,
        so anything other than a dict is treated the same as "no sidecar".
        """
        try:
            sidecar_file = photo_file.with_suffix(".json")
            if not sidecar_file.exists():
                return {}
            parsed = json.loads(sidecar_file.read_text(encoding="utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def get_photo_info(self, photo_id: str) -> Dict:
        """Get information about a specific photo."""
        # Get all photos without pagination to search through them
        all_photos_data = self.get_all_photos(page=1, limit=10000)  # Large limit to get all
        photos = all_photos_data["photos"]
        for photo in photos:
            if photo["id"] == photo_id:
                return photo
        raise HTTPException(status_code=404, detail="Photo not found")


class DashboardExtension:
    """Base interface for dashboard photo extensions."""

    id = ""
    label = ""
    action_label = ""
    requires_dithered = True

    def get_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        return settings.get("extensions", {}).get(self.id, {})

    def enabled(self, settings: Dict[str, Any]) -> bool:
        return bool(self.get_settings(settings).get("enabled", False))

    def configured(self, settings: Dict[str, Any]) -> bool:
        return self.enabled(settings)

    def public_action(self, settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.configured(settings):
            return None
        return {
            "id": self.id,
            "label": self.label,
            "action_label": self.action_label,
            "requires_dithered": self.requires_dithered
        }

    async def run(self, photo: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class ArenaExtension(DashboardExtension):
    """Upload dithered photos to an Are.na channel using the v3 API."""

    id = "arena"
    label = "Are.na"
    action_label = "are.na"
    requires_dithered = True
    api_base = "https://api.are.na"
    s3_public_base = "https://s3.amazonaws.com/arena_images-temp"
    user_agent = "reFrame"
    max_retries = 3
    retry_delay = 2

    def configured(self, settings: Dict[str, Any]) -> bool:
        extension_settings = self.get_settings(settings)
        return (
            self.enabled(settings)
            and bool(extension_settings.get("channel"))
            and bool(extension_settings.get("access_token"))
        )

    async def run(self, photo: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
        extension_settings = self.get_settings(settings)
        channel = str(extension_settings.get("channel", "")).strip()
        access_token = str(extension_settings.get("access_token", "")).strip()
        dithered_path = photo.get("dithered_path")

        if not self.enabled(settings):
            raise HTTPException(status_code=400, detail="Are.na extension is disabled")
        if not channel:
            raise HTTPException(status_code=400, detail="Are.na channel is not configured")
        if not access_token:
            raise HTTPException(status_code=400, detail="Are.na access token is not configured")
        if not dithered_path:
            raise HTTPException(status_code=400, detail="This photo does not have a dithered version to upload")
        if not os.path.exists(dithered_path):
            raise HTTPException(status_code=404, detail="Dithered photo file was not found")

        upscale_2x = bool(settings.get("exports", {}).get("upscale_dithered_2x", False))
        try:
            upload_content, filename, upload_content_type = await asyncio.to_thread(
                prepare_dithered_export,
                dithered_path,
                upscale_2x
            )
        except OSError as error:
            raise HTTPException(status_code=500, detail=f"Could not prepare dithered upload: {error}") from error

        headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": self.user_agent
        }
        timeout = httpx.Timeout(60.0)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                presign_resp = await self._request_with_retry(
                    client,
                    "post",
                    f"{self.api_base}/v3/uploads/presign",
                    headers=headers,
                    json={"files": [{"filename": filename, "content_type": upload_content_type}]}
                )
                self._raise_for_arena_error(presign_resp, "Could not create Are.na upload URL")
                presign_data = presign_resp.json()
                presigned_file = (presign_data.get("files") or [None])[0]
                if not presigned_file:
                    raise HTTPException(status_code=502, detail="Are.na did not return an upload URL")

                upload_url = presigned_file.get("upload_url")
                key = presigned_file.get("key")
                content_type = presigned_file.get("content_type", upload_content_type)
                if not upload_url or not key:
                    raise HTTPException(status_code=502, detail="Are.na upload URL response was incomplete")

                upload_resp = await client.put(
                    upload_url,
                    content=upload_content,
                    headers={"Content-Type": content_type}
                )
                if upload_resp.status_code >= 400:
                    raise HTTPException(status_code=502, detail="Upload to Are.na storage failed")

                s3_url = f"{self.s3_public_base}/{key}"
                photo_id = photo.get("id", "unknown")
                created = photo.get("created_at") or photo.get("created")
                description = self._build_block_description(created, settings)

                block_resp = await self._request_with_retry(
                    client,
                    "post",
                    f"{self.api_base}/v3/blocks",
                    headers=headers,
                    json={
                        "value": s3_url,
                        "channels": [{"id": channel}],
                        "title": f"reFrame {photo_id}",
                        "description": description,
                        "metadata": {
                            "source": "reframe",
                            "photo_id": photo_id
                        }
                    }
                )
                self._raise_for_arena_error(block_resp, "Could not create Are.na block")
                block = block_resp.json()
        except HTTPException:
            raise
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach Are.na: {e}") from e

        block_id = block.get("id")
        block_url = (
            block.get("url")
            or block.get("href")
            or block.get("_links", {}).get("self", {}).get("href")
        )
        return {
            "status": "success",
            "message": "Uploaded dithered photo to Are.na",
            "extension": self.id,
            "block_id": block_id,
            "url": block_url
        }

    async def _request_with_retry(self, client, method: str, url: str, **kwargs) -> httpx.Response:
        last_error = None
        for attempt in range(self.max_retries):
            try:
                response = await getattr(client, method)(url, **kwargs)
            except httpx.RequestError as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay)
                    continue
                raise

            if response.status_code == 429 and attempt < self.max_retries - 1:
                await asyncio.sleep(self._rate_limit_wait_seconds(response))
                continue

            if response.status_code >= 500 and attempt < self.max_retries - 1:
                await asyncio.sleep(self.retry_delay)
                continue

            return response

        if last_error:
            raise last_error
        raise HTTPException(status_code=502, detail="Are.na request failed after retries")

    def _rate_limit_wait_seconds(self, response: httpx.Response) -> int:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(1, min(60, int(float(retry_after))))
            except ValueError:
                pass

        reset = response.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                return max(1, min(60, int(float(reset)) - int(time.time())))
            except ValueError:
                pass

        return self.retry_delay

    def _raise_for_arena_error(self, response: httpx.Response, fallback: str) -> None:
        if response.status_code < 400:
            return

        detail_by_status = {
            401: "Are.na access token is invalid or missing",
            403: "Are.na token does not have write access or cannot post to this channel",
            404: "Are.na channel was not found",
            408: "Are.na request timed out",
            429: "Are.na rate limit reached; try again later"
        }
        detail = detail_by_status.get(response.status_code, fallback)

        try:
            data = response.json()
            message = data.get("details", {}).get("message") or data.get("message")
            if message:
                detail = f"{detail}: {message}"
        except Exception:
            pass

        status_code = response.status_code if response.status_code in detail_by_status else 502
        raise HTTPException(status_code=status_code, detail=detail)

    def _build_block_description(self, created, settings: Dict[str, Any]) -> str:
        description = "Dithered photo shot on [reframe.camera](https://reframe.camera)"
        captured = self._format_captured_at(created)
        if captured:
            camera_name = str(settings.get("system", {}).get("camera_name", "")).strip()
            attribution = f" by {camera_name}" if camera_name else ""
            description = f"{description}\n\nCaptured on {captured}{attribution}"
        return description

    def _format_captured_at(self, created) -> Optional[str]:
        if not created:
            return None

        try:
            if isinstance(created, (int, float)):
                dt = datetime.fromtimestamp(created, timezone.utc)
            elif isinstance(created, str):
                normalized = created.replace("Z", "+00:00")
                dt = datetime.fromisoformat(normalized)
            else:
                return str(created)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
            return f'{dt.strftime("%B %-d, %Y at %-I:%M %p")} UTC'
        except Exception:
            return str(created)


class ExtensionRegistry:
    """Registry for server-side dashboard extensions."""

    def __init__(self, extensions: List[DashboardExtension]):
        self.extensions = {extension.id: extension for extension in extensions}

    def get(self, extension_id: str) -> Optional[DashboardExtension]:
        return self.extensions.get(extension_id)

    def public_actions(self, settings: Dict[str, Any]) -> List[Dict[str, Any]]:
        actions = []
        for extension in self.extensions.values():
            action = extension.public_action(settings)
            if action:
                actions.append(action)
        return actions

# Initialize managers
settings_manager = SettingsManager()
photo_manager = PhotoManager()
extension_registry = ExtensionRegistry([ArenaExtension()])

# Initialize HTTP client to the hardware service
REFRAME_API_BASE = os.environ.get("REFRAME_API_BASE", "http://127.0.0.1:8077/api")
reframe_client = ReframeClient(REFRAME_API_BASE)

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the main dashboard interface."""
    return HTMLResponse(content=DASHBOARD_HTML)

@app.get("/api/photos")
async def list_photos(page: int = 1, limit: int = 20):
    """Get paginated list of photos with metadata from the hardware service."""
    if page < 1:
        page = 1
    if limit < 1 or limit > 100:
        limit = 20
    try:
        # Fetch all photos from hardware service and paginate here for simplicity
        all_photos = await reframe_client.get("/photos")
        # Rewrite absolute file system paths to dashboard-served URLs
        for photo in all_photos:
            try:
                if photo.get("original_path"):
                    from os.path import basename as _bn
                    orig_name = _bn(photo["original_path"])
                    photo["original_path"] = f"/photos/{orig_name}"
                if photo.get("dithered_path"):
                    from os.path import basename as _bn
                    dith_name = _bn(photo["dithered_path"])
                    photo["dithered_path"] = f"/dithered/{dith_name}"
            except Exception:
                pass
            # Merge provenance from the sidecar the hardware service doesn't know about.
            sidecar = photo_manager._read_sidecar(Path(PHOTOS_PATH) / photo.get("id", ""))
            photo["group_id"] = sidecar.get("group_id")
            photo["frame_label"] = sidecar.get("frame_label")
            photo["recipe_name"] = sidecar.get("recipe_name")
            photo["program"] = sidecar.get("program")
    except Exception as e:
        logging.warning(f"Error fetching photos from hardware service: {e}")
        all_photos = []
    start = (page - 1) * limit
    end = start + limit
    total = len(all_photos)
    total_pages = (total + limit - 1) // limit if total else 1
    return {
        "photos": all_photos[start:end],
        "pagination": {
            "page": page,
            "limit": limit,
            "total_photos": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
    }

@app.get("/api/photos/{photo_id}")
async def get_photo_info(photo_id: str):
    """Get information about a specific photo from the hardware service."""
    try:
        photo = await reframe_client.get(f"/photos/{photo_id}")
        # Rewrite paths to URLs served by this dashboard
        from os.path import basename as _bn
        if photo.get("original_path"):
            photo["original_path"] = f"/photos/{_bn(photo['original_path'])}"
        if photo.get("dithered_path"):
            photo["dithered_path"] = f"/dithered/{_bn(photo['dithered_path'])}"
        # Merge provenance from the sidecar the hardware service doesn't know about.
        sidecar = photo_manager._read_sidecar(Path(PHOTOS_PATH) / photo.get("id", photo_id))
        photo["group_id"] = sidecar.get("group_id")
        photo["frame_label"] = sidecar.get("frame_label")
        photo["recipe_name"] = sidecar.get("recipe_name")
        photo["program"] = sidecar.get("program")
        return photo
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/photos/{filename}")
async def serve_original_photo(filename: str):
    """Serve original photo file."""
    file_path = os.path.join(PHOTOS_PATH, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Photo not found")
    return FileResponse(file_path)

@app.get("/dithered/{filename}")
async def serve_dithered_photo(filename: str):
    """Serve dithered photo file."""
    file_path = os.path.join(DITHERED_PHOTOS_PATH, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Dithered photo not found")
    return FileResponse(file_path)


@app.get("/api/download/dithered/{filename}")
async def download_dithered_photo(filename: str):
    """Download one dithered photo with optional on-demand 2x scaling."""
    if filename != os.path.basename(filename):
        raise HTTPException(status_code=400, detail="Invalid photo filename")

    file_path = os.path.join(DITHERED_PHOTOS_PATH, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Dithered photo not found")

    settings = settings_manager.load_settings()
    upscale_2x = bool(settings.get("exports", {}).get("upscale_dithered_2x", False))
    if not upscale_2x:
        return FileResponse(file_path, filename=filename)

    try:
        content, export_filename, media_type = await asyncio.to_thread(
            prepare_dithered_export,
            file_path,
            True
        )
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"Could not prepare dithered download: {error}") from error

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{export_filename}"'}
    )

@app.get("/api/settings")
async def get_settings():
    """Get current settings."""
    return settings_manager.load_public_settings()

@app.post("/api/settings")
async def update_settings(request: Request):
    """Update settings and notify the hardware service to reload/apply."""
    try:
        settings_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Settings body must be valid JSON")

    previous_settings = settings_manager.load_settings()
    try:
        success = settings_manager.save_settings(settings_data)
    except SettingsValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if not success:
        raise HTTPException(status_code=500, detail="Failed to save settings")

    try:
        await reframe_client.post("/settings/reload")
    except Exception as apply_error:
        try:
            settings_manager.replace_settings(previous_settings)
            await reframe_client.post("/settings/reload")
        except Exception as rollback_error:
            logging.error(f"Settings rollback failed: {rollback_error}")
        raise HTTPException(
            status_code=502,
            detail=f"Camera rejected the settings; previous settings were restored: {apply_error}"
        )

    return {"status": "success", "message": "Settings updated successfully"}

@app.post("/api/settings/reset")
async def reset_settings():
    """Replace settings with a fresh copy of the shipped defaults.

    Unlike update_settings(), this does not merge with what's already on
    disk -- the whole point of "reset to defaults" is that every value goes
    back to exactly what the software ships with, including the active
    recipe and the derived camera/processing cache (and, same as before this
    route existed, the stored Are.na access token). A deep copy is required:
    default_settings is a long-lived object and the recipe routes mutate
    whatever load_settings() hands them in place.
    """
    previous_settings = settings_manager.load_settings()
    defaults = copy.deepcopy(settings_manager.default_settings)
    try:
        settings_manager.replace_settings(defaults)
    except SettingsValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        await reframe_client.post("/settings/reload")
    except Exception as apply_error:
        try:
            settings_manager.replace_settings(previous_settings)
            await reframe_client.post("/settings/reload")
        except Exception as rollback_error:
            logging.error(f"Settings reset rollback failed: {rollback_error}")
        raise HTTPException(
            status_code=502,
            detail=f"Camera rejected the reset settings; previous settings were restored: {apply_error}"
        )

    return {"status": "success", "message": "Settings reset to defaults"}

async def _save_recipes_section(section: Dict[str, Any]) -> None:
    """Persist a recipes section and tell the camera to reload.

    Mirrors update_settings()'s rollback contract: if the camera rejects the
    new settings, the previous ones are restored so the device is never left
    running configuration the dashboard has already forgotten.
    """
    previous_settings = settings_manager.load_settings()
    try:
        success = settings_manager.save_settings({"recipes": section})
    except SettingsValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if not success:
        raise HTTPException(status_code=500, detail="Failed to save settings")

    try:
        await reframe_client.post("/settings/reload")
    except Exception as apply_error:
        try:
            settings_manager.replace_settings(previous_settings)
            await reframe_client.post("/settings/reload")
        except Exception as rollback_error:
            logging.error(f"Recipe rollback failed: {rollback_error}")
        raise HTTPException(
            status_code=502,
            detail=f"Camera rejected the recipe; previous settings were restored: {apply_error}"
        )


def _recipes_section() -> Dict[str, Any]:
    return settings_manager.load_settings()["recipes"]


RECIPE_BODY_FIELDS = ("id", "name", "capture", "render")


def _allowed_recipe_fields(recipe: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce an incoming recipe body to the fields recipes actually own.

    An unknown top-level key would otherwise be stored verbatim -- and
    settings.json is re-parsed on every load_settings() call, i.e. every API
    request, so a large junk field bloats the cost of every request forever.
    """
    return {key: recipe[key] for key in RECIPE_BODY_FIELDS if key in recipe}


@app.get("/api/recipes")
async def list_recipes():
    """List all recipes and which one is active."""
    return _recipes_section()


@app.post("/api/recipes")
async def create_recipe(request: Request):
    """Add a new recipe."""
    try:
        recipe = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Recipe body must be valid JSON")
    if not isinstance(recipe, dict) or not recipe.get("id"):
        raise HTTPException(status_code=400, detail="Recipe must be an object with an id")
    recipe = _allowed_recipe_fields(recipe)

    section = _recipes_section()
    if any(existing.get("id") == recipe["id"] for existing in section["items"]):
        raise HTTPException(status_code=409, detail=f"Recipe '{recipe['id']}' already exists")
    if len(section["items"]) >= recipes.RECIPE_LIMIT:
        raise HTTPException(
            status_code=409,
            detail=f"Recipe limit of {recipes.RECIPE_LIMIT} reached; delete one first")

    section["items"].append(recipe)
    await _save_recipes_section(section)
    return {"status": "success", "id": recipe["id"]}


@app.put("/api/recipes/{recipe_id}")
async def update_recipe(recipe_id: str, request: Request):
    """Replace an existing recipe."""
    try:
        recipe = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Recipe body must be valid JSON")
    if not isinstance(recipe, dict):
        raise HTTPException(status_code=400, detail="Recipe must be an object")
    recipe = _allowed_recipe_fields(recipe)

    section = _recipes_section()
    for index, existing in enumerate(section["items"]):
        if existing.get("id") == recipe_id:
            recipe["id"] = recipe_id  # the URL owns identity, not the body
            section["items"][index] = recipe
            await _save_recipes_section(section)
            return {"status": "success", "id": recipe_id}
    raise HTTPException(status_code=404, detail=f"No recipe '{recipe_id}'")


@app.delete("/api/recipes/{recipe_id}")
async def delete_recipe(recipe_id: str):
    """Delete a recipe that is neither active nor the last one."""
    section = _recipes_section()
    if not any(existing.get("id") == recipe_id for existing in section["items"]):
        raise HTTPException(status_code=404, detail=f"No recipe '{recipe_id}'")
    if section["active"] == recipe_id:
        raise HTTPException(
            status_code=409, detail="Cannot delete the active recipe; activate another first")
    if len(section["items"]) <= 1:
        raise HTTPException(status_code=409, detail="Cannot delete the last remaining recipe")

    section["items"] = [r for r in section["items"] if r.get("id") != recipe_id]
    await _save_recipes_section(section)
    return {"status": "success", "id": recipe_id}


@app.post("/api/recipes/{recipe_id}/activate")
async def activate_recipe(recipe_id: str):
    """Make a recipe active, regenerating the derived camera/processing cache."""
    section = _recipes_section()
    if not any(existing.get("id") == recipe_id for existing in section["items"]):
        raise HTTPException(status_code=404, detail=f"No recipe '{recipe_id}'")

    section["active"] = recipe_id
    await _save_recipes_section(section)
    return {"status": "success", "active": recipe_id}

@app.get("/api/update/status")
async def update_status():
    """Check whether the git checkout has a fast-forward update available."""
    return await get_update_status(fetch=True)

@app.post("/api/update/install")
async def install_update():
    """Pull a fast-forward update and apply its dependencies and service files."""
    status = await get_update_status(fetch=True)

    if status["up_to_date"] and not status["post_install_pending"]:
        return {
            **status,
            "status": "success",
            "message": "Software is already up to date."
        }

    if status["diverged"]:
        raise HTTPException(
            status_code=409,
            detail="This checkout has diverged from upstream and cannot be updated automatically"
        )

    if status["has_tracked_changes"]:
        raise HTTPException(
            status_code=409,
            detail="Local code changes must be committed or discarded before updating"
        )

    if not status["update_available"] and not status["post_install_pending"]:
        raise HTTPException(status_code=409, detail="No automatic update is available")

    preserved_paths = ignored_user_data_paths()
    backup_dir = None
    pull_output = ""

    if not os.path.exists(UPDATE_HELPER_PATH):
        raise HTTPException(
            status_code=501,
            detail="Update installer is missing. Run install.sh once over SSH to enable complete updates."
        )

    if status["update_available"]:
        backup_dir = backup_settings_for_update()
        upstream = status["upstream"]
        pull_args = ["pull", "--ff-only"]
        if "/" in upstream:
            remote, branch = upstream.split("/", 1)
            pull_args.extend([remote, branch])

        pull_result = await run_git(pull_args, timeout=180)
        if pull_result["returncode"] != 0:
            detail = git_error_detail(pull_result, "git pull failed")
            raise HTTPException(status_code=500, detail=f"Could not install update: {detail}")
        pull_output = pull_result["stdout"]

        pending_path = Path(UPDATE_PENDING_PATH)
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(status["remote_revision"] + "\n", encoding="utf-8")

    apply_result = await run_repo_command(
        ["sudo", "-n", UPDATE_HELPER_PATH],
        timeout=300
    )
    if apply_result["returncode"] != 0:
        detail = git_error_detail(apply_result, "post-update installation failed")
        raise HTTPException(
            status_code=500,
            detail=f"Code was updated, but installation did not finish: {detail}"
        )

    try:
        os.unlink(UPDATE_PENDING_PATH)
    except FileNotFoundError:
        pass

    new_status = await get_update_status(fetch=False)
    message = "Update installed. Reboot the camera to finish."
    if backup_dir:
        message = f"{message} Settings backup: {backup_dir}"

    return {
        **new_status,
        "status": "success",
        "message": message,
        "backup_dir": backup_dir,
        "preserved_paths": preserved_paths,
        "pull_output": pull_output,
        "apply_output": apply_result["stdout"]
    }

@app.get("/api/extensions/actions")
async def get_extension_actions():
    """Get extension photo actions safe for the dashboard to render."""
    settings = settings_manager.load_settings()
    return {"actions": extension_registry.public_actions(settings)}

@app.post("/api/extensions/{extension_id}/photos/{photo_id}")
async def run_extension_action(extension_id: str, photo_id: str):
    """Run an enabled extension against one photo."""
    extension = extension_registry.get(extension_id)
    if extension is None:
        raise HTTPException(status_code=404, detail="Extension not found")

    settings = settings_manager.load_settings()
    if not extension.configured(settings):
        raise HTTPException(status_code=400, detail=f"{extension.label} extension is not fully configured")

    try:
        photo = await reframe_client.get(f"/photos/{photo_id}")
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Photo not found: {e}")

    result = await extension.run(photo, settings)
    return result

@app.get("/api/settings/camera")
async def get_camera_settings():
    """Get camera-specific settings."""
    return settings_manager.get_camera_settings()

@app.get("/api/camera/limits")
async def get_camera_limits():
    """Proxy the sensor's real control ranges from the hardware service.

    Not merged into validate_settings: the dashboard and camera are separate
    processes with no startup ordering guarantee, so settings validation must
    work whether or not the camera is up. These bound the UI instead.
    """
    try:
        return await reframe_client.get("/camera/limits")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not read camera limits: {e}")

@app.get("/api/settings/processing") 
async def get_processing_settings():
    """Get processing-specific settings."""
    return settings_manager.get_processing_settings()

@app.post("/api/capture")
async def capture_photo(background_tasks: BackgroundTasks):
    """Capture a new photo with current settings."""
    try:
        result = await reframe_client.post("/capture")
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/display/clear")
async def clear_display():
    """Proxy to clear the e-ink display on the hardware service."""
    try:
        resp = await reframe_client.post("/display/clear")
        return resp
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear display: {str(e)}")

@app.post("/api/display/{photo_id}")
async def display_photo_on_screen(photo_id: str):
    """Display a specific photo on the e-ink screen."""
    try:
        result = await reframe_client.post(f"/display/{photo_id}")
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/dashboard/qr")
async def show_dashboard_qr():
    """Display the dashboard access QR on the e-ink screen."""
    try:
        result = await reframe_client.post("/dashboard/qr")
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to show dashboard QR: {str(e)}")

@app.post("/api/reprocess/{photo_id}")
async def reprocess_photo(photo_id: str, request: Request):
    """Reprocess an existing photo with new settings."""
    try:
        processing_settings = None
        try:
            body = await request.json()
            processing_settings = body.get("processing_settings")
        except Exception:
            processing_settings = None
        result = await reframe_client.post(f"/reprocess/{photo_id}", json={"processing_settings": processing_settings})
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/status")
async def get_system_status():
    """Get system status information."""
    try:
        status = await reframe_client.get("/status")
        return status
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Hardware service unavailable: {str(e)}")

# ═══════════════════════════════════════════════════════════════════
# HARDWARE: Battery — PiSugar 3 via pisugar-server TCP
# Battery level is read by sending "get battery" to pisugar-server
# on TCP port 8423. pisugar-server must be installed separately.
# To use a different battery monitor, replace this endpoint.
# See: https://github.com/PiSugar/PiSugar/wiki/PiSugar-Power-Manager-(Software)
# ═══════════════════════════════════════════════════════════════════
@app.get("/api/battery")
async def get_battery_level():
    """Get battery level from PiSugar."""
    import subprocess
    
    try:
        # Use TCP method (working reliably)
        result = subprocess.run(
            ['nc', '-q', '0', '127.0.0.1', '8423'],
            input='get battery\n',
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            response = result.stdout.strip()
            if response.startswith('battery:'):
                # Handle decimal values and leading spaces
                battery_str = response.split(':')[1].strip()
                battery_level = int(float(battery_str))  # Convert decimal to int
                return {"battery_level": battery_level, "source": "tcp"}
        
        # If TCP fails, return unknown
        return {"battery_level": None, "source": "unknown", "error": "Could not connect to PiSugar"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get battery level: {str(e)}")

@app.post("/api/timeout/reset")
async def reset_timeout():
    """Reset the timeout timer (extend the timeout period)."""
    try:
        result = await reframe_client.post("/timeout/reset")
        return result
    except Exception as e:
        logging.info(f"Hardware timeout reset unavailable: {e}")
        return {
            "status": "unavailable",
            "message": "Hardware service is not ready yet"
        }

@app.get("/api/timeout/status")
async def get_timeout_status():
    """Get current timeout status and remaining time."""
    try:
        result = await reframe_client.get("/timeout/status")
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get timeout status: {str(e)}")

@app.post("/api/photos/{photo_id}/reprocess")
async def reprocess_single_photo(photo_id: str):
    """Reprocess a single photo to create missing dithered version."""
    try:
        result = await reframe_client.post(f"/reprocess/{photo_id}")
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reprocess photo: {str(e)}")

@app.post("/api/photos/{photo_id}/develop")
async def develop_photo_with_recipe(photo_id: str, request: Request):
    """Re-render an existing photo with another recipe's look.

    Only the render half is reapplicable — the capture settings are gone the
    moment the shutter fired. That asymmetry is why recipes are split in two.
    """
    body = await request.json()
    recipe_id = body.get("recipe_id")

    settings = settings_manager.load_settings()
    match = [r for r in settings["recipes"]["items"] if r.get("id") == recipe_id]
    if not match:
        raise HTTPException(status_code=404, detail=f"No recipe '{recipe_id}'")

    original = Path(PHOTOS_PATH) / f"{photo_id}.jpg"
    if not original.exists():
        raise HTTPException(status_code=404, detail=f"No photo '{photo_id}'")

    try:
        result = await reframe_client.post(
            f"/reprocess/{photo_id}",
            json={"processing_settings": match[0]["render"]})
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not develop photo: {e}")

    return {"status": "success", "photo_id": photo_id, "recipe_id": recipe_id,
            "result": result}

# Global variable to track download progress
download_progress = {"status": "idle", "processed": 0, "total": 0, "message": ""}
download_abort = False
download_job_active = False

# Global variable to track delete progress
delete_progress = {"status": "idle", "processed": 0, "total": 0, "message": ""}
delete_abort = False

@app.post("/api/photos/download-all/start")
async def start_download_all(background_tasks: BackgroundTasks):
    """Start the download process and return immediately."""
    global download_progress, download_job_active
    
    if download_job_active or download_progress.get("status") in {"preparing", "creating", "completed", "downloading"}:
        raise HTTPException(status_code=409, detail="A photo download is already in progress")

    try:
        # Get all photos from hardware service
        all_photos = await reframe_client.get("/photos")
        
        if not all_photos:
            raise HTTPException(status_code=404, detail="No photos found")
        
        # Initialize progress
        global download_abort
        download_abort = False
        download_job_active = True
        download_progress = {
            "status": "preparing",
            "processed": 0,
            "total": len(all_photos),
            "message": "Preparing download..."
        }
        
        # Start background task
        background_tasks.add_task(create_zip_background, all_photos)
        
        return {"status": "started", "total_photos": len(all_photos)}
        
    except HTTPException:
        raise
    except Exception as e:
        download_job_active = False
        download_progress = {"status": "error", "processed": 0, "total": 0, "message": str(e)}
        raise HTTPException(status_code=500, detail=f"Failed to start download: {str(e)}")

@app.get("/api/photos/download-all/progress")
async def get_download_progress():
    """Get current download progress."""
    global download_progress
    return download_progress

@app.post("/api/photos/download-all/abort")
async def abort_download():
    """Abort the current download process."""
    global download_abort, download_progress
    download_abort = True
    zip_path = download_progress.get("zip_path")
    if download_progress.get("status") == "completed" and zip_path:
        try:
            os.unlink(zip_path)
        except FileNotFoundError:
            pass
    download_progress = {
        "status": "aborted",
        "processed": download_progress.get("processed", 0),
        "total": download_progress.get("total", 0),
        "message": "Download aborted by user"
    }
    return {"status": "aborted", "message": "Download aborted"}

@app.get("/api/photos/download-all/result")
async def get_download_result():
    """Get the completed ZIP file."""
    global download_progress
    
    print(f"Download result requested. Status: {download_progress.get('status')}")
    
    if download_progress["status"] != "completed":
        print(f"Download not completed. Current status: {download_progress.get('status')}")
        raise HTTPException(status_code=400, detail="Download not completed yet")
    
    zip_path = download_progress.get("zip_path")
    print(f"ZIP path: {zip_path}")
    
    if not zip_path or not os.path.exists(zip_path):
        print(f"ZIP file not found at: {zip_path}")
        raise HTTPException(status_code=404, detail="ZIP file not found")
    
    print(f"Starting file stream for: {zip_path}")
    download_progress["status"] = "downloading"

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"reframe-photos-{datetime.now().strftime('%Y%m%d')}.zip",
        background=BackgroundTask(finish_download_archive, zip_path)
    )


def finish_download_archive(zip_path):
    """Remove an archive after the browser finishes or abandons its transfer."""
    global download_progress
    try:
        os.unlink(zip_path)
        print("Temporary ZIP cleaned up after browser download")
    except FileNotFoundError:
        pass
    except Exception as cleanup_error:
        logging.warning(f"Could not clean up downloaded ZIP {zip_path}: {cleanup_error}")
    finally:
        if download_progress.get("status") in {"downloading", "aborted"}:
            download_progress = {"status": "idle", "processed": 0, "total": 0, "message": ""}


def create_zip_file(all_photos, temp_path):
    """Create the archive in a worker thread and report whether it completed."""
    global download_progress, download_abort
    import zipfile

    download_progress["status"] = "creating"
    download_progress["message"] = "Creating ZIP file..."
    total_photos = len(all_photos)

    with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zip_file:
        for processed, photo in enumerate(all_photos, start=1):
            if download_abort:
                return False

            try:
                original_path = photo.get("original_path")
                if original_path and os.path.exists(original_path):
                    zip_file.write(original_path, f"original/{os.path.basename(original_path)}")

                dithered_path = photo.get("dithered_path")
                if dithered_path and os.path.exists(dithered_path):
                    zip_file.write(dithered_path, f"dithered/{os.path.basename(dithered_path)}")
            except Exception as e:
                print(f"Error adding photo {photo.get('id', 'unknown')} to ZIP: {e}")

            if download_abort:
                return False
            download_progress["processed"] = processed
            download_progress["message"] = f"Processing photo {processed}/{total_photos}"

    return not download_abort


def expire_download_archive(zip_path):
    """Remove a completed archive if no browser claims it within ten minutes."""
    global download_progress
    if (
        download_progress.get("status") == "completed"
        and download_progress.get("zip_path") == zip_path
    ):
        try:
            os.unlink(zip_path)
        except FileNotFoundError:
            pass
        except Exception as cleanup_error:
            logging.warning(f"Could not expire temporary ZIP {zip_path}: {cleanup_error}")
            return
        download_progress = {"status": "idle", "processed": 0, "total": 0, "message": ""}


async def create_zip_background(all_photos):
    """Create one ZIP off the event loop and always clean incomplete files."""
    global download_progress, download_job_active
    temp_path = None
    keep_archive = False
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as temp_file:
            temp_path = temp_file.name

        completed = await asyncio.to_thread(create_zip_file, all_photos, temp_path)
        if not completed:
            download_progress["status"] = "aborted"
            download_progress["message"] = "Download aborted by user"
            return

        archive_size = os.path.getsize(temp_path)
        download_progress["status"] = "completed"
        download_progress["message"] = "ZIP file ready for download"
        download_progress["zip_path"] = temp_path
        download_progress["size_bytes"] = archive_size
        keep_archive = True
        expiry_timer = threading.Timer(600, expire_download_archive, args=(temp_path,))
        expiry_timer.daemon = True
        expiry_timer.start()
        print(f"ZIP creation completed. File: {temp_path}, Size: {archive_size} bytes")
    except Exception as e:
        download_progress["status"] = "error"
        download_progress["message"] = f"Error: {str(e)}"
    finally:
        download_job_active = False
        if temp_path and not keep_archive:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            except Exception as cleanup_error:
                logging.warning(f"Could not remove temporary ZIP {temp_path}: {cleanup_error}")

async def delete_photos_background(all_photos):
    """Background task to delete photos."""
    global delete_progress, delete_abort
    import asyncio
    
    try:
        delete_progress["status"] = "deleting"
        delete_progress["message"] = "Deleting photos..."
        
        total_photos = len(all_photos)
        deleted_count = 0
        
        for photo in all_photos:
            # Check for abort
            if delete_abort:
                delete_progress["status"] = "aborted"
                delete_progress["message"] = "Deletion aborted by user"
                return
            
            try:
                result = await reframe_client.delete(f"/photos/{photo['id']}")
                if result.get("success"):
                    deleted_count += 1
                
                delete_progress["processed"] = deleted_count
                delete_progress["message"] = f"Deleted {deleted_count}/{total_photos} photos"
                
                # Small delay for responsiveness
                if deleted_count % 2 == 0:
                    await asyncio.sleep(0.01)
                    
            except Exception as e:
                print(f"Error deleting photo {photo.get('id', 'unknown')}: {e}")
                delete_progress["processed"] = deleted_count
                continue
        
        # Check for abort before marking as completed
        if delete_abort:
            delete_progress["status"] = "aborted"
            delete_progress["message"] = "Deletion aborted by user"
            return
        
        # Mark as completed
        delete_progress["status"] = "completed"
        delete_progress["message"] = f"Successfully deleted {deleted_count} photos"
        
    except Exception as e:
        delete_progress["status"] = "error"
        delete_progress["message"] = f"Error: {str(e)}"

@app.post("/api/photos/delete-all/start")
async def start_delete_all(background_tasks: BackgroundTasks):
    """Start the delete process and return immediately."""
    global delete_progress
    
    try:
        # Get all photos from hardware service
        all_photos = await reframe_client.get("/photos")
        
        if not all_photos:
            return {"status": "completed", "message": "No photos to delete", "deleted_count": 0}
        
        # Initialize progress
        global delete_abort
        delete_abort = False
        delete_progress = {
            "status": "preparing",
            "processed": 0,
            "total": len(all_photos),
            "message": "Preparing deletion..."
        }
        
        # Start background task
        background_tasks.add_task(delete_photos_background, all_photos)
        
        return {"status": "started", "total_photos": len(all_photos)}
        
    except Exception as e:
        delete_progress = {"status": "error", "processed": 0, "total": 0, "message": str(e)}
        raise HTTPException(status_code=500, detail=f"Failed to start deletion: {str(e)}")

@app.get("/api/photos/delete-all/progress")
async def get_delete_progress():
    """Get current delete progress."""
    global delete_progress
    return delete_progress

@app.post("/api/photos/delete-all/abort")
async def abort_delete():
    """Abort the current delete process."""
    global delete_abort, delete_progress
    delete_abort = True
    delete_progress = {
        "status": "aborted",
        "processed": delete_progress.get("processed", 0),
        "total": delete_progress.get("total", 0),
        "message": "Deletion aborted by user"
    }
    return {"status": "aborted", "message": "Deletion aborted"}

@app.post("/api/photos/delete-all")
async def delete_all_photos():
    """Delete all photos from the system."""
    try:
        # Get all photos from hardware service
        all_photos = await reframe_client.get("/photos")
        
        if not all_photos:
            return {"success": True, "message": "No photos to delete"}
        
        deleted_count = 0
        
        # Delete each photo through the hardware service
        for photo in all_photos:
            try:
                result = await reframe_client.delete(f"/photos/{photo['id']}")
                if result.get("success"):
                    deleted_count += 1
            except Exception as e:
                print(f"Error deleting photo {photo.get('id', 'unknown')}: {e}")
                continue
        
        return {
            "success": True, 
            "message": f"Successfully deleted {deleted_count} photos",
            "deleted_count": deleted_count
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete photos: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
