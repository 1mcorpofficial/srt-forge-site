"""Gyvi subtitrai: slenkantis langas + vietinio sutarimo taisyklė (žodis patvirtinamas, kai dvi eilės hipotezės sutampa)."""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

try:
    import numpy as np
except ImportError:          # sugadinta numpy neturi nuversti visos programos (gyvi subtitrai tuomet nepasileidžia)
    np = None

from .core import Cue, Word

SAMPLE_RATE = 16000
MAX_BUFFER_S = 24.0           # ilgesnį garso langą Whisper apdoroja per lėtai
MIN_TICK_S = 1.0              # kaip dažnai perskaičiuojama hipotezė
MAX_LINE = 42
SENTENCE_END = (".", "?", "!", "…")
Hypothesis = List[Tuple[str, float, float]]      # (žodis, pradžia, pabaiga) santykinai pagal garso langą


def _norm(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


class LiveSession:
    def __init__(self, transcribe_fn: Callable[[object], Hypothesis], targets: Optional[list] = None,
                 translator=None, source_language: str = "lt", clock=time.time):
        if np is None:
            raise RuntimeError("nepavyko įkelti numpy")
        self.fn = transcribe_fn
        self.targets = targets or []
        self.translator = translator
        self.source = source_language
        self.clock = clock
        self.lock = threading.Lock()
        self.audio = np.zeros(0, dtype=np.float32)
        self.buf_start = 0.0                       # absoliutus garso lango pradžios laikas (s)
        self.total_s = 0.0
        self.prev: List[Word] = []                 # ankstesnė hipotezė (absoliutūs laikai)
        self.committed: List[Word] = []            # patvirtinti žodžiai, dar nesujungti į eilutę
        self.lines: List[dict] = []                # baigtos eilutės: start, end, text, tr{}
        self.partial = ""
        self.running = True
        self.error: Optional[str] = None
        self.last_ticked = 0.0
        self.latency = 0.0
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    # ——— įvestis ———
    def feed(self, pcm16: bytes) -> None:
        samples = np.frombuffer(pcm16[: len(pcm16) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        with self.lock:
            self.audio = np.concatenate([self.audio, samples])
            self.total_s += len(samples) / SAMPLE_RATE
        self.wake.set()

    def stop(self) -> None:
        self.running = False
        self.wake.set()
        self.thread.join(timeout=30)
        with self.lock:
            self._flush(force=True)

    # ——— ciklas ———
    def _loop(self) -> None:
        while self.running:
            self.wake.wait(0.25)
            self.wake.clear()
            with self.lock:
                have = self.total_s - self.buf_start
                due = have >= 1.0 and self.total_s - self.last_ticked >= MIN_TICK_S
                snapshot = self.audio.copy() if due else None
                start = self.buf_start
                mark = self.total_s
            if snapshot is None:
                continue
            began = self.clock()
            try:
                hyp = self.fn(snapshot)
            except Exception as error:  # noqa: BLE001
                self.error = str(error)
                self.running = False
                return
            with self.lock:
                self.last_ticked = mark
                self.latency = round(self.clock() - began, 2)
                self._update([Word(t, start + a, start + b) for t, a, b in hyp if t.strip()])

    def _update(self, hyp: List[Word]) -> None:
        # nauja hipotezė gali apimti jau patvirtintus žodžius: praleidžiame juos pagal laiką
        done_until = self.committed[-1].end if self.committed else (self.lines[-1]["end"] if self.lines else -1.0)
        hyp = [w for w in hyp if w.start >= done_until - 0.05]
        agree = 0
        prev = [w for w in self.prev if w.start >= done_until - 0.05]
        for a, b in zip(prev, hyp):
            if _norm(a.text) != _norm(b.text):
                break
            agree += 1
        self.committed.extend(hyp[:agree])
        self.prev = hyp
        tail = " ".join(w.text for w in hyp[agree:])
        self._flush(force=False)
        if self.committed and not hyp[agree:] and self.total_s - self.committed[-1].end > 1.5:
            self._flush(force=True)                # kalbėtojas nutilo: eilutė užbaigiama nelaukiant
        pending = " ".join(w.text for w in self.committed)
        self.partial = (pending + " " + tail).strip()
        self._trim()

    def _flush(self, force: bool) -> None:
        if force:                                  # sesijos pabaiga ar ilga tyla: viskas, ką turime, tampa galutiniu
            done = self.committed[-1].end if self.committed else (self.lines[-1]["end"] if self.lines else -1.0)
            self.committed.extend(w for w in self.prev if w.start >= done - 0.05 and w not in self.committed)
            self.prev = []
        while self.committed:
            text = ""
            cut = None
            for i, w in enumerate(self.committed):
                candidate = (text + " " + w.text).strip()
                gap = w.start - self.committed[i - 1].end if i else 0.0
                if i and (len(candidate) > MAX_LINE or gap >= 0.8):
                    cut = i
                    break
                text = candidate
                if w.text.endswith(SENTENCE_END) and len(text) >= MAX_LINE // 3:
                    cut = i + 1
                    break
            if cut is None:
                if not force:
                    return
                cut = len(self.committed)
            group, self.committed = self.committed[:cut], self.committed[cut:]
            self._emit(group)
        self.partial = "" if force else self.partial

    def _emit(self, words: List[Word]) -> None:
        line = {"start": words[0].start, "end": max(words[-1].end, words[0].start + 0.3),
                "text": " ".join(w.text for w in words), "tr": {}}
        self.lines.append(line)
        if self.targets and self.translator:
            threading.Thread(target=self._translate, args=(line,), daemon=True).start()

    def _translate(self, line: dict) -> None:
        for lang in self.targets:
            try:
                line["tr"][lang] = self.translator.translate([line["text"]], self.source, lang)[0]
            except Exception:  # noqa: BLE001
                line["tr"][lang] = ""

    def _trim(self) -> None:
        """Atmetame seną garsą: tai, kas jau virto eilutėmis, Whisperiui nebereikalinga."""
        cut = self.lines[-1]["end"] if self.lines and not self.committed else (self.committed[0].start if self.committed else self.buf_start)
        if self.total_s - self.buf_start > MAX_BUFFER_S:
            cut = max(cut, self.total_s - MAX_BUFFER_S / 2)
            self.committed = [w for w in self.committed if w.start >= cut]
        if cut > self.buf_start:
            drop = int((cut - self.buf_start) * SAMPLE_RATE)
            self.audio = self.audio[drop:]
            self.buf_start += drop / SAMPLE_RATE
            self.prev = [w for w in self.prev if w.start >= self.buf_start]

    # ——— išvestis ———
    def state(self, after: int = 0) -> dict:
        with self.lock:
            return {"running": self.running, "error": self.error, "latency": self.latency, "total": round(self.total_s, 1),
                    "partial": self.partial, "count": len(self.lines), "lines": self.lines[after:], "targets": self.targets}

    def cues(self, lang: str = "") -> List[Cue]:
        with self.lock:
            return [Cue(l["start"], l["end"], (l["tr"].get(lang) or l["text"]) if lang else l["text"], []) for l in self.lines]


def whisper_fn(model: str, language: Optional[str], model_dir: Optional[str], prompt: Optional[str]):
    from .engine import load_model

    whisper = load_model(model, model_dir)

    def run(audio) -> Hypothesis:
        segments, _ = whisper.transcribe(audio, language=language or None, vad_filter=True, word_timestamps=True,
                                         condition_on_previous_text=False, initial_prompt=prompt or None, beam_size=1)
        return [(w.word.strip(), float(w.start), float(w.end)) for s in segments for w in (s.words or []) if w.word.strip()]

    return run
