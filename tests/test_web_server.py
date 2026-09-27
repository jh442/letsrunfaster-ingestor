"""Test web server endpoints."""

import unittest
from fastapi.testclient import TestClient
from garmin_engine.web_server import app

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
        import tempfile
        from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
