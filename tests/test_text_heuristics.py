import unittest

from utils.text_heuristics import (
    DIGIT_RE,
    ENGLISH_WORD_RE,
    KOREAN_CHAR_RE,
    SENSEVOICE_NOISE_TAGS,
    SENSEVOICE_TAG_RE,
    SENTENCE_COMPLETE_ENDINGS,
    SENTENCE_INCOMPLETE_ENDINGS,
    STT_FRAGMENTED_MARKERS,
    STT_GARBAGE_KEYWORDS,
)


class TestTextHeuristics(unittest.TestCase):
    def test_sensevoice_tag_regex_strips_metadata(self):
        self.assertEqual(SENSEVOICE_TAG_RE.sub("", "<|ko|><|Speech|>안녕"), "안녕")

    def test_noise_tags_exclude_speech(self):
        self.assertIn("<|BGM|>", SENSEVOICE_NOISE_TAGS)
        self.assertNotIn("<|Speech|>", SENSEVOICE_NOISE_TAGS)

    def test_sentence_endings_are_longest_first(self):
        self.assertLess(
            SENTENCE_COMPLETE_ENDINGS.index("ㅋㅋ"),
            SENTENCE_COMPLETE_ENDINGS.index("ㅠ"),
        )
        self.assertLess(
            SENTENCE_INCOMPLETE_ENDINGS.index("는데"),
            SENTENCE_INCOMPLETE_ENDINGS.index("고"),
        )

    def test_translator_garbage_markers_are_available(self):
        self.assertIn("사이트", STT_GARBAGE_KEYWORDS)
        self.assertIn("약간", STT_FRAGMENTED_MARKERS)

    def test_shared_regexes_match_expected_text_classes(self):
        self.assertTrue(KOREAN_CHAR_RE.search("안녕하세요"))
        self.assertEqual(ENGLISH_WORD_RE.findall("abc xy defg"), ["abc", "defg"])
        self.assertEqual(DIGIT_RE.findall("45로 45키로"), ["45", "45"])


if __name__ == "__main__":
    unittest.main()
