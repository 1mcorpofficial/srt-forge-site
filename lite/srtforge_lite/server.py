"""Vietinis web UI: 127.0.0.1, atsitiktinis prievadas, sesijos tokenas, Host patikra (apsauga nuo DNS rebinding)."""
from __future__ import annotations

import json
import os
import queue
import secrets
import shutil
import tempfile
import threading
import sys
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional

from . import __version__
from .core import drop_hallucinated, find_hallucinations, to_json, to_srt, to_ts, to_txt, to_vtt
from .engine import MODELS, embed_subtitles, has_video, recommend_model, total_ram_gb, transcribe
from .models import system_report

UI_PATH = os.path.join(os.path.dirname(__file__), "ui.html")
# fmt -> (Content-Type, plėtinys, atvaizdavimas pagal darbą)
FORMATS = {
    "srt": ("application/x-subrip", "srt", lambda job: to_srt(job.cues)),
    "vtt": ("text/vtt", "vtt", lambda job: to_vtt(job.cues)),
    "txt": ("text/plain", "txt", lambda job: to_txt(job.cues)),
    "ts": ("text/plain", "laikai.txt", lambda job: to_ts(job.cues)),
    "json": ("application/json", "json", lambda job: to_json(job.cues, job.language, job.options.get("task", "transcribe"))),
}
TASKS = ("transcribe", "translate")
MAX_NAME = 200


class Job:
    def __init__(self, name: str, path: str, options: dict):
        self.id = uuid.uuid4().hex[:12]
        self.name = name
        self.path = path
        self.options = options
        self.state = "waiting"          # waiting | running | done | failed | cancelled
        self.stage = "Eilėje"
        self.progress = 0.0
        self.error: Optional[str] = None
        self.cues = []
        self.warnings = []
        self.language: Optional[str] = None
        self.cancel = threading.Event()
        self.has_video = False
        self.embed: dict = {"state": "none", "mode": None, "error": None, "path": None, "name": None}

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "state": self.state, "stage": self.stage,
                "progress": round(self.progress, 3), "error": self.error, "warnings": self.warnings,
                "language": self.language, "cues": len(self.cues), "task": self.options.get("task", "transcribe"),
                "has_video": self.has_video,
                "embed": {k: self.embed[k] for k in ("state", "mode", "error", "name")}}

    def remove_files(self) -> None:
        for path in (self.path, self.embed.get("path")):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass


