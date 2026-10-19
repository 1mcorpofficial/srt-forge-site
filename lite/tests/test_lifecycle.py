import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

LITE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RunFilePermissions(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX teisės")
    def test_run_file_and_data_dir_are_private_from_creation(self):
        import stat
        from srtforge_lite.server import App, data_dir, write_run_file

        with tempfile.TemporaryDirectory() as parent:
            os.environ["SRTFORGE_DATA_DIR"] = os.path.join(parent, "d")
            try:
                directory = data_dir()
                self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
                app = App(runner=lambda *a, **k: None)
                try:
                    path = write_run_file(app, "http://127.0.0.1:5555/?t=x", directory)
                    self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
                    self.assertEqual(json.load(open(path))["port"], 5555)
                    open(path + ".tmp", "w").close()        # liekana po nulūžimo netrukdo
                    write_run_file(app, "http://127.0.0.1:5556/?t=x", directory)
                    self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
                finally:
                    app.stop()
            finally:
                os.environ.pop("SRTFORGE_DATA_DIR", None)


class Lifecycle(unittest.TestCase):
    def start(self, data, *extra):
        env = dict(os.environ, SRTFORGE_DATA_DIR=data, PYTHONPATH=LITE, PYTHONUNBUFFERED="1")
        return subprocess.Popen([sys.executable, "-m", "srtforge_lite", "--no-browser", *extra], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def test_second_launch_reuses_running_instance_and_idle_exit_cleans_up(self):
        with tempfile.TemporaryDirectory() as data:
            first = self.start(data, "--idle-exit", "4")
            try:
                for _ in range(50):
                    if os.path.exists(os.path.join(data, "run.json")):
                        break
                    time.sleep(0.1)
                info = json.load(open(os.path.join(data, "run.json")))
                self.assertEqual(info["pid"], first.pid)

                second = self.start(data)
                out, _ = second.communicate(timeout=20)
                self.assertEqual(second.returncode, 0)
                self.assertIn("jau veikia", out)
                self.assertIn(f":{info['port']}/", out)

                first.wait(timeout=25)            # niekas nesikreipia -> išsijungia po ~4 s
                self.assertEqual(first.returncode, 0)
                self.assertFalse(os.path.exists(os.path.join(data, "run.json")), "run.json turi būti pašalintas")
            finally:
                if first.poll() is None:
                    first.kill()

    def test_stale_run_file_does_not_block_start(self):
        with tempfile.TemporaryDirectory() as data:
            with open(os.path.join(data, "run.json"), "w") as handle:
                json.dump({"pid": 999999, "port": 1, "token": "x"}, handle)
            proc = self.start(data, "--idle-exit", "2")
            try:
                out = proc.stdout.readline() + proc.stdout.readline()
                self.assertIn("Atidaryk naršyklėje", out)
                proc.wait(timeout=20)
            finally:
                if proc.poll() is None:
                    proc.kill()

    def test_keeps_running_while_client_polls(self):
        import urllib.request
        with tempfile.TemporaryDirectory() as data:
            proc = self.start(data, "--idle-exit", "3")
            try:
                line = ""
                while "t=" not in line:
                    line = proc.stdout.readline()
                url = line.split("naršyklėje: ")[1].strip()
                token = url.split("t=")[1]
                base = url.split("/?")[0]
                for _ in range(8):                      # 8 s > 3 s idle riba, bet kas sekundę „ping“
                    req = urllib.request.Request(base + "/api/ping", headers={"X-Token": token})
                    urllib.request.urlopen(req, timeout=5).read()
                    time.sleep(1)
                self.assertIsNone(proc.poll(), "programa išsijungė, nors naršyklė aktyvi")
                proc.wait(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()


if __name__ == "__main__":
    unittest.main()
