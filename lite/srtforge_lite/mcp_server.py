"""MCP serveris (stdio): Claude Desktop, Claude Code ir kiti MCP klientai valdo SRT Forge be API raktų.

Paleidimas: `srtforge mcp`. Protokolas: JSON-RPC 2.0, po vieną JSON eilutę (MCP stdio)."""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Callable, Optional

from . import __version__, files as fl
from . import translate as tr
from .engine import MODELS, recommend_model, total_ram_gb
from .models import system_report
from .server import App, Job, parse_options

PROTOCOL = "2025-06-18"
WAIT_LIMIT = 6 * 3600

TOOLS = [
    {"name": "transcribe",
     "description": "Atpažįsta kalbą garso ar video faile ir sukuria subtitrus. Rezultatai įrašomi šalia originalo (arba į out_dir), esami failai "
                    "neperrašomi. Viskas vyksta šiame kompiuteryje. Gali užtrukti, ilgi failai trunka ilgiau nei jų trukmė lėtuose kompiuteriuose.",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string", "description": "Pilnas kelias į garso ar video failą"},
         "language": {"type": "string", "description": "Kalbos kodas (lt, en, lv, et, pl, ru, de...) arba auto", "default": "lt"},
         "model": {"type": "string", "description": "tiny, base, small, medium, large-v3-turbo... (tuščia = rekomenduojamas šiam kompiuteriui)"},
         "outputs": {"type": "array", "items": {"type": "string", "enum": list(fl.OUTPUTS)}, "default": ["srt"],
                     "description": "srt, vtt, txt, json, soft (video su subtitrų takeliu), burn (subtitrai įdegti į vaizdą)"},
         "targets": {"type": "array", "items": {"type": "string"}, "description": "Vertimo kalbos, pvz. [\"en\",\"de\"] (iki 8, orientacinis vertimas)"},
         "speakers": {"type": "string", "description": "off, auto arba kalbėtojų skaičius 2-12", "default": "off"},
         "style": {"type": "string", "enum": list(fl.STYLES), "default": "standard", "description": "standard, tv (37 simb.), social (trumpi)"},
         "out_dir": {"type": "string", "description": "Pilnas išvesties aplanko kelias (nebūtina)"},
         "prompt": {"type": "string", "description": "Vardai ir terminai, padedantys atpažinti"}}}},
    {"name": "system_check",
     "description": "Parodo kompiuterio galimybes ir kurie Whisper modeliai jau įdiegti. Naudok prieš didelį darbą, kad pasirinktum modelį.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "languages",
     "description": "Vertimo kalbos, kurias palaiko transcribe parametras targets.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _text(payload) -> dict:
    return {"content": [{"type": "text", "text": payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)}]}


class McpServer:
    def __init__(self, app: App, out: Callable[[dict], None]):
        self.app = app
        self.out = out

    # ——— įrankiai ———
    def call_tool(self, name: str, args: dict, progress: Optional[Callable[[float, str], None]] = None) -> dict:
        if name == "system_check":
            return _text(system_report(MODELS, total_ram_gb(), recommend_model(), [self.app.model_dir] if self.app.model_dir else None))
        if name == "languages":
            return _text(tr.LANGUAGES)
        if name != "transcribe":
            raise KeyError(name)
        path = os.path.realpath(str(args.get("path", "")))
        if not os.path.isabs(str(args.get("path", ""))) or not os.path.isfile(path):
            return {**_text(f"Failas nerastas arba kelias nėra pilnas: {args.get('path')!r}"), "isError": True}
        query = {"language": str(args.get("language") or "lt"), "speakers": str(args.get("speakers") or "off"),
                 "style": str(args.get("style") or "standard"),
                 "outputs": ",".join(args.get("outputs") or ["srt"]), "targets": ",".join(args.get("targets") or [])}
        for key in ("model", "out_dir", "prompt"):
            if args.get(key):
                query[key] = str(args[key])
        options, error = parse_options(query)
        if error:
            return {**_text(error), "isError": True}
        job = Job(os.path.basename(path), path, options, owned=False)
        self.app.jobs[job.id] = job
        self.app.queue.put(job)
        began = time.time()
        while job.state in ("waiting", "running"):
            if time.time() - began > WAIT_LIMIT:
                job.cancel.set()
                return {**_text("Per ilgai trunka, darbas nutrauktas."), "isError": True}
            if progress:
                progress(job.progress, job.stage)
            time.sleep(1)
        self.app.jobs.pop(job.id, None)
        if job.state != "done":
            return {**_text(f"Nepavyko ({job.state}): {job.error or job.stage}"), "isError": True}
        preview = " ".join(c.text for c in job.cues)[:600]
        return _text({"saved": job.saved, "language": job.language, "segments": len(job.cues), "speakers": [s["name"] for s in job.speaker_list()],
                      "translations": sorted(job.translations), "warnings": job.warnings, "preview": preview,
                      "seconds": round(time.time() - began, 1)})

    # ——— protokolas ———
    def handle(self, message: dict) -> Optional[dict]:
        method, mid, params = message.get("method"), message.get("id"), message.get("params") or {}
        if mid is None:                                   # pranešimai (pvz. notifications/initialized) atsakymo nereikalauja
            return None
        try:
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "srt-forge", "version": __version__}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                token = (params.get("_meta") or {}).get("progressToken")

                def progress(value: float, stage: str) -> None:
                    if token is not None:
                        self.out({"jsonrpc": "2.0", "method": "notifications/progress",
                                  "params": {"progressToken": token, "progress": round(value * 100), "total": 100, "message": stage}})

                result = self.call_tool(params.get("name", ""), params.get("arguments") or {}, progress)
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"nežinomas metodas {method}"}}
        except KeyError as error:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"nežinomas įrankis {error}"}}
        except Exception as error:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "result": {**_text(f"Klaida: {error}"), "isError": True}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(model_dir: Optional[str] = None) -> None:
    real_out = sys.stdout
    sys.stdout = sys.stderr                                # bet kokie print() nesugadintų protokolo
    import threading

    lock = threading.Lock()

    def out(message: dict) -> None:
        with lock:
            real_out.write(json.dumps(message, ensure_ascii=False) + "\n")
            real_out.flush()

    server = McpServer(App(model_dir=model_dir), out)

    def run(message: dict) -> None:
        reply = server.handle(message)
        if reply:
            out(reply)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            out({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "neteisingas JSON"}})
            continue
        threading.Thread(target=run, args=(message,), daemon=True).start()    # ilgas transcribe neblokuoja ping/list
