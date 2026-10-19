"""Gryna (be ML) subtitrų logika: žodžiai -> subtitrai -> SRT/VTT/TXT, haliucinacijų aptikimas."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Word:
    text: str
    start: float
    end: float
    speaker: Optional[int] = None


@dataclass
class Cue:
    start: float
    end: float
    text: str
    words: List[Word] = field(default_factory=list)
    speaker: Optional[int] = None


@dataclass
class Turn:
    start: float
    end: float
    speaker: int


def assign_speakers(words: List[Word], turns: List[Turn]) -> List[Word]:
    """Kiekvienam žodžiui – kalbėtojas, su kuriuo jo laikas labiausiai persidengia; be persidengimo – artimiausias turnas."""
    for word in words:
        best, best_overlap = None, 0.0
        for turn in turns:
            overlap = min(word.end, turn.end) - max(word.start, turn.start)
            if overlap > best_overlap:
                best, best_overlap = turn.speaker, overlap
        if best is None and turns:
            mid = (word.start + word.end) / 2
            best = min(turns, key=lambda t: min(abs(mid - t.start), abs(mid - t.end))).speaker
        word.speaker = best
    return words


def speaker_label(speaker: Optional[int], names: Optional[dict] = None) -> str:
    if speaker is None:
        return ""
    return (names or {}).get(speaker) or f"Kalbėtojas {speaker + 1}"


@dataclass
class Rules:
    max_line: int = 42
    max_lines: int = 2
    min_duration: float = 1.0
    max_duration: float = 7.0
    min_gap: float = 0.1
    split_on_pause: float = 0.8


def wrap(text: str, rules: Rules) -> str:
    if len(text) <= rules.max_line or rules.max_lines < 2:
        return text
    words = text.split()
    if len(words) < 2:
        return text
    best, best_score = 1, None
    for cut in range(1, len(words)):
        left, right = " ".join(words[:cut]), " ".join(words[cut:])
        overflow = max(0, len(left) - rules.max_line) + max(0, len(right) - rules.max_line)
        score = overflow * 100 + abs(len(left) - len(right))
        if best_score is None or score < best_score:
            best, best_score = cut, score
    return " ".join(words[:best]) + "\n" + " ".join(words[best:])


def _fits(text: str, rules: Rules) -> bool:
    lines = wrap(text, rules).split("\n")
    return len(lines) <= rules.max_lines and all(len(line) <= rules.max_line for line in lines)


def _ends_sentence(word: str) -> bool:
    return bool(word) and word[-1] in ".?!…"


def build_cues(words: List[Word], rules: Optional[Rules] = None) -> List[Cue]:
    """Žodžių laikai -> subtitrai. Pradžios niekada nestumdomos; tik pabaigos pailginamos į tylą."""
    rules = rules or Rules()
    groups: List[List[Word]] = []
    current: List[Word] = []
    for word in words:
        if current:
            text = " ".join(w.text for w in current + [word])
            too_long = not _fits(text, rules)
            too_slow = word.end - current[0].start > rules.max_duration
            paused = word.start - current[-1].end >= rules.split_on_pause
            sentence = _ends_sentence(current[-1].text) and len(" ".join(w.text for w in current)) >= rules.max_line // 2
            changed = word.speaker is not None and current[-1].speaker is not None and word.speaker != current[-1].speaker
            if too_long or too_slow or paused or sentence or changed:
                groups.append(current)
                current = []
        current.append(word)
    if current:
        groups.append(current)

    def majority(group):
        votes: dict = {}
        for w in group:
            if w.speaker is not None:
                votes[w.speaker] = votes.get(w.speaker, 0) + 1
        return max(votes, key=votes.get) if votes else None

    cues = [
        Cue(g[0].start, max(g[-1].end, g[0].start + 0.05), wrap(" ".join(w.text for w in g), rules), g, majority(g))
        for g in groups
    ]
    for i, cue in enumerate(cues):
        limit = cues[i + 1].start - rules.min_gap if i + 1 < len(cues) else float("inf")
        if cue.end - cue.start < rules.min_duration:
            cue.end = max(cue.end, min(cue.start + rules.min_duration, limit))
        if cue.end > limit and limit > cue.start + 0.05:
            cue.end = limit
    return cues


def timestamp(seconds: float, sep: str = ",") -> str:
    ms = max(0, int(round(seconds * 1000)))
    return f"{ms // 3_600_000:02d}:{ms % 3_600_000 // 60_000:02d}:{ms % 60_000 // 1000:02d}{sep}{ms % 1000:03d}"


def _prefix(cues: List[Cue], index: int, names: Optional[dict]) -> str:
    """„Vardas: “ rodomas, kai kalbėtojas pasikeičia (arba pirmame subtitre)."""
    speaker = cues[index].speaker
    if speaker is None or (index > 0 and cues[index - 1].speaker == speaker):
        return ""
    return speaker_label(speaker, names) + ": "


def to_srt(cues: List[Cue], names: Optional[dict] = None) -> str:
    return "\n\n".join(
        f"{i}\n{timestamp(c.start)} --> {timestamp(c.end)}\n{_prefix(cues, i - 1, names)}{c.text}"
        for i, c in enumerate(cues, 1)) + "\n"


def to_vtt(cues: List[Cue], names: Optional[dict] = None) -> str:
    def body(c: Cue) -> str:
        return f"<v {speaker_label(c.speaker, names)}>{c.text}" if c.speaker is not None else c.text

    parts = "\n\n".join(f"{timestamp(c.start, '.')} --> {timestamp(c.end, '.')}\n{body(c)}" for c in cues)
    return "WEBVTT\n\n" + parts + "\n"


def to_txt(cues: List[Cue], paragraph_gap: float = 1.5, names: Optional[dict] = None) -> str:
    """Skaitomas tekstas: pastraipos ties ilgesnėmis pauzėmis ir kalbėtojo pasikeitimu."""
    paragraphs: List[str] = []
    current: List[str] = []
    for index, cue in enumerate(cues):
        if current and (cue.start - cues[index - 1].end >= paragraph_gap or
                        (cue.speaker is not None and cue.speaker != cues[index - 1].speaker)):
            paragraphs.append(" ".join(current))
            current = []
        piece = cue.text.replace("\n", " ").strip()
        if not current:
            piece = _prefix(cues, index, names) + piece
        current.append(piece)
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs) + "\n"


def to_ts(cues: List[Cue], names: Optional[dict] = None) -> str:
    """Tekstas su laiko žymomis: [00:01:02] Vardas: sakinys."""
    return "\n".join(
        f"[{timestamp(c.start).split(',')[0]}] {_prefix(cues, i, names)}{c.text.replace(chr(10), ' ')}"
        for i, c in enumerate(cues)) + "\n"


def to_json(cues: List[Cue], language: Optional[str] = None, task: str = "transcribe", names: Optional[dict] = None) -> str:
    """Segmentai, kalbėtojai ir žodžių laikai (kūrėjams / tolesniam apdorojimui)."""
    def segment(index: int, c: Cue) -> dict:
        item = {"id": index, "start": round(c.start, 3), "end": round(c.end, 3), "text": c.text.replace("\n", " ")}
        if c.speaker is not None:
            item["speaker"] = speaker_label(c.speaker, names)
            item["speaker_id"] = c.speaker
        item["words"] = [{"word": w.text, "start": round(w.start, 3), "end": round(w.end, 3)} for w in c.words]
        return item

    return json.dumps({"language": language, "task": task, "segments": [segment(i, c) for i, c in enumerate(cues)]},
                      ensure_ascii=False, indent=2) + "\n"


_TOKEN = re.compile(r"\w+", re.UNICODE)


def tokens(text: str) -> List[str]:
    return _TOKEN.findall(text.lower())


def find_hallucinations(cues: List[Cue], min_repeats: int = 3, min_single_word: int = 5, max_phrase: int = 6):
    """Grąžina [(pirmo_subtitro_indeksas, paskutinio_indeksas, frazė, kartai)]; 2 pasikartojimai nežymimi."""
    flags = []
    keys = [" ".join(tokens(c.text)) for c in cues]
    i = 0
    flagged = set()
    while i < len(cues):
        if not keys[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(cues) and keys[j + 1] == keys[i]:
            j += 1
        if j - i + 1 >= min_repeats:
            flags.append((i, j, cues[i].text.replace("\n", " "), j - i + 1))
            flagged.update(range(i, j + 1))
        i = j + 1

    for index, cue in enumerate(cues):
        if index in flagged:
            continue
        words = tokens(cue.text)
        if len(words) < 2:
            continue
        for size in range(1, min(max_phrase, len(words)) + 1):
            need = min_single_word if size == 1 else min_repeats
            pos = 0
            while pos + size <= len(words):
                window, count, nxt = words[pos:pos + size], 1, pos + size
                while nxt + size <= len(words) and words[nxt:nxt + size] == window:
                    count += 1
                    nxt += size
                if count >= need:
                    flags.append((index, index, " ".join(window), count))
                    pos = nxt
                else:
                    pos += 1
    return sorted(flags)


def drop_hallucinated(cues: List[Cue], flags) -> List[Cue]:
    """Pašalina visiškai pasikartojančių subtitrų grandines (palieka pirmą). Frazių ciklus palieka (tik įspėjimas)."""
    remove = set()
    for first, last, _phrase, count in flags:
        if first != last:
            remove.update(range(first + 1, last + 1))
    return [c for i, c in enumerate(cues) if i not in remove]
