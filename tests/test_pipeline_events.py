"""Unit tests for pipeline event carriers, focused on utterance_id propagation."""
import unittest

from modules.pipeline_events import (
    SentenceEvent,
    TranscriptionEvent,
    sentence_metadata,
    transcription_to_sentence,
)


class TestUtteranceIdPropagation(unittest.TestCase):
    def test_transcription_to_sentence_carries_utterance_id(self):
        source = TranscriptionEvent(
            text="안녕하세요",
            engine="groq",
            profile_id="stellive_hina",
            utterance_id="utt-7",
            avg_logprob=-0.2,
        )
        sentence = transcription_to_sentence("안녕하세요", incomplete=False, source=source)
        self.assertEqual(sentence.utterance_id, "utt-7")

    def test_transcription_to_sentence_without_source_has_empty_id(self):
        sentence = transcription_to_sentence("x", incomplete=True, source=None)
        self.assertEqual(sentence.utterance_id, "")

    def test_sentence_metadata_from_event_includes_utterance_id(self):
        event = SentenceEvent(text="x", stt_engine="groq", utterance_id="utt-3")
        self.assertEqual(sentence_metadata(event)["utterance_id"], "utt-3")

    def test_sentence_metadata_from_dict_includes_utterance_id(self):
        meta = sentence_metadata({"text": "x", "utterance_id": "utt-9"})
        self.assertEqual(meta["utterance_id"], "utt-9")

    def test_sentence_metadata_defaults_utterance_id(self):
        # Bare string carriers (legacy path) must still expose the key.
        self.assertEqual(sentence_metadata("x")["utterance_id"], "")
        self.assertEqual(sentence_metadata({"text": "x"})["utterance_id"], "")

    def test_transcription_to_sentence_carries_source_utterance_ids(self):
        source = TranscriptionEvent(text="x", engine="groq", profile_id="", utterance_id="utt-9")
        sentence = transcription_to_sentence(
            "x", incomplete=False, source=source, source_utterance_ids=("utt-7", "utt-8", "utt-9")
        )
        self.assertEqual(sentence.source_utterance_ids, ("utt-7", "utt-8", "utt-9"))

    def test_transcription_to_sentence_defaults_ids_to_single_source(self):
        # No explicit list -> fall back to the single source's id.
        source = TranscriptionEvent(text="x", engine="groq", profile_id="", utterance_id="utt-3")
        sentence = transcription_to_sentence("x", incomplete=False, source=source)
        self.assertEqual(sentence.source_utterance_ids, ("utt-3",))

    def test_sentence_metadata_exposes_source_utterance_ids(self):
        event = SentenceEvent(text="x", source_utterance_ids=("utt-1", "utt-2"))
        self.assertEqual(sentence_metadata(event)["source_utterance_ids"], ["utt-1", "utt-2"])
        self.assertEqual(
            sentence_metadata({"text": "x", "source_utterance_ids": ("utt-5",)})["source_utterance_ids"],
            ["utt-5"],
        )
        self.assertEqual(sentence_metadata("x")["source_utterance_ids"], [])


if __name__ == "__main__":
    unittest.main()
