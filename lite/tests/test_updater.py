import base64
import hashlib
import json
import time
import os
import subprocess
import sys
import tempfile
import unittest

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    HAVE = True
except Exception:  # noqa: BLE001
    HAVE = False

from srtforge_lite import updater

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SWIFT_CLI = os.path.join(ROOT, ".build", "debug", "srtforge-release")


def pub_b64(private):
    return base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def signed(private, version="9.0.0", build="1", data=b"paketas", url="https://srt.example/lite/a.tar.gz"):
    sha = hashlib.sha256(data).hexdigest()
    sig = base64.b64encode(private.sign(updater.message(version, build, sha, url))).decode()
    return {"version": version, "build": build, "downloadURL": url, "sha256": sha, "signature": sig}


@unittest.skipUnless(HAVE, "nėra cryptography")
class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.private = Ed25519PrivateKey.generate()
        self.key = pub_b64(self.private)

    def test_valid_manifest_passes_and_each_tamper_fails(self):
        m = signed(self.private)
        updater.verify_manifest(m, self.key)
        for field, value in [("version", "9.9.9"), ("build", "2"), ("sha256", "0" * 64), ("downloadURL", "https://evil.example/a.tar.gz")]:
            bad = dict(m, **{field: value})
            with self.assertRaises(updater.UpdateError, msg=field):
                updater.verify_manifest(bad, self.key)

    def test_fail_closed_without_signature_or_key(self):
        m = signed(self.private)
        with self.assertRaises(updater.UpdateError):
            updater.verify_manifest({k: v for k, v in m.items() if k != "signature"}, self.key)
        with self.assertRaises(updater.UpdateError):
            updater.verify_manifest(m, "")
        with self.assertRaises(updater.UpdateError):
            updater.verify_manifest(m, pub_b64(Ed25519PrivateKey.generate()))

    def test_version_ordering(self):
        self.assertTrue(updater.is_newer("0.10.0", "0.9.9"))
        self.assertTrue(updater.is_newer("1.0", "0.9.9"))
        self.assertFalse(updater.is_newer("1.0.0", "1.0"))
        self.assertFalse(updater.is_newer("0.1.0", "0.1.0"))

    def test_http_and_remote_plain_urls_refused(self):
        with self.assertRaises(updater.UpdateError):
            updater.fetch("http://evil.example/latest.json")
        with self.assertRaises(updater.UpdateError):
            updater.fetch("http://127.0.0.1/latest.json")  # lokalus tik testams (allow_local)

    def test_check_returns_manifest_only_when_newer_and_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "latest.json")
            with open(path, "w") as handle:
                json.dump(signed(self.private, version="9.0.0"), handle)
            self.assertEqual(updater.check("file://" + path, self.key, allow_local=True)["version"], "9.0.0")
            with open(path, "w") as handle:
                json.dump(signed(self.private, version="0.0.1"), handle)
            self.assertIsNone(updater.check("file://" + path, self.key, allow_local=True))

    def test_install_rejects_modified_package_before_pip_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "a.tar.gz")
            with open(pkg, "wb") as handle:
                handle.write(b"PAKEISTAS")
            m = signed(self.private, data=b"originalus", url="file://" + pkg)
            with self.assertRaisesRegex(updater.UpdateError, "sha256 nesutampa"):
                updater.install(m, self.key, allow_local=True, python="/nonexistent/python")

    @unittest.skipUnless(os.path.exists(SWIFT_CLI), "nėra srtforge-release (swift build)")
    def test_python_verifies_manifest_signed_by_the_swift_release_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file, dmg = os.path.join(tmp, "key"), os.path.join(tmp, "a.tar.gz")
            with open(dmg, "wb") as handle:
                handle.write(os.urandom(4096))
            pub = subprocess.run([SWIFT_CLI, "keygen", key_file], capture_output=True, text=True, check=True).stdout.strip()
            out = subprocess.run([SWIFT_CLI, "manifest", dmg, "0.2.0", "5", "https://srt.example/lite/a.tar.gz", key_file],
                                 capture_output=True, text=True, check=True).stdout
            manifest = json.loads(out)
            updater.verify_manifest(manifest, pub)       # Swift pasirašė -> Python patikrina
            with self.assertRaises(updater.UpdateError):
                updater.verify_manifest(dict(manifest, downloadURL="https://evil.example/x"), pub)

    def test_real_pip_install_from_verified_tarball(self):
        lite_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as tmp:
            venv = os.path.join(tmp, "venv")
            subprocess.run([sys.executable, "-m", "venv", venv], check=True)
            python = os.path.join(venv, "bin", "python")
            if not os.path.exists(python):
                python = os.path.join(venv, "Scripts", "python.exe")
            sdist_dir = os.path.join(tmp, "sd")
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "build"], check=False, capture_output=True)
            built = subprocess.run([sys.executable, "-m", "build", "--sdist", "--outdir", sdist_dir, lite_root], capture_output=True, text=True)
            if built.returncode != 0:
                self.skipTest("build modulis nepasiekiamas: " + built.stderr[-200:])
            tarball = os.path.join(sdist_dir, sorted(os.listdir(sdist_dir))[0])
            with open(tarball, "rb") as handle:
                data = handle.read()
            m = signed(self.private, version="0.1.0", data=data, url="file://" + tarball)
            self.assertEqual(updater.install(m, self.key, allow_local=True, python=python), "0.1.0")
            shown = subprocess.run([python, "-m", "pip", "show", "srtforge-lite"], capture_output=True, text=True)
            self.assertIn("Version: 0.1.0", shown.stdout)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE, "nėra cryptography")
