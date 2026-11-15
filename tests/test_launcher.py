"""Tests for engine startup, version reporting, and on-disk state paths."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from garmin_engine import launcher, paths
from garmin_engine import settings as app_settings
from garmin_engine.auth import GarminAuthManager
from garmin_engine.cli import app as cli_app
from garmin_engine.version import __version__

runner = CliRunner()


class TestVersion(unittest.TestCase):
    def test_package_exposes_version(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+")

    def test_cli_version_flag(self):
        result = runner.invoke(cli_app, ["--version"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn(__version__, result.output)
        self.assertIn("LetsRunFaster Ingestor", result.output)


class TestStatePaths(unittest.TestCase):
    def test_home_override_moves_tokens_and_config(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"LRF_INGESTOR_HOME": tmp}, clear=False
        ):
            self.assertEqual(paths.app_home(), Path(tmp))
            self.assertEqual(paths.token_dir(), Path(tmp) / "tokens")
            self.assertEqual(paths.config_file(), Path(tmp) / "config.json")
            # settings.py and auth.py both have to honor the same override,
            # otherwise the desktop shell's userData folder is half-used.
            self.assertEqual(app_settings.config_path(), Path(tmp) / "config.json")
            manager = GarminAuthManager()
            self.assertEqual(manager.token_dir, Path(tmp) / "tokens")
            self.assertTrue(manager.token_dir.is_dir())

    def test_explicit_config_override_wins(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cfg:
            explicit = Path(cfg) / "custom.json"
            with patch.dict(
                os.environ,
                {"LRF_INGESTOR_HOME": home, "LRF_INGESTOR_CONFIG": str(explicit)},
                clear=False,
            ):
                self.assertEqual(app_settings.config_path(), explicit)

    def test_default_home_is_legacy_location(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LRF_INGESTOR_HOME", None)
            self.assertEqual(paths.app_home(), Path.home() / ".garmin-ingestor")

    def test_relative_export_dir_resolves_under_home(self):
        # A packaged app may have an unwritable working directory, so a
        # relative destination must never be resolved against the cwd.
        self.assertEqual(
            paths.resolve_export_dir("./garmin_exports"), Path.home() / "garmin_exports"
        )
        self.assertEqual(paths.resolve_export_dir("~/runs"), Path.home() / "runs")
        self.assertEqual(paths.resolve_export_dir(""), paths.default_export_dir())
        self.assertEqual(
            paths.resolve_export_dir(str(Path.home() / "abs")), Path.home() / "abs"
        )


class TestLauncher(unittest.TestCase):
    def test_entry_point_imports_without_package_context(self):
        # PyInstaller runs launcher.py as __main__, where relative imports
        # raise ImportError. A frozen build that cannot start is worse than
        # useless, so load the file the same way the bootloader does.
        import importlib.util

        path = Path(launcher.__file__)
        spec = importlib.util.spec_from_file_location("launcher_entry", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # must not raise
        self.assertTrue(hasattr(module, "main"))
        self.assertTrue(hasattr(module, "run_ui"))

    def test_port_zero_binds_ephemeral_port(self):
        sock = launcher._bind("127.0.0.1", 0)
        try:
            self.assertGreater(sock.getsockname()[1], 0)
        finally:
            sock.close()

    def test_busy_port_fails_instead_of_silently_rebinding(self):
        sock = launcher._bind("127.0.0.1", 0)
        try:
            port = sock.getsockname()[1]
            with self.assertRaises(SystemExit):
                launcher._bind("127.0.0.1", port, attempts=1)
        finally:
            sock.close()

    def test_ready_line_is_single_line_and_parseable(self):
        line = launcher.ready_line("http://127.0.0.1:54321", 54321)
        self.assertTrue(line.startswith(launcher.READY_PREFIX + " "))
        self.assertNotIn("\n", line)
        fields = dict(part.split("=", 1) for part in line.split(" ")[1:])
        self.assertEqual(fields["url"], "http://127.0.0.1:54321")
        self.assertEqual(fields["port"], "54321")
        self.assertEqual(fields["version"], __version__)
