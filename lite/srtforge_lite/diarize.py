"""Kalbėtojų atskyrimas (diarizacija) be paskyrų ir be debesies: sherpa-onnx + atviri ONNX modeliai.

Modeliai (atsisiunčiami vieną kartą, tikrinamas sha256):
  - pyannote segmentation 3.0 (MIT), ~7 MB
  - 3D-Speaker ERes2Net base kalbėtojo vektoriai (Apache-2.0), ~40 MB
"""
from __future__ import annotations

import hashlib
import os
import tarfile
import tempfile
import urllib.request
import wave
from typing import Callable, List, Optional

from .core import Turn

SEGMENTATION_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
                    "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
SEGMENTATION_SHA256 = "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488"
EMBEDDING_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
                 "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")
EMBEDDING_SHA256 = "1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b"
EMBEDDING_NAME = "speaker-embedding-eres2net-base.onnx"
SEGMENTATION_NAME = "speaker-segmentation-pyannote-3.0.onnx"
AUTO_THRESHOLD = 0.65          # išmatuota: 0,6–0,7 duoda teisingą balsų skaičių, ≥0,8 sujungia skirtingus
MAX_SPEAKERS = 12


class DiarizationError(Exception):
    pass


def models_dir(base: Optional[str] = None) -> str:
    if base:
        return base
    from .server import data_dir

    path = os.path.join(data_dir(), "models", "diarization")
    os.makedirs(path, exist_ok=True)
    return path


def _download(url: str, target: str, sha256: str, progress: Optional[Callable[[float], None]] = None,
              allow_local: bool = False) -> None:
    if not (url.startswith("https://") or (allow_local and url.startswith(("file://", "http://127.0.0.1")))):
        raise DiarizationError("modelio adresas turi būti https")
    digest = hashlib.sha256()
    tmp = target + ".part"
    try:
        with urllib.request.urlopen(url, timeout=60) as response, open(tmp, "wb") as handle:    # noqa: S310
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                handle.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
        if digest.hexdigest() != sha256:
            raise DiarizationError(f"modelio sha256 nesutampa ({digest.hexdigest()[:12]}…), failas atmestas")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def ensure_models(directory: Optional[str] = None, progress: Optional[Callable[[str, float], None]] = None,
                  urls: Optional[dict] = None, allow_local: bool = False) -> dict:
    """Grąžina {'segmentation': kelias, 'embedding': kelias}; trūkstamus modelius atsisiunčia."""
    base = models_dir(directory)
    urls = urls or {}
    segmentation = os.path.join(base, SEGMENTATION_NAME)
    embedding = os.path.join(base, EMBEDDING_NAME)
    say = progress or (lambda stage, value: None)

    if not os.path.isfile(embedding):
        say("Atsisiunčiami kalbėtojų atpažinimo modeliai (~40 MB)", 0.0)
        _download(urls.get("embedding", EMBEDDING_URL), embedding, urls.get("embedding_sha256", EMBEDDING_SHA256),
                  lambda v: say("Atsisiunčiami kalbėtojų atpažinimo modeliai (~40 MB)", v), allow_local)
    if not os.path.isfile(segmentation):
        say("Atsisiunčiamas kalbėtojų skyrimo modelis (~7 MB)", 0.0)
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "seg.tar.bz2")
            _download(urls.get("segmentation", SEGMENTATION_URL), archive,
                      urls.get("segmentation_sha256", SEGMENTATION_SHA256), None, allow_local)
            with tarfile.open(archive, "r:bz2") as tar:
                member = next((m for m in tar.getmembers() if m.name.endswith("/model.onnx") and m.isfile()), None)
                if member is None:
                    raise DiarizationError("segmentavimo archyve nerastas model.onnx")
                source = tar.extractfile(member)
                with open(segmentation + ".part", "wb") as handle:
                    handle.write(source.read())              # tik vienas žinomas failas, jokių kelių iš archyvo
                os.replace(segmentation + ".part", segmentation)
    return {"segmentation": segmentation, "embedding": embedding}


def read_wav16k(path: str):
    import numpy as np

    with wave.open(path, "rb") as handle:
        if handle.getframerate() != 16000 or handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise DiarizationError("diarizacijai reikia 16 kHz mono PCM16 WAV")
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16).astype("float32") / 32768.0


def diarize(wav_path: str, num_speakers: Optional[int] = None, directory: Optional[str] = None,
            progress: Optional[Callable[[str, float], None]] = None, threads: int = 2) -> List[Turn]:
    """16 kHz mono WAV -> kalbėtojų turnai. `num_speakers` = None – nustatoma automatiškai."""
    try:
        import sherpa_onnx
    except ImportError as error:
        raise DiarizationError("trūksta sherpa-onnx (kalbėtojų skyrimui)") from error

    models = ensure_models(directory, progress)
    clusters = -1 if not num_speakers else max(1, min(int(num_speakers), MAX_SPEAKERS))
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=models["segmentation"]),
            num_threads=threads),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=models["embedding"], num_threads=threads),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=clusters, threshold=AUTO_THRESHOLD),
        min_duration_on=0.3, min_duration_off=0.5)
    if not config.validate():
        raise DiarizationError("netinkama kalbėtojų skyrimo konfigūracija")
    engine = sherpa_onnx.OfflineSpeakerDiarization(config)
    if progress:
        progress("Skiriami kalbėtojai", 0.0)
    result = engine.process(read_wav16k(wav_path)).sort_by_start_time()
    return [Turn(float(r.start), float(r.end), int(r.speaker)) for r in result]
