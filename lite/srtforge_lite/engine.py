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
    task: str = "transcribe",
    speakers: Optional[str] = None,
    warn: Optional[Callable[[str], None]] = None,
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
            task="translate" if task == "translate" else "transcribe",   # translate: į anglų kalbą
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

        words = [w for w in words if w.text]
        if speakers and speakers != "off" and words:
            try:
                from .core import assign_speakers
                from .diarize import diarize

                count = None if speakers == "auto" else int(speakers)
                turns = diarize(wav, count, progress=lambda stage, value: say(stage, 0.95 + 0.02 * value))
                assign_speakers(words, turns)
            except Exception as error:  # noqa: BLE001 - kalbėtojų skyrimas nėra kritinis
                for word in words:
                    word.speaker = None
                if warn:
                    warn(f"Kalbėtojų atskirti nepavyko: {error}")
                _log_failure("diarizacija", error)

        say("Formuojami subtitrai", 0.97)
        return build_cues(words, rules), info.language, info.duration


def _log_failure(what: str, error: Exception) -> None:
    try:
        from .server import _log_exception

        _log_exception(f"{what}: {error}")
    except Exception:  # noqa: BLE001
        pass


def has_video(path: str) -> bool:
    """Ar faile yra tikras vaizdo srautas (ne albumo viršelis). Naudoja `ffmpeg -i` (ffprobe nebūtinas)."""
    result = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", path], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return any("Video:" in line and "attached pic" not in line for line in result.stderr.splitlines())


def embed_subtitles(source: str, srt_text: str, target_base: str, mode: str) -> str:
    """Subtitrai į video. mode='soft' – atskiras subtitrų takelis (mp4, o jei nepavyksta – mkv); 'burn' – įdegami į vaizdą.
    Grąžina sukurto failo kelią."""
    ffmpeg = ffmpeg_path()
    with tempfile.TemporaryDirectory(prefix="srtforge-embed-") as tmp:
        srt = os.path.join(tmp, "subs.srt")
        with open(srt, "w", encoding="utf-8") as handle:
            handle.write(srt_text)

        def run(cmd):
            result = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.returncode != 0:
                raise RuntimeError("ffmpeg nepavyko: " + (result.stderr.strip().splitlines() or ["nežinoma klaida"])[-1])

        if mode == "burn":
            out = target_base + ".mp4"
            style = ("FontName=Arial,FontSize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&HAA000000,BorderStyle=1,"
                     "Outline=2,Shadow=0,Alignment=2,MarginV=40")
            run([ffmpeg, "-y", "-v", "error", "-i", source, "-vf", f"subtitles=subs.srt:force_style='{style}'",
                 "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "copy", out])
            return out

        out = target_base + ".mp4"
        try:
            run([ffmpeg, "-y", "-v", "error", "-i", source, "-i", srt, "-map", "0:v:0", "-map", "0:a?", "-map", "1:0",
                 "-c", "copy", "-c:s", "mov_text", "-metadata:s:s:0", "language=lit", out])
            return out
        except RuntimeError:
            if os.path.exists(out):
                os.remove(out)
            out = target_base + ".mkv"
            run([ffmpeg, "-y", "-v", "error", "-i", source, "-i", srt, "-map", "0:v:0", "-map", "0:a?", "-map", "1:0",
                 "-c", "copy", "-c:s", "srt", out])
            return out
