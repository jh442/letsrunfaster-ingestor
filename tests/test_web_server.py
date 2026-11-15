"""Test web server endpoints."""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from garmin_engine.web_server import app
from garmin_engine import settings as app_settings

client = TestClient(app)


def _built_assets():
    """The page plus the CSS/JS bundles it references.

    The UI is built by Vite (``ui/`` -> ``garmin_engine/static``), so styling
    assertions have to read the emitted stylesheet rather than the HTML.
    Filenames are stable by config; they are still discovered here so a
    naming change fails loudly instead of silently asserting nothing.
    """
    index = client.get("/")
    css = re.search(r'href="\./(assets/[^"]+\.css)"', index.text)
    js = re.search(r'src="\./(assets/[^"]+\.js)"', index.text)
    css_resp = client.get("/" + css.group(1)) if css else None
    js_resp = client.get("/" + js.group(1)) if js else None
    return index, css_resp, js_resp


class TestWebServer(unittest.TestCase):
    def test_health_endpoint(self):
        # The desktop shell polls this before showing a window, so it must
        # stay dependency-free and cheap (no Garmin network call).
        resp = client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIs(data["ok"], True)
        self.assertEqual(data["app"], "lrf-ingestor")
        self.assertRegex(data["version"], r"^\d+\.\d+\.\d+")

    def test_config_endpoint_reports_default_destination(self):
        resp = client.get("/api/config")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("default_output_dir", data)
        self.assertTrue(Path(data["default_output_dir"]).is_absolute())
        self.assertIn("app_home", data)

    def test_auth_status_endpoint(self):
        resp = client.get("/api/auth/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("authenticated", data)

    def test_static_index(self):
        index, css, js = _built_assets()
        resp = index
        self.assertEqual(resp.status_code, 200)
        self.assertIn("LETS", resp.text)
        self.assertIn("RUN", resp.text)
        self.assertIn("FASTER", resp.text)
        self.assertIn("Ingestor", resp.text)
        self.assertIn("Import activities", resp.text)
        self.assertIn("Garmin account", resp.text)
        self.assertIn("Delete files", resp.text)
        # Flat text wordmark; icons come from the inline sprite, so the
        # Font Awesome CDN is gone (offline-safe for the desktop shell).
        self.assertNotIn("fa-person-running", resp.text)
        self.assertNotIn("font-awesome", resp.text)
        self.assertNotIn('class="fa-', resp.text)
        # The build has to be present: a missing bundle means somebody edited
        # ui/ but forgot `npm run build` before committing.
        self.assertIsNotNone(css, "index.html does not reference a built stylesheet")
        self.assertIsNotNone(js, "index.html does not reference a built script")
        self.assertEqual(css.status_code, 200)
        self.assertEqual(js.status_code, 200)

    def test_no_remote_assets_for_offline_desktop_app(self):
        # Nothing may be fetched from the network: the packaged app has to
        # render identically offline and behind a proxy.
        index, css, js = _built_assets()
        for blob in (index.text, css.text, js.text):
            for host in ("cdn.tailwindcss.com", "fonts.googleapis.com", "fonts.gstatic.com", "cdnjs.cloudflare.com"):
                self.assertNotIn(host, blob)

    def test_self_hosted_fonts_are_bundled(self):
        _, css, _ = _built_assets()
        self.assertIn("@font-face", css.text)
        self.assertIn("Plus Jakarta Sans Variable", css.text)
        self.assertIn("Space Grotesk Variable", css.text)
        self.assertIn("plus-jakarta-sans-latin-wght-normal.woff2", css.text)
        self.assertIn("font-display:swap", css.text)

    def test_export_range_contrast_and_date_inputs(self):
        # Issue #9: quick presets sit on the page background, the selected
        # format card is expressed in CSS (:has) instead of rewritten by JS,
        # and date inputs keep small text so the picker icon cannot clip.
        index, css, _ = _built_assets()
        self.assertEqual(index.status_code, 200)
        self.assertIn("preset-btn", index.text)
        self.assertIn('data-preset="week"', index.text)
        self.assertIn("bg-[#0b0e14] p-1", index.text)  # preset track
        self.assertNotIn("bg-[#121722]", index.text)
        self.assertIn(".format-card:has(input:checked)", css.text)
        self.assertEqual(index.text.count("font-mono text-[11px] text-slate-200 outline-none transition [color-scheme:dark]"), 2)
        self.assertEqual(index.text.count("[color-scheme:dark]"), 2)

    def test_hidden_sections_actually_hide(self):
        # Tailwind's preflight does not reset [hidden], and an author
        # `display: grid` utility beats the UA rule, so the stylesheet has to
        # neutralize the attribute itself or toggled sections stay visible.
        _, css, _ = _built_assets()
        self.assertIn("[hidden]{display:none!important}", css.text)

    def test_folder_picker_button_is_desktop_only(self):
        index, _, js = _built_assets()
        self.assertIn('id="btn-pick-folder"', index.text)
        self.assertIn('id="i-folder-open"', index.text)
        # The button is revealed by the platform shim only inside Electron.
        self.assertIn('id="btn-pick-folder"', index.text)
        self.assertIn("pickDirectory", js.text)
        self.assertIn("lrfDesktop", js.text)

    def test_no_browser_dialogs_for_desktop_shell(self):
        # alert()/confirm() are browser chrome. The Electron renderer must
        # use in-page feedback so the app never opens a stray browser dialog.
        index, _, js = _built_assets()
        for blob in (index.text, js.text):
            self.assertNotIn("alert(", blob)
            self.assertNotIn("window.confirm(", blob)
        self.assertIn('id="toasts"', index.text)

    def test_preferences_are_stored_by_the_engine(self):
        # The desktop shell's origin (and its localStorage) changes every
        # launch, so the push toggle and destination live in config.json.
        index, _, js = _built_assets()
        self.assertNotIn("localStorage", js.text)
        self.assertIn("push_enabled", js.text)
        self.assertIn("export_dir", js.text)
        # Progress text is announced rather than only painted.
        self.assertIn('id="progress-current-item" aria-live="polite"', index.text)

    def test_sync_new_rescans_latest_day(self):
        # Issue #9: sync-new must start at latest_date itself so a second
        # same-day activity is never skipped (duplicates are skipped
        # server-side), falling back to suggested_start only when nothing is
        # logged yet. Asserted against the built bundle: minification renames
        # locals, so match the property accesses instead of the source text.
        _, _, js = _built_assets()
        bundle = js.text.replace(" ", "")
        self.assertRegex(bundle, r"\.latest_date\|\|")
        self.assertRegex(bundle, r"!\w+\.latest_date&&!\w+\.suggested_start")

    def test_clear_exports_endpoint_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir) / "exports"
            test_dir.mkdir()
            (test_dir / "test1.fit").write_text("dummy 1")
            (test_dir / "test2.gpx").write_text("dummy 2")
            sub_dir = test_dir / "nested_dir"
            sub_dir.mkdir()

            resp = client.post("/api/exports/clear", json={"output_dir": str(test_dir)})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["deleted_count"], 2)

            # Files inside target_dir are deleted, but subdirectories remain intact
            self.assertFalse((test_dir / "test1.fit").exists())
            self.assertFalse((test_dir / "test2.gpx").exists())
            self.assertTrue(sub_dir.exists())

    def test_clear_exports_restrictions(self):
        # Non-existent dir
        resp = client.post("/api/exports/clear", json={"output_dir": "/non/existent/path/here"})
        self.assertEqual(resp.status_code, 404)

        # Disallow root or home or system dirs
        resp = client.post("/api/exports/clear", json={"output_dir": "/"})
        self.assertEqual(resp.status_code, 403)

        # Filesystem anchor root explicitly ("/" on POSIX, e.g. "C:\\" on Windows)
        anchor = Path(tempfile.gettempdir()).anchor
        resp = client.post("/api/exports/clear", json={"output_dir": anchor})
        self.assertEqual(resp.status_code, 403)


