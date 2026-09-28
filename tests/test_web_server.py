"""Test web server endpoints."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from garmin_engine.web_server import app
from garmin_engine import settings as app_settings

client = TestClient(app)


class TestWebServer(unittest.TestCase):
    def test_auth_status_endpoint(self):
        resp = client.get("/api/auth/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("authenticated", data)

    def test_static_index(self):
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("LETS", resp.text)
        self.assertIn("RUN", resp.text)
        self.assertIn("FASTER", resp.text)
        self.assertIn("INGESTOR", resp.text)
        self.assertIn("Bulk export garmin activities", resp.text)
        self.assertIn("Delete All Export Files", resp.text)
        # Logo icon removed - flat text logo only
        self.assertNotIn("fa-person-running", resp.text)

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

    def test_resolve_latest_url(self):
        self.assertEqual(
            app_settings.resolve_latest_url("https://letsrunfaster.com/api/activities/import"),
            "https://letsrunfaster.com/api/activities/latest",
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
