"""Unit tests for garmin_engine."""

import unittest
from unittest.mock import MagicMock, patch
import io
import zipfile
import requests
from garmin_engine.auth import GarminAuthManager, AuthStatus
from garmin_engine.fit_parser import (
    _build_canonical_time_series,
    _build_splits_from_laps,
    _build_splits_from_telemetry,
)
from garmin_engine.exporter import (
    GarminExporter,
    ActivityItem,
    ExportFormat,
    parse_remote_response,
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


class TestRemoteResponseMapping(unittest.TestCase):
    """Non-block results must not be confused with the legacy block shapes."""

    def test_block_append_without_linkage_is_legacy_block(self):
        mapped = parse_remote_response(
            {"ok": True, "matched": True, "appended_to": "primary"},
            "https://www.letsrunfaster.com/api/activities/import",
        )
        self.assertEqual(mapped["remote_linkage"], "block")
        self.assertIs(mapped["remote_matched"], True)
        self.assertEqual(mapped["remote_detail"], "primary")
        self.assertIsNone(mapped["remote_created"])

    def test_block_replace_and_dedupe(self):
        replaced = parse_remote_response({"matched": True, "replaced": True, "slot": "second"}, None)
        self.assertEqual(replaced["remote_detail"], "updated:second")

        deduped = parse_remote_response({"matched": True, "deduped": True}, None)
        self.assertEqual(deduped["remote_detail"], "duplicate")

    def test_block_converted_plan(self):
        mapped = parse_remote_response(
            {"matched": True, "converted_plan": True, "appended_to": "primary"}, None
        )
        self.assertTrue(mapped["remote_converted_plan"])
        self.assertEqual(mapped["remote_detail"], "primary")

    def test_no_block_found_stays_legacy(self):
        mapped = parse_remote_response(
            {"matched": False, "code": "no_block_found"}, None
        )
        self.assertEqual(mapped["remote_linkage"], "block")
        self.assertIs(mapped["remote_matched"], False)
        self.assertEqual(mapped["remote_detail"], "no_block_found")

    def test_nonblock_created_maps_to_run_and_absolute_url(self):
        mapped = parse_remote_response(
            {
                "matched": False, "linkage": "nonblock", "created": True,
                "social_activity_id": 514, "url": "/activity/514",
            },
            "https://www.letsrunfaster.com/api/activities/import",
        )
        self.assertEqual(mapped["remote_linkage"], "nonblock")
        self.assertIs(mapped["remote_created"], True)
        self.assertEqual(mapped["remote_detail"], "run")
        self.assertEqual(mapped["remote_social_activity_id"], 514)
        self.assertEqual(mapped["remote_activity_url"], "https://www.letsrunfaster.com/activity/514")

    def test_nonblock_dedupe_and_deleted(self):
        deduped = parse_remote_response(
            {"linkage": "nonblock", "created": False, "deduped": True, "social_activity_id": "9"},
            None,
        )
        self.assertEqual(deduped["remote_detail"], "duplicate")
        self.assertEqual(deduped["remote_social_activity_id"], 9)

        deleted = parse_remote_response(
            {"linkage": "nonblock", "deduped": True, "deleted": True}, None
        )
        self.assertTrue(deleted["remote_deleted"])
        self.assertEqual(deleted["remote_detail"], "deleted")

    def test_malformed_body_is_harmless(self):
        for body in (None, [], "nonsense"):
            mapped = parse_remote_response(body, None)
            self.assertIsNone(mapped["remote_linkage"])
            self.assertIsNone(mapped["remote_detail"])


class TestRemoteRequest(unittest.TestCase):
    """mode/is_private ride in the form body, never in the URL."""

    def _export(self, response_body, **kwargs):
        client_mock = MagicMock()
        client_mock.download_activity.return_value = b"<gpx></gpx>"
        exporter = GarminExporter(client_mock)
        activity = ActivityItem(
            activity_id=24974201517,
            activity_name="Ballwin Run",
            activity_type="running",
            start_time_local="2026-09-28 07:00:00",
            distance_meters=8000.0,
        )
        resp = MagicMock()
        resp.json.return_value = response_body
        with patch("garmin_engine.exporter.requests.post", return_value=resp) as post:
            result = exporter.export_activity(
                activity,
                ExportFormat.GPX,
                remote_url="https://www.letsrunfaster.com/api/activities/import",
                remote_api_key="lrf_secret",
                remote_mode=kwargs.get("remote_mode", "auto"),
                import_private=kwargs.get("import_private", False),
            )
        return result, post

    def test_mode_is_a_form_field_not_a_query_parameter(self):
        result, post = self._export(
            {"matched": False, "linkage": "nonblock", "created": True,
             "social_activity_id": 514, "url": "/activity/514"},
        )
        called_url = post.call_args.args[0]
        form = post.call_args.kwargs["data"]
        self.assertEqual(called_url, "https://www.letsrunfaster.com/api/activities/import")
        self.assertNotIn("mode=", called_url)
        self.assertEqual(form["mode"], "auto")
        self.assertNotIn("is_private", form)
        self.assertEqual(result.remote_linkage, "nonblock")
        self.assertEqual(result.remote_detail, "run")
        self.assertTrue(result.remote_synced)

    def test_private_import_sends_flag(self):
        _, post = self._export({}, remote_mode="activity", import_private=True)
        form = post.call_args.kwargs["data"]
        self.assertEqual(form["mode"], "activity")
        self.assertEqual(form["is_private"], "1")

    def test_legacy_server_response_maps_to_block(self):
        result, _ = self._export({"matched": False, "code": "no_block_found"})
        self.assertEqual(result.remote_linkage, "block")
        self.assertEqual(result.remote_detail, "no_block_found")

    def test_http_error_surfaces_server_message_without_aborting(self):
        client_mock = MagicMock()
        client_mock.download_activity.return_value = b"<gpx></gpx>"
        exporter = GarminExporter(client_mock)
        activity = ActivityItem(
            activity_id=1, activity_name="run", activity_type="running",
            start_time_local="2026-09-28 07:00:00",
        )
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError("422 Client Error")
        resp.json.return_value = {"error": "mode must be block, auto or activity"}
        with patch("garmin_engine.exporter.requests.post", return_value=resp):
            result = exporter.export_activity(
                activity, ExportFormat.GPX,
                remote_url="https://www.letsrunfaster.com/api/activities/import",
                remote_mode="auto",
            )
        self.assertFalse(result.success)
        self.assertIn("mode must be block", result.error_message)


class TestFitTelemetryShape(unittest.TestCase):
    """The site's ElevationChart/SplitsTable read canonical keys only."""

    def _samples(self, miles=2.5, pace_sec=450.0):
        # 1Hz-ish synthetic track: distMi/timeSec/eleFt/hr/cad (legacy inputs)
        samples = []
        total_sec = miles * pace_sec
        n = int(total_sec)
        for i in range(n + 1):
            samples.append({
                "distMi": round(miles * i / n, 3),
                "timeSec": round(float(i), 1),
                "eleFt": 500 + (i % 10),
                "hr": 150,
                "cad": 170,
            })
        return samples

    def test_canonical_time_series_keys(self):
        series = _build_canonical_time_series(self._samples())
        self.assertGreaterEqual(len(series), 2)
        for pt in series:
            for key in ("distanceMi", "timeSec", "paceMinPerMi", "hrBpm", "cadenceSpm", "elevationFt"):
                self.assertIn(key, pt)
            self.assertTrue(3.0 < pt["paceMinPerMi"] < 40.0)
        # legacy shorthand keys must be gone
        self.assertNotIn("distMi", series[0])
        self.assertNotIn("eleFt", series[0])

    def test_telemetry_splits_cover_full_and_partial_miles(self):
        splits = _build_splits_from_telemetry(self._samples(miles=2.5), 150)
        self.assertEqual(len(splits), 3)  # mile 1, 2 + 0.5 partial
        self.assertEqual(splits[0]["mile"], 1)
        self.assertEqual(splits[0]["pace"], "7:30")
        self.assertTrue(any(s.get("isFastest") for s in splits))

    def test_lap_splits_preferred_shape(self):
        laps = [
            {"total_distance": 1609.34, "total_timer_time": 440.0, "avg_heart_rate": 150,
             "avg_cadence": 85, "total_ascent": 10.0, "total_descent": 4.0},
            {"total_distance": 1609.34, "total_timer_time": 430.0, "avg_heart_rate": 155,
             "avg_cadence": 86, "total_ascent": 5.0, "total_descent": 8.0},
        ]
        splits = _build_splits_from_laps(laps, 150)
        assert splits is not None
        self.assertEqual(len(splits), 2)
        self.assertEqual(splits[1]["pace"], "7:10")
        self.assertTrue(splits[1]["isFastest"])
        self.assertEqual(splits[0]["cadence"], 170)  # single-leg doubled to SPM

    def test_lap_splits_none_when_missing(self):
        self.assertIsNone(_build_splits_from_laps([], 150))
        self.assertIsNone(_build_splits_from_laps([{"sport": "running"}], 150))


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
