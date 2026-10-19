import os
import tempfile
import unittest
from unittest import mock

from srtforge_lite import window


class WindowTests(unittest.TestCase):
    def test_chromium_command_uses_app_mode_and_private_profile(self):
        cmd = window.chromium_command("/usr/bin/chromium", "http://127.0.0.1:5000/?t=x", "/tmp/prof")
        self.assertEqual(cmd[0], "/usr/bin/chromium")
        self.assertIn("--app=http://127.0.0.1:5000/?t=x", cmd)
        self.assertIn("--user-data-dir=/tmp/prof", cmd)

    def test_find_chromium_returns_existing_file_only(self):
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "chromium")
            open(fake, "w").close()
            os.chmod(fake, 0o755)
            with mock.patch.dict(os.environ, {"PATH": d}), mock.patch.object(window.sys, "platform", "linux"):
                self.assertEqual(window.find_chromium(), fake)
            with mock.patch.dict(os.environ, {"PATH": "/nera/tokio"}), mock.patch.object(window.sys, "platform", "linux"):
                self.assertIsNone(window.find_chromium())

    def test_falls_back_when_no_window_available(self):
        with mock.patch.object(window, "have_webview", return_value=False), mock.patch.object(window, "find_chromium", return_value=None):
            self.assertFalse(window.open_window("http://x", "/tmp/p"))
            self.assertEqual(window.describe(), "window:none")

    def test_chromium_window_blocks_until_closed_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(window, "have_webview", return_value=False):
            ok = os.path.join(d, "ok.sh")
            open(ok, "w").write("#!/bin/sh\nsleep 0.3\nexit 0\n")
            bad = os.path.join(d, "bad.sh")
            open(bad, "w").write("#!/bin/sh\nexit 3\n")
            os.chmod(ok, 0o755)
            os.chmod(bad, 0o755)
            with mock.patch.object(window, "find_chromium", return_value=ok):
                self.assertTrue(window.open_window("http://x", os.path.join(d, "p")))
            with mock.patch.object(window, "find_chromium", return_value=bad):
                self.assertFalse(window.open_window("http://x", os.path.join(d, "p")))


if __name__ == "__main__":
    unittest.main()
