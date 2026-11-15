"""Tests for login-strategy selection and crash diagnostics.

Both exist because of a real field failure: the v0.2.0 arm64 Windows build
died with exit code 1 immediately after a login request, leaving no
traceback. These tests pin the two halves of the fix -- avoid the native
curl_cffi login path on Windows ARM, and always leave a diagnosable log.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from garmin_engine import auth, diagnostics


class TestCffiStrategies(unittest.TestCase):
    def test_env_override_disables_cffi(self):
        for value in ("0", "false", "no", "off", " FALSE "):
            with self.subTest(value=value), patch.dict(
                os.environ, {"LRF_ENABLE_CFFI": value}, clear=False
            ):
                self.assertTrue(auth.cffi_disabled())

    def test_env_override_enables_cffi(self):
        for value in ("1", "true", "yes", "on", " YES "):
            with self.subTest(value=value), patch.dict(
                os.environ, {"LRF_ENABLE_CFFI": value}, clear=False
            ):
                self.assertFalse(auth.cffi_disabled())

    def test_default_only_skips_cffi_on_windows_arm(self):
        with patch.dict(os.environ, {"LRF_ENABLE_CFFI": ""}, clear=False):
            with patch.object(auth.sys, "platform", "win32"), patch.object(
                auth.platform, "machine", return_value="ARM64"
            ):
                self.assertTrue(auth.cffi_disabled())
            with patch.object(auth.sys, "platform", "win32"), patch.object(
                auth.platform, "machine", return_value="AMD64"
            ):
                self.assertFalse(auth.cffi_disabled())
            with patch.object(auth.sys, "platform", "linux"), patch.object(
                auth.platform, "machine", return_value="aarch64"
            ):
                self.assertFalse(auth.cffi_disabled())

    def test_skip_set_reaches_the_inner_client(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"LRF_ENABLE_CFFI": "0"}, clear=False
        ):
            manager = auth.GarminAuthManager(token_dir=Path(tmp))
            client = manager.get_client()
        # garminconnect keeps the strategy set on the inner Client object;
        # setting it on the Garmin wrapper would be silently ignored.
        inner = getattr(client, "client", client)
        self.assertEqual(set(inner.skip_strategies), set(auth.CFFI_STRATEGIES))

    def test_cffi_allowed_leaves_strategies_untouched(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"LRF_ENABLE_CFFI": "1"}, clear=False
        ):
            manager = auth.GarminAuthManager(token_dir=Path(tmp))
            client = manager.get_client()
        inner = getattr(client, "client", client)
        self.assertEqual(set(inner.skip_strategies), set())

    def test_client_is_created_without_an_mfa_prompt(self):
        # A prompt_mfa callable makes garminconnect call it and lose the
        # "MFA Required" auth error the UI depends on (it would surface as a
        # generic login failure instead, breaking two-step verification).
        with tempfile.TemporaryDirectory() as tmp:
            manager = auth.GarminAuthManager(token_dir=Path(tmp))
            client = manager.get_client()
        self.assertIsNone(getattr(client, "prompt_mfa", None))


class TestDiagnostics(unittest.TestCase):
    def test_install_writes_a_startup_banner_and_exit_marker(self):
        # Run in a child process: `install()` is process-global (it replaces
        # sys.excepthook and registers atexit), so it cannot be exercised
        # twice inside one interpreter.
        script = (
            "from garmin_engine import diagnostics\n"
            "path = diagnostics.install()\n"
            "diagnostics.install()\n"  # idempotent: no duplicate handlers
            "diagnostics.write('sentinel line')\n"
            "print(path)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "LRF_INGESTOR_HOME": tmp}
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            log = Path(tmp) / "logs" / "engine-python.log"
            self.assertTrue(log.is_file(), result.stderr)
            text = log.read_text(encoding="utf-8")
            self.assertIn("engine startup", text)
            self.assertIn("curl_cffi", text)
            self.assertIn("sentinel line", text)
            self.assertIn("exiting normally", text)
            # faulthandler keeps its own handle; a duplicate install must not
            # append a second startup banner.
            self.assertEqual(text.count("engine startup version="), 1)

    def test_uncaught_thread_exception_is_recorded(self):
        script = (
            "import threading\n"
            "from garmin_engine import diagnostics\n"
            "diagnostics.install()\n"
            "def boom():\n"
            "    raise ValueError('thread exploded')\n"
            "t = threading.Thread(target=boom)\n"
            "t.start()\n"
            "t.join()\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "LRF_INGESTOR_HOME": tmp}
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            text = (Path(tmp) / "logs" / "engine-python.log").read_text(encoding="utf-8")
            self.assertIn("uncaught exception in thread", text)
            self.assertIn("thread exploded", text)
            self.assertIn("Traceback", text)

    def test_log_path_falls_back_when_home_is_unwritable(self):
        # A read-only state directory must not stop the engine from booting.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"LRF_INGESTOR_HOME": str(Path(tmp) / "missing" / "nested")}
        ):
            path = diagnostics.log_path()
        self.assertTrue(str(path).endswith("engine-python.log"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