class App:
    def __init__(self, model_dir: Optional[str] = None, work_dir: Optional[str] = None, runner=transcribe,
                 update_checker=None, update_installer=None, update_delay: float = 15):
        self.token = secrets.token_urlsafe(24)
        self.model_dir = model_dir
        self.work_dir = work_dir or tempfile.mkdtemp(prefix="srtforge-uploads-")
        self.jobs: Dict[str, Job] = {}
        self.queue: "queue.Queue[Job]" = queue.Queue()
        self.runner = runner
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.last_seen = time.time()
        self.update_entry: Optional[dict] = None
        self.update_checker = update_checker
        self.update_installer = update_installer
        threading.Thread(target=self._worker, daemon=True).start()
        if update_checker:
            timer = threading.Timer(update_delay, self._check_update)          # po paleidimo, kad DNS/tinklas nestabdytų UI
            timer.daemon = True
            timer.start()

    def _check_update(self) -> None:
        try:
            self.update_entry = self.update_checker()
        except Exception:  # noqa: BLE001 - atnaujinimų patikra niekada netrukdo darbui
            self.update_entry = None

    def busy(self) -> bool:
        return any(job.state in ("waiting", "running") for job in self.jobs.values())

    def idle_seconds(self) -> float:
        return time.time() - self.last_seen

    def _worker(self) -> None:
        while True:
            job = self.queue.get()
            if job.cancel.is_set():
                job.state, job.stage = "cancelled", "Nutraukta"
                continue
            job.state = "running"

            def progress(stage: str, value: float, job=job) -> None:
                job.stage, job.progress = stage, value

            try:
                cues, language, _duration = self.runner(
                    job.path, model=job.options["model"], language=job.options["language"],
                    model_dir=self.model_dir, prompt=job.options.get("prompt"), task=job.options.get("task", "transcribe"),
                    progress=progress, cancelled=job.cancel.is_set)
                flags = find_hallucinations(cues)
                job.warnings = [f"Tikėtina Whisper haliucinacija: „{p}“ ×{n} (subtitrai {a + 1}–{b + 1})" for a, b, p, n in flags]
                job.cues = drop_hallucinated(cues, flags)
                job.language = language
                try:
                    job.has_video = has_video(job.path)       # video failą paliekame subtitrų įdėjimui
                except Exception:  # noqa: BLE001
                    job.has_video = False
                job.state, job.stage, job.progress = "done", "Baigta", 1.0
            except InterruptedError:
                job.state, job.stage = "cancelled", "Nutraukta"
            except Exception as error:  # noqa: BLE001
                job.state, job.stage, job.error = "failed", "Klaida", str(error)
                _log_exception(job.name)
            finally:
                if not job.has_video:
                    try:
                        os.remove(job.path)
                    except OSError:
                        pass

    def embed_worker(self, job: Job, mode: str) -> None:
        job.embed = {"state": "running", "mode": mode, "error": None, "path": None, "name": None}
        try:
            base = os.path.join(self.work_dir, uuid.uuid4().hex)
            out = embed_subtitles(job.path, to_srt(job.cues), base, mode)
            stem = os.path.splitext(job.name)[0] or "video"
            suffix = ".subtitrai" if mode == "soft" else ".su-subtitrais"
            job.embed = {"state": "done", "mode": mode, "error": None, "path": out,
                         "name": f"{stem}{suffix}{os.path.splitext(out)[1]}"}
        except Exception as error:  # noqa: BLE001
            job.embed = {"state": "failed", "mode": mode, "error": str(error), "path": None, "name": None}
            _log_exception(job.name)

    def start(self, port: int = 0, open_browser: bool = True) -> str:
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SRTForgeLite"

            def log_message(self, *args):  # tyliai (derinimo režime užrašoma)
                if os.environ.get("SRTFORGE_DEBUG"):
                    _debug_log("request " + (args[0] % args[1:] if args else "") + f" t={time.time():.3f}")

            # --- apsauga ---
            def _authorized(self) -> bool:
                host = (self.headers.get("Host") or "").split(":")[0]
                if host not in ("127.0.0.1", "localhost"):
                    return False
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                supplied = self.headers.get("X-Token") or (query.get("t") or [""])[0]
                ok = secrets.compare_digest(supplied, app.token)
                if ok:
                    app.last_seen = time.time()
                return ok

            def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: Optional[dict] = None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _stream_file(self, path: str, name: str) -> None:
                try:
                    size = os.path.getsize(path)
                    handle = open(path, "rb")
                except OSError:
                    return self._json(404, {"error": "failas dingo"})
                with handle:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
                    self.end_headers()
                    shutil.copyfileobj(handle, self.wfile, 1 << 20)

            def _json(self, code: int, payload) -> None:
                self._send(code, json.dumps(payload, ensure_ascii=False).encode())

            # --- maršrutai ---
            def do_GET(self):
                path = urllib.parse.urlparse(self.path).path
                if not self._authorized():
                    return self._json(403, {"error": "forbidden"})
                if path == "/":
                    with open(UI_PATH, encoding="utf-8") as handle:
                        html = handle.read().replace("__TOKEN__", app.token).replace("__VERSION__", __version__)
                    return self._send(200, html.encode(), "text/html; charset=utf-8")
                if path == "/api/ping":
                    return self._json(200, {"ok": True, "version": __version__})
                if path == "/api/update":
                    entry = app.update_entry
                    return self._json(200, {"available": bool(entry), "version": entry and entry["version"],
                                            "title": entry and entry.get("title"), "size": entry and entry.get("size")})
                if path == "/api/info":
                    return self._json(200, {"version": __version__, "models": MODELS, "recommended": recommend_model(),
                                            "ram_gb": round(total_ram_gb(), 1)})
                if path == "/api/system":
                    return self._json(200, system_report(MODELS, total_ram_gb(), recommend_model(),
                                                         [app.model_dir] if app.model_dir else None))
                if path == "/api/jobs":
                    return self._json(200, [j.public() for j in app.jobs.values()])
                parts = path.strip("/").split("/")
                if len(parts) >= 3 and parts[0] == "api" and parts[1] == "jobs":
                    job = app.jobs.get(parts[2])
                    if not job:
                        return self._json(404, {"error": "nerasta"})
                    if len(parts) == 3:
                        return self._json(200, job.public())
                    fmt = parts[3]
                    if fmt == "transcript" and job.state == "done":
                        return self._json(200, {"language": job.language, "task": job.options.get("task", "transcribe"),
                                                "cues": [{"start": c.start, "end": c.end, "text": c.text.replace("\n", " ")}
                                                         for c in job.cues]})
                    if fmt == "video" and job.embed.get("state") == "done" and job.embed.get("path"):
                        return self._stream_file(job.embed["path"], job.embed["name"])
                    if fmt in FORMATS and job.state == "done":
                        ctype, ext, render = FORMATS[fmt]
                        stem = os.path.splitext(job.name)[0] or "irasas"
                        quoted = urllib.parse.quote(f"{stem}.{ext}")
                        return self._send(200, render(job).encode("utf-8"), ctype + "; charset=utf-8",
                                          {"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"})
                return self._json(404, {"error": "nerasta"})

            def do_PUT(self):  # failo įkėlimas: PUT /api/upload?name=...&model=...&language=...
                parsed = urllib.parse.urlparse(self.path)
                if not self._authorized():
                    return self._json(403, {"error": "forbidden"})
                if parsed.path != "/api/upload":
                    return self._json(404, {"error": "nerasta"})
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                name = os.path.basename(query.get("name", "irasas"))[:MAX_NAME] or "irasas"
                model = query.get("model") or recommend_model()
                if model not in MODELS:
                    return self._json(400, {"error": "nežinomas modelis"})
                language = query.get("language") or None
                if language == "auto":
                    language = None
                task = query.get("task") or "transcribe"
                if task not in TASKS:
                    return self._json(400, {"error": "nežinoma užduotis"})
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return self._json(400, {"error": "tuščias failas"})

                suffix = os.path.splitext(name)[1][:10]
                path = os.path.join(app.work_dir, uuid.uuid4().hex + suffix)
                remaining = length
                with open(path, "wb") as handle:
                    while remaining > 0:
                        chunk = self.rfile.read(min(1 << 20, remaining))
                        if not chunk:
                            break
                        handle.write(chunk)
                        remaining -= len(chunk)
                if remaining:
                    os.remove(path)
                    return self._json(400, {"error": "įkėlimas nutrūko"})

                job = Job(name, path, {"model": model, "language": language, "prompt": query.get("prompt"), "task": task})
                app.jobs[job.id] = job
                app.queue.put(job)
                return self._json(200, job.public())

            def do_POST(self):
                if not self._authorized():
                    return self._json(403, {"error": "forbidden"})
                parsed = urllib.parse.urlparse(self.path)
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "embed":
                    job = app.jobs.get(parts[2])
                    mode = (urllib.parse.parse_qs(parsed.query).get("mode") or ["soft"])[0]
                    if not job or job.state != "done":
                        return self._json(404, {"error": "nerasta"})
                    if not job.has_video or mode not in ("soft", "burn"):
                        return self._json(400, {"error": "šis failas neturi vaizdo arba režimas neteisingas"})
                    if job.embed.get("state") == "running":
                        return self._json(409, {"error": "jau vyksta"})
                    threading.Thread(target=app.embed_worker, args=(job, mode), daemon=True).start()
                    time.sleep(0.05)
                    return self._json(200, job.public())
                if parsed.path != "/api/update/install" or not app.update_entry or not app.update_installer:
                    return self._json(404, {"error": "nerasta"})
                try:
                    path = app.update_installer(app.update_entry)
                    return self._json(200, {"ok": True, "file": os.path.basename(path)})
                except Exception as error:  # noqa: BLE001
                    return self._json(502, {"error": str(error)})

            def do_DELETE(self):
                if not self._authorized():
                    return self._json(403, {"error": "forbidden"})
                parsed = urllib.parse.urlparse(self.path)
                parts = parsed.path.strip("/").split("/")
                job = app.jobs.get(parts[2]) if len(parts) == 3 and parts[:2] == ["api", "jobs"] else None
                if not job:
                    return self._json(404, {"error": "nerasta"})
                if job.state in ("done", "failed", "cancelled"):      # baigtą darbą pašaliname kartu su failais
                    job.remove_files()
                    app.jobs.pop(job.id, None)
                    return self._json(200, {"removed": True})
                job.cancel.set()
                if job.state == "waiting":
                    job.state, job.stage = "cancelled", "Nutraukta"
                return self._json(200, job.public())

        class Server(ThreadingHTTPServer):
            def get_request(self):
                request, address = super().get_request()
                if os.environ.get("SRTFORGE_DEBUG"):
                    _debug_log(f"accept {address} t={time.time():.3f}")
                return request, address

        app.httpd = Server(("127.0.0.1", port), Handler)
        if os.environ.get("SRTFORGE_DEBUG"):
            _debug_log(f"listening {app.httpd.server_address} t={time.time():.3f}")
        url = f"http://127.0.0.1:{app.httpd.server_address[1]}/?t={app.token}"
        threading.Thread(target=app.httpd.serve_forever, daemon=True).start()
        if open_browser:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        return url

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
        shutil.rmtree(self.work_dir, ignore_errors=True)


