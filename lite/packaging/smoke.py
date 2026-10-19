#!/usr/bin/env python3
"""Supakuoto paketo dūmų testas: įdiegia/išpakuoja TIKRĄ išvesties failą, paleidžia be Python aplinkos, perduoda trumpą
lietuvišką įrašą per HTTP ir tikrina SRT. Skirtas CI (Windows, macOS, Linux).

  python lite/packaging/smoke.py dist-lite/<paketas>
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # Windows cp1252 neturi ą, č, ė...
    except (AttributeError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
CLIP = os.path.join(os.path.dirname(HERE), "tests", "data", "speech_lt.wav")
NAME = "SRT Forge Lite"


def install(package: str, root: str) -> str:
    """Grąžina vykdomojo failo kelią po diegimo/išpakavimo."""
    if package.endswith(".tar.gz"):
        with tarfile.open(package) as tar:
            tar.extractall(root)
        folder = os.path.join(root, os.listdir(root)[0])
        env = dict(os.environ, HOME=root)
        subprocess.run(["sh", os.path.join(folder, "Install.sh")], check=True, env=env)
        return os.path.join(root, ".local", "share", "srtforge-lite", NAME)
    if package.endswith(".dmg"):
        mount = os.path.join(root, "mnt")
        os.makedirs(mount)
        subprocess.run(["hdiutil", "attach", package, "-nobrowse", "-readonly", "-mountpoint", mount], check=True)
        try:
            target = os.path.join(root, f"{NAME}.app")
            shutil.copytree(os.path.join(mount, f"{NAME}.app"), target, symlinks=True)
        finally:
            subprocess.run(["hdiutil", "detach", mount, "-force"], check=False)
        return os.path.join(target, "Contents", "MacOS", NAME)
    if package.endswith(".exe"):
        target = os.path.join(root, "app")
        subprocess.run([package, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/DIR={target}"], check=True)
        return os.path.join(target, f"{NAME}.exe")
    if package.endswith(".zip"):
        shutil.unpack_archive(package, root)
        return os.path.join(root, NAME, f"{NAME}.exe")
    sys.exit(f"Nežinomas paketas: {package}")


def main() -> int:
    package = os.path.abspath(sys.argv[1])
    work = tempfile.mkdtemp(prefix="srtforge-smoke-")
    data = os.path.join(work, "data")
    models = os.path.join(work, "models")
    exe = install(package, work)
    print("vykdomasis:", exe, "dydis", os.path.getsize(exe), flush=True)
    assert os.path.exists(exe), "po diegimo nėra vykdomojo failo"

    env = {k: v for k, v in os.environ.items() if k.upper() not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env.update(SRTFORGE_DATA_DIR=data, SRTFORGE_MODEL_DIR=models, SRTFORGE_DEBUG="1")
    # PATH palikti: Windows reikia sisteminių DLL; ffmpeg turi būti paties paketo viduje (tikrinam žemiau)
    if sys.platform == "darwin":                  # macOS: savas langas turi būti supakuotas (pywebview), ne naršyklė
        probe = subprocess.run([exe, "--check-window"], env=env, capture_output=True, text=True, timeout=120)
        print("langas:", probe.stdout.strip(), probe.stderr.strip()[-300:], flush=True)
        assert probe.stdout.strip() == "window:webview", f"nėra natyvaus lango: {probe.stdout!r} {probe.stderr[-500:]!r}"
    print(f"paleidžiama t={time.time():.3f}", flush=True)
    proc = subprocess.Popen([exe, "--no-browser", "--idle-exit", "120"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        run_json = os.path.join(data, "run.json")
        for _ in range(240):
            if os.path.exists(run_json):
                break
            if proc.poll() is not None:
                log = os.path.join(data, "srtforge.log")
                print(open(log, encoding="utf-8").read() if os.path.exists(log) else "(žurnalo nėra)")
                sys.exit(f"programa baigėsi iš karto, kodas {proc.returncode}")
            time.sleep(0.5)
        else:
            sys.exit("per 120 s neatsirado run.json")
        info = json.load(open(run_json, encoding="utf-8"))
        base = f"http://127.0.0.1:{info['port']}"

        # Grynas lizdas be DNS (getaddrinfo Windows runneriuose gali užstrigti net 127.0.0.1) ir be tarpinių serverių
        def call(method, path, body=None, timeout=30):
            import socket

            print(f"  klientas jungiasi t={time.time():.3f} {method} {path}", flush=True)
            payload = body or b""
            request = (f"{method} {path} HTTP/1.0\r\nHost: 127.0.0.1:{info['port']}\r\nX-Token: {info['token']}\r\n"
                       f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode() + payload
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(("127.0.0.1", int(info["port"])))
                sock.sendall(request)
                chunks = []
                while True:
                    data = sock.recv(1 << 16)
                    if not data:
                        break
                    chunks.append(data)
            raw = b"".join(chunks)
            head, _, content = raw.partition(b"\r\n\r\n")
            status = int(head.split(b" ", 2)[1])
            if status >= 400:
                raise RuntimeError(f"HTTP {status}: {content[:200]!r}")
            return content

        def diagnose(reason):
            print("=== DIAGNOSTIKA:", reason, "===", flush=True)
            print("procesas gyvas:", proc.poll() is None, "kodas:", proc.poll(), flush=True)
            for name in ("srtforge.log", "run.json"):
                path = os.path.join(data, name)
                if os.path.exists(path):
                    text = open(path, encoding="utf-8", errors="replace").read()
                    print(f"--- {name} ---\n{text[-3000:] if name != 'run.json' else '(yra)'}", flush=True)
            if os.name == "nt":
                print("--- procesai SRT Forge:", flush=True)
                subprocess.run(["tasklist", "/fi", "imagename eq SRT Forge Lite.exe", "/v"], check=False)
                out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
                print("--- netstat (127.0.0.1):", flush=True)
                print("\n".join(l for l in out.splitlines() if "127.0.0.1" in l), flush=True)

        try:
            call("GET", "/api/ping", timeout=40)       # >25 s: faulthandler spėja išmesti pėdsakus
        except Exception as error:  # noqa: BLE001
            time.sleep(3)
            diagnose(f"ping nepavyko: {error!r}")
            raise

        assert json.loads(call("GET", "/api/ping"))["ok"] is True
        page = call("GET", f"/?t={info['token']}").decode()
        assert "SRT Forge Lite" in page and "__TOKEN__" not in page
        real_token = info["token"]
        info["token"] = "blogas"
        try:
            call("GET", "/api/info")
            sys.exit("serveris priėmė blogą tokeną")
        except RuntimeError as error:
            assert "HTTP 403" in str(error), error
        finally:
            info["token"] = real_token

        with open(CLIP, "rb") as handle:
            created = json.loads(call("PUT", "/api/upload?name=smoke.wav&model=tiny&language=lt", handle.read()))
        print("užduotis", created["id"], flush=True)
        deadline = time.time() + 900          # modelio atsisiuntimas + atpažinimas
        job = created
        while time.time() < deadline and job["state"] not in ("done", "failed", "cancelled"):
            time.sleep(3)
            job = json.loads(call("GET", f"/api/jobs/{created['id']}"))
            print(" ", job["state"], round(job["progress"], 2), job["stage"], flush=True)
        if job["state"] != "done":
            log = os.path.join(data, "srtforge.log")
            if os.path.exists(log):
                print("=== srtforge.log ===")
                print(open(log, encoding="utf-8", errors="replace").read()[-6000:])
        assert job["state"] == "done", f"užduotis nebaigta: {job}"
        assert job["cues"] >= 1, "nėra subtitrų"
        srt = call("GET", f"/api/jobs/{created['id']}/srt").decode("utf-8")
        assert "-->" in srt and srt.startswith("1\n"), srt[:200]
        assert call("GET", f"/api/jobs/{created['id']}/vtt").startswith(b"WEBVTT")
        # naujos funkcijos: eksportai, vertimas, video su subtitrais
        assert json.loads(call("GET", f"/api/jobs/{created['id']}/json"))["segments"], "JSON be segmentų"
        assert call("GET", f"/api/jobs/{created['id']}/ts").startswith(b"["), "laikų tekstas"
        assert call("GET", f"/api/jobs/{created['id']}/txt").strip(), "tuščias tekstas"

        import imageio_ffmpeg
        video = os.path.join(work, "klipas.mp4")
        made = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=4:s=320x240:r=25",
                               "-i", CLIP, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", video],
                              capture_output=True, text=True)
        assert made.returncode == 0, made.stderr
        with open(video, "rb") as handle:
            vjob = json.loads(call("PUT", "/api/upload?name=klipas.mp4&model=tiny&language=lt", handle.read()))
        deadline = time.time() + 600
        while time.time() < deadline and vjob["state"] not in ("done", "failed"):
            time.sleep(3)
            vjob = json.loads(call("GET", f"/api/jobs/{vjob['id']}"))
        assert vjob["state"] == "done" and vjob["has_video"], f"video darbas: {vjob}"
        for mode in ("soft", "burn"):
            started = json.loads(call("POST", f"/api/jobs/{vjob['id']}/embed?mode={mode}", b""))
            end = time.time() + 300
            while time.time() < end and started["embed"]["state"] == "running":
                time.sleep(2)
                started = json.loads(call("GET", f"/api/jobs/{vjob['id']}"))
            assert started["embed"]["state"] == "done", f"{mode}: {started['embed']}"
            data = call("GET", f"/api/jobs/{vjob['id']}/video", timeout=120)
            assert len(data) > 2000, f"{mode}: per mažas video ({len(data)} B)"
            print("  video", mode, "OK", len(data), "B", flush=True)

        # kalbėtojų atskyrimas: du skirtingi balsai (A B A B), nurodyti 2 kalbėtojai
        talk = os.path.join(HERE, "..", "tests", "data", "two_speakers.wav")
        with open(talk, "rb") as handle:
            sjob = json.loads(call("PUT", "/api/upload?name=pokalbis.wav&model=base&language=lt&speakers=2", handle.read()))
        deadline = time.time() + 1500          # base modelis + ~47 MB diarizacijos modeliai
        while time.time() < deadline and sjob["state"] not in ("done", "failed"):
            time.sleep(4)
            sjob = json.loads(call("GET", f"/api/jobs/{sjob['id']}"))
        assert sjob["state"] == "done", f"kalbėtojų darbas: {sjob}"
        speakers = [x["name"] for x in sjob["speakers"]]
        assert len(speakers) == 2, f"turėjo būti 2 kalbėtojai: {speakers}; įspėjimai: {sjob['warnings']}"
        spk_txt = call("GET", f"/api/jobs/{sjob['id']}/txt").decode("utf-8")
        assert "Kalbėtojas 1:" in spk_txt and "Kalbėtojas 2:" in spk_txt, spk_txt[:300]
        print("  kalbėtojai OK", speakers, flush=True)

        print("OK", platform.platform(), "subtitrų:", job["cues"])
        print(srt[:300])
        return 0
    except (ConnectionError, OSError):
        print("programa nebeatsako; procesas baigėsi:", proc.poll(), flush=True)
        log = os.path.join(data, "srtforge.log")
        print(open(log, encoding="utf-8", errors="replace").read()[-3000:] if os.path.exists(log) else "(žurnalo nėra)", flush=True)
        raise
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
