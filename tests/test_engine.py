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
    sort_activities_oldest_first,
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


class TestExportOrdering(unittest.TestCase):
    def _act(self, aid, start):
        return ActivityItem(
            activity_id=aid,
            activity_name=f"run {aid}",
            activity_type="running",
            start_time_local=start,
        )

    def test_oldest_first(self):
        acts = [
            self._act(3, "2026-09-28 18:00:00"),
            self._act(1, "2026-09-27 07:00:00"),
            self._act(2, "2026-09-28 07:00:00"),
        ]
        ordered = sort_activities_oldest_first(acts)
        self.assertEqual([a.activity_id for a in ordered], [1, 2, 3])
        # Pure: input order untouched
        self.assertEqual([a.activity_id for a in acts], [3, 1, 2])

    def test_missing_timestamp_last_tie_breaks_by_id(self):
        acts = [self._act(9, ""), self._act(5, "2026-09-28 07:00:00"), self._act(7, "")]
        ordered = sort_activities_oldest_first(acts)
        self.assertEqual([a.activity_id for a in ordered], [5, 7, 9])

    def test_bulk_export_runs_chronologically(self):
        exporter = GarminExporter(MagicMock())
        seen = []

        def fake_export(activity, **kwargs):
            seen.append(activity.activity_id)
            res = MagicMock()
            res.success = True
            return res

        exporter.export_activity = fake_export
        acts = [
            self._act(30, "2026-09-28 18:00:00"),
            self._act(10, "2026-09-27 07:00:00"),
            self._act(20, "2026-09-28 07:00:00"),
        ]
        exporter.bulk_export(acts, ExportFormat.FIT, output_dir=None, delay_seconds=0)
        # First run of the day exports first -> lands in the primary slot
        self.assertEqual(seen, [10, 20, 30])