def _log_exception(name: str) -> None:
    """Pilnas klaidos pėdsakas į duomenų katalogo žurnalą (palaikymui ir CI derinimui); niekada neišmeta klaidos."""
    try:
        import traceback

        with open(os.path.join(data_dir(), "srtforge.log"), "a", encoding="utf-8") as handle:
            handle.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} užduotis {name}\n{traceback.format_exc()}\n")
    except Exception:  # noqa: BLE001
        pass


def _debug_log(text: str) -> None:
    try:
        with open(os.path.join(data_dir(), "srtforge.log"), "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except Exception:  # noqa: BLE001
        pass


def data_dir() -> str:
    override = os.environ.get("SRTFORGE_DATA_DIR")
    if override:
        base = override
    elif os.name == "nt":
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "SRTForgeLite")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/SRT Forge Lite")
    else:
        base = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "srtforge-lite")
    os.makedirs(base, mode=0o700, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(base, 0o700)
        except OSError as error:
            raise RuntimeError(f"Negaliu apsaugoti duomenų katalogo {base}: {error}") from error
    return base


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # type: ignore[attr-defined]
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
                return True
            return False
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running_instance_url(directory: Optional[str] = None) -> Optional[str]:
    """Jei jau veikia kita kopija (gyvas procesas ir atsako į /api/ping), grąžina jos adresą."""
    path = os.path.join(directory or data_dir(), "run.json")
    try:
        with open(path, encoding="utf-8") as handle:
            info = json.load(handle)
        if not _pid_alive(int(info["pid"])):
            return None
        req = urllib.request.Request(f"http://127.0.0.1:{int(info['port'])}/api/ping", headers={"X-Token": info["token"]})
        with urllib.request.urlopen(req, timeout=2) as response:
            if response.status == 200:
                return f"http://127.0.0.1:{int(info['port'])}/?t={info['token']}"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def write_run_file(app: "App", url: str, directory: Optional[str] = None) -> str:
    """Tokenas rašomas į failą, kuris nuo pat sukūrimo yra 0600 (be TOCTOU tarpo), o ne chmod po rašymo."""
    path = os.path.join(directory or data_dir(), "run.json")
    port = int(urllib.parse.urlparse(url).port or 0)
    tmp = path + ".tmp"
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "port": port, "token": app.token}, handle)
    os.replace(tmp, path)
    return path


