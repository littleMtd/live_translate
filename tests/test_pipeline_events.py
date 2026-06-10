"""Unit tests for pipeline event carriers, focused on utterance_id propagation."""
import unittest

from modules.pipeline_events import (
    AudioChunk,
    SentenceEvent,
    SegmentInfo,
    TranscriptionEvent,
    sentence_metadata,
    source_confidence_summary,
    transcription_to_sentence,
)


class TestUtteranceIdPropagation(unittest.TestCase):
    def test_audio_chunk_proxies_legacy_audio_shape_access(self):
        audio = [1, 2, 3]
        chunk = AudioChunk(audio=audio, overlap_seconds=0.5, vad_cut_reason="silence")

        self.assertEqual(len(chunk), 3)
        self.assertEqual(chunk[1], 2)
        self.assertEqual(chunk.overlap_seconds, 0.5)
        self.assertEqual(chunk.vad_cut_reason, "silence")

    def test_transcription_event_can_carry_segment_metadata(self):
        event = TranscriptionEvent(
            text="안녕하세요",
            engine="groq",
            profile_id="",
            segments=(
                SegmentInfo(start=0.1, end=0.8, text="안녕하세요", avg_logprob=-0.2, no_speech_prob=0.1),
            ),
            overlap_seconds=0.4,
            vad_cut_reason="silence",
        )

        self.assertEqual(event.segments[0].start, 0.1)
        self.assertEqual(event.segments[0].end, 0.8)
        self.assertEqual(event.overlap_seconds, 0.4)
        self.assertEqual(event.vad_cut_reason, "silence")

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

    def test_sentence_metadata_exposes_evidence_source_utterance_ids(self):
        event = SentenceEvent(
            text="x",
            source_utterance_ids=("utt-2",),
            evidence_source_utterance_ids=("utt-1",),
        )
        meta = sentence_metadata(event)

        self.assertEqual(meta["source_utterance_ids"], ["utt-2"])
        self.assertEqual(meta["source_count"], 1)
        self.assertEqual(meta["evidence_source_utterance_ids"], ["utt-1"])
        self.assertEqual(meta["evidence_source_count"], 1)
        self.assertEqual(
            sentence_metadata({"text": "x", "evidence_source_utterance_ids": ("utt-7",)})[
                "evidence_source_utterance_ids"
            ],
            ["utt-7"],
        )

    def test_sentence_metadata_aligns_source_confidence_arrays(self):
        event = SentenceEvent(
            text="x",
            source_utterance_ids=("utt-1", "utt-2", "utt-1"),
            source_avg_logprobs=(-0.4, None, -0.8),
            source_no_speech_probs=(None, 0.7, 0.2),
        )

        meta = sentence_metadata(event)

        self.assertEqual(meta["source_count"], 3)
        self.assertEqual(meta["source_avg_logprobs"], [-0.4, None, -0.8])
        self.assertEqual(meta["min_avg_logprob"], -0.8)
        self.assertEqual(meta["source_no_speech_probs"], [None, 0.7, 0.2])
        self.assertEqual(meta["max_no_speech_prob"], 0.7)

    def test_sentence_metadata_preserves_none_aggregates_when_all_values_missing(self):
        meta = sentence_metadata(
            {
                "text": "x",
                "source_utterance_ids": ["utt-1", "utt-2"],
                "source_avg_logprobs": [None, None],
                "source_no_speech_probs": [None, None],
            }
        )

        self.assertEqual(meta["source_avg_logprobs"], [None, None])
        self.assertIsNone(meta["min_avg_logprob"])
        self.assertEqual(meta["source_no_speech_probs"], [None, None])
        self.assertIsNone(meta["max_no_speech_prob"])

    def test_sentence_metadata_pads_missing_arrays_for_backward_compatibility(self):
        meta = sentence_metadata({"text": "x", "source_utterance_ids": ["utt-1", "utt-2"]})

        self.assertEqual(meta["source_avg_logprobs"], [None, None])
        self.assertEqual(meta["source_no_speech_probs"], [None, None])
        self.assertIsNone(meta["min_avg_logprob"])
        self.assertIsNone(meta["max_no_speech_prob"])

    def test_source_confidence_summary_truncates_to_source_id_alignment(self):
        summary = source_confidence_summary(
            ["utt-1"],
            [-0.3, -0.9],
            [0.1, 0.8],
        )

        self.assertEqual(summary["source_avg_logprobs"], [-0.3])
        self.assertEqual(summary["source_no_speech_probs"], [0.1])
        self.assertEqual(summary["min_avg_logprob"], -0.3)
        self.assertEqual(summary["max_no_speech_prob"], 0.1)


if __name__ == "__main__":
    unittest.main()