class TestRemoteSettings(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        tmp.close()
        self.cfg_path = tmp.name
        self._old_env = os.environ.get("LRF_INGESTOR_CONFIG")
        os.environ["LRF_INGESTOR_CONFIG"] = self.cfg_path

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("LRF_INGESTOR_CONFIG", None)
        else:
            os.environ["LRF_INGESTOR_CONFIG"] = self._old_env
        try:
            os.unlink(self.cfg_path)
        except OSError:
            pass

    def test_get_defaults_without_saved_config(self):
        resp = client.get("/api/settings/remote")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["remote_url"], app_settings.DEFAULT_REMOTE_URL)
        self.assertFalse(data["has_key"])
        self.assertIsNone(data["key_prefix"])

    def test_save_and_reload_roundtrip(self):
        resp = client.post(
            "/api/settings/remote",
            json={"remote_url": "https://example.com/api/activities/import", "remote_api_key": "lrf_abcdef123456"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertTrue(data["has_key"])
        self.assertEqual(data["key_prefix"], "lrf_abcdef12")
        # Full secret is never returned
        self.assertNotIn("lrf_abcdef123456", json.dumps(data))

        resp = client.get("/api/settings/remote")
        data = resp.json()
        self.assertEqual(data["remote_url"], "https://example.com/api/activities/import")
        self.assertTrue(data["has_key"])
        self.assertNotIn("lrf_abcdef123456", json.dumps(data))

        # Saved pair is what export/CLI/RPC default to
        url, key = app_settings.get_remote_settings()
        self.assertEqual(url, "https://example.com/api/activities/import")
        self.assertEqual(key, "lrf_abcdef123456")

    def test_omitted_key_keeps_existing(self):
        client.post("/api/settings/remote", json={"remote_api_key": "lrf_keepme123456"})
        client.post("/api/settings/remote", json={"remote_url": "https://example.com/x"})
        _, key = app_settings.get_remote_settings()
        self.assertEqual(key, "lrf_keepme123456")

    def test_preferences_roundtrip_through_config(self):
        # push_enabled/export_dir live server-side: the desktop shell's origin
        # (and thus localStorage) changes on every launch.
        default = client.get("/api/settings/remote").json()
        self.assertIs(default["push_enabled"], True)
        self.assertIsNone(default["export_dir"])

        resp = client.post(
            "/api/settings/remote",
            json={"push_enabled": False, "export_dir": "~/runs"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIs(data["push_enabled"], False)
        self.assertEqual(data["export_dir"], str(Path.home() / "runs"))

        reloaded = client.get("/api/settings/remote").json()
        self.assertIs(reloaded["push_enabled"], False)
        self.assertEqual(reloaded["export_dir"], str(Path.home() / "runs"))

        # Stored absolute so the destination survives a different cwd.
        stored = json.loads(Path(self.cfg_path).read_text())
        self.assertTrue(Path(stored["export_dir"]).is_absolute())

        # An empty export_dir clears the preference (back to the default).
        client.post("/api/settings/remote", json={"export_dir": ""})
        cleared = client.get("/api/settings/remote").json()
        self.assertIsNone(cleared["export_dir"])
        self.assertIs(cleared["push_enabled"], False)

    def test_preferences_do_not_disturb_saved_key(self):
        client.post("/api/settings/remote", json={"remote_api_key": "lrf_prefkey123456"})
        client.post("/api/settings/remote", json={"push_enabled": False})
        _, key = app_settings.get_remote_settings()
        self.assertEqual(key, "lrf_prefkey123456")

    def test_resolve_latest_url(self):
        self.assertEqual(
            app_settings.resolve_latest_url("https://www.letsrunfaster.com/api/activities/import"),
            "https://www.letsrunfaster.com/api/activities/latest",
        )
        self.assertEqual(
            app_settings.resolve_latest_url("https://example.com/api/activities/import/"),
            "https://example.com/api/activities/latest",
        )
        # Custom base without the import suffix just appends the path
        self.assertEqual(
            app_settings.resolve_latest_url("https://example.com/custom"),
            "https://example.com/custom/activities/latest",
        )

    def test_apex_url_canonicalized_to_www(self):
        # The apex 301-redirects to www and HTTP clients drop Authorization
        # across that hop (surfaces as a site 401), so apex input must be
        # rewritten everywhere: resolver, saver, and reader.
        self.assertEqual(
            app_settings.canonicalize_url("https://letsrunfaster.com/api/activities/import"),
            "https://www.letsrunfaster.com/api/activities/import",
        )
        self.assertEqual(
            app_settings.resolve_latest_url("https://letsrunfaster.com/api/activities/import"),
            "https://www.letsrunfaster.com/api/activities/latest",
        )
        # www, lookalike hosts, and custom/dev URLs pass through untouched.
        self.assertEqual(
            app_settings.canonicalize_url("https://www.letsrunfaster.com/api/activities/import"),
            "https://www.letsrunfaster.com/api/activities/import",
        )
        self.assertEqual(
            app_settings.canonicalize_url("http://localhost:8080/api/activities/import"),
            "http://localhost:8080/api/activities/import",
        )
        client.post("/api/settings/remote", json={"remote_url": "https://letsrunfaster.com/api/activities/import"})
        url, _ = app_settings.get_remote_settings()
        self.assertEqual(url, "https://www.letsrunfaster.com/api/activities/import")

    def test_sync_latest_requires_key(self):
        resp = client.post("/api/sync/latest", json={})
        self.assertEqual(resp.status_code, 400)

    def test_sync_latest_proxies_upstream(self):
        client.post("/api/settings/remote", json={"remote_api_key": "lrf_testkey123456"})
        upstream = {
            "ok": True,
            "latest_date": "2026-09-20",
            "suggested_start": "2026-09-21",
            "block_id": 7,
            "day_id": 42,
            "block": {"id": 7, "start": "2026-08-01", "end": "2026-10-25"},
        }
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = upstream
        with patch("requests.get", return_value=fake_resp) as mock_get:
            resp = client.post("/api/sync/latest", json={})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), upstream)
        called_url, = mock_get.call_args.args
        self.assertTrue(called_url.endswith("/api/activities/latest"))
        self.assertEqual(
            mock_get.call_args.kwargs["headers"]["Authorization"], "Bearer lrf_testkey123456"
        )

    def test_sync_latest_rejects_bad_key(self):
        client.post("/api/settings/remote", json={"remote_api_key": "lrf_badkey"})
        fake_resp = MagicMock(status_code=401)
        with patch("requests.get", return_value=fake_resp):
            resp = client.post("/api/sync/latest", json={})
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
