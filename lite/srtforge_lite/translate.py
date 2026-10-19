"""Vertimas į kelias kalbas vienu metu: M2M100 418M (MIT) per CTranslate2 int8, visiškai vietoje.

Mašininis vertimas yra orientacinis: dažniausiai tinka suprasti turinį ir pirmam subtitrų juodraščiui, bet ne
leidybai be žmogaus peržiūros (pavyzdžiui, atskiri vietiniai terminai gali būti išversti netiksliai).
"""
from __future__ import annotations

import copy
import hashlib
import os
import threading
import urllib.request
from typing import Callable, Dict, List, Optional

from .core import Cue, Rules, wrap

REVISION = "7c1b2620a4e58dacecbd8bf89cfd6da7eb9eb7b0"
REPO = "jncraton/m2m100_418M-ct2-int8"
BASE_URL = f"https://huggingface.co/{REPO}/resolve/{REVISION}/"
FILES = {   # failas -> sha256
    "model.bin": "d6703dd9f920ff896e45c3d97b490761bed5944937b90bbe6a7245f5652542d4",
    "config.json": "4244772990e30069563e3ddfb4ad6dc95bdfd2ac3de667ea8858c9b0a8433fa8",
    "shared_vocabulary.json": "7eb5d0ff184c6095c7c10f9911c0aea492250abd12854f9c3d787c64b1c6397e",
    "sentencepiece.bpe.model": "d8f7c76ed2a5e0822be39f0a4f95a55eb19c78f4593ce609e2edbc2aea4d380a",
}
DOWNLOAD_MB = 490

# kodas -> pavadinimas (rodomas programoje)
LANGUAGES = {
    "en": "Anglų", "de": "Vokiečių", "fr": "Prancūzų", "es": "Ispanų", "it": "Italų", "pt": "Portugalų",
    "pl": "Lenkų", "ru": "Rusų", "uk": "Ukrainiečių", "lv": "Latvių", "et": "Estų", "lt": "Lietuvių",
    "fi": "Suomių", "sv": "Švedų", "da": "Danų", "no": "Norvegų", "nl": "Olandų", "cs": "Čekų",
}


class TranslationError(Exception):
    pass


def model_dir(base: Optional[str] = None) -> str:
    if base:
        return base
    from .server import data_dir

    path = os.path.join(data_dir(), "models", "m2m100-418m-int8")
    os.makedirs(path, exist_ok=True)
    return path


def model_ready(directory: Optional[str] = None) -> bool:
    path = model_dir(directory)
    return all(os.path.isfile(os.path.join(path, name)) for name in FILES)


def ensure_model(directory: Optional[str] = None, progress: Optional[Callable[[str, float], None]] = None,
                 base_url: str = BASE_URL, files: Optional[Dict[str, str]] = None, allow_local: bool = False) -> str:
    path = model_dir(directory)
    files = files or FILES
    say = progress or (lambda stage, value: None)
    if not (base_url.startswith("https://") or (allow_local and base_url.startswith(("file://", "http://127.0.0.1")))):
        raise TranslationError("modelio adresas turi būti https")
    for name, sha in files.items():
        target = os.path.join(path, name)
        if os.path.isfile(target):
            continue
        label = f"Atsisiunčiamas vertimo modelis ({name}, bendras ~{DOWNLOAD_MB} MB; vienkartinis)"
        tmp = target + ".part"
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(base_url + name, timeout=60) as response, open(tmp, "wb") as handle:     # noqa: S310
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    digest.update(chunk)
                    handle.write(chunk)
                    done += len(chunk)
                    if total:
                        say(label, done / total)
            if digest.hexdigest() != sha:
                raise TranslationError(f"{name}: sha256 nesutampa, failas atmestas")
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return path


class Translator:
    """Įkeliamas tik kai reikia; saugus naudoti iš kelių gijų (vertimas užrakinamas)."""

    def __init__(self, directory: Optional[str] = None, threads: int = 0):
        import ctranslate2
        import sentencepiece

        path = model_dir(directory)
        if not model_ready(path):
            raise TranslationError("vertimo modelis neįdiegtas")
        self._ct = ctranslate2.Translator(path, device="auto", compute_type="int8", inter_threads=1, intra_threads=threads)
        self._sp = sentencepiece.SentencePieceProcessor()
        self._sp.load(os.path.join(path, "sentencepiece.bpe.model"))
        self._lock = threading.Lock()

    def translate(self, texts: List[str], source: str, target: str) -> List[str]:
        if source not in LANGUAGES or target not in LANGUAGES:
            raise TranslationError(f"nepalaikoma kalba: {source}→{target}")
        if source == target:
            return list(texts)
        out: List[str] = []
        with self._lock:
            for text in texts:
                if not text.strip():
                    out.append("")
                    continue
                tokens = [f"__{source}__"] + self._sp.encode(text, out_type=str) + ["</s>"]
                result = self._ct.translate_batch(
                    [tokens], target_prefix=[[f"__{target}__"]], beam_size=4,
                    max_decoding_length=int(len(tokens) * 2.2) + 10,
                    no_repeat_ngram_size=3)[0]            # apsauga nuo ciklo („priekšsēdētāja priekšsēdētāja…“)
                out.append(self._sp.decode(result.hypotheses[0][1:]))
        return out


def translate_cues(cues: List[Cue], translator, source: str, target: str, rules: Optional[Rules] = None) -> List[Cue]:
    """Subtitrų laikai ir kalbėtojai išlaikomi, tekstas išverčiamas ir iš naujo suskaidomas į eilutes."""
    rules = rules or Rules()
    texts = translator.translate([c.text.replace("\n", " ") for c in cues], source, target)
    result = []
    for cue, text in zip(cues, texts):
        clone = copy.copy(cue)
        clone.text = wrap(text.strip(), rules)
        clone.words = []                     # žodžių laikai atitiko originalo kalbą
        result.append(clone)
    return result
