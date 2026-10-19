"""ffmpeg + faster-whisper (CPU/GPU). Tas pats kodas Windows, macOS ir Linux."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Callable, List, Optional

from .core import Word, build_cues, Rules

MODELS = {
    # modelis: (apytikris dydis MB, rekomenduojama RAM GB)
    "tiny": (75, 1),
    "base": (145, 1),
    "small": (480, 2),
    "medium": (1500, 5),
    "large-v2": (3100, 8),
    "large-v3": (3100, 8),
    "large-v3-turbo": (1600, 6),
}


def ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # sudedamas diegimo metu: statinis ffmpeg visoms OS

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as error:  # noqa: BLE001
        raise RuntimeError("ffmpeg nerastas. Paleisk diegimo skriptą iš naujo.") from error


def recommend_model(ram_gb: Optional[float] = None) -> str:
    if ram_gb is None:
        ram_gb = total_ram_gb()
    if ram_gb >= 16:
        return "large-v3"
    if ram_gb >= 8:
        return "medium"
    if ram_gb >= 4:
        return "small"
    return "base"


def total_ram_gb() -> float:
    try:
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (ValueError, OSError):
        pass
    try:  # Windows
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
        return status.ullTotalPhys / 1024 ** 3
    except Exception:  # noqa: BLE001
        return 4.0


def to_wav(source: str, target: str) -> None:
    command = [ffmpeg_path(), "-y", "-v", "error", "-i", source, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", target]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError("ffmpeg nepavyko: " + (result.stderr.strip().splitlines() or ["nežinoma klaida"])[-1])


_MODEL_CACHE: dict = {}


def load_model(name: str, model_dir: Optional[str] = None, threads: int = 0):
    """Pirmiausia naudoja JAU TURIMĄ modelį (HuggingFace talpykla ar nurodyti katalogai); atsisiunčia tik jei jo nėra."""
    from .models import find_models

    key = (name, model_dir)
    if key not in _MODEL_CACHE:
        from faster_whisper import WhisperModel

        local = find_models([model_dir] if model_dir else None).get(name)
        _MODEL_CACHE.clear()  # laikome tik vieną modelį (RAM)
        if local:
            _MODEL_CACHE[key] = WhisperModel(local["path"], device="auto", compute_type="int8", cpu_threads=threads)
        else:
            _MODEL_CACHE[key] = WhisperModel(name, device="auto", compute_type="int8", cpu_threads=threads,
                                             download_root=model_dir)
    return _MODEL_CACHE[key]


def transcribe(
    source: str,
    *,
    model: str = "small",
    language: Optional[str] = "lt",
    model_dir: Optional[str] = None,
    prompt: Optional[str] = None,
    rules: Optional[Rules] = None,
    progress: Optional[Callable[[str, float], None]] = None,
    cancelled: Callable[[], bool] = lambda: False,
):
    """Failas -> (subtitrai, kalba, trukmė_s). `progress(etapas, 0..1)`."""
    say = progress or (lambda stage, value: None)
    with tempfile.TemporaryDirectory(prefix="srtforge-") as work:
        wav = os.path.join(work, "audio.wav")
        say("Ruošiamas garsas", 0.02)
        to_wav(source, wav)
        if cancelled():
            raise InterruptedError("nutraukta")

        say("Kraunamas modelis (pirmą kartą atsisiunčiamas)", 0.05)
        whisper = load_model(model, model_dir)

        segments, info = whisper.transcribe(
            wav,
            language=language or None,
            vad_filter=True,                       # tyla niekada nepatenka į Whisper -> mažiau haliucinacijų
            word_timestamps=True,
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
            beam_size=5,
        )
        duration = max(info.duration, 0.001)
        words: List[Word] = []
        for segment in segments:
            if cancelled():
                raise InterruptedError("nutraukta")
            for w in segment.words or []:
                words.append(Word(w.word.strip(), float(w.start), float(w.end)))
            say("Atpažįstama kalba", 0.1 + 0.85 * min(1.0, segment.end / duration))

        say("Formuojami subtitrai", 0.97)
        words = [w for w in words if w.text]
        return build_cues(words, rules), info.language, info.duration
