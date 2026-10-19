import unittest

from srtforge_lite.core import (Cue, Rules, Turn, Word, assign_speakers, build_cues, drop_hallucinated, find_hallucinations, timestamp,
                                to_json, to_srt, to_ts, to_txt, to_vtt, wrap)


def words(text, start=0.0, step=0.4):
    return [Word(w, start + i * step, start + i * step + step - 0.05) for i, w in enumerate(text.split())]


class CueTests(unittest.TestCase):
    def test_pause_splits_and_start_times_are_untouched(self):
        ws = words("Labas rytas.", 1.0) + words("Kaip sekasi?", 3.5)
        cues = build_cues(ws)
        self.assertEqual([c.start for c in cues], [1.0, 3.5])
        self.assertEqual(cues[0].text, "Labas rytas.")

    def test_long_text_wrapped_in_two_balanced_lines_and_lossless(self):
        text = "Šiandien kalbėsime apie maisto produktų kainų pokyčius parduotuvėse visoje Lietuvoje ir kaimyninėse šalyse"
        cues = build_cues(words(text))
        self.assertGreater(len(cues), 1)
        for cue in cues:
            lines = cue.text.split("\n")
            self.assertLessEqual(len(lines), 2)
            self.assertTrue(all(len(line) <= 42 for line in lines), cue.text)
            self.assertLessEqual(cue.end - cue.start, 7.0 + 1e-9)
        self.assertEqual(" ".join(c.text.replace("\n", " ") for c in cues), text)

    def test_short_cue_extends_only_into_silence_and_keeps_gap(self):
        ws = [Word("Taip.", 0.0, 0.3), Word("Gerai.", 0.8, 1.2)]
        a, b = build_cues(ws, Rules(split_on_pause=0.3))
        self.assertAlmostEqual(a.end, 0.7)
        self.assertEqual(b.start, 0.8)
        self.assertAlmostEqual(b.end, 1.8)

    def test_no_overlap_on_dense_speech(self):
        ws = words("a b c d e f g h i j k l m n o p q r s t u v", step=0.2)
        cues = build_cues(ws)
        for x, y in zip(cues, cues[1:]):
            self.assertLessEqual(x.end, y.start + 1e-9)

    def test_empty(self):
        self.assertEqual(build_cues([]), [])

    def test_formats(self):
        cue = Cue(61.5, 63.25, "Pirma\nantra")
        self.assertEqual(to_srt([cue]), "1\n00:01:01,500 --> 00:01:03,250\nPirma\nantra\n")
        self.assertEqual(to_vtt([cue]), "WEBVTT\n\n00:01:01.500 --> 00:01:03.250\nPirma\nantra\n")
        self.assertEqual(to_txt([cue]), "Pirma antra\n")
        self.assertEqual(timestamp(3661.0049), "01:01:01,005")

    def test_txt_groups_paragraphs_on_long_pauses(self):
        cues = [Cue(0, 1, "Pirmas."), Cue(1.2, 2, "Antras."), Cue(5, 6, "Naujas skyrius.")]
        self.assertEqual(to_txt(cues), "Pirmas. Antras.\n\nNaujas skyrius.\n")

    def test_timed_text_and_json_with_word_times(self):
        cue = Cue(61.5, 63.0, "Labas rytas", words=[Word("Labas", 61.5, 62.0), Word("rytas", 62.1, 62.9)])
        self.assertEqual(to_ts([cue]), "[00:01:01] Labas rytas\n")
        import json
        doc = json.loads(to_json([cue], "lt", "translate"))
        self.assertEqual((doc["language"], doc["task"]), ("lt", "translate"))
        self.assertEqual(doc["segments"][0]["words"][1], {"word": "rytas", "start": 62.1, "end": 62.9})

    def test_speakers_are_assigned_by_overlap_and_split_cues(self):
        ws = words("Sveiki aš Jonas.", 0.0) + words("Labas aš Ona.", 2.0)
        turns = [Turn(0.0, 1.7, 0), Turn(1.9, 3.5, 1)]
        assign_speakers(ws, turns)
        self.assertEqual([w.speaker for w in ws], [0, 0, 0, 1, 1, 1])
        cues = build_cues(ws)
        self.assertEqual([c.speaker for c in cues], [0, 1])
        self.assertEqual(cues[1].text, "Labas aš Ona.")

    def test_word_in_a_gap_takes_the_nearest_turn(self):
        w = [Word("tarpe", 2.05, 2.15)]
        assign_speakers(w, [Turn(0, 2.0, 3), Turn(5.0, 6.0, 7)])
        self.assertEqual(w[0].speaker, 3)

    def test_exports_label_speaker_only_when_it_changes(self):
        cues = [Cue(0, 1, "Labas.", speaker=0), Cue(1.1, 2, "Kaip sekasi?", speaker=0), Cue(2.2, 3, "Gerai.", speaker=1)]
        names = {0: "Jonas"}
        self.assertEqual(to_srt(cues, names),
                         "1\n00:00:00,000 --> 00:00:01,000\nJonas: Labas.\n\n2\n00:00:01,100 --> 00:00:02,000\nKaip sekasi?\n\n"
                         "3\n00:00:02,200 --> 00:00:03,000\nKalbėtojas 2: Gerai.\n")
        self.assertEqual(to_txt(cues, names=names), "Jonas: Labas. Kaip sekasi?\n\nKalbėtojas 2: Gerai.\n")
        self.assertIn("<v Jonas>Labas.", to_vtt(cues, names))
        self.assertTrue(to_ts(cues, names).startswith("[00:00:00] Jonas: Labas.\n[00:00:01] Kaip"))

    def test_without_speakers_exports_are_unchanged(self):
        cue = Cue(0, 1, "Labas.")
        self.assertEqual(to_srt([cue]), "1\n00:00:00,000 --> 00:00:01,000\nLabas.\n")
        self.assertNotIn("<v", to_vtt([cue]))

    def test_wrap_short_untouched(self):
        self.assertEqual(wrap("Trumpas.", Rules()), "Trumpas.")


