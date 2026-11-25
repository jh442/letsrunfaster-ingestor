"""CLI configuration tests for the non-block import choices."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from garmin_engine.cli import app


class TestConfigureModes(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        tmp.close()
        self.cfg_path = tmp.name
        self._old_env = os.environ.get("LRF_INGESTOR_CONFIG")
        os.environ["LRF_INGESTOR_CONFIG"] = self.cfg_path
        self.runner = CliRunner()

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("LRF_INGESTOR_CONFIG", None)
        else:
            os.environ["LRF_INGESTOR_CONFIG"] = self._old_env
        try:
            os.unlink(self.cfg_path)
        except OSError:
            pass

    def test_configure_persists_mode_and_visibility(self):
        result = self.runner.invoke(
            app, ["configure", "--remote-mode", "activity", "--private"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        stored = json.loads(Path(self.cfg_path).read_text())
        self.assertEqual(stored["remote_mode"], "activity")
        self.assertIs(stored["import_private"], True)

    def test_show_prints_mode_never_key(self):
        self.runner.invoke(
            app, ["configure", "--remote-key", "lrf_abcdef123456", "--remote-mode", "auto"]
        )
        result = self.runner.invoke(app, ["configure", "--show"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("auto", result.output)
        self.assertIn("lrf_abcdef12", result.output)
        self.assertNotIn("lrf_abcdef123456", result.output)

    def test_unknown_mode_exits_with_usage_error(self):
        result = self.runner.invoke(app, ["configure", "--remote-mode", "everything"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("block", result.output)

    def test_export_rejects_bad_mode_before_network(self):
        # No Garmin login on purpose: a bad flag must fail fast, not after auth.
        result = self.runner.invoke(
            app, ["export", "--start", "2026-09-01", "--end", "2026-09-02",
                  "--remote-mode", "everything"]
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("block", result.output)


if __name__ == "__main__":
    unittest.main()
