"""Sistemos patikra: jau turimi Whisper modeliai, RAM, diskas, GPU. Nieko neatsisiunčia."""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from typing import Dict, List, Optional

# HF repo -> mūsų modelio vardas (faster-whisper / CTranslate2 formatas)
REPOS = {
    "Systran/faster-whisper-tiny": "tiny",
    "Systran/faster-whisper-base": "base",
    "Systran/faster-whisper-small": "small",
    "Systran/faster-whisper-medium": "medium",
    "Systran/faster-whisper-large-v2": "large-v2",
    "Systran/faster-whisper-large-v3": "large-v3",
    "mobiuslabsgmbh/faster-whisper-large-v3-turbo": "large-v3-turbo",
    "deepdml/faster-whisper-large-v3-turbo-ct2": "large-v3-turbo",
}
REQUIRED = ("config.json", "model.bin", "tokenizer.json")
VOCAB = ("vocabulary.json", "vocabulary.txt")


def _complete(path: str) -> bool:
    return (all(os.path.isfile(os.path.join(path, name)) for name in REQUIRED)
            and any(os.path.isfile(os.path.join(path, name)) for name in VOCAB)
            and os.path.getsize(os.path.join(path, "model.bin")) > 1_000_000)


def _size(path: str) -> int:
    return os.path.getsize(os.path.realpath(os.path.join(path, "model.bin")))


def hub_dirs(extra: Optional[List[str]] = None) -> List[str]:
    """HuggingFace talpyklos (ir SRTFORGE_MODEL_DIR / papildomi katalogai). Tik žinomos vietos, ne visas diskas."""
    home = os.path.expanduser("~")
    candidates = [
        os.environ.get("SRTFORGE_MODEL_DIR"),
        os.environ.get("HUGGINGFACE_HUB_CACHE"),
        os.path.join(os.environ["HF_HOME"], "hub") if os.environ.get("HF_HOME") else None,
        os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache"), "huggingface", "hub"),
        os.path.join(home, ".cache", "huggingface", "hub"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "huggingface", "hub") if os.name == "nt" else None,
    ]
    candidates += (extra or []) + [p for p in os.environ.get("SRTFORGE_EXTRA_MODEL_DIRS", "").split(os.pathsep) if p]
    seen, result = set(), []
    for item in candidates:
        if item and os.path.isdir(item) and os.path.realpath(item) not in seen:
            seen.add(os.path.realpath(item))
            result.append(item)
    return result


def find_models(extra: Optional[List[str]] = None) -> Dict[str, dict]:
    """Grąžina {modelio_vardas: {path, source, size}} tik pilniems, naudojamiems faster-whisper modeliams."""
    found: Dict[str, dict] = {}

    def add(name: str, path: str, source: str) -> None:
        if not _complete(path):
            return
        size = _size(path)
        if name not in found or size > found[name]["size"]:
            found[name] = {"path": path, "source": source, "size": size}

    for hub in hub_dirs(extra):
        for repo, name in REPOS.items():
            snapshots = os.path.join(hub, "models--" + repo.replace("/", "--"), "snapshots")
            for snapshot in sorted(glob.glob(os.path.join(snapshots, "*"))):
                add(name, snapshot, f"HuggingFace talpykla ({hub})")
        # Ranka sudėti katalogai: <hub>/<vardas>/model.bin
        try:
            for entry in os.scandir(hub):
                if entry.is_dir() and entry.name in REPOS.values():
                    add(entry.name, entry.path, f"katalogas {hub}")
        except OSError:
            pass
    return found


def find_other_engines() -> List[dict]:
    """Kitų programų Whisper modeliai (whisper.cpp ggml). Rodome vartotojui, bet jų Lite NENAUDOJA (kitas formatas)."""
    home = os.path.expanduser("~")
    patterns = [
        os.path.join(home, "Library", "Application Support", "SRT Forge", "Models", "ggml-*.bin"),
        os.path.join(home, "whisper.cpp", "models", "ggml-*.bin"),
        os.path.join(home, ".cache", "whisper*", "ggml-*.bin"),
        "/opt/homebrew/share/whisper-cpp/ggml-*.bin",
        "/usr/local/share/whisper-cpp/ggml-*.bin",
    ]
    result = []
    for pattern in patterns:
        for path in glob.glob(pattern):
            if os.path.isfile(path) and os.path.getsize(path) > 1_000_000:
                result.append({"path": path, "size": os.path.getsize(path), "format": "whisper.cpp (ggml)"})
    return result


def gpu_summary() -> Optional[str]:
    if sys.platform == "darwin":
        return "Apple Silicon" if os.uname().machine == "arm64" else None
    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            out = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True,
                                 text=True, timeout=5)
            line = (out.stdout.strip().splitlines() or [""])[0].strip()
            return line or None
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def disk_free_gb(path: str) -> float:
    probe = path
    while probe and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    return shutil.disk_usage(probe or os.path.expanduser("~")).free / 1024 ** 3


def system_report(models_info: Dict[str, tuple], ram_gb: float, recommended: str, extra: Optional[List[str]] = None) -> dict:
    local = find_models(extra)
    hub = (hub_dirs(extra) or [os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")])[0]
    choices = []
    for name, (mb, need_ram) in models_info.items():
        entry = local.get(name)
        choices.append({"name": name, "download_mb": mb, "min_ram_gb": need_ram, "installed": bool(entry),
                        "source": entry["source"] if entry else None,
                        "fits_ram": ram_gb >= need_ram})
    usable = [c for c in choices if c["installed"] and c["fits_ram"]]
    # Rekomenduojame: jau turimą, tinkantį pagal RAM (didžiausią); kitaip – pagal RAM
    order = list(models_info)
    best_local = max(usable, key=lambda c: order.index(c["name"]))["name"] if usable else None
    return {
        "ram_gb": round(ram_gb, 1), "cpu_threads": os.cpu_count() or 1, "gpu": gpu_summary(),
        "disk_free_gb": round(disk_free_gb(hub), 1), "model_cache": hub,
        "models": choices, "recommended": best_local or recommended,
        "recommended_installed": best_local is not None,
        "other_engines": find_other_engines(),
    }