class HallucinationTests(unittest.TestCase):
    def c(self, *texts):
        return [Cue(i * 2.0, i * 2.0 + 1.5, t) for i, t in enumerate(texts)]

    def test_normal_and_borderline_are_not_flagged(self):
        cues = self.c("Taip.", "Taip.", "Ir tada.", "Labas rytas, labas rytas, kaip sekasi?", "Ne ne ne, to nereikia.", "Taip.", "Taip.")
        self.assertEqual(find_hallucinations(cues), [])

    def test_repeated_segments_flagged_with_range_and_collapsed(self):
        cues = self.c("Sveiki.", "Ačiū, kad žiūrėjote.", "ačiū kad žiūrėjote!", "Ačiū, kad žiūrėjote.", "Ačiū, kad žiūrėjote.")
        flags = find_hallucinations(cues)
        self.assertEqual(flags, [(1, 4, "Ačiū, kad žiūrėjote.", 4)])
        self.assertEqual([c.text for c in drop_hallucinated(cues, flags)], ["Sveiki.", "Ačiū, kad žiūrėjote."])

    def test_phrase_loop_in_cue_flagged_but_kept(self):
        cues = self.c("Ir tada ir tada ir tada ir tada pasakė.")
        flags = find_hallucinations(cues)
        self.assertEqual(flags[0][2:], ("ir tada", 4))
        self.assertEqual(len(drop_hallucinated(cues, flags)), 1)

    def test_single_word_needs_five(self):
        self.assertEqual(find_hallucinations(self.c("Na na na na ir viskas.")), [])
        self.assertEqual(find_hallucinations(self.c("Na na na na na na ir viskas."))[0][2:], ("na", 6))


if __name__ == "__main__":
    unittest.main()
