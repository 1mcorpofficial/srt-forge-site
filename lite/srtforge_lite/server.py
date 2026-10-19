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
from .core import drop_hallucinated, find_hallucinations, speaker_label, to_json, to_srt, to_ts, to_txt, to_vtt
from .engine import MODELS, embed_subtitles, has_video, recommend_model, total_ram_gb, transcribe
from .models import system_report
from . import translate as tr
from . import files as fl
from . import live as lv

UI_PATH = os.path.join(os.path.dirname(__file__), "ui.html")
OVERLAY_PATH = os.path.join(os.path.dirname(__file__), "overlay.html")
# fmt -> (Content-Type, plėtinys, atvaizdavimas pagal darbą)
FORMATS = {
    "srt": ("application/x-subrip", "srt", lambda job, cues: to_srt(cues, job.speaker_names)),
    "vtt": ("text/vtt", "vtt", lambda job, cues: to_vtt(cues, job.speaker_names)),
    "txt": ("text/plain", "txt", lambda job, cues: to_txt(cues, names=job.speaker_names)),
    "ts": ("text/plain", "laikai.txt", lambda job, cues: to_ts(cues, job.speaker_names)),
    "json": ("application/json", "json", lambda job, cues: to_json(cues, job.language, job.options.get("task", "transcribe"),
                                                                     job.speaker_names)),
}
TASKS = ("transcribe", "translate")
MAX_NAME = 200


def parse_options(query: dict):
    """Bendri darbo nustatymai (įkėlimui ir vietiniam failui). Grąžina (options, klaida)."""
    model = query.get("model") or recommend_model()
    if model not in MODELS:
        return None, "nežinomas modelis"
    language = query.get("language") or None
    if language == "auto":
        language = None
    task = query.get("task") or "transcribe"
    if task not in TASKS:
        return None, "nežinoma užduotis"
    targets = [t for t in (query.get("targets") or "").split(",") if t]
    if any(t not in tr.LANGUAGES for t in targets) or len(targets) > 8:
        return None, "nežinoma vertimo kalba"
    speakers = query.get("speakers") or "off"
    if speakers not in ("off", "auto") and not (speakers.isdigit() and 1 <= int(speakers) <= 12):
        return None, "neteisingas kalbėtojų skaičius"
    style = query.get("style") or "standard"
    if style not in fl.STYLES:
        return None, "nežinomas subtitrų stilius"
    rules = fl.STYLES[style]
    outputs = [o for o in (query.get("outputs") or "").split(",") if o]
    if any(o not in fl.OUTPUTS for o in outputs):
        return None, "nežinomas išvesties formatas"
    out_dir = query.get("out_dir") or None
    if out_dir and not os.path.isabs(out_dir):
        return None, "išvesties aplankas turi būti pilnas kelias"
    return {"model": model, "language": language, "prompt": query.get("prompt"), "task": task, "speakers": speakers,
            "targets": targets, "rules": rules, "style": style, "outputs": outputs, "out_dir": out_dir}, None


class Job:
    def __init__(self, name: str, path: str, options: dict, owned: bool = True):
        self.id = uuid.uuid4().hex[:12]
        self.owned = owned                     # False = vartotojo originalas, jo niekada netriname
        self.saved: list = []                  # automatiškai įrašyti failai (pilni keliai)
        self.name = name
        self.path = path
        self.options = options
        self.state = "waiting"          # waiting | running | done | failed | cancelled
        self.stage = "Eilėje"
        self.progress = 0.0
        self.error: Optional[str] = None
        self.cues = []
        self.warnings = []
        self.extra_warnings: list = []
        self.language: Optional[str] = None
        self.cancel = threading.Event()
        self.has_video = False
        self.speaker_names: Dict[int, str] = {}
        self.translations: Dict[str, list] = {}
        self.embed: dict = {"state": "none", "mode": None, "error": None, "path": None, "name": None}

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "state": self.state, "stage": self.stage,
                "progress": round(self.progress, 3), "error": self.error, "warnings": self.warnings,
                "language": self.language, "cues": len(self.cues), "task": self.options.get("task", "transcribe"),
                "has_video": self.has_video, "speakers": self.speaker_list(),
                "translations": sorted(self.translations),
                "saved": [os.path.basename(p) for p in self.saved], "folder": os.path.dirname(self.saved[0]) if self.saved else None,
                "embed": {k: self.embed[k] for k in ("state", "mode", "error", "name")}}

    def speaker_list(self) -> list:
        ids = sorted({c.speaker for c in self.cues if c.speaker is not None})
        return [{"id": i, "name": speaker_label(i, self.speaker_names)} for i in ids]

    def remove_files(self) -> None:
        for path in (self.path if self.owned else None, self.embed.get("path")):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass


