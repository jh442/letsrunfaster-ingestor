"""Unit tests for garmin_engine."""

import unittest
from unittest.mock import MagicMock, patch
import io
import zipfile
from garmin_engine.auth import GarminAuthManager, AuthStatus
from garmin_engine.exporter import (
    GarminExporter,
    ActivityItem,
    ExportFormat,
    sanitize_filename,
)


class TestGarminEngine(unittest.TestCase):
    def test_sanitize_filename(self):
        name = "Morning Run / 5k (Fast!)"
        sanitized = sanitize_filename(name)
        self.assertEqual(sanitized, "morning_run_5k_fast")

    def test_activity_item_parsing(self):
        raw = {
            "activityId": 12345678,
            "activityName": "Track Intervals",
            "activityType": {"typeKey": "running"},
            "startTimeLocal": "2024-05-01 08:30:00",
            "distance": 5200.5,
            "duration": 1500.0,
        }
        item = ActivityItem.from_dict(raw)
        self.assertEqual(item.activity_id, 12345678)
        self.assertEqual(item.activity_name, "Track Intervals")
        self.assertEqual(item.activity_type, "running")
        self.assertEqual(item.distance_meters, 5200.5)

    def test_fit_download_zip_extraction(self):
        """Verify that zip-wrapped ORIGINAL fit files are unzipped properly."""
        client_mock = MagicMock()
        
        # Create a fake zip containing activity.fit
        fake_fit_content = b"\x0e\x10\x00\x00.FIT_DATA"
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("123456_ACTIVITY.fit", fake_fit_content)
        
        client_mock.download_activity.return_value = zip_buf.getvalue()
        
        exporter = GarminExporter(client_mock)
        extracted_data, ext = exporter.download_activity_data(123456, ExportFormat.FIT)
        self.assertEqual(ext, "fit")
        self.assertEqual(extracted_data, fake_fit_content)

    def test_auth_manager_saved_tokens_missing(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = GarminAuthManager(token_dir=tmpdir)
            res = mgr.login_with_tokens()
            self.assertEqual(res.status, AuthStatus.FAILED)


if __name__ == "__main__":
    unittest.main()

    def test_tcx_and_tpx_formats(self):
        client_mock = MagicMock()
        fake_xml = b"<TrainingCenterDatabase>...</TrainingCenterDatabase>"
        client_mock.download_activity.return_value = fake_xml
        exporter = GarminExporter(client_mock)
        
        tcx_data, ext_tcx = exporter.download_activity_data(1234, ExportFormat.TCX)
        self.assertEqual(ext_tcx, "tcx")
        self.assertEqual(tcx_data, fake_xml)

        tpx_data, ext_tpx = exporter.download_activity_data(1234, ExportFormat.TPX)
        self.assertEqual(ext_tpx, "tpx")
        self.assertEqual(tpx_data, fake_xml)
