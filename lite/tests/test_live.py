import os
import time
import unittest

import numpy as np

from srtforge_lite.core import to_srt
from srtforge_lite.live import SAMPLE_RATE, LiveSession

SCRIPT = [("Sveiki,", 0.2, 0.7), ("šiandien", 0.8, 1.3), ("kalbėsime", 1.4, 2.0), ("apie", 2.1, 2.4), ("kainas.", 2.5, 3.1),
          ("Pieno", 4.2, 4.6), ("kaina", 4.7, 5.1), ("pakilo", 5.2, 5.7)]


HOLDER = {}


def scripted(audio):
    """Apsimeta Whisper: mato tik tuos žodžius, kurie telpa į dabartinį garso langą (laikai santykiniai lango pradžiai)."""
    start = HOLDER["s"].buf_start if "s" in HOLDER else 0.0
    end = start + len(audio) / SAMPLE_RATE
    return [(t, a - start, b - start) for t, a, b in SCRIPT if a >= start and b <= end]


def make(fn=scripted, **kw):
    HOLDER.clear()
    session = LiveSession(fn, **kw)
    HOLDER["s"] = session
    return session


def feed_seconds(session, seconds, step=0.5, offset=0.0):
    for _ in range(int(seconds / step)):
        session.feed((np.zeros(int(SAMPLE_RATE * step), dtype="<i2")).tobytes())
        time.sleep(0.05)


class LiveTests(unittest.TestCase):
    def test_words_commit_only_after_two_agreeing_passes_and_split_into_lines(self):
        session = make()
        total = 0
        for _ in range(14):
            session.feed(np.zeros(int(SAMPLE_RATE * 0.5), dtype="<i2").tobytes())
            total += 0.5
            time.sleep(0.4)
            st = session.state()
            if total < 1.5:
                self.assertEqual(st["count"], 0)                                   # per anksti: nieko nepatvirtinta
        session.stop()
        texts = [l["text"] for l in session.state()["lines"]]
        self.assertEqual(texts[0], "Sveiki, šiandien kalbėsime apie kainas.")
        self.assertEqual(" ".join(texts), "Sveiki, šiandien kalbėsime apie kainas. Pieno kaina pakilo")
        srt = to_srt(session.cues())
        self.assertIn("1\n00:00:00,200 --> 00:00:03,100\nSveiki, šiandien kalbėsime apie kainas.", srt)

    def test_partial_shows_unconfirmed_tail(self):
        session = make()
        for _ in range(4):
            session.feed(np.zeros(int(SAMPLE_RATE * 0.5), dtype="<i2").tobytes())
            time.sleep(0.5)
        self.assertTrue(session.state()["partial"].startswith("Sveiki"))
        session.stop()

    def test_translation_added_to_finished_lines(self):
        class T:
            def translate(self, texts, s, t):
                return [f"[{t}] {x}" for x in texts]

        session = make(targets=["en", "de"], translator=T())
        for _ in range(14):
            session.feed(np.zeros(int(SAMPLE_RATE * 0.5), dtype="<i2").tobytes())
            time.sleep(0.4)
        session.stop()
        time.sleep(0.3)
        first = session.state()["lines"][0]
        self.assertEqual(first["tr"]["de"], "[de] " + first["text"])
        self.assertTrue(session.cues("en")[0].text.startswith("[en]"))

    def test_engine_failure_is_reported_not_hidden(self):
        def boom(audio):
            raise RuntimeError("modelis nepasiekiamas")

        session = LiveSession(boom)
        session.feed(np.zeros(SAMPLE_RATE * 2, dtype="<i2").tobytes())
        time.sleep(1.0)
        st = session.state()
        self.assertFalse(st["running"])
        self.assertIn("modelis nepasiekiamas", st["error"])

    def test_buffer_is_trimmed_so_long_sessions_stay_fast(self):
        session = LiveSession(lambda a: [])
        for _ in range(40):
            session.feed(np.zeros(SAMPLE_RATE * 2, dtype="<i2").tobytes())
        time.sleep(1.5)
        session.stop()
        self.assertLessEqual(len(session.audio) / SAMPLE_RATE, 30)


if __name__ == "__main__":
    unittest.main()


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL_DIR = os.environ.get("SRTFORGE_MODEL_DIR") or os.path.join(ROOT, ".venv-whisperx", "models")
CLIP = os.path.join(os.path.dirname(__file__), "data", "speech_lt.wav")


@unittest.skipUnless(os.path.isdir(MODEL_DIR) and os.path.exists(CLIP), "nėra modelio ar garso failo")
class RealLive(unittest.TestCase):
    def test_real_whisper_streamed_in_realtime_speed(self):
        import wave

        from srtforge_lite.live import whisper_fn

        with wave.open(CLIP) as w:
            self.assertEqual((w.getframerate(), w.getnchannels()), (16000, 1))
            pcm = w.readframes(w.getnframes())
        session = LiveSession(whisper_fn("base", "lt", MODEL_DIR, None), source_language="lt")
        step = SAMPLE_RATE * 2 // 2                        # 0,5 s baitais
        began = time.time()
        for i in range(0, len(pcm), step):
            session.feed(pcm[i:i + step])
            time.sleep(0.5 / 4)                            # 4x greičiau nei realiu laiku: testas turi būti trumpas
        time.sleep(4)
        session.stop()
        text = " ".join(l["text"] for l in session.state()["lines"]).lower()
        print("LIVE:", text, "| latency", session.latency)
        self.assertGreaterEqual(len(text.split()), 1)
        self.assertIsNone(session.error)