class App:
    def __init__(self, model_dir: Optional[str] = None, work_dir: Optional[str] = None, runner=transcribe,
                 update_checker=None, update_installer=None, update_delay: float = 15, translator=None,
                 translation_dir: Optional[str] = None, picker=fl.pick_files, revealer=fl.reveal, live_factory=None):
        self.live_factory = live_factory or self._make_live
        self.live: Optional[lv.LiveSession] = None
        self.live_id = ""
        self.window_mode = False
        self.picker = picker
        self.revealer = revealer
        self.picked: set = set()               # tik pasirinkti per sistemos langą keliai gali būti apdorojami vietoje
        self.token = secrets.token_urlsafe(24)
        self.model_dir = model_dir
        self.work_dir = work_dir or tempfile.mkdtemp(prefix="srtforge-uploads-")
        self.jobs: Dict[str, Job] = {}
        self.queue: "queue.Queue[Job]" = queue.Queue()
        self.runner = runner
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.last_seen = time.time()
        self.translator = translator
        self.translation_dir = translation_dir
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

    def _make_live(self, options: dict) -> "lv.LiveSession":
        translator = self._get_translator() if options["targets"] else None
        language = options["language"] or "lt"
        fn = lv.whisper_fn(options["model"], options["language"], self.model_dir, options.get("prompt"))
        return lv.LiveSession(fn, options["targets"], translator, language)

    def busy(self) -> bool:
        if self.live and self.live.running:
            return True
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
                    speakers=job.options.get("speakers"), warn=lambda text, job=job: job.extra_warnings.append(text),
                    progress=progress, cancelled=job.cancel.is_set, rules=job.options.get("rules"))
                flags = find_hallucinations(cues)
                job.warnings = [f"Tikėtina Whisper haliucinacija: „{p}“ ×{n} (subtitrai {a + 1}–{b + 1})" for a, b, p, n in flags] + job.extra_warnings
                job.cues = drop_hallucinated(cues, flags)
                job.language = language
                try:
                    job.has_video = has_video(job.path)       # video failą paliekame subtitrų įdėjimui
                except Exception:  # noqa: BLE001
                    job.has_video = False
                targets = job.options.get("targets") or []
                if targets:
                    self._translate_job(job, targets, progress)
                if job.options.get("outputs"):
                    job.stage = "Įrašomi failai"
                    self._save_outputs(job)
                job.state, job.stage, job.progress = "done", "Baigta", 1.0
            except InterruptedError:
                job.state, job.stage = "cancelled", "Nutraukta"
            except Exception as error:  # noqa: BLE001
                job.state, job.stage, job.error = "failed", "Klaida", str(error)
                _log_exception(job.name)
            finally:
                if not job.has_video and job.owned:
                    try:
                        os.remove(job.path)
                    except OSError:
                        pass

    def _get_translator(self, progress=None):
        if self.translator is None:
            tr.ensure_model(self.translation_dir, progress)
            self.translator = tr.Translator(self.translation_dir)
        return self.translator

    def _translate_job(self, job: Job, targets: list, progress) -> None:
        source = job.language if job.options.get("task") != "translate" else "en"
        if source not in tr.LANGUAGES:
            job.extra_warnings.append(f"Šaltinio kalba „{source}“ vertimui nepalaikoma, vertimai praleisti.")
            return
        try:
            translator = self._get_translator(lambda stage, value: progress(stage, 0.97 + 0.01 * value))
            for index, target in enumerate(targets):
                if job.cancel.is_set():
                    raise InterruptedError("nutraukta")
                progress(f"Verčiama į {tr.LANGUAGES[target]} ({index + 1}/{len(targets)})", 0.98 + 0.02 * index / len(targets))
                job.translations[target] = tr.translate_cues(job.cues, translator, source, target)
        except InterruptedError:
            raise
        except Exception as error:  # noqa: BLE001
            job.extra_warnings.append(f"Vertimas nepavyko: {error}")
            _log_exception(f"vertimas: {error}")
            job.warnings = job.warnings + [f"Vertimas nepavyko: {error}"]

    def _save_outputs(self, job: Job) -> None:
        """Įrašo pasirinktus failus šalia originalo (arba į pasirinktą aplanką); niekada neperrašo esamų."""
        directory = job.options.get("out_dir") or (os.path.dirname(job.path) if not job.owned else fl.default_out_dir())
        os.makedirs(directory, exist_ok=True)
        stem = os.path.splitext(job.name)[0] or "irasas"
        variants = [("", job.cues)] + [(f".{lang}", job.translations[lang]) for lang in sorted(job.translations)]
        for fmt in job.options["outputs"]:
            if fmt in ("soft", "burn"):
                if not job.has_video:
                    job.warnings.append("Subtitrai į video neįdėti: tai ne video failas")
                    continue
                for tag, cues in variants:
                    target = fl.unique_path(os.path.join(directory, f"{stem}{'.subtitrai' if fmt == 'soft' else '.su-subtitrais'}{tag}"))
                    out = embed_subtitles(job.path, to_srt(cues, job.speaker_names), os.path.join(self.work_dir, uuid.uuid4().hex), fmt)
                    final = fl.unique_path(target + os.path.splitext(out)[1])
                    shutil.move(out, final)
                    job.saved.append(final)
                continue
            ctype, ext, render = FORMATS[fmt]
            for tag, cues in variants:
                final = fl.unique_path(os.path.join(directory, f"{stem}{tag}.{ext}"))
                with open(final, "w", encoding="utf-8", newline="") as handle:
                    handle.write(render(job, cues))
                job.saved.append(final)

    def embed_worker(self, job: Job, mode: str, lang: str = "") -> None:
        job.embed = {"state": "running", "mode": mode, "error": None, "path": None, "name": None}
        try:
            base = os.path.join(self.work_dir, uuid.uuid4().hex)
            cues = job.translations.get(lang) if lang else job.cues
            out = embed_subtitles(job.path, to_srt(cues, job.speaker_names), base, mode)
            stem = os.path.splitext(job.name)[0] or "video"
            suffix = (".subtitrai" if mode == "soft" else ".su-subtitrais") + (f".{lang}" if lang else "")
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
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                if not self._authorized():
                    return self._json(403, {"error": "forbidden"})
                if path == "/":
                    with open(UI_PATH, encoding="utf-8") as handle:
                        html = handle.read().replace("__TOKEN__", app.token).replace("__VERSION__", __version__)
                    return self._send(200, html.encode(), "text/html; charset=utf-8")
                if path == "/live-worklet.js":
                    with open(os.path.join(os.path.dirname(__file__), "worklet.js"), "rb") as handle:
                        return self._send(200, handle.read(), "text/javascript; charset=utf-8")
                if path == "/overlay":
                    with open(OVERLAY_PATH, encoding="utf-8") as handle:
                        return self._send(200, handle.read().replace("__TOKEN__", app.token).encode(), "text/html; charset=utf-8")
                if path.startswith("/api/live/"):
                    parts = path.strip("/").split("/")
                    if len(parts) < 3 or parts[2] != app.live_id or not app.live:
                        return self._json(404, {"error": "nerasta"})
                    query = urllib.parse.parse_qs(parsed.query)
                    if len(parts) == 3:
                        after = int((query.get("after") or ["0"])[0] or 0)
                        return self._json(200, app.live.state(after))
                    if parts[3] in ("srt", "vtt", "txt"):
                        lang = (query.get("lang") or [""])[0]
                        cues = app.live.cues(lang)
                        body = {"srt": to_srt, "vtt": to_vtt, "txt": lambda c: to_txt(c)}[parts[3]](cues)
                        return self._send(200, body.encode(), "text/plain; charset=utf-8",
                                          {"Content-Disposition": f"attachment; filename=\"gyvi-subtitrai{('.' + lang) if lang else ''}.{parts[3]}\""})
                if path == "/api/ping":
                    return self._json(200, {"ok": True, "version": __version__})
                if path == "/api/update":
                    entry = app.update_entry
                    return self._json(200, {"available": bool(entry), "version": entry and entry["version"],
                                            "title": entry and entry.get("title"), "size": entry and entry.get("size")})
                if path == "/api/info":
                    return self._json(200, {"version": __version__, "models": MODELS, "recommended": recommend_model(),
                                            "ram_gb": round(total_ram_gb(), 1), "out_dir": fl.default_out_dir(), "window": app.window_mode})
                if path == "/api/languages":
                    return self._json(200, {"targets": tr.LANGUAGES, "model_mb": tr.DOWNLOAD_MB,
                                            "model_ready": app.translator is not None or tr.model_ready(app.translation_dir)})
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
                                                "speakers": job.speaker_list(),
                                                "cues": [{"start": c.start, "end": c.end, "text": c.text.replace("\n", " "),
                                                          "speaker": c.speaker, "speaker_name": speaker_label(c.speaker, job.speaker_names)}
                                                         for c in job.cues]})
                    if fmt == "video" and job.embed.get("state") == "done" and job.embed.get("path"):
                        return self._stream_file(job.embed["path"], job.embed["name"])
                    if fmt in FORMATS and job.state == "done":
                        ctype, ext, render = FORMATS[fmt]
                        stem = os.path.splitext(job.name)[0] or "irasas"
                        quoted = urllib.parse.quote(f"{stem}.{ext}")
                        lang = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("lang") or [""])[0]
                        cues = job.translations.get(lang) if lang else job.cues
                        if cues is None:
                            return self._json(404, {"error": "tokio vertimo nėra"})
                        if lang:
                            stem, ext = stem + "." + lang, ext
                            quoted = urllib.parse.quote(f"{stem}.{ext}")
                        return self._send(200, render(job, cues).encode("utf-8"), ctype + "; charset=utf-8",
                                          {"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"})
                return self._json(404, {"error": "nerasta"})

            def do_PUT(self):  # failo įkėlimas: PUT /api/upload?name=...&model=...&language=...
                parsed = urllib.parse.urlparse(self.path)
                if not self._authorized():
                    return self._json(403, {"error": "forbidden"})
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "live"] and parts[3] == "audio":
                    if parts[2] != app.live_id or not app.live or not app.live.running:
                        return self._json(404, {"error": "sesija nevyksta"})
                    length = min(int(self.headers.get("Content-Length") or 0), 4 << 20)
                    app.live.feed(self.rfile.read(length))
                    return self._json(200, {"ok": True})
                if parsed.path != "/api/upload":
                    return self._json(404, {"error": "nerasta"})
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                name = os.path.basename(query.get("name", "irasas"))[:MAX_NAME] or "irasas"
                options, error = parse_options(query)
                if error:
                    return self._json(400, {"error": error})
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

                job = Job(name, path, options)
                app.jobs[job.id] = job
                app.queue.put(job)
                return self._json(200, job.public())

            def do_POST(self):
                if not self._authorized():
                    return self._json(403, {"error": "forbidden"})
                parsed = urllib.parse.urlparse(self.path)
                parts = parsed.path.strip("/").split("/")
                if parsed.path == "/api/live":
                    query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                    options, error = parse_options(query)
                    if error:
                        return self._json(400, {"error": error})
                    if app.live:
                        app.live.stop()
                    try:
                        app.live = app.live_factory(options)
                    except Exception as error:  # noqa: BLE001
                        _log_exception("live")
                        return self._json(502, {"error": f"nepavyko paleisti: {error}"})
                    app.live_id = uuid.uuid4().hex[:10]
                    return self._json(200, {"id": app.live_id, "targets": options["targets"]})
                if len(parts) == 4 and parts[:2] == ["api", "live"] and parts[3] == "stop":
                    if parts[2] != app.live_id or not app.live:
                        return self._json(404, {"error": "nerasta"})
                    app.live.stop()
                    return self._json(200, app.live.state())
                if parsed.path == "/api/open-browser":
                    webbrowser.open(f"http://127.0.0.1:{app.httpd.server_address[1]}/?t={app.token}")
                    return self._json(200, {"ok": True})
                if parsed.path == "/api/pick":
                    paths = app.picker()
                    if paths is None:
                        return self._json(501, {"error": "šioje sistemoje failų langas nepasiekiamas"})
                    app.picked.update(os.path.realpath(p) for p in paths)
                    return self._json(200, {"paths": [{"path": os.path.realpath(p), "name": os.path.basename(p)} for p in paths]})
                if parsed.path == "/api/jobs/local":
                    try:
                        payload = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 100_000)) or b"{}")
                        paths = [os.path.realpath(str(p)) for p in payload.get("paths", [])][:50]
                        query = {k: ",".join(v) if isinstance(v, list) else str(v) for k, v in (payload.get("options") or {}).items()}
                    except (ValueError, TypeError, AttributeError):
                        return self._json(400, {"error": "neteisinga užklausa"})
                    options, error = parse_options(query)
                    if error:
                        return self._json(400, {"error": error})
                    if not paths or any(p not in app.picked or not os.path.isfile(p) for p in paths):
                        return self._json(403, {"error": "failas nepasirinktas per langą"})
                    made = []
                    for p in paths:
                        job = Job(os.path.basename(p), p, dict(options), owned=False)
                        app.jobs[job.id] = job
                        app.queue.put(job)
                        made.append(job.public())
                    return self._json(200, {"jobs": made})
                if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "reveal":
                    job = app.jobs.get(parts[2])
                    if not job or not job.saved:
                        return self._json(404, {"error": "nėra įrašytų failų"})
                    return self._json(200, {"ok": app.revealer(job.saved[0])})
                if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "embed":
                    job = app.jobs.get(parts[2])
                    mode = (urllib.parse.parse_qs(parsed.query).get("mode") or ["soft"])[0]
                    lang = (urllib.parse.parse_qs(parsed.query).get("lang") or [""])[0]
                    if not job or job.state != "done":
                        return self._json(404, {"error": "nerasta"})
                    if not job.has_video or mode not in ("soft", "burn"):
                        return self._json(400, {"error": "šis failas neturi vaizdo arba režimas neteisingas"})
                    if job.embed.get("state") == "running":
                        return self._json(409, {"error": "jau vyksta"})
                    if lang and lang not in job.translations:
                        return self._json(400, {"error": "tokio vertimo nėra"})
                    threading.Thread(target=app.embed_worker, args=(job, mode, lang), daemon=True).start()
                    time.sleep(0.05)
                    return self._json(200, job.public())
                if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "speakers":
                    job = app.jobs.get(parts[2])
                    if not job or job.state != "done":
                        return self._json(404, {"error": "nerasta"})
                    try:
                        length = min(int(self.headers.get("Content-Length") or 0), 10_000)
                        payload = json.loads(self.rfile.read(length) or b"{}")
                        names = {int(k): str(v).strip()[:60] for k, v in (payload.get("names") or {}).items() if str(v).strip()}
                    except (ValueError, TypeError, AttributeError):
                        return self._json(400, {"error": "neteisingi vardai"})
                    job.speaker_names = names
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
    parser.add_argument("--browser", action="store_true", help="atidaryti numatytojoje naršyklėje, o ne programos lange")
    parser.add_argument("--check-window", action="store_true", help="parodo, koks programos langas bus naudojamas")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("command", nargs="?", choices=["update", "mcp", "mcp-config"],
                        help="update: patikrinti ir įdiegti naują versiją; mcp: MCP serveris Claude/kitų agentų valdymui (stdio)")
    args = parser.parse_args()
    if args.version:
        print(__version__)
        return
    if args.check_window:
        from .window import describe

        print(describe())
        return
    if args.command == "mcp-config":
        command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "srtforge_lite"]
        print(json.dumps({"mcpServers": {"srt-forge": {"command": command[0], "args": command[1:] + ["mcp"]}}}, indent=2))
        print("\nĮdėk šį bloką į Claude Desktop konfigūraciją (Settings > Developer > Edit Config) arba paleisk Claude Code:\n"
              f"  claude mcp add srt-forge -- {' '.join(command)} mcp", file=sys.stderr)
        return
    if args.command == "mcp":
        from .mcp_server import serve

        return serve(args.model_dir)
    if args.command == "update":
        from .updater import run_cli

        raise SystemExit(run_cli())

    # Dvigubas paleidimas: atidarome jau veikiančios kopijos langą ir išeiname.
    existing = running_instance_url()
    if existing:
        if not args.no_browser:
            from .window import open_window

            if args.browser or not open_window(existing, os.path.join(data_dir(), "window-profile")):
                webbrowser.open(existing)
        print(f"SRT Forge Lite jau veikia: {existing}", flush=True)
        return

    if os.environ.get("SRTFORGE_DEBUG"):
        # Derinimui: jei procesas užstringa, po 25 s į žurnalą išmetami visų gijų pėdsakai
        import faulthandler

        faulthandler.dump_traceback_later(25, repeat=False, file=open(os.path.join(data_dir(), "srtforge.log"), "a", buffering=1))
    app = App(model_dir=args.model_dir, **_update_hooks())
    use_window = not args.no_browser and not args.browser
    url = app.start(args.port, open_browser=not args.no_browser and not use_window)
    app.window_mode = use_window
    run_file = write_run_file(app, url)
    print(f"SRT Forge Lite {__version__}\nAtidaryk naršyklėje: {url}\nSustabdyti: Ctrl+C", flush=True)
    try:
        if use_window:
            from .window import open_window

            if open_window(url, os.path.join(data_dir(), "window-profile")):
                print("Langas uždarytas – išjungiama.", flush=True)
                return
            app.window_mode = False
            webbrowser.open(url)                      # tinkamo lango nėra: naudojame naršyklę
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