def _update_hooks() -> dict:
    """Atnaujinimų patikra tik jei programoje yra viešasis raktas (supakuotas arba įdiegtas leidimas)."""
    from . import updater

    if not updater.public_key() or os.environ.get("SRTFORGE_NO_UPDATE_CHECK"):
        return {}

    def checker():
        return updater.check_installer()

    def installer(entry):
        directory = os.path.join(os.path.expanduser("~"), "Downloads")
        path = updater.download_installer(entry, directory if os.path.isdir(directory) else tempfile.gettempdir())
        updater.open_file(path)
        return path

    return {"update_checker": checker, "update_installer": installer}


def _force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def main() -> None:
    import argparse

    _force_utf8_output()

    parser = argparse.ArgumentParser(prog="srtforge", description="SRT Forge Lite: vietinė garso transkripcija į subtitrus")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--model-dir", default=os.environ.get("SRTFORGE_MODEL_DIR"))
    parser.add_argument("--idle-exit", type=int, default=0, metavar="SEK",
                        help="išsijungti, kai naršyklės langas uždarytas SEK sekundžių ir nėra darbų (0 = niekada)")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("command", nargs="?", choices=["update"], help="update: patikrinti ir įdiegti naują versiją")
    args = parser.parse_args()
    if args.version:
        print(__version__)
        return
    if args.command == "update":
        from .updater import run_cli

        raise SystemExit(run_cli())

    # Dvigubas paleidimas: atidarome jau veikiančios kopijos langą ir išeiname.
    existing = running_instance_url()
    if existing:
        if not args.no_browser:
            webbrowser.open(existing)
        print(f"SRT Forge Lite jau veikia: {existing}", flush=True)
        return

    if os.environ.get("SRTFORGE_DEBUG"):
        # Derinimui: jei procesas užstringa, po 25 s į žurnalą išmetami visų gijų pėdsakai
        import faulthandler

        faulthandler.dump_traceback_later(25, repeat=False, file=open(os.path.join(data_dir(), "srtforge.log"), "a", buffering=1))
    app = App(model_dir=args.model_dir, **_update_hooks())
    url = app.start(args.port, open_browser=not args.no_browser)
    run_file = write_run_file(app, url)
    print(f"SRT Forge Lite {__version__}\nAtidaryk naršyklėje: {url}\nSustabdyti: Ctrl+C", flush=True)
    try:
        while True:
            time.sleep(1)
            if args.idle_exit and not app.busy() and app.idle_seconds() > args.idle_exit:
                print("Langas uždarytas – išjungiama.", flush=True)
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            os.remove(run_file)
        except OSError:
            pass
        app.stop()