class InstallerUpdateTests(unittest.TestCase):
    def setUp(self):
        self.private = Ed25519PrivateKey.generate()
        self.key = pub_b64(self.private)
        self.tmp = tempfile.TemporaryDirectory()
        self.pkg = os.path.join(self.tmp.name, "SRT-Forge-Lite-9.0.0-linux-x64.tar.gz")
        with open(self.pkg, "wb") as handle:
            handle.write(os.urandom(2048))

    def tearDown(self):
        self.tmp.cleanup()

    def downloads(self, version="9.0.0", tamper=None):
        with open(self.pkg, "rb") as handle:
            sha = hashlib.sha256(handle.read()).hexdigest()
        url = "file://" + self.pkg
        sig = base64.b64encode(self.private.sign(updater.message(version, "1", sha, url))).decode()
        entry = {"platform": "linux", "arch": "x64", "title": "Linux", "kind": "archive", "name": os.path.basename(self.pkg),
                 "url": url, "size": 2048, "sha256": sha, "signature": sig}
        entry.update(tamper or {})
        path = os.path.join(self.tmp.name, "downloads.json")
        with open(path, "w") as handle:
            json.dump({"version": version, "build": "1", "files": [entry]}, handle)
        return "file://" + path

    def test_finds_newer_file_for_this_platform_and_installs_verified(self):
        entry = updater.check_installer(self.downloads(), self.key, allow_local=True, target=("linux", "x64"))
        self.assertEqual(entry["version"], "9.0.0")
        out = os.path.join(self.tmp.name, "dl")
        path = updater.download_installer(entry, out, self.key, allow_local=True)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.path.basename(path), os.path.basename(self.pkg))

    def test_other_platform_old_version_and_forged_entries(self):
        url = self.downloads()
        self.assertIsNone(updater.check_installer(url, self.key, allow_local=True, target=("windows", "x64")))
        self.assertIsNone(updater.check_installer(self.downloads(version="0.0.1"), self.key, allow_local=True, target=("linux", "x64")))
        with self.assertRaises(updater.UpdateError):
            updater.check_installer(self.downloads(tamper={"url": "https://evil.example/x.tar.gz"}), self.key,
                                    allow_local=True, target=("linux", "x64"))

    def test_modified_file_is_not_saved(self):
        entry = updater.check_installer(self.downloads(), self.key, allow_local=True, target=("linux", "x64"))
        with open(self.pkg, "ab") as handle:
            handle.write(b"PAKEISTA")
        out = os.path.join(self.tmp.name, "dl2")
        with self.assertRaisesRegex(updater.UpdateError, "sha256 nesutampa"):
            updater.download_installer(entry, out, self.key, allow_local=True)
        self.assertFalse(os.path.exists(out) and os.listdir(out))

    def test_unsigned_name_field_is_ignored_filename_comes_from_signed_url(self):
        entry = updater.check_installer(self.downloads(), self.key, allow_local=True, target=("linux", "x64"))
        entry["name"] = "../../evil.sh"          # nepasirašytas laukas: turi būti ignoruojamas
        out = os.path.join(self.tmp.name, "dl3")
        path = updater.download_installer(entry, out, self.key, allow_local=True)
        self.assertEqual(os.path.dirname(path), out)
        self.assertEqual(os.path.basename(path), "SRT-Forge-Lite-9.0.0-linux-x64.tar.gz")

    def test_disallowed_extension_in_signed_url_is_refused(self):
        for bad in ("evil.sh", "x.bat", "run.py", "..", "noext"):
            with self.assertRaises(updater.UpdateError, msg=bad):
                updater._signed_filename("https://github.com/x/releases/download/v1/" + bad)
        self.assertEqual(updater._signed_filename("https://h/p/A%20b.dmg"), "A b.dmg")

    def test_existing_file_is_never_overwritten(self):
        entry = updater.check_installer(self.downloads(), self.key, allow_local=True, target=("linux", "x64"))
        out = os.path.join(self.tmp.name, "dl4")
        os.makedirs(out)
        victim = os.path.join(out, "SRT-Forge-Lite-9.0.0-linux-x64.tar.gz")
        with open(victim, "wb") as handle:
            handle.write(b"SVARBU")
        path = updater.download_installer(entry, out, self.key, allow_local=True)
        self.assertNotEqual(path, victim)
        self.assertTrue(path.endswith("-1.tar.gz"))
        with open(victim, "rb") as handle:
            self.assertEqual(handle.read(), b"SVARBU")

    def test_server_exposes_update_and_installs_via_injected_hooks(self):
        import urllib.request
        from srtforge_lite.server import App

        opened = []
        entry = {"version": "9.0.0", "title": "Linux", "size": 5}
        app = App(runner=lambda *a, **k: None, update_checker=lambda: entry, update_delay=0,
                  update_installer=lambda e: opened.append(e) or "/tmp/x/installer.tar.gz")
        url = app.start(open_browser=False)
        base, token = url.split("/?t=")
        try:
            time.sleep(0.3)
            def call(method, path):
                req = urllib.request.Request(base + path, method=method, headers={"X-Token": token}, data=b"" if method == "POST" else None)
                return json.loads(urllib.request.urlopen(req, timeout=5).read())
            self.assertEqual(call("GET", "/api/update")["version"], "9.0.0")
            self.assertEqual(call("POST", "/api/update/install"), {"ok": True, "file": "installer.tar.gz"})
            self.assertEqual(opened, [entry])
        finally:
            app.stop()
