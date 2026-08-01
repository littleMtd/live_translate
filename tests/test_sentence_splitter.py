import queue
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from modules.sentence_splitter import (
    _can_merge_cuts,
    _is_complete,
    _merge_cuts,
    _semantic_mode_setting,
    start,
)
from modules.pipeline_events import TranscriptionEvent
from modules.sentence_buffer import SentenceCut

# Fast config used by all thread tests: min_wait=0.3s, force_cut=0.8s.
# Default config (3s / 8s) would make thread tests take 10–30 s each.
def _fast_cfg(
    min_wait: float = 0.3,
    force_cut: float = 0.8,
    pending_timeout: float = 8.0,
) -> MagicMock:
    m = MagicMock()
    m.splitter.min_wait_seconds = min_wait
    m.splitter.force_cut_seconds = force_cut
    m.splitter.max_merge_source_count = 2
    m.splitter.max_merge_text_chars = 120
    m.splitter.segment_gap_split_enabled = False
    m.splitter.segment_gap_seconds = 0.6
    m.splitter.silence_complete_enabled = False
    m.splitter.pending_incomplete_timeout_seconds = pending_timeout
    m.splitter.semantic_early_cut_mode = "off"
    return m


class TestIsComplete(unittest.TestCase):

    def test_complete_endings(self):
        cases = [
            "안녕하세요",       # 요
            "그렇죠",           # 죠
            "맞다",             # 다
            "진짜야",           # 야
            "ㅋㅋ",             # ㅋㅋ
            "정말요?",          # ?
            "대박!",            # !
            "괜찮아",           # 아
            "그렇네",           # 네
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(_is_complete(text), f"Expected complete: {text!r}")

    def test_incomplete_endings(self):
        cases = [
            "지금 게임 하고",   # 고
            "밥을 먹으면",       # 면
            "그래서",           # 서
            "왜냐하면 니까",     # 니까
            "알잖아 거든",       # 거든
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertFalse(_is_complete(text), f"Expected incomplete: {text!r}")

    def test_unknown_ending_returns_false(self):
        # 未知語尾預設為 False（不切）
        self.assertFalse(_is_complete("뭔가이상한"))

    def test_empty_string(self):
        self.assertFalse(_is_complete(""))

    def test_whitespace_only(self):
        self.assertFalse(_is_complete("   "))

    def test_new_complete_endings(self):
        cases = [
            "그렇군",       # 군
            "그렇구나",     # 구나
            "가겠어",       # 겠어
            "먹겠다",       # 겠다
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(_is_complete(text), f"Expected complete: {text!r}")

    def test_new_incomplete_endings(self):
        cases = [
            "학교에서",     # 에서
            "집으로",       # 으로
            "밥을 먹아서",  # 아서
            "공부하고",     # 하고
            "여기이고",     # 이고
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertFalse(_is_complete(text), f"Expected incomplete: {text!r}")

    def test_incomplete_overrides_complete_substring(self):
        # "배고서" ends with 서 (incomplete 이서/어서 family) even though 고 is also checked
        self.assertFalse(_is_complete("배고서"))


class TestSemanticEarlyCutMode(unittest.TestCase):
    def test_only_explicit_shadow_is_enabled(self):
        self.assertEqual(_semantic_mode_setting("shadow"), "shadow")
        for value in ("off", "active", "invalid", None):
            with self.subTest(value=value):
                self.assertEqual(_semantic_mode_setting(value), "off")


class TestSentenceSplitterThread(unittest.TestCase):

    def _run(self, tokens: list[str], wait: float) -> list[dict]:
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()
        with patch("modules.sentence_splitter.cfg", _fast_cfg()):
            thread = start(tq, sq, stop)
            for token in tokens:
                tq.put(token)
            time.sleep(wait)
            stop.set()
            thread.join(timeout=2)

        results = []
        while not sq.empty():
            results.append(sq.get_nowait())
        return results

    def test_complete_sentence_sent(self):
        results = self._run(["안녕하세요"], wait=1.0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].incomplete, False)

    def test_force_cut_marks_incomplete(self):
        # 語尾是 "고"（incomplete），等待超過 force_cut=0.8s
        results = self._run(["게임 하고"], wait=1.5)
        self.assertGreater(len(results), 0)
        self.assertTrue(results[0].incomplete)

    def test_incomplete_force_cut_waits_for_next_chunk(self):
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()

        with patch("modules.sentence_splitter.cfg", _fast_cfg(min_wait=0.1, force_cut=0.3)):
            thread = start(tq, sq, stop)
            tq.put("partial thought")
            time.sleep(0.6)
            self.assertTrue(sq.empty(), "Incomplete cut should be buffered, not emitted immediately")

            tq.put("finished!")
            result = sq.get(timeout=2)
            stop.set()
            thread.join(timeout=2)

        self.assertEqual(result.text, "partial thought finished!")
        self.assertFalse(result.incomplete)

    def test_pending_incomplete_times_out_without_next_chunk(self):
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()

        with patch(
            "modules.sentence_splitter.cfg",
            _fast_cfg(min_wait=0.1, force_cut=0.2, pending_timeout=0.35),
        ):
            thread = start(tq, sq, stop)
            tq.put("partial thought")
            result = sq.get(timeout=2)
            stop.set()
            thread.join(timeout=2)

        self.assertEqual(result.text, "partial thought")
        self.assertTrue(result.incomplete)

    def test_two_incomplete_cuts_emit_bounded_merge(self):
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()

        with patch("modules.sentence_splitter.cfg", _fast_cfg(min_wait=0.1, force_cut=0.3)):
            thread = start(tq, sq, stop)
            tq.put("first fragment")
            time.sleep(0.6)
            self.assertTrue(sq.empty())

            tq.put("second fragment")
            result = sq.get(timeout=2)
            stop.set()
            thread.join(timeout=2)

        self.assertEqual(result.text, "first fragment second fragment")
        self.assertTrue(result.incomplete)

    def test_unsafe_incomplete_merge_emits_pending_then_buffers_current(self):
        first = TranscriptionEvent(
            text="first fragment",
            engine="groq",
            profile_id="a",
            utterance_id="utt-1",
        )
        second = TranscriptionEvent(
            text="second fragment",
            engine="groq",
            profile_id="a",
            utterance_id="utt-2",
        )
        third = TranscriptionEvent(
            text="third fragment",
            engine="groq",
            profile_id="a",
            utterance_id="utt-3",
        )

        tq = queue.Queue()
        sq = queue.Queue()
        stop = threading.Event()
        with patch("modules.sentence_splitter.cfg", _fast_cfg(min_wait=0.1, force_cut=0.3)):
            thread = start(tq, sq, stop)
            tq.put(first)
            tq.put(second)
            time.sleep(0.6)
            self.assertTrue(sq.empty())

            # A third incomplete cut would exceed max_merge_source_count if
            # merged with the pending two-source cut, so the pending cut is
            # emitted alone and the third cut remains pending until shutdown.
            tq.put(third)
            first_result = sq.get(timeout=2)
            stop.set()
            second_result = sq.get(timeout=2)
            thread.join(timeout=2)

        self.assertEqual(first_result.source_utterance_ids, ("utt-1", "utt-2"))
        self.assertEqual(second_result.text, "third fragment")
        self.assertEqual(second_result.source_utterance_ids, ("utt-3",))

    def test_buffer_accumulates_multiple_tokens(self):
        results = self._run(["진짜", "대박이에요"], wait=1.0)
        self.assertEqual(len(results), 1)
        self.assertIn("진짜", results[0].text)
        self.assertIn("대박이에요", results[0].text)

    def test_empty_queue_no_output(self):
        results = self._run([], wait=0.5)
        self.assertEqual(len(results), 0)

    def test_transcription_event_metadata_is_propagated(self):
        token = TranscriptionEvent(
            text="안녕하세요",
            engine="groq",
            profile_id="isegye_lilpa",
            avg_logprob=-0.2,
            no_speech_prob=0.1,
        )
        results = self._run([token], wait=1.0)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].text, token.text)
        self.assertEqual(results[0].stt_engine, "groq")
        self.assertEqual(results[0].profile_id, "isegye_lilpa")
        self.assertEqual(results[0].avg_logprob, -0.2)
        self.assertEqual(results[0].no_speech_prob, 0.1)

    def test_silence_complete_config_emits_without_waiting_for_complete_ending(self):
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()
        cfg = _fast_cfg(min_wait=5.0, force_cut=8.0)
        cfg.splitter.silence_complete_enabled = True

        with patch("modules.sentence_splitter.cfg", cfg):
            thread = start(tq, sq, stop)
            tq.put(
                TranscriptionEvent(
                    text="지금 여기까지 말했어",
                    engine="groq",
                    profile_id="a",
                    vad_cut_reason="silence",
                )
            )
            result = sq.get(timeout=2)
            stop.set()
            thread.join(timeout=2)

        self.assertEqual(result.cut_reason, "silence_complete")
        self.assertFalse(result.incomplete)


class TestSentenceRuntimeEvent(unittest.TestCase):
    """The splitter emits a `sentence` runtime event describing assembly."""

    def _sentence_emits(self, tokens, wait, cfg=None):
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()
        with patch("modules.sentence_splitter.cfg", cfg or _fast_cfg()), \
                patch("modules.sentence_splitter.runtime_events") as events:
            thread = start(tq, sq, stop)
            for token in tokens:
                tq.put(token)
            time.sleep(wait)
            stop.set()
            thread.join(timeout=2)
        return [c.kwargs for c in events.emit.call_args_list
                if c.args and c.args[0] == "sentence"]

    def test_natural_cut_emits_sentence_event(self):
        token = TranscriptionEvent(
            text="안녕하세요",
            engine="groq",
            profile_id="isegye_lilpa",
            utterance_id="utt-5",
            audio_seconds=1.2,
            avg_logprob=-0.4,
            no_speech_prob=0.2,
        )
        emits = self._sentence_emits([token], wait=1.0)

        self.assertEqual(len(emits), 1)
        kw = emits[0]
        self.assertEqual(kw["utterance_id"], "utt-5")
        self.assertEqual(kw["cut_reason"], "natural")
        self.assertEqual(kw["chunk_count"], 1)
        self.assertEqual(kw["audio_seconds"], 1.2)
        self.assertEqual(kw["source_count"], 1)
        self.assertEqual(kw["source_avg_logprobs"], [-0.4])
        self.assertEqual(kw["min_avg_logprob"], -0.4)
        self.assertEqual(kw["source_no_speech_probs"], [0.2])
        self.assertEqual(kw["max_no_speech_prob"], 0.2)
        self.assertFalse(kw["incomplete"])
        self.assertFalse(kw["forced"])

    def test_semantic_shadow_observes_each_prefilled_chunk_without_cutting(self):
        first = TranscriptionEvent(
            text="안녕하세요",
            engine="groq",
            profile_id="a",
            utterance_id="utt-1",
            vad_cut_reason="soft_max_pause",
        )
        second = TranscriptionEvent(
            text="게임 하고",
            engine="groq",
            profile_id="a",
            utterance_id="utt-2",
            vad_cut_reason="hard_max",
        )
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()
        cfg = _fast_cfg(min_wait=5.0, force_cut=8.0)
        cfg.splitter.semantic_early_cut_mode = "shadow"
        tq.put(first)
        tq.put(second)

        with patch("modules.sentence_splitter.cfg", cfg), \
                patch("modules.sentence_splitter.runtime_events") as events:
            thread = start(tq, sq, stop)
            time.sleep(0.25)
            self.assertTrue(sq.empty())
            stop.set()
            thread.join(timeout=2)

        semantic = [
            call.kwargs
            for call in events.emit.call_args_list
            if call.args and call.args[0] == "sentence_early_cut"
        ]
        self.assertEqual(len(semantic), 2)
        self.assertEqual([event["drain_batch_position"] for event in semantic], [1, 2])
        self.assertEqual([event["drain_batch_size"] for event in semantic], [2, 2])
        self.assertTrue(semantic[0]["would_cut"])
        self.assertFalse(semantic[0]["legacy_would_cut"])
        self.assertEqual(semantic[0]["saved_wait_ms"], 5000.0)
        self.assertEqual(semantic[1]["classification"], "incomplete")
        self.assertFalse(semantic[1]["would_cut"])
        self.assertFalse(semantic[0]["applied"])
        self.assertFalse(semantic[0]["actual_cut_ready"])
        final = sq.get_nowait()
        self.assertEqual(final.text, "안녕하세요 게임 하고")

    def test_semantic_shadow_snapshots_metrics_only_after_normal_cut(self):
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()
        cfg = _fast_cfg(min_wait=0.0, force_cut=8.0)
        cfg.splitter.semantic_early_cut_mode = "shadow"
        tq.put(
            TranscriptionEvent(
                text="안녕하세요",
                engine="groq",
                profile_id="a",
                utterance_id="utt-1",
            )
        )
        fake_metrics = MagicMock()

        def snapshot_after_cut():
            self.assertFalse(sq.empty())
            result = MagicMock()
            result.counters = {}
            return result

        fake_metrics.snapshot.side_effect = snapshot_after_cut
        with patch("modules.sentence_splitter.cfg", cfg), \
                patch("modules.sentence_splitter.metrics", fake_metrics), \
                patch("modules.sentence_splitter.runtime_events"):
            thread = start(tq, sq, stop)
            time.sleep(0.25)
            stop.set()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        fake_metrics.snapshot.assert_called_once_with()
        self.assertEqual(sq.get_nowait().text, "안녕하세요")

    def test_merged_cut_reports_combined_assembly(self):
        first = TranscriptionEvent(
            text="first fragment", engine="groq", profile_id="a",
            utterance_id="utt-1", audio_seconds=1.0,
            avg_logprob=-0.9, no_speech_prob=None,
        )
        second = TranscriptionEvent(
            text="second fragment", engine="groq", profile_id="a",
            utterance_id="utt-2", audio_seconds=2.0,
            avg_logprob=None, no_speech_prob=0.6,
        )
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()
        with patch("modules.sentence_splitter.cfg", _fast_cfg(min_wait=0.1, force_cut=0.3)), \
                patch("modules.sentence_splitter.runtime_events") as events:
            thread = start(tq, sq, stop)
            tq.put(first)
            time.sleep(0.6)   # force-cut "first" as incomplete -> buffered
            tq.put(second)
            sq.get(timeout=2)
            stop.set()
            thread.join(timeout=2)

        emits = [c.kwargs for c in events.emit.call_args_list
                 if c.args and c.args[0] == "sentence"]
        self.assertEqual(len(emits), 1)
        kw = emits[0]
        self.assertTrue(kw["cut_reason"].startswith("merged:"))
        self.assertEqual(kw["chunk_count"], 2)
        self.assertEqual(kw["audio_seconds"], 3.0)
        # Merged sentence keeps the latest source's correlation id...
        self.assertEqual(kw["utterance_id"], "utt-2")
        # ...but lists every contributing chunk for full attribution.
        self.assertEqual(kw["source_utterance_ids"], ["utt-1", "utt-2"])
        self.assertEqual(kw["source_count"], 2)
        self.assertEqual(kw["source_avg_logprobs"], [-0.9, None])
        self.assertEqual(kw["min_avg_logprob"], -0.9)
        self.assertEqual(kw["source_no_speech_probs"], [None, 0.6])
        self.assertEqual(kw["max_no_speech_prob"], 0.6)

    def test_hold_shadow_records_first_next_chunk_without_changing_merge(self):
        first = TranscriptionEvent(
            text="지금 게임 하고",
            engine="groq",
            profile_id="a",
            utterance_id="utt-1",
        )
        second = TranscriptionEvent(
            text="있어요",
            engine="groq",
            profile_id="a",
            utterance_id="utt-2",
        )
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()
        with patch(
            "modules.sentence_splitter.cfg",
            _fast_cfg(min_wait=0.05, force_cut=0.15),
        ), patch("modules.sentence_splitter.runtime_events") as events:
            thread = start(tq, sq, stop)
            tq.put(first)
            time.sleep(0.35)
            self.assertTrue(sq.empty())
            tq.put(second)
            result = sq.get(timeout=2)
            stop.set()
            thread.join(timeout=2)

        shadow = [
            call.kwargs
            for call in events.emit.call_args_list
            if call.args and call.args[0] == "sentence_hold_shadow"
        ]
        candidate = next(event for event in shadow if event["phase"] == "candidate")
        outcome = next(event for event in shadow if event["phase"] == "outcome")
        self.assertEqual(candidate["signals"], ["unfinished_connector"])
        self.assertEqual(candidate["disposition"], "buffered")
        self.assertTrue(outcome["observed_next_chunk"])
        self.assertEqual(outcome["next_chunk_utterance_id"], "utt-2")
        self.assertTrue(outcome["raw_continuation_heuristic"])
        self.assertFalse(outcome["useful_merge_heuristic"])
        self.assertEqual(result.text, "지금 게임 하고 있어요")

    def test_hold_shadow_timeout_has_one_candidate_and_terminal_outcome(self):
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()
        with patch(
            "modules.sentence_splitter.cfg",
            _fast_cfg(
                min_wait=0.05,
                force_cut=0.1,
                pending_timeout=0.25,
            ),
        ), patch("modules.sentence_splitter.runtime_events") as events:
            thread = start(tq, sq, stop)
            tq.put("지금 게임 하고")
            result = sq.get(timeout=2)
            stop.set()
            thread.join(timeout=2)

        shadow = [
            call.kwargs
            for call in events.emit.call_args_list
            if call.args and call.args[0] == "sentence_hold_shadow"
        ]
        self.assertEqual(
            [event["phase"] for event in shadow],
            ["candidate", "outcome"],
        )
        self.assertEqual(shadow[1]["outcome_reason"], "pending_incomplete_timeout")
        self.assertFalse(shadow[1]["observed_next_chunk"])
        self.assertEqual(result.text, "지금 게임 하고")

    def test_hold_shadow_stop_closes_buffered_candidate_without_duplicate(self):
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()
        with patch(
            "modules.sentence_splitter.cfg",
            _fast_cfg(min_wait=0.05, force_cut=0.1),
        ), patch("modules.sentence_splitter.runtime_events") as events:
            thread = start(tq, sq, stop)
            tq.put("지금 게임 하고")
            time.sleep(0.3)
            self.assertTrue(sq.empty())
            stop.set()
            thread.join(timeout=2)
            result = sq.get(timeout=1)

        shadow = [
            call.kwargs
            for call in events.emit.call_args_list
            if call.args and call.args[0] == "sentence_hold_shadow"
        ]
        self.assertEqual(
            [event["phase"] for event in shadow],
            ["candidate", "outcome"],
        )
        self.assertEqual(shadow[1]["outcome_reason"], "splitter_stopped")
        self.assertEqual(result.text, "지금 게임 하고")


class TestSentenceMergeGuardrail(unittest.TestCase):
    def test_merge_cuts_keeps_evidence_separate_from_current_source(self):
        first = SentenceCut(
            text="first fragment",
            incomplete=True,
            source=None,
            elapsed=1.0,
            forced=True,
            source_utterance_ids=(),
            evidence_source_utterance_ids=("utt-prior",),
            chunk_count=0,
            audio_seconds=0.0,
        )
        second = SentenceCut(
            text="second fragment",
            incomplete=False,
            source=None,
            elapsed=1.0,
            forced=False,
            source_utterance_ids=("utt-current",),
            evidence_source_utterance_ids=("utt-context",),
            chunk_count=1,
            audio_seconds=1.5,
        )

        merged = _merge_cuts(first, second)

        self.assertEqual(merged.source_utterance_ids, ("utt-current",))
        self.assertEqual(merged.evidence_source_utterance_ids, ("utt-prior", "utt-context"))
        self.assertEqual(merged.chunk_count, 1)
        self.assertEqual(merged.audio_seconds, 1.5)

    def test_merge_guard_rejects_source_count_over_limit(self):
        first = SentenceCut(
            text="first fragment",
            incomplete=True,
            source=None,
            elapsed=1.0,
            forced=True,
            source_utterance_ids=("utt-1", "utt-2"),
            chunk_count=2,
        )
        second = SentenceCut(
            text="second fragment",
            incomplete=True,
            source=None,
            elapsed=1.0,
            forced=True,
            source_utterance_ids=("utt-3",),
            chunk_count=1,
        )

        with patch("modules.sentence_splitter.cfg", _fast_cfg()):
            self.assertFalse(_can_merge_cuts(first, second))

    def test_merge_guard_rejects_text_over_limit(self):
        first = SentenceCut(
            text="a" * 80,
            incomplete=True,
            source=None,
            elapsed=1.0,
            forced=True,
            source_utterance_ids=("utt-1",),
            chunk_count=1,
        )
        second = SentenceCut(
            text="b" * 80,
            incomplete=True,
            source=None,
            elapsed=1.0,
            forced=True,
            source_utterance_ids=("utt-2",),
            chunk_count=1,
        )

        with patch("modules.sentence_splitter.cfg", _fast_cfg()):
            self.assertFalse(_can_merge_cuts(first, second))


class TestSentenceSplitterPause(unittest.TestCase):

    def test_pause_prevents_output(self):
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()
        pause = threading.Event()
        pause.set()   # start paused

        with patch("modules.sentence_splitter.cfg", _fast_cfg()):
            start(tq, sq, stop, pause_event=pause)
            tq.put("안녕하세요")   # complete ending — would normally emit
            time.sleep(0.5)
            stop.set()

        self.assertTrue(sq.empty(), "No output expected while paused")

    def test_resume_after_pause_produces_output(self):
        tq: queue.Queue = queue.Queue()
        sq: queue.Queue = queue.Queue()
        stop = threading.Event()
        pause = threading.Event()

        with patch("modules.sentence_splitter.cfg", _fast_cfg()):
            start(tq, sq, stop, pause_event=pause)
            # Add a token, immediately pause, wait, then unpause
            tq.put("환영합니다")   # 다 = complete
            time.sleep(0.1)
            pause.set()
            time.sleep(0.3)
            pause.clear()
            # Wait > 0.5 s so the splitter's pause-loop tick expires and the
            # drain completes before we add the post-resume token.
            time.sleep(0.7)
            tq.put("감사합니다")   # 다 = complete, arrives after drain
            time.sleep(1.0)
            stop.set()

        results = []
        while not sq.empty():
            results.append(sq.get_nowait())
        # At least the post-resume sentence should arrive
        self.assertGreater(len(results), 0)
        texts = " ".join(r.text for r in results)
        self.assertIn("감사합니다", texts)


if __name__ == "__main__":
    unittest.main()
