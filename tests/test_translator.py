import sys

from contextlib import contextmanager
from unittest.mock import MagicMock, patch
for _mod in ("anthropic", "google", "google.genai"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import queue
import tempfile
import threading
import time
import unittest
import unittest.mock
import modules.translation_engines as translation_engines_module
import modules.translator as translator_module
from modules.translation_engines import (
    _build_engine_chain, _build_user_message, TranslationEngine, ClaudeEngine, GoogleTranslateEngine,
    DeepLEngine, GroqTranslationEngine, NvidiaEngine, OpenRouterTranslationEngine, _deepl_base_url,
    get_last_engine_api_diagnostics, get_last_engine_diagnostics, get_last_token_usage,
    build_effective_deepseek_messages,
    build_effective_qwen_messages,
)
from modules.provisional_subtitles import (
    ProvisionalCandidate,
    ProvisionalRequest,
    provisional_fingerprint,
)
from modules.translation_prompts import (
    _BASE_PROMPT,
    _build_base_prompt,
    get_translation_profile,
    translation_profile_ids,
)
from modules.translation_policy import RepetitionEvidence
from modules.unknown_name_escrow import resolve_unknown_name_escrow
from modules.semantic_terminology import resolve_semantic_terminology
from modules.translator import (
    _apply_source_aware_corrections,
    _is_legitimate_preserve_as_is,
    _looks_like_meta_garbage_output,
    _looks_untranslated,
    _translation_output_guard,
    _normalize_source_before_matching,
    _new_translation_memory,
    _token_usage_for_outcome,
    _write_history,
    get_corrections,
    reset_corrections,
    TranslationOutcome,
    Translator,
)
import modules.db as _db_module
from modules.activity_context import (
    bind_activity_snapshot,
    bind_profile_id,
    capture_activity_snapshot,
    effective_activity_value,
    effective_profile_id,
)
from modules.pipeline_events import SentenceEvent


class _NoOpDB:
    """No-op DB that keeps unit tests isolated from the on-disk production DB."""
    @property
    def available(self) -> bool:
        return False
    def lookup(self, *a, **kw):
        return None
    def store(self, *a, **kw):
        pass
    def close(self):
        pass


_db_patch = None
_history_patch = None


def setUpModule():
    global _db_patch, _history_patch
    _db_patch = unittest.mock.patch("modules.translator._get_db", return_value=_NoOpDB())
    _db_patch.start()
    _history_patch = unittest.mock.patch("modules.translator._write_history")
    _history_patch.start()


def tearDownModule():
    if _db_patch:
        _db_patch.stop()
    if _history_patch:
        _history_patch.stop()


class TestTranslationOutcomeQualityClassifications(unittest.TestCase):
    def test_telemetry_only_terms_do_not_expand_publication_preservation(self):
        publication_terms = translator_module._publication_approved_terms("isegye_lilpa")
        telemetry_terms = translator_module._quality_telemetry_approved_terms(
            "isegye_lilpa",
            "PVP에서 주르르가 이겼어요",
        )

        self.assertNotIn("Jururu", publication_terms)
        self.assertNotIn("PVP", publication_terms)
        self.assertIn("Jururu", telemetry_terms)
        self.assertIn("PVP", telemetry_terms)

    def test_known_common_acronym_is_approved_without_profile(self):
        outcome = TranslationOutcome(
            source_text="SOOP",
            target_text="SOOP",
            status="success",
            result_source="api",
            cache_status="miss",
            incomplete=False,
        )

        fields = outcome.as_event_fields(
            10.0,
            {"profile_id": "url", "profile_applied": False},
        )

        self.assertEqual(fields["target_approved_latin_spans"], ["SOOP"])
        self.assertEqual(fields["target_unexpected_latin_spans"], [])

    def test_profile_terms_only_change_telemetry_classifications(self):
        outcome = TranslationOutcome(
            source_text="모카가 Wish Me Love를 소개했어",
            target_text="모카介紹了Wish Me Love。",
            status="success",
            result_source="api",
            cache_status="miss",
            incomplete=False,
            engine="openrouter",
            model="qwen/qwen3-30b-a3b-instruct-2507",
        )

        approved = outcome.as_event_fields(
            100.0,
            {"profile_id": "url", "profile_applied": True},
        )
        unapproved = outcome.as_event_fields(
            100.0,
            {"profile_id": "url", "profile_applied": False},
        )

        self.assertEqual(
            approved["route_id"],
            "openrouter:qwen/qwen3-30b-a3b-instruct-2507",
        )
        self.assertEqual(approved["target_approved_hangul_spans"], ["모카"])
        self.assertEqual(approved["target_unexpected_hangul_spans"], [])
        self.assertEqual(
            approved["target_approved_latin_spans"],
            ["Wish", "Me", "Love"],
        )
        self.assertIn(
            "target_has_approved_hangul",
            approved["quality_classifications"],
        )
        self.assertEqual(unapproved["target_approved_hangul_spans"], [])
        self.assertEqual(unapproved["target_unexpected_hangul_spans"], ["모카"])
        self.assertEqual(
            unapproved["target_unexpected_latin_spans"],
            ["Wish", "Me", "Love"],
        )
        self.assertIn(
            "target_has_unexpected_hangul",
            unapproved["quality_classifications"],
        )

        # T04 is telemetry-only: profile knowledge must not alter the legacy
        # signals currently consumed by retry, cache, or offline tooling.
        self.assertEqual(approved["quality_flags"], unapproved["quality_flags"])
        self.assertEqual(approved["quality_score"], unapproved["quality_score"])
        self.assertEqual(
            approved["quality_severity"],
            unapproved["quality_severity"],
        )

    def test_profile_canonical_output_is_approved_only_when_applied(self):
        outcome = TranslationOutcome(
            source_text="주르르가 방송을 시작했어요",
            target_text="Jururu開始直播了。",
            status="success",
            result_source="api",
            cache_status="miss",
            incomplete=False,
        )

        approved = outcome.as_event_fields(
            10.0,
            {"profile_id": "isegye_lilpa", "profile_applied": True},
        )
        unapproved = outcome.as_event_fields(
            10.0,
            {"profile_id": "isegye_lilpa", "profile_applied": False},
        )

        self.assertEqual(approved["target_unexpected_latin_spans"], [])
        self.assertIn(
            "target_high_latin_approved_only",
            approved["quality_classifications"],
        )
        self.assertEqual(unapproved["target_unexpected_latin_spans"], ["Jururu"])
        self.assertIn(
            "target_high_latin_unexpected",
            unapproved["quality_classifications"],
        )

    def test_profile_name_telemetry_uses_same_collision_policy_as_publication(self):
        member = TranslationOutcome(
            source_text="랑코 어딨어? 야 랑코야.",
            target_text="랑코在哪？喂，랑코啊。",
            status="success",
            result_source="api",
            cache_status="miss",
            incomplete=False,
        ).as_event_fields(
            10.0,
            {"profile_id": "url", "profile_applied": True},
        )
        interjection = TranslationOutcome(
            source_text="오아 진짜요?",
            target_text="오아真的嗎？",
            status="success",
            result_source="api",
            cache_status="miss",
            incomplete=False,
        ).as_event_fields(
            10.0,
            {"profile_id": "url", "profile_applied": True},
        )

        self.assertEqual(member["target_unexpected_hangul_spans"], [])
        self.assertEqual(
            interjection["target_unexpected_hangul_spans"], ["오아"]
        )
    def test_irise_corrected_canonical_is_normalized_not_suspicious(self):
        reset_corrections()
        with _active_translation_profile("irise"):
            corrected = _apply_source_aware_corrections(
                "키리씨가 왔어요",
                "基里來了。",
            )
        corrections = get_corrections()
        outcome = TranslationOutcome(
            source_text="키리씨가 왔어요",
            target_text=corrected,
            status="success",
            result_source="api",
            cache_status="miss",
            incomplete=False,
        )

        fields = outcome.as_event_fields(
            10.0,
            {
                "profile_id": "irise",
                "profile_applied": True,
                "corrections": corrections,
                "correction_count": len(corrections),
            },
        )

        self.assertEqual(corrected, "KIIRI來了。")
        self.assertEqual(fields["target_expected_canonical_terms"], ["KIIRI"])
        self.assertEqual(fields["target_missing_canonical_terms"], [])
        self.assertEqual(fields["translation_qa_flags"], [])
        self.assertEqual(fields["translation_qa_disposition"], "normalized")
        self.assertEqual(fields["correction_count"], 1)
        self.assertEqual(fields["corrections"][0]["stage"], "name_render")

    def test_irise_missing_canonical_is_diagnostic_only(self):
        outcome = TranslationOutcome(
            source_text="키리가 왔어요",
            target_text="她今天到了。",
            status="success",
            result_source="api",
            cache_status="miss",
            incomplete=False,
            engine="openrouter",
            model="example-model",
        )

        baseline = outcome.as_event_fields(
            10.0,
            {"profile_id": "irise", "profile_applied": False},
        )
        suspicious = outcome.as_event_fields(
            10.0,
            {"profile_id": "irise", "profile_applied": True},
        )

        self.assertEqual(suspicious["target_expected_canonical_terms"], ["KIIRI"])
        self.assertEqual(suspicious["target_missing_canonical_terms"], ["KIIRI"])
        self.assertEqual(
            suspicious["translation_qa_flags"],
            ["target_missing_profile_canonical"],
        )
        self.assertIn(
            "target_missing_profile_canonical",
            suspicious["quality_classifications"],
        )
        self.assertEqual(suspicious["translation_qa_disposition"], "suspicious")
        self.assertEqual(suspicious["route_id"], "openrouter:example-model")
        self.assertEqual(suspicious["quality_flags"], baseline["quality_flags"])
        self.assertEqual(suspicious["quality_score"], baseline["quality_score"])
        self.assertEqual(
            suspicious["quality_severity"],
            baseline["quality_severity"],
        )

    def test_irise_music_part_department_candidate_is_narrow_and_diagnostic(self):
        positive_sources = (
            "저의 고음 파트가 가장 매력적이에요",
            "브릿지 파트를 다시 녹음했어요",
            "이 파트는 보컬 하모니가 좋아요",
            "저희 파트를 수정했어요",
        )
        for source in positive_sources:
            with self.subTest(source=source):
                outcome = TranslationOutcome(
                    source_text=source,
                    target_text="我的部門最有魅力。",
                    status="success",
                    result_source="api",
                    cache_status="miss",
                    incomplete=False,
                )
                fields = outcome.as_event_fields(
                    10.0,
                    {"profile_id": "irise", "profile_applied": True},
                )
                self.assertEqual(
                    fields["target_profile_semantic_candidates"],
                    ["music_part_rendered_as_department"],
                )
                self.assertIn(
                    "target_profile_semantic_candidate",
                    fields["quality_classifications"],
                )
                self.assertEqual(fields["translation_qa_disposition"], "suspicious")

        negative_sources = (
            "사업 파트를 맡았어요",
            "담당 파트가 바뀌었어요",
            "고음 파트너를 만났어요",
            "랩탑 사업 파트를 맡았어요",
            "노래를 들으며 사업 파트를 맡았어요",
        )
        for source in negative_sources:
            with self.subTest(source=source):
                outcome = TranslationOutcome(
                    source_text=source,
                    target_text="這個部門變了。",
                    status="success",
                    result_source="api",
                    cache_status="miss",
                    incomplete=False,
                )
                fields = outcome.as_event_fields(
                    10.0,
                    {"profile_id": "irise", "profile_applied": True},
                )
                self.assertEqual(fields["target_profile_semantic_candidates"], [])

        outcome = TranslationOutcome(
            source_text="고음 파트가 좋아요",
            target_text="高音部門很棒。",
            status="success",
            result_source="api",
            cache_status="miss",
            incomplete=False,
        )
        for metadata in (
            {"profile_id": "url", "profile_applied": True},
            {"profile_id": "irise", "profile_applied": False},
        ):
            fields = outcome.as_event_fields(10.0, metadata)
            self.assertEqual(fields["target_profile_semantic_candidates"], [])

    def test_script_drift_uses_existing_quality_evidence_in_qa_disposition(self):
        outcome = TranslationOutcome(
            source_text="오늘 무대가 예뻐요",
            target_text="今天的舞台很キレイ。",
            status="success",
            result_source="api",
            cache_status="miss",
            incomplete=False,
        )

        fields = outcome.as_event_fields(
            10.0,
            {"profile_id": "irise", "profile_applied": True},
        )

        self.assertIn("target_has_japanese", fields["quality_flags"])
        self.assertEqual(fields["translation_qa_disposition"], "suspicious")

    def test_translation_output_guard_rejects_any_kana_or_unapproved_hangul(self):
        engine = MagicMock()
        engine.engine_name = "deepseek"

        self.assertEqual(
            _translation_output_guard(engine, "這是キリ", "키리")["reason"],
            "unexpected_japanese",
        )
        self.assertEqual(
            _translation_output_guard(engine, "這是느졋", "늦었어")["reason"],
            "unexpected_hangul",
        )

    def test_source_gated_name_render_can_rescue_its_raw_script_violation(self):
        engine = MagicMock()
        engine.engine_name = "deepseek"

        with _active_translation_profile("isegye_lilpa"):
            guard = _translation_output_guard(engine, "릴파", "릴파")

        self.assertNotIn("reason", guard)
        self.assertEqual(guard["accepted_after_name_render"], ["unexpected_hangul"])
        self.assertEqual(guard["candidate_raw_output"], "릴파")
        self.assertEqual(guard["candidate_output"], "Lilpa")
        self.assertEqual(guard["candidate_corrections"][0]["stage"], "name_render")
        self.assertIn(
            "target_has_unexpected_hangul",
            guard["candidate_raw_quality_classifications"],
        )
        self.assertNotIn(
            "target_has_unexpected_hangul",
            guard["candidate_quality_classifications"],
        )

    def test_live23_chzzk_primary_is_repaired_before_fallback(self):
        engine = MagicMock()
        engine.engine_name = "deepseek"
        source = "몇몇 애들은 치지직 가기도 하고"

        with _active_translation_profile("hades_chxxnnx"):
            guard = _translation_output_guard(
                engine,
                "有些人去 치지직 了。",
                source,
            )

        self.assertNotIn("reason", guard)
        self.assertEqual(guard["candidate_output"], "有些人去 CHZZK 了。")
        self.assertEqual(guard["accepted_after_name_render"], ["unexpected_hangul"])
        self.assertEqual(guard["canonical_obligations"]["satisfied"], ["CHZZK"])

    def test_live23_memnon_repairs_known_fallback_but_not_unrelated_hangul(self):
        engine = MagicMock()
        source = "아가 멤논급이야."

        with _active_translation_profile("hades_chxxnnx"):
            engine.engine_name = "deepseek"
            primary = _translation_output_guard(
                engine,
                "아가 是 Memnon 等級的。",
                source,
            )
            fallback = _translation_output_guard(engine, "啊，是成員級的。", source)

        self.assertEqual(primary["reason"], "unexpected_hangul")
        self.assertEqual(fallback["candidate_output"], "啊，是Memnon級的。")
        self.assertNotIn("reason", fallback)
        self.assertEqual(fallback["canonical_obligations"]["satisfied"], ["Memnon"])

    def test_live23_repeated_moka_particle_is_repaired_without_hard_obligation(self):
        engine = MagicMock()
        engine.engine_name = "deepseek"
        source = "모카랑은 모카랑은 나루토 노래 부르면서 뛰어다니고."

        with _active_translation_profile("url"):
            guard = _translation_output_guard(
                engine,
                "모카랑是邊唱火影忍者的歌邊跑。",
                source,
            )

        self.assertNotIn("reason", guard)
        self.assertEqual(guard["candidate_output"], "모카是邊唱火影忍者的歌邊跑。")
        self.assertEqual(guard["canonical_obligations"]["expected"], [])

    def test_name_render_without_source_evidence_cannot_rescue_raw_hangul(self):
        engine = MagicMock()
        engine.engine_name = "deepseek"

        with _active_translation_profile("isegye_lilpa"):
            guard = _translation_output_guard(
                engine,
                "릴파",
                "오늘 방송 재미있다",
            )

        self.assertEqual(guard["reason"], "unexpected_hangul")
        self.assertEqual(guard["candidate_output"], "릴파")
        self.assertEqual(guard["candidate_corrections"], [])

    def test_name_render_rescue_is_authoritative_for_fallback_providers(self):
        with _active_translation_profile("isegye_lilpa"):
            for engine_name in ("openrouter", "deepl", "groq"):
                engine = MagicMock()
                engine.engine_name = engine_name
                with self.subTest(engine=engine_name):
                    guard = _translation_output_guard(
                        engine,
                        "릴파",
                        "릴파",
                    )
                    self.assertNotIn("reason", guard)
                    self.assertEqual(guard["candidate_output"], "Lilpa")
                    self.assertEqual(
                        guard["accepted_after_name_render"],
                        ["unexpected_hangul"],
                    )

    def test_partial_name_render_rejects_unrelated_script_residue(self):
        engine = MagicMock()
        engine.engine_name = "deepseek"

        with _active_translation_profile("isegye_lilpa"):
            guard = _translation_output_guard(
                engine,
                "릴파와 느졋",
                "릴파가 왔어요",
            )

        self.assertEqual(guard["reason"], "unexpected_hangul")
        self.assertEqual(guard["candidate_output"], "Lilpa와 느졋")
        self.assertIn(
            "target_has_unexpected_hangul",
            guard["candidate_quality_classifications"],
        )

    def test_non_name_target_correction_cannot_rescue_flash_raw_script(self):
        engine = MagicMock()
        engine.engine_name = "deepseek"
        replacement = (("문장",), (("느졋", "太晚"),), False)

        with patch.object(
            translator_module,
            "_SOURCE_AWARE_TARGET_REPLACEMENTS",
            (replacement,),
        ):
            guard = _translation_output_guard(
                engine,
                "這是느졋",
                "이 문장입니다",
            )

        self.assertEqual(guard["candidate_output"], "這是太晚")
        self.assertEqual(
            guard["candidate_corrections"][0]["stage"],
            "target_correction",
        )
        self.assertEqual(guard["reason"], "unexpected_hangul")
        self.assertNotIn("accepted_after_name_render", guard)

    def test_corrected_script_boundary_is_provider_independent(self):
        for engine_name in ("openrouter", "deepl", "groq"):
            engine = MagicMock()
            engine.engine_name = engine_name
            with self.subTest(engine=engine_name, script="hangul"):
                self.assertEqual(
                    _translation_output_guard(
                        engine,
                        "這是느졋",
                        "늦었어",
                    )["reason"],
                    "unexpected_hangul",
                )
            with self.subTest(engine=engine_name, script="japanese"):
                self.assertEqual(
                    _translation_output_guard(
                        engine,
                        "這是テスト",
                        "테스트예요",
                    )["reason"],
                    "unexpected_japanese",
                )

    def test_production_fallback_hiatus_mistranslation_is_repaired(self):
        engine = MagicMock()
        engine.engine_name = "deepseek"

        hiatus = _translation_output_guard(
            engine,
            "現在Lilpa正在長期休眠，還特地來了。",
            "릴파님은 장기 휴방 중이신데 또 찾아와 주셔서.",
        )
        self.assertNotIn("reason", hiatus)
        self.assertEqual(
            hiatus["candidate_output"],
            "現在Lilpa正在長期休播，還特地來了。",
        )


    def test_protected_guard_allows_approved_hangul_and_preserves_trace(self):
        engine = MagicMock()
        engine.engine_name = "deepseek"
        reset_corrections()
        translator_module._record_correction("seed", "seed", "before", "after")
        before = get_corrections()

        with patch.object(
            translator_module,
            "_publication_approved_terms",
            return_value=frozenset({"해둥이"}),
        ):
            guard = _translation_output_guard(
                engine,
                "今天是해둥이的直播",
                "오늘은 해둥이 방송이에요",
            )

        self.assertNotIn("reason", guard)
        self.assertEqual(guard["candidate_output"], "今天是해둥이的直播")
        self.assertEqual(guard["candidate_corrections"], [])
        self.assertEqual(get_corrections(), before)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_engine(name: str, return_value: str = "你好") -> MagicMock:
    """Create a mock TranslationEngine with a given name and default return value."""
    engine = MagicMock(spec=TranslationEngine)
    engine.engine_name = name
    engine.model_name = f"{name}-test-model"
    engine.available = True
    engine.translate.return_value = return_value
    return engine


def _route_engine(name: str, return_value: str) -> MagicMock:
    """Mock either a frozen-capsule or ordinary fallback route."""
    engine = MagicMock()
    engine.engine_name = name
    engine.model_name = f"{name}-model"
    engine.available = True
    engine.translate.return_value = return_value
    engine.translate_messages.return_value = return_value
    return engine


def _set_provider_failure(
    engine: MagicMock,
    *,
    error_type: str = "timeout",
    message_class: str = "read_timeout",
) -> None:
    def fail(*_args):
        translation_engines_module._set_last_engine_diagnostics(
            str(engine.engine_name),
            api_attempt_count=1,
            api_error_type=error_type,
            api_error_message_class=message_class,
        )
        return None

    engine.translate.side_effect = fail


def _make_translator() -> Translator:
    """Build a Translator backed by mock engines — no real API clients."""
    from modules.translator import _CACHE_MAX_SIZE
    from modules.translation_policy import TranslationPolicy
    from modules.translation_memory import TranslationMemory
    from config import cfg
    t = Translator.__new__(Translator)
    t._active_idx = 0
    t._consecutive_primary_failures = 0
    t._last_input = ""
    t._engines = [_mock_engine(name) for name in ("gemini", "claude")]
    t._policy = TranslationPolicy(
        slang=cfg.translation.slang,
        min_translate_chars=2,
        repetition_confidence_exempt_enabled=(
            cfg.translation.repetition_confidence_exempt_enabled
        ),
        repetition_avg_logprob_threshold=cfg.stt.context_avg_logprob_threshold,
        repetition_no_speech_threshold=cfg.stt.context_no_speech_threshold,
    )
    t._memory = TranslationMemory(
        recent_window=3,
        max_cache_size=_CACHE_MAX_SIZE,
        db_factory=lambda: _NoOpDB(),
        history_writer=MagicMock(),
    )
    return t


def _claude_resp(text: str) -> MagicMock:
    r = MagicMock()
    r.content = [MagicMock(text=text)]
    return r


def _sys_prompt(t: "Translator") -> str:
    return _BASE_PROMPT


@contextmanager
def _active_translation_profile(profile_id: str, use_profile: bool = True):
    from config import cfg

    original_profile = cfg.translation.streamer_profile
    original_use_profile = cfg.translation.use_profile
    object.__setattr__(cfg.translation, "streamer_profile", profile_id)
    object.__setattr__(cfg.translation, "use_profile", use_profile)
    try:
        yield
    finally:
        object.__setattr__(cfg.translation, "streamer_profile", original_profile)
        object.__setattr__(cfg.translation, "use_profile", original_use_profile)


@contextmanager
def _translation_mode(mode: str):
    from config import cfg

    original_mode = cfg.translation.translation_mode
    object.__setattr__(cfg.translation, "translation_mode", mode)
    try:
        yield
    finally:
        object.__setattr__(cfg.translation, "translation_mode", original_mode)


@contextmanager
def _live_backend(name: str):
    from config import cfg

    original_backend = cfg.live_engine
    object.__setattr__(cfg, "live_engine", name)
    try:
        yield
    finally:
        object.__setattr__(cfg, "live_engine", original_backend)


# ---------------------------------------------------------------------------
# Correction recording (source normalization / target corrections)
# ---------------------------------------------------------------------------

class TestCorrectionRecording(unittest.TestCase):
    def setUp(self):
        reset_corrections()

    def test_source_normalization_records_rule_before_after(self):
        with _active_translation_profile("stellive_hina"):
            out = _normalize_source_before_matching("해동이 안녕")

        self.assertEqual(out, "해둥이 안녕")
        recs = get_corrections()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["stage"], "source_norm")
        self.assertEqual(recs[0]["before"], "해동이")
        self.assertEqual(recs[0]["after"], "해둥이")

    def test_shared_target_correction_records(self):
        # 어금니 ("molar") present in source -> mistranslated 牙齦 ("gum") fixed.
        out = _apply_source_aware_corrections("어금니 아파요", "牙齦很痛")

        self.assertEqual(out, "臼齒很痛")
        recs = get_corrections()
        self.assertTrue(
            any(r["stage"] == "target_correction" and r["before"] == "牙齦" and r["after"] == "臼齒"
                for r in recs)
        )

    def test_profile_target_correction_records_海洞_rescue(self):
        with _active_translation_profile("stellive_hina"):
            out = _apply_source_aware_corrections("해둥이들 안녕", "海洞們 你好")

        self.assertIn("해둥이們", out)
        recs = get_corrections()
        self.assertTrue(any(r["after"] == "해둥이們" for r in recs))

    def test_no_correction_records_nothing(self):
        out = _apply_source_aware_corrections("일반적인 문장입니다", "這是一個普通的句子")

        self.assertEqual(out, "這是一個普通的句子")
        self.assertEqual(get_corrections(), [])


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

class TestBuildBasePrompt(unittest.TestCase):

    def test_contains_target_lang(self):
        from config import cfg
        self.assertIn(cfg.translation.target_lang, _build_base_prompt())

    def test_contains_all_slang(self):
        from config import cfg
        prompt = _build_base_prompt()
        for ko, zh in cfg.translation.slang.items():
            self.assertIn(ko, prompt)
            self.assertIn(zh, prompt)

    def test_contains_rules(self):
        prompt = _build_base_prompt()
        self.assertIn("Output the translation only", prompt)
        self.assertIn("Preserve As-Is", prompt)
        self.assertIn("STT", prompt)

    def test_live_mode_stt_section(self):
        from config import cfg
        orig = cfg.translation.translation_mode
        object.__setattr__(cfg.translation, "translation_mode", "live")
        try:
            prompt = _build_base_prompt()
            self.assertIn("STT Correction - Live Mode", prompt)
            self.assertNotIn("STT Correction - Clip Mode", prompt)
        finally:
            object.__setattr__(cfg.translation, "translation_mode", orig)

    def test_clip_mode_stt_section(self):
        from config import cfg
        orig = cfg.translation.translation_mode
        object.__setattr__(cfg.translation, "translation_mode", "clip")
        try:
            prompt = _build_base_prompt()
            self.assertIn("STT Correction - Clip Mode", prompt)
            self.assertNotIn("STT Correction - Live Mode", prompt)
        finally:
            object.__setattr__(cfg.translation, "translation_mode", orig)

    def test_get_translation_profile_returns_profile_text(self):
        self.assertTrue(get_translation_profile("hades_chxxnnx"))

    def test_get_translation_profile_unknown_returns_empty(self):
        self.assertEqual(get_translation_profile("unknown"), "")

    def test_get_translation_profile_supports_qwen_profiles(self):
        self.assertTrue(get_translation_profile("hades_chxxnnx", qwen=True))

    def test_translation_profile_ids_match_streamer_registry(self):
        from modules.streamer_profiles import known_profile_ids

        expected = known_profile_ids() - {""}
        self.assertEqual(translation_profile_ids(), expected)
        self.assertEqual(translation_profile_ids(qwen=True), expected)

    def test_translator_uses_translation_profile_helper(self):
        translator = _make_translator()

        with patch("modules.translator.get_translation_profile", return_value="PROFILE TEXT") as helper:
            prompt = translator._build_system_prompt()

        self.assertIn("PROFILE TEXT", prompt)
        helper.assert_called_once()

    def test_translator_skips_profile_helper_when_profiles_disabled(self):
        from config import cfg

        translator = _make_translator()
        orig = cfg.translation.use_profile
        object.__setattr__(cfg.translation, "use_profile", False)
        try:
            with patch("modules.translator.get_translation_profile") as helper:
                prompt = translator._build_system_prompt()
        finally:
            object.__setattr__(cfg.translation, "use_profile", orig)

        self.assertNotIn("PROFILE TEXT", prompt)
        helper.assert_not_called()


class TestBuildUserMessage(unittest.TestCase):

    def test_contains_text(self):
        msg = _build_user_message("안녕하세요", incomplete=False)
        self.assertIn("안녕하세요", msg)
        self.assertNotIn("incomplete", msg)

    def test_incomplete_flag_present(self):
        self.assertIn("incomplete", _build_user_message("게임 하고", incomplete=True))

    def test_complete_no_incomplete_flag(self):
        self.assertNotIn("incomplete", _build_user_message("안녕하세요", incomplete=False))



# ---------------------------------------------------------------------------
# ClaudeEngine unit tests
# ---------------------------------------------------------------------------

class TestClaudeEngine(unittest.TestCase):

    def _make_engine(self, resp_text: str = "你好", side_effect=None) -> ClaudeEngine:
        e = ClaudeEngine.__new__(ClaudeEngine)
        e._client = MagicMock()
        e._timeout = 5.0
        if side_effect:
            e._client.messages.create.side_effect = side_effect
        else:
            e._client.messages.create.return_value = _claude_resp(resp_text)
        return e

    def test_returns_translated_text(self):
        e = self._make_engine("你好")
        self.assertEqual(e.translate("안녕하세요", _sys_prompt(_make_translator()), False), "你好")

    def test_strips_whitespace(self):
        e = self._make_engine("  你好  ")
        self.assertEqual(e.translate("안녕하세요", _sys_prompt(_make_translator()), False), "你好")

    def test_returns_none_on_exception(self):
        e = self._make_engine(side_effect=Exception("API down"))
        self.assertIsNone(e.translate("안녕하세요", _sys_prompt(_make_translator()), False))

    def test_timeout_exposes_route_cap_and_provider_diagnostics(self):
        e = self._make_engine(side_effect=TimeoutError("timed out"))

        self.assertIsNone(
            e.translate("안녕하세요", _sys_prompt(_make_translator()), False)
        )

        diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(e.request_timeout_seconds, 5.0)
        self.assertEqual(diagnostics["api_attempt_count"], 1)
        self.assertEqual(diagnostics["api_timeout_count"], 1)
        self.assertEqual(diagnostics["api_error_type"], "timeout")
        self.assertEqual(
            diagnostics["api_error_message_class"],
            "read_timeout",
        )

    def test_http_status_diagnostics_distinguish_provider_and_auth(self):
        class StatusError(Exception):
            def __init__(self, status_code):
                super().__init__(f"HTTP {status_code}")
                self.status_code = status_code

        for status_code, expected_class in ((503, "http_5xx"), (401, "http_4xx")):
            with self.subTest(status_code=status_code):
                e = self._make_engine(side_effect=StatusError(status_code))
                self.assertIsNone(e.translate("안녕하세요", "system", False))
                self.assertEqual(
                    get_last_engine_api_diagnostics()[
                        "api_error_message_class"
                    ],
                    expected_class,
                )

    def test_returns_none_when_client_is_none(self):
        e = ClaudeEngine.__new__(ClaudeEngine)
        e._client = None
        self.assertIsNone(e.translate("안녕하세요", "system", False))




# ---------------------------------------------------------------------------
# GoogleTranslateEngine unit tests
# ---------------------------------------------------------------------------

class TestGoogleTranslateEngine(unittest.TestCase):

    def _call(self, resp_text: str = "你好", side_effect=None, text: str = "안녕하세요") -> "str | None":
        import json
        e = GoogleTranslateEngine.__new__(GoogleTranslateEngine)
        e._api_key = "fake-key"
        e._target_lang = "zh-TW"
        e._timeout = 5.0
        if side_effect:
            with patch("urllib.request.urlopen", side_effect=side_effect):
                return e.translate(text, "system", False)
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps(
            {"data": {"translations": [{"translatedText": resp_text}]}}
        ).encode()
        with patch("urllib.request.urlopen", return_value=mock_resp):
            return e.translate(text, "system", False)

    def test_returns_translated_text(self):
        self.assertEqual(self._call("你好"), "你好")

    def test_strips_whitespace(self):
        self.assertEqual(self._call("  你好  "), "你好")

    def test_returns_none_on_exception(self):
        self.assertIsNone(self._call(side_effect=Exception("network down")))

    def test_timeout_exposes_route_cap_and_provider_diagnostics(self):
        import socket
        import urllib.error

        e = GoogleTranslateEngine.__new__(GoogleTranslateEngine)
        e._api_key = "fake-key"
        e._target_lang = "zh-TW"
        e._timeout = 5.0
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(socket.timeout("timed out")),
        ):
            self.assertIsNone(e.translate("안녕하세요", "system", False))

        diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(e.request_timeout_seconds, 5.0)
        self.assertEqual(diagnostics["api_attempt_count"], 1)
        self.assertEqual(diagnostics["api_timeout_count"], 1)
        self.assertEqual(diagnostics["api_error_type"], "timeout")
        self.assertEqual(
            diagnostics["api_error_message_class"],
            "read_timeout",
        )

    def test_http_status_diagnostics_distinguish_provider_and_auth(self):
        import urllib.error

        for status_code, expected_class in ((503, "http_5xx"), (401, "http_4xx")):
            with self.subTest(status_code=status_code):
                error = urllib.error.HTTPError(
                    "https://translation.googleapis.com",
                    status_code,
                    "failure",
                    {},
                    None,
                )
                self.assertIsNone(self._call(side_effect=error))
                self.assertEqual(
                    get_last_engine_api_diagnostics()[
                        "api_error_message_class"
                    ],
                    expected_class,
                )

    def test_returns_none_when_api_key_empty(self):
        e = GoogleTranslateEngine.__new__(GoogleTranslateEngine)
        e._api_key = ""
        e._target_lang = "zh-TW"
        self.assertIsNone(e.translate("안녕하세요", "system", False))

    def test_ignores_system_prompt_and_incomplete(self):
        # Direct-translation engine — system_prompt / incomplete do not affect output
        self.assertEqual(self._call("你好", text="안녕하세요"), "你好")
        self.assertEqual(self._call("你好", text="안녕하세요"), "你好")


# ---------------------------------------------------------------------------
# DeepLEngine unit tests
# ---------------------------------------------------------------------------

class TestDeepLEngine(unittest.TestCase):

    def _engine(self, api_key: str = "fake-key:fx") -> DeepLEngine:
        e = DeepLEngine.__new__(DeepLEngine)
        e._api_key = api_key
        e._target_lang = "ZH-HANT"
        e._timeout = 4.0
        e._base_url = _deepl_base_url(api_key)
        return e

    @staticmethod
    def _response(text: str):
        import json

        response = MagicMock()
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        response.read.return_value = json.dumps({
            "translations": [{"text": text, "billed_characters": 8}],
        }).encode()
        return response

    def test_free_key_uses_free_endpoint_and_short_context(self):
        import json

        engine = self._engine()
        with patch("urllib.request.urlopen", return_value=self._response("大家好")) as urlopen:
            result = engine.translate("안녕하세요", "ignored system prompt", False,
                                      [("방송 시작", "直播開始")])

        self.assertEqual(result, "大家好")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api-free.deepl.com/v2/translate")
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["authorization"], "DeepL-Auth-Key fake-key:fx")
        self.assertEqual(headers["accept"], "application/json")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["source_lang"], "KO")
        self.assertEqual(payload["target_lang"], "ZH-HANT")
        self.assertIn("Recent subtitle: 방송 시작 -> 直播開始.", payload["context"])
        self.assertNotIn("custom_instructions", payload)

    def test_uses_certifi_ssl_context(self):
        engine = self._engine()
        ssl_context = object()
        with patch.object(
            translation_engines_module,
            "_deepl_ssl_context",
            return_value=ssl_context,
        ), patch(
            "urllib.request.urlopen",
            return_value=self._response("ok"),
        ) as urlopen:
            self.assertEqual(engine.translate("hello", "system", False), "ok")

        self.assertIs(urlopen.call_args.kwargs["context"], ssl_context)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 4.0)

    def test_certifi_ssl_context_keeps_verification_enabled(self):
        import ssl

        translation_engines_module._deepl_ssl_context.cache_clear()
        try:
            context = translation_engines_module._deepl_ssl_context()
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertGreater(context.cert_store_stats()["x509_ca"], 0)
        finally:
            translation_engines_module._deepl_ssl_context.cache_clear()

    def test_pro_key_uses_pro_endpoint(self):
        self.assertEqual(_deepl_base_url("fake-key"), "https://api.deepl.com/v2")

    def test_selects_direct_api_source_language_from_text(self):
        import json

        engine = self._engine()
        cases = (
            ("오늘은 즐거웠어요", "KO"),
            ("Today was really fun", "EN"),
            ("今日はとても楽しかったです", "JA"),
            ("URL 멤버", "KO"),
        )
        for source, expected in cases:
            with self.subTest(source=source), patch(
                "urllib.request.urlopen", return_value=self._response("翻譯")
            ) as urlopen:
                self.assertEqual(engine.translate(source, "system", False), "翻譯")
                payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
                self.assertEqual(payload["source_lang"], expected)

    def test_returns_none_without_key(self):
        engine = self._engine("")
        with patch("urllib.request.urlopen") as urlopen:
            self.assertIsNone(engine.translate("안녕하세요", "system", False))
        urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# GroqTranslationEngine unit tests
# ---------------------------------------------------------------------------

class TestGroqTranslationEngine(unittest.TestCase):

    def _engine(self) -> GroqTranslationEngine:
        e = GroqTranslationEngine.__new__(GroqTranslationEngine)
        e._api_key = "fake-key"
        e._model = "qwen/qwen3-32b"
        e._timeout = 12
        e._max_tokens = 512
        e._retry_max_tokens = 256
        e._strip_think = True
        return e

    def _response(self, content: str):
        import json
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode()
        return mock_resp

    def test_request_includes_headers_required_by_groq_edge(self):
        e = self._engine()

        with patch("urllib.request.urlopen", return_value=self._response("ok")) as urlopen:
            result = e.translate("hello", "system", False)

        self.assertEqual(result, "ok")
        req = urlopen.call_args.args[0]
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["user-agent"], "live_translate/1.0")
        self.assertEqual(headers["authorization"], "Bearer fake-key")

    def test_gpt_oss_request_uses_low_reasoning_effort(self):
        import json

        e = self._engine()
        e._model = "openai/gpt-oss-120b"
        e._strip_think = False

        with patch("urllib.request.urlopen", return_value=self._response("ok")) as urlopen:
            result = e.translate("hello", "system", False)

        self.assertEqual(result, "ok")
        payload = json.loads(urlopen.call_args.args[0].data.decode())
        self.assertEqual(payload["reasoning_effort"], "low")

    def test_non_gpt_oss_request_omits_reasoning_effort(self):
        import json

        e = self._engine()

        with patch("urllib.request.urlopen", return_value=self._response("ok")) as urlopen:
            result = e.translate("hello", "system", False)

        self.assertEqual(result, "ok")
        payload = json.loads(urlopen.call_args.args[0].data.decode())
        self.assertNotIn("reasoning_effort", payload)

    def test_uses_compact_prompt_by_default(self):
        import json

        e = self._engine()
        long_prompt = "full quality prompt " * 500

        with patch("urllib.request.urlopen", return_value=self._response("ok")) as urlopen:
            result = e.translate("hello", long_prompt, False)

        self.assertEqual(result, "ok")
        req = urlopen.call_args.args[0]
        payload = json.loads(req.data.decode())
        system_message = payload["messages"][0]["content"]
        self.assertIn("Traditional Chinese live subtitle translator", system_message)
        self.assertIn("Korean, English, or Japanese", system_message)
        self.assertNotIn("full quality prompt", system_message)

    def test_strips_closed_and_unclosed_think_blocks(self):
        e = self._engine()

        with patch("urllib.request.urlopen", return_value=self._response("<think>notes</think>你好")):
            self.assertEqual(e.translate("hello", "system", False), "你好")

        with patch("urllib.request.urlopen", return_value=self._response("<think>unfinished notes")):
            self.assertIsNone(e.translate("hello", "system", False))

    def test_limits_history_and_tokens_for_groq_fallback(self):
        import json
        from config import cfg

        e = self._engine()
        history = [
            (f"source-{i}-" + "x" * 240, f"target-{i}-" + "y" * 300)
            for i in range(4)
        ]
        original_window = cfg.translation.groq_translation_context_window

        try:
            object.__setattr__(cfg.translation, "groq_translation_context_window", 2)
            with patch("urllib.request.urlopen", return_value=self._response("ok")) as urlopen:
                result = e.translate("hello", "system", False, history)
        finally:
            object.__setattr__(
                cfg.translation,
                "groq_translation_context_window",
                original_window,
            )

        self.assertEqual(result, "ok")
        req = urlopen.call_args.args[0]
        payload = json.loads(req.data.decode())
        messages = payload["messages"]

        self.assertEqual(payload["max_tokens"], 512)
        self.assertEqual(len(messages), 6)
        self.assertNotIn("source-1-", str(messages))
        self.assertIn("source-2-", messages[1]["content"])
        self.assertIn("source-3-", messages[3]["content"])
        self.assertTrue(messages[-1]["content"].startswith("/no_think\ninput:"))
        self.assertLessEqual(len(messages[1]["content"]), len("input: ") + 163)
        self.assertLessEqual(len(messages[2]["content"]), 223)

    def test_retries_413_token_limit_without_history(self):
        import io
        import json
        import urllib.error
        from config import cfg

        e = self._engine()
        history = [("source", "target")]
        original_window = cfg.translation.groq_translation_context_window
        error = urllib.error.HTTPError(
            url="https://api.groq.com/openai/v1/chat/completions",
            code=413,
            msg="request too large",
            hdrs=None,
            fp=io.BytesIO(b'{"error":{"message":"TPM token limit","code":"rate_limit_exceeded"}}'),
        )

        try:
            object.__setattr__(cfg.translation, "groq_translation_context_window", 1)
            with patch("urllib.request.urlopen", side_effect=[error, self._response("ok")]) as urlopen:
                result = e.translate("hello", "system", False, history)
        finally:
            object.__setattr__(
                cfg.translation,
                "groq_translation_context_window",
                original_window,
            )

        self.assertEqual(result, "ok")
        self.assertEqual(urlopen.call_count, 2)

        first_req = urlopen.call_args_list[0].args[0]
        retry_req = urlopen.call_args_list[1].args[0]
        first_payload = json.loads(first_req.data.decode())
        retry_payload = json.loads(retry_req.data.decode())

        self.assertEqual(len(first_payload["messages"]), 4)
        self.assertEqual(len(retry_payload["messages"]), 2)
        self.assertEqual(retry_payload["max_tokens"], 256)


# ---------------------------------------------------------------------------
# OpenRouterTranslationEngine unit tests
# ---------------------------------------------------------------------------

class TestOpenRouterTranslationEngine(unittest.TestCase):

    def _engine(self) -> OpenRouterTranslationEngine:
        e = OpenRouterTranslationEngine.__new__(OpenRouterTranslationEngine)
        e._api_key = "fake-openrouter-key"
        e._model = "qwen/qwen3-next-80b-a3b-instruct"
        e._timeout = 8
        e._max_tokens = 200
        e._strip_think = True
        return e

    def _response(self, content):
        import json
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": content}}],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
                "cost": 0.00012345,
            },
        }).encode()
        return mock_resp

    def test_request_uses_openrouter_endpoint_headers_and_selected_model(self):
        import json

        e = self._engine()
        long_prompt = "full quality prompt " * 500

        with _active_translation_profile("url"), \
                patch("urllib.request.urlopen", return_value=self._response("translated")) as urlopen:
            result = e.translate("source", long_prompt, False)

        self.assertEqual(result, "translated")
        req = urlopen.call_args.args[0]
        headers = {k.lower(): v for k, v in req.header_items()}
        payload = json.loads(req.data.decode())

        self.assertEqual(req.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["authorization"], "Bearer fake-openrouter-key")
        self.assertEqual(headers["user-agent"], "live_translate/1.0")
        self.assertEqual(headers["http-referer"], "http://localhost/live_translate")
        self.assertEqual(headers["x-title"], "live_translate")
        self.assertEqual(payload["model"], "qwen/qwen3-next-80b-a3b-instruct")
        self.assertEqual(payload["reasoning"], {"effort": "none", "exclude": True})
        self.assertEqual(payload["max_tokens"], 200)
        system_message = payload["messages"][0]["content"]
        self.assertIn("noisy live-stream subtitles", system_message)
        self.assertIn("Korean, English, or Japanese", system_message)
        self.assertIn("[Active profile facts]", system_message)
        self.assertIn("유아렐/유아엘=UR:L", system_message)
        self.assertNotIn("full quality prompt", system_message)
        self.assertEqual(
            get_last_token_usage(),
            {
                "prompt": 11,
                "output": 7,
                "total": 18,
                "cache_read": None,
                "cache_write": None,
            },
        )
        diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(diagnostics["engine"], "openrouter")
        self.assertEqual(diagnostics["api_attempt_count"], 1)
        self.assertEqual(diagnostics["timeout_config_ms"], 8000)
        self.assertEqual(diagnostics["api_cost_usd"], 0.00012345)

    def test_limits_history_for_live_fallback(self):
        import json
        from config import cfg

        e = self._engine()
        history = [
            (f"source-{i}-" + "x" * 240, f"target-{i}-" + "y" * 300)
            for i in range(4)
        ]
        original_window = cfg.translation.openrouter_context_window

        try:
            object.__setattr__(cfg.translation, "openrouter_context_window", 2)
            with patch("urllib.request.urlopen", return_value=self._response("ok")) as urlopen:
                result = e.translate("hello", "system", False, history)
        finally:
            object.__setattr__(cfg.translation, "openrouter_context_window", original_window)

        self.assertEqual(result, "ok")
        req = urlopen.call_args.args[0]
        payload = json.loads(req.data.decode())
        messages = payload["messages"]

        self.assertEqual(len(messages), 6)
        self.assertNotIn("source-1-", str(messages))
        self.assertIn("source-2-", messages[1]["content"])
        self.assertIn("source-3-", messages[3]["content"])
        self.assertIn("[CONTEXT ONLY — DO NOT TRANSLATE OR REPEAT]", messages[1]["content"])
        self.assertNotIn("[CONTEXT", messages[2]["content"])
        self.assertTrue(messages[2]["content"].startswith("target-2-"))
        self.assertIn("[CURRENT INPUT — TRANSLATE ONLY THIS]", messages[-1]["content"])
        self.assertLessEqual(
            len(messages[1]["content"]),
            len("[CONTEXT ONLY — DO NOT TRANSLATE OR REPEAT]\nsource: ") + 163,
        )
        self.assertLessEqual(len(messages[2]["content"]), 223)

    def test_incomplete_current_input_forbids_clause_completion(self):
        import json

        e = self._engine()
        with patch("urllib.request.urlopen", return_value=self._response("片段")) as urlopen:
            result = e.translate("제가 저거 예전에...", "system", True)

        self.assertEqual(result, "片段")
        req = urlopen.call_args.args[0]
        current = json.loads(req.data.decode())["messages"][-1]["content"]
        self.assertIn("[CURRENT INPUT — TRANSLATE ONLY THIS]", current)
        self.assertIn("translate only the meaning that is present", current)
        self.assertIn("do not complete the missing clause", current)
        self.assertNotIn("translate as best as possible", current)

    def test_published_activity_reaches_production_openrouter_capsule(self):
        import json
        import time
        from config import cfg
        from modules.activity_context import (
            AutomaticActivityPublication,
            activity_publication_store,
        )
        from modules.translation_engines import engine_chain_config_key

        e = self._engine()
        translator = Translator()
        translator._engines = [e]
        translator._engines_key = engine_chain_config_key()
        original_manual = cfg.translation.current_activity
        original_enabled = cfg.scene.publish_translation_activity
        activity_publication_store.replace(
            AutomaticActivityPublication(
                activity_id="league_of_legends",
                display_label="League of Legends",
                confirmed_at_utc="2026-08-12T00:00:00+00:00",
                fresh_until_monotonic=time.monotonic() + 60,
                confidence=1.0,
                evidence_count=2,
                activity_kind="game",
            )
        )
        object.__setattr__(cfg.translation, "current_activity", "")
        object.__setattr__(cfg.scene, "publish_translation_activity", True)
        try:
            with patch(
                "urllib.request.urlopen",
                return_value=self._response("現在回城吧"),
            ) as urlopen:
                outcome = translator.translate_event("집 가자, 지금은 귀환이야")
        finally:
            object.__setattr__(
                cfg.scene,
                "publish_translation_activity",
                original_enabled,
            )
            object.__setattr__(cfg.translation, "current_activity", original_manual)
            activity_publication_store.replace(None)

        self.assertEqual(outcome.status, "success")
        payload = json.loads(urlopen.call_args.args[0].data.decode())
        system = payload["messages"][0]["content"]
        current = payload["messages"][-1]["content"]
        self.assertEqual(system.count("Current stream activity: League of Legends"), 1)
        self.assertIn("집=recall/base", system)
        self.assertIn("Never translate, mention, or copy it", system)
        self.assertNotIn("League of Legends", current)
        self.assertIn("집 가자, 지금은 귀환이야", current)

    def test_returns_none_for_reasoning_only_or_empty_content(self):
        e = self._engine()

        with patch("urllib.request.urlopen", return_value=self._response(None)):
            self.assertIsNone(e.translate("hello", "system", False))

        with patch("urllib.request.urlopen", return_value=self._response("<think>notes only")):
            self.assertIsNone(e.translate("hello", "system", False))

    def test_timeout_records_structured_diagnostics(self):
        import socket
        import urllib.error

        e = self._engine()
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(socket.timeout("timed out")),
        ):
            self.assertIsNone(e.translate("hello", "system", False))

        diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(diagnostics["engine"], "openrouter")
        self.assertEqual(diagnostics["api_attempt_count"], 1)
        self.assertEqual(diagnostics["api_timeout_count"], 1)
        self.assertEqual(diagnostics["timeout_config_ms"], 8000)
        self.assertEqual(diagnostics["api_error_type"], "timeout")
        self.assertEqual(diagnostics["api_error_message_class"], "read_timeout")


class TestOpenRouterFallbackChain(unittest.TestCase):
    def test_unknown_name_escrow_rejects_invention_and_reuses_mapping_on_fallback(self):
        primary = _route_engine("deepseek", "這件事跟師玉老師談的話。")
        fallback = _route_engine(
            "openrouter",
            "這件事跟__LT_UNK_1__談的話，他會說你還不行。",
        )
        translator = _make_translator()
        translator._engines = [primary, fallback]
        translation_engines_module.reset_translation_call_trace()

        outcome = translator.translate_event(
            "\uadf8\ub7f0 \uac70\ub97c \uc0ac\uc625\uc324\uc774\ub791 \uc598\uae30\ud558\uba74 \ub10c \uc544\uc9c1 \uc548 \ub3fc",
            False,
        )

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.engine, "openrouter")
        self.assertIn("\uc0ac\uc625\uc324", outcome.target_text)
        self.assertNotIn("師玉", outcome.target_text)
        attempts = translation_engines_module.get_translation_attempts()
        self.assertEqual(attempts[0]["status"], "rejected_output")
        self.assertEqual(
            attempts[0]["output_guard"]["reason"],
            "unknown_name_placeholder_invalid",
        )
        self.assertEqual(attempts[1]["status"], "success")
        self.assertEqual(translator._active_idx, 0)
        current_message = fallback.translate_messages.call_args.args[0][-1][1]
        self.assertIn("__LT_UNK_1__", current_message)
        self.assertNotIn("\uc0ac\uc625\uc324", current_message)

    def test_mixed_known_canonical_and_unknown_escrow_both_remain_required(self):
        translator = _make_translator()
        translator._engines = [
            _route_engine(
                "deepseek",
                "랑코和__LT_UNK_1__今天都吃了牛肉。",
            )
        ]

        with _active_translation_profile("url"):
            outcome = translator.translate_event(
                "랑코가 푸코도 오늘 소 먹었네요",
                False,
            )

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.target_text, "랑코和푸코今天都吃了牛肉。")
        self.assertEqual(
            outcome.canonical_obligation_evaluation.satisfied,
            ("랑코",),
        )
        event = outcome.as_event_fields(
            1.0,
            {"profile_id": "url", "profile_applied": True},
        )
        self.assertEqual(event["target_unknown_name_escrow_terms"], ["푸코"])
        self.assertNotIn(
            "target_has_unexpected_hangul",
            event["quality_classifications"],
        )

    def test_all_placeholder_mutations_fail_without_weakening_script_guard(self):
        translator = _make_translator()
        translator._engines = [
            _route_engine("deepseek", "得去找莫奇。"),
            _route_engine("openrouter", "得去找Mochi。"),
            _route_engine("deepl", "得去找__LT_UNKNOWN_1__。"),
            _route_engine("groq", "得去找__LT_UNK_1____LT_UNK_1__。"),
        ]
        translation_engines_module.reset_translation_call_trace()

        outcome = translator.translate_event("모찌한테 가야 돼", False)

        self.assertEqual(outcome.status, "failed")
        self.assertIsNone(outcome.target_text)
        attempts = translation_engines_module.get_translation_attempts()
        self.assertTrue(all(row["status"] == "rejected_output" for row in attempts))
        self.assertTrue(
            all(
                row["output_guard"]["reason"]
                == "unknown_name_placeholder_invalid"
                for row in attempts
            )
        )

    def test_confirmed_unknown_name_matrix_restores_exact_source_spelling(self):
        cases = (
            ("그런 거를 사옥쌤이랑 얘기하면", "跟__LT_UNK_1__說的話", "사옥쌤"),
            ("푸코도 같이 가요", "__LT_UNK_1__也一起去", "푸코"),
            ("저는 푸순이에요", "我是__LT_UNK_1__", "푸순"),
            ("모찌한테 가야 돼", "得去找__LT_UNK_1__", "모찌"),
        )
        for source, candidate, expected_name in cases:
            with self.subTest(source=source):
                translator = _make_translator()
                translator._engines = [_route_engine("deepseek", candidate)]

                outcome = translator.translate_event(source, False)

                self.assertEqual(outcome.status, "success")
                self.assertIn(expected_name, outcome.target_text)
                self.assertNotIn("__LT_", outcome.target_text)

    def test_placeholder_plus_invented_alias_is_content_rejection(self):
        translator = _make_translator()
        translator._engines = [
            _route_engine("deepseek", "去找__LT_UNK_1__，也叫Mochi。"),
            _route_engine("openrouter", "去找__LT_UNK_1__，也叫莫奇。"),
        ]
        translation_engines_module.reset_translation_call_trace()

        outcome = translator.translate_event("모찌한테 가야 돼", False)

        self.assertEqual(outcome.status, "failed")
        attempts = translation_engines_module.get_translation_attempts()
        self.assertTrue(all(row["status"] == "rejected_output" for row in attempts))
        self.assertEqual(
            [row["output_guard"]["unknown_name_escrow"]["invented_aliases"] for row in attempts],
            [["Mochi"], ["莫奇"]],
        )

    def test_canonical_obligation_rejects_primary_and_accepts_fallback_without_health_change(self):
        primary = _route_engine("deepseek", "她來了。")
        fallback = _route_engine("openrouter", "모카來了。")
        translator = _make_translator()
        translator._engines = [primary, fallback]
        translator._active_idx = 0
        translation_engines_module.reset_translation_call_trace()

        with _active_translation_profile("url"):
            outcome = translator.translate_event("모카가 왔어", False)

        self.assertEqual(outcome.target_text, "모카來了。")
        self.assertEqual(outcome.engine, "openrouter")
        self.assertEqual(translator._active_idx, 0)
        attempts = translation_engines_module.get_translation_attempts()
        self.assertEqual(attempts[0]["status"], "rejected_output")
        self.assertEqual(attempts[0]["failure_scope"], "content")
        self.assertEqual(
            attempts[0]["output_guard"]["reason"],
            "canonical_obligation_missing",
        )
        evidence = attempts[0]["output_guard"]["canonical_obligations"]
        self.assertEqual(evidence["expected"], ["모카"])
        self.assertEqual(evidence["missing"], ["모카"])
        self.assertEqual(evidence["obligations"][0]["matched_alias"], "모카")
        self.assertEqual(evidence["obligations"][0]["source_spans"], [[0, 2]])

    def test_arbitrary_unknown_rendering_cannot_satisfy_required_canonical(self):
        translator = _make_translator()
        translator._engines = [
            _route_engine("deepseek", "Moqaa來了。"),
            _route_engine("openrouter", "她來了。"),
        ]
        translation_engines_module.reset_translation_call_trace()

        with _active_translation_profile("url"):
            outcome = translator.translate_event("모카가 왔어", False)

        self.assertEqual(outcome.status, "failed")
        self.assertIsNone(outcome.target_text)
        attempts = translation_engines_module.get_translation_attempts()
        self.assertTrue(all(row["status"] == "rejected_output" for row in attempts))
        self.assertTrue(all(row["failure_scope"] == "content" for row in attempts))
        self.assertFalse(any(row["selected_for_output"] for row in attempts))

    def test_known_wrong_form_is_repaired_before_obligation_acceptance(self):
        translator = _make_translator()
        translator._engines = [
            _route_engine("deepseek", "摩卡來了。"),
            _route_engine("openrouter", "不應被呼叫"),
        ]
        translation_engines_module.reset_translation_call_trace()

        with _active_translation_profile("url"):
            outcome = translator.translate_event("모카가 왔어", False)

        self.assertEqual(outcome.target_text, "모카來了。")
        translator._engines[1].translate_messages.assert_not_called()
        guard = translation_engines_module.get_translation_attempts()[0]["output_guard"]
        self.assertTrue(guard["canonical_obligations"]["passed"])
        self.assertEqual(guard["candidate_output"], "모카來了。")

    def test_wrong_profile_boundary_and_repeated_source_do_not_activate_v1(self):
        cases = (
            ("irise", "모카가 왔어"),
            ("url", "마냥히 웃었어"),
            ("url", "모카랑 모카가 왔어"),
        )
        for profile_id, source in cases:
            with self.subTest(profile=profile_id, source=source):
                translator = _make_translator()
                translator._engines = [_route_engine("deepseek", "她來了。")]
                translation_engines_module.reset_translation_call_trace()
                with _active_translation_profile(profile_id):
                    outcome = translator.translate_event(source, False)
                self.assertEqual(outcome.status, "success")
                self.assertEqual(
                    outcome.canonical_obligation_evaluation.expected,
                    (),
                )

    def test_collision_prone_ordinary_word_does_not_create_hard_obligation(self):
        translator = _make_translator()
        natural_target = "所以就隨意一點，用『Let's go』那種感覺就好。"
        translator._engines = [_route_engine("deepseek", natural_target)]
        translation_engines_module.reset_translation_call_trace()

        with _active_translation_profile("url"):
            outcome = translator.translate_event(
                "그러니까 마냥 그냥 렛츠고 이런 느낌으로 해요.",
                False,
            )

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.target_text, natural_target)
        self.assertEqual(outcome.canonical_obligation_evaluation.expected, ())
        attempt = translation_engines_module.get_translation_attempts()[0]
        self.assertEqual(attempt["status"], "success")
        self.assertEqual(
            attempt["output_guard"]["canonical_obligations"]["expected"],
            [],
        )

    def test_cache_with_missing_canonical_is_invalidated_and_falls_through(self):
        translator = _make_translator()
        translator._engines = [_route_engine("deepseek", "모카來了。")]
        invalidator = MagicMock()
        translator._invalidate_cached_translation = invalidator
        translator._lookup_existing_translation_event = MagicMock(
            return_value=translator_module.MemoryLookup("她來了。", "memory_hit")
        )

        with _active_translation_profile("url"):
            outcome = translator.translate_event("모카가 왔어", False)

        invalidator.assert_called_once()
        self.assertEqual(outcome.result_source, "api")
        self.assertEqual(outcome.target_text, "모카來了。")

    def test_slang_missing_canonical_falls_through_to_provider(self):
        translator = _make_translator()
        translator._engines = [_route_engine("deepseek", "모카來了。")]
        translator._translate_slang = MagicMock(return_value="她來了。")

        with _active_translation_profile("url"):
            outcome = translator.translate_event("모카가 왔어", False)

        self.assertEqual(outcome.result_source, "api")
        self.assertEqual(outcome.target_text, "모카來了。")
        translator._engines[0].translate_messages.assert_called_once()

    def test_required_canonical_matrix_resolves_and_accepts(self):
        cases = (
            ("url", "모카가 왔어", "모카來了。", "모카"),
            ("url", "랑코가 왔어", "랑코來了。", "랑코"),
            ("url", "마냥이 왔어", "마냥來了。", "마냥"),
            ("url", "솜먕이 왔어", "솜먕來了。", "솜먕"),
            ("isegye_lilpa", "주르르가 왔어", "Jururu來了。", "Jururu"),
            ("isegye_lilpa", "릴파가 왔어", "Lilpa來了。", "Lilpa"),
            ("irise", "키리가 왔어", "KIIRI來了。", "KIIRI"),
            ("irise", "하트 크러쉬 좋아", "Heart Crush很好聽。", "Heart Crush"),
        )
        for profile_id, source, target, canonical in cases:
            with self.subTest(canonical=canonical):
                translator = _make_translator()
                translator._engines = [_route_engine("deepseek", target)]
                with _active_translation_profile(profile_id):
                    outcome = translator.translate_event(source, False)
                self.assertEqual(outcome.status, "success")
                self.assertEqual(
                    outcome.canonical_obligation_evaluation.satisfied,
                    (canonical,),
                )

    def test_active_profile_does_not_globally_allow_unrelated_hangul_name(self):
        engine = _route_engine("deepseek", "今天是모카的直播")
        with _active_translation_profile("url"):
            guard = _translation_output_guard(
                engine,
                "今天是모카的直播",
                "오늘 방송이야",
            )
        self.assertEqual(guard["reason"], "unexpected_hangul")
        self.assertEqual(guard["canonical_obligations"]["expected"], [])

    def test_obligation_preview_telemetry_does_not_contaminate_selected_trace(self):
        engine = _route_engine("deepseek", "摩卡來了。")
        reset_corrections()
        translator_module._record_correction("seed", "seed", "before", "after")
        before = get_corrections()
        with _active_translation_profile("url"):
            guard = _translation_output_guard(
                engine,
                "摩卡來了。",
                "모카가 왔어",
            )
        self.assertTrue(guard["canonical_obligations"]["passed"])
        self.assertEqual(guard["candidate_corrections"][0]["stage"], "name_render")
        self.assertEqual(get_corrections(), before)

    def test_flash_name_render_rescue_is_selected_and_keeps_raw_telemetry(self):
        flash = _route_engine("deepseek", "릴파")
        qwen = _route_engine("openrouter", "不應被呼叫")
        translator = _make_translator()
        translator._engines = [flash, qwen]
        translator._active_idx = 0
        translation_engines_module.reset_translation_call_trace()

        with _active_translation_profile("isegye_lilpa"):
            outcome = translator.translate_event("릴파", False)

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.target_text, "Lilpa")
        self.assertEqual(outcome.engine, "deepseek")
        qwen.translate_messages.assert_not_called()
        attempts = translation_engines_module.get_translation_attempts()
        self.assertEqual(len(attempts), 1)
        self.assertTrue(attempts[0]["selected_for_output"])
        guard = attempts[0]["output_guard"]
        self.assertEqual(guard["candidate_raw_output"], "릴파")
        self.assertEqual(guard["candidate_output"], "Lilpa")
        self.assertEqual(
            guard["accepted_after_name_render"],
            ["unexpected_hangul"],
        )
        self.assertIn(
            "target_has_unexpected_hangul",
            guard["candidate_raw_quality_classifications"],
        )
        self.assertNotIn(
            "target_has_unexpected_hangul",
            guard["candidate_quality_classifications"],
        )

    def test_clean_flash_output_is_selected_without_calling_qwen(self):
        class CapsuleEngine(TranslationEngine):
            def __init__(self, name: str, model: str, result: str):
                self._name = name
                self._model = model
                self._result = result
                self.messages = []

            @property
            def engine_name(self):
                return self._name

            @property
            def model_name(self):
                return self._model

            @property
            def available(self):
                return True

            def translate(self, text, system_prompt, incomplete, history=None):
                raise AssertionError("Qwen capsule route must use frozen messages")

            def translate_messages(self, messages):
                self.messages.append(messages)
                return self._result

        flash = CapsuleEngine(
            "deepseek",
            "deepseek-v4-flash",
            "這是乾淨的繁體中文結果",
        )
        qwen = CapsuleEngine(
            "openrouter",
            "qwen/qwen3-next-80b-a3b-instruct",
            "不應被呼叫",
        )
        translator = _make_translator()
        translator._engines = [flash, qwen]
        translator._active_idx = 0
        translation_engines_module.reset_translation_call_trace()

        outcome = translator.translate_event("이것은 깨끗한 테스트 문장입니다", False)

        self.assertEqual(outcome.target_text, "這是乾淨的繁體中文結果")
        self.assertEqual(outcome.engine, "deepseek")
        self.assertEqual(len(flash.messages), 1)
        self.assertEqual(qwen.messages, [])
        attempts = translation_engines_module.get_translation_attempts()
        self.assertEqual(len(attempts), 1)
        self.assertTrue(attempts[0]["selected_for_output"])

        cached = translator._memory.cache_lookup(
            "이것은 깨끗한 테스트 문장입니다",
            False,
            outcome.prompt_version,
            flash,
        )

        self.assertEqual(cached, "這是乾淨的繁體中文結果")
        self.assertEqual(len(flash.messages), 1)
        self.assertEqual(qwen.messages, [])

    def test_flash_script_guard_selects_qwen_without_candidate_state_leak(self):
        class CapsuleEngine(TranslationEngine):
            def __init__(self, name: str, model: str, result: str):
                self._name = name
                self._model = model
                self._result = result
                self.messages = []

            @property
            def engine_name(self):
                return self._name

            @property
            def model_name(self):
                return self._model

            @property
            def available(self):
                return True

            def translate(self, text, system_prompt, incomplete, history=None):
                raise AssertionError("Qwen capsule route must use frozen messages")

            def translate_messages(self, messages):
                self.messages.append(messages)
                return self._result

        flash = CapsuleEngine("deepseek", "deepseek-v4-flash", "這是느졋")
        qwen = CapsuleEngine(
            "openrouter",
            "qwen/qwen3-next-80b-a3b-instruct",
            "這是正式的繁體中文結果",
        )
        translator = _make_translator()
        translator._engines = [flash, qwen]
        translator._active_idx = 0
        translation_engines_module.reset_translation_call_trace()
        reset_corrections()

        outcome = translator.translate_event("이것은 테스트 문장입니다", False)

        self.assertEqual(outcome.target_text, "這是正式的繁體中文結果")
        self.assertEqual(outcome.engine, "openrouter")
        self.assertEqual(len(flash.messages), 1)
        self.assertNotEqual(flash.messages, qwen.messages)
        self.assertIn("overwhelmingly more likely", flash.messages[0][0][1])
        self.assertIn(
            "You translate noisy live-stream subtitles",
            qwen.messages[0][0][1],
        )
        attempts = translation_engines_module.get_translation_attempts()
        self.assertEqual(attempts[0]["status"], "rejected_output")
        self.assertEqual(
            attempts[0]["output_guard"]["reason"],
            "unexpected_hangul",
        )
        self.assertFalse(attempts[0]["selected_for_output"])
        self.assertTrue(attempts[1]["selected_for_output"])
        self.assertEqual(get_corrections(), [])

    def test_invalid_qwen_and_deepl_script_residue_continue_to_valid_groq(self):
        translator = _make_translator()
        translator._engines = [
            _route_engine("deepseek", "Flash느졋"),
            _route_engine("openrouter", "Qwen느졋"),
            _route_engine("deepl", "DeepLテスト"),
            _route_engine("groq", "Groq提供的有效繁體中文"),
        ]
        translator._active_idx = 0
        translation_engines_module.reset_translation_call_trace()

        outcome = translator.translate_event("이것은 테스트 문장입니다", False)

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.engine, "groq")
        self.assertEqual(outcome.target_text, "Groq提供的有效繁體中文")
        self.assertEqual(translator._active_idx, 0)
        attempts = translation_engines_module.get_translation_attempts()
        self.assertEqual(
            [(row["engine"], row["status"]) for row in attempts],
            [
                ("deepseek", "rejected_output"),
                ("openrouter", "rejected_output"),
                ("deepl", "rejected_output"),
                ("groq", "success"),
            ],
        )
        self.assertEqual(
            [row["output_guard"]["reason"] for row in attempts[:3]],
            ["unexpected_hangul", "unexpected_hangul", "unexpected_japanese"],
        )
        self.assertTrue(attempts[-1]["selected_for_output"])

    def test_invalid_groq_final_fallback_is_not_emitted(self):
        translator = _make_translator()
        translator._engines = [
            _route_engine("deepseek", "Flash느졋"),
            _route_engine("openrouter", "Qwen느졋"),
            _route_engine("deepl", "DeepLテスト"),
            _route_engine("groq", "Groq느졋"),
        ]
        translator._active_idx = 0
        translation_engines_module.reset_translation_call_trace()

        outcome = translator.translate_event("이것은 테스트 문장입니다", False)

        self.assertEqual(outcome.status, "failed")
        self.assertIsNone(outcome.target_text)
        self.assertEqual(translator._active_idx, 0)
        attempts = translation_engines_module.get_translation_attempts()
        self.assertEqual(
            [row["engine"] for row in attempts],
            ["deepseek", "openrouter", "deepl", "groq"],
        )
        self.assertTrue(all(row["status"] == "rejected_output" for row in attempts))
        self.assertFalse(any(row["selected_for_output"] for row in attempts))

    def test_default_backend_builds_protected_deepseek_chain_without_nvidia(self):
        class FakeEngine:
            def __init__(self, name: str):
                self._name = name

            @property
            def engine_name(self) -> str:
                return self._name

            @property
            def model_name(self) -> str:
                return f"{self._name}-model"

            @property
            def available(self) -> bool:
                return True

            def translate(self, text, system_prompt, incomplete, history=None):
                return "ok"

        original_mode = translator_module.cfg.translation.translation_mode
        try:
            object.__setattr__(
                translator_module.cfg.translation,
                "translation_mode",
                "live",
            )
            with patch.object(
                translation_engines_module,
                "_make_engine",
                side_effect=lambda name: FakeEngine(name),
            ):
                engines = _build_engine_chain()
        finally:
            object.__setattr__(
                translator_module.cfg.translation,
                "translation_mode",
                original_mode,
            )

        self.assertEqual(translator_module.cfg.live_engine, "anthropic")
        self.assertEqual(
            [engine.engine_name for engine in engines],
            ["deepseek", "openrouter", "deepl", "groq"],
        )

    def test_deepseek_route_off_restores_exact_previous_chain(self):
        original_route = translator_module.cfg.translation.deepseek_route
        try:
            object.__setattr__(translator_module.cfg.translation, "deepseek_route", "off")
            with patch.object(
                translation_engines_module,
                "_make_engine",
                side_effect=lambda name: _mock_engine(name),
            ):
                engines = _build_engine_chain()
        finally:
            object.__setattr__(
                translator_module.cfg.translation,
                "deepseek_route",
                original_route,
            )
        self.assertEqual(
            [engine.engine_name for engine in engines],
            ["openrouter", "deepl", "groq"],
        )

    def test_nvidia_backend_uses_openrouter_before_deepl_and_groq(self):
        from config import cfg

        class FakeEngine:
            def __init__(self, name: str):
                self._name = name

            @property
            def engine_name(self) -> str:
                return self._name

            @property
            def model_name(self) -> str:
                return f"{self._name}-model"

            @property
            def available(self) -> bool:
                return True

            def translate(self, text, system_prompt, incomplete, history=None):
                return "ok"

        original_mode = cfg.translation.translation_mode
        original_chain = cfg.translation.engine_chain
        original_live_engine = cfg.live_engine
        try:
            object.__setattr__(cfg.translation, "translation_mode", "live")
            object.__setattr__(
                cfg.translation,
                "engine_chain",
                ("openrouter", "deepl", "groq"),
            )
            object.__setattr__(cfg, "live_engine", "nvidia")
            # Patch the registry boundary actually used by _build_engine_chain.
            # Patching class names is ineffective because EngineSpec factories
            # are captured when translation_engines is imported; it also made
            # this test accidentally depend on local API keys.
            with patch.object(
                translation_engines_module,
                "_make_engine",
                side_effect=lambda name: FakeEngine(name),
            ):
                engines = _build_engine_chain()
        finally:
            object.__setattr__(cfg.translation, "translation_mode", original_mode)
            object.__setattr__(cfg.translation, "engine_chain", original_chain)
            object.__setattr__(cfg, "live_engine", original_live_engine)

        self.assertEqual(
            [engine.engine_name for engine in engines],
            ["nvidia", "openrouter", "deepl", "groq"],
        )

    def test_openrouter_success_is_attributed_and_stops_deeper_fallbacks(self):
        t = _make_translator()
        t._engines = [
            _mock_engine("nvidia", None),
            _mock_engine("openrouter", "正確翻譯"),
            _mock_engine("deepl", "deepl"),
            _mock_engine("groq", "groq"),
        ]
        _set_provider_failure(t._engines[0])

        with _translation_mode("live"):
            outcome = t.translate_event("안녕하세요")

        self.assertEqual(outcome.target_text, "正確翻譯")
        self.assertEqual(outcome.engine, "openrouter")
        self.assertEqual(outcome.model, "openrouter-test-model")
        self.assertEqual(t._active_idx, 1)
        t._engines[2].translate.assert_not_called()
        t._engines[3].translate.assert_not_called()


# ---------------------------------------------------------------------------
# NvidiaEngine unit tests
# ---------------------------------------------------------------------------

class TestNvidiaEngine(unittest.TestCase):

    def _engine(self) -> NvidiaEngine:
        e = NvidiaEngine.__new__(NvidiaEngine)
        e._api_key = "fake-key"
        e._model = "qwen/qwen3-next-80b-a3b-instruct"
        e._timeout = 10
        e._is_qwen3 = True
        e._strip_think = True
        return e

    def _response(self, content: str):
        import json
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode()
        return mock_resp

    def test_default_live_timeout_fails_fast(self):
        from config import cfg

        self.assertEqual(cfg.nvidia.live_timeout, 5)

    def test_live_mode_uses_live_timeout(self):
        from config import cfg

        orig_mode = cfg.translation.translation_mode
        orig_key = cfg.keys.nvidia
        orig_timeout = cfg.nvidia.timeout
        orig_live_timeout = cfg.nvidia.live_timeout
        object.__setattr__(cfg.translation, "translation_mode", "live")
        object.__setattr__(cfg.keys, "nvidia", "fake-key")
        object.__setattr__(cfg.nvidia, "timeout", 10)
        object.__setattr__(cfg.nvidia, "live_timeout", 7)
        try:
            engine = NvidiaEngine()
        finally:
            object.__setattr__(cfg.translation, "translation_mode", orig_mode)
            object.__setattr__(cfg.keys, "nvidia", orig_key)
            object.__setattr__(cfg.nvidia, "timeout", orig_timeout)
            object.__setattr__(cfg.nvidia, "live_timeout", orig_live_timeout)

        self.assertEqual(engine._timeout, 7)
        self.assertFalse(engine._retry_transient_errors)

    def test_live_mode_default_falls_back_to_regular_timeout(self):
        from config import cfg

        orig_mode = cfg.translation.translation_mode
        orig_key = cfg.keys.nvidia
        orig_timeout = cfg.nvidia.timeout
        orig_live_timeout = cfg.nvidia.live_timeout
        object.__setattr__(cfg.translation, "translation_mode", "live")
        object.__setattr__(cfg.keys, "nvidia", "fake-key")
        object.__setattr__(cfg.nvidia, "timeout", 10)
        object.__setattr__(cfg.nvidia, "live_timeout", 0)
        try:
            engine = NvidiaEngine()
        finally:
            object.__setattr__(cfg.translation, "translation_mode", orig_mode)
            object.__setattr__(cfg.keys, "nvidia", orig_key)
            object.__setattr__(cfg.nvidia, "timeout", orig_timeout)
            object.__setattr__(cfg.nvidia, "live_timeout", orig_live_timeout)

        self.assertEqual(engine._timeout, 10)
        self.assertFalse(engine._retry_transient_errors)

    def test_live_mode_falls_back_to_regular_timeout_when_live_timeout_none(self):
        from config import cfg

        orig_mode = cfg.translation.translation_mode
        orig_key = cfg.keys.nvidia
        orig_timeout = cfg.nvidia.timeout
        orig_live_timeout = cfg.nvidia.live_timeout
        object.__setattr__(cfg.translation, "translation_mode", "live")
        object.__setattr__(cfg.keys, "nvidia", "fake-key")
        object.__setattr__(cfg.nvidia, "timeout", 10)
        object.__setattr__(cfg.nvidia, "live_timeout", None)
        try:
            engine = NvidiaEngine()
        finally:
            object.__setattr__(cfg.translation, "translation_mode", orig_mode)
            object.__setattr__(cfg.keys, "nvidia", orig_key)
            object.__setattr__(cfg.nvidia, "timeout", orig_timeout)
            object.__setattr__(cfg.nvidia, "live_timeout", orig_live_timeout)

        self.assertEqual(engine._timeout, 10)

    def test_clip_mode_uses_regular_timeout(self):
        from config import cfg

        orig_mode = cfg.translation.translation_mode
        orig_key = cfg.keys.nvidia
        orig_timeout = cfg.nvidia.timeout
        orig_live_timeout = cfg.nvidia.live_timeout
        object.__setattr__(cfg.translation, "translation_mode", "clip")
        object.__setattr__(cfg.keys, "nvidia", "fake-key")
        object.__setattr__(cfg.nvidia, "timeout", 10)
        object.__setattr__(cfg.nvidia, "live_timeout", 5)
        try:
            engine = NvidiaEngine()
        finally:
            object.__setattr__(cfg.translation, "translation_mode", orig_mode)
            object.__setattr__(cfg.keys, "nvidia", orig_key)
            object.__setattr__(cfg.nvidia, "timeout", orig_timeout)
            object.__setattr__(cfg.nvidia, "live_timeout", orig_live_timeout)

        self.assertEqual(engine._timeout, 10)
        self.assertTrue(engine._retry_transient_errors)

    def test_retries_once_after_empty_response(self):
        e = self._engine()
        side_effect = [self._response(""), self._response("你好")]

        with patch("urllib.request.urlopen", side_effect=side_effect) as urlopen, \
                patch("time.sleep") as sleep:
            result = e.translate("안녕하세요", "system", False)

        self.assertEqual(result, "你好")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(
            get_last_engine_diagnostics(),
            {"engine": "nvidia", "retry_count": 1, "retry_reason": "empty_response"},
        )
        api_diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(api_diagnostics["api_attempt_count"], 2)
        self.assertEqual(api_diagnostics["api_timeout_count"], 0)
        self.assertIsNotNone(api_diagnostics["api_total_wall_ms"])
        self.assertIsNotNone(api_diagnostics["api_final_attempt_ms"])
        self.assertGreaterEqual(api_diagnostics["retry_sleep_ms"], 0)
        self.assertEqual(api_diagnostics["timeout_config_ms"], 10000)
        self.assertIsNone(api_diagnostics["api_error_type"])
        self.assertIsNone(api_diagnostics["api_error_message_class"])

    def test_retries_once_after_timeout_error(self):
        import urllib.error

        e = self._engine()
        side_effect = [urllib.error.URLError("timed out"), self._response("你好")]

        with patch("urllib.request.urlopen", side_effect=side_effect) as urlopen, \
                patch("time.sleep") as sleep:
            result = e.translate("안녕하세요", "system", False)

        self.assertEqual(result, "你好")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(
            get_last_engine_diagnostics(),
            {"engine": "nvidia", "retry_count": 1, "retry_reason": "timeout"},
        )
        api_diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(api_diagnostics["api_attempt_count"], 2)
        self.assertEqual(api_diagnostics["api_timeout_count"], 1)
        self.assertIsNotNone(api_diagnostics["api_total_wall_ms"])
        self.assertIsNotNone(api_diagnostics["api_final_attempt_ms"])
        self.assertIsNotNone(api_diagnostics["api_first_attempt_ms"])
        self.assertIsNotNone(api_diagnostics["api_retry_attempt_ms"])
        self.assertGreaterEqual(api_diagnostics["retry_sleep_ms"], 0)
        self.assertEqual(api_diagnostics["timeout_config_ms"], 10000)
        self.assertEqual(api_diagnostics["api_attempt_timeout_ms"], 10000)
        self.assertEqual(api_diagnostics["api_attempt_index"], 2)
        self.assertEqual(api_diagnostics["api_inflight_count_at_start"], 0)
        self.assertIsNotNone(api_diagnostics["source_text_char_count"])
        self.assertEqual(api_diagnostics["prompt_char_count"], len("system"))
        self.assertGreater(api_diagnostics["request_body_char_count"], 0)
        self.assertEqual(api_diagnostics["message_count"], 2)
        self.assertEqual(api_diagnostics["context_item_count"], 0)
        self.assertIsNone(api_diagnostics["api_error_type"])
        self.assertIsNone(api_diagnostics["api_error_message_class"])

    def test_live_mode_does_not_retry_timeout_error(self):
        import urllib.error

        e = self._engine()
        e._retry_transient_errors = False
        side_effect = [urllib.error.URLError("timed out"), self._response("你好")]

        with patch("urllib.request.urlopen", side_effect=side_effect) as urlopen, \
                patch("time.sleep") as sleep:
            result = e.translate("안녕하세요", "system", False)

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(
            get_last_engine_diagnostics(),
            {"engine": "nvidia", "retry_count": 0, "retry_reason": ""},
        )
        api_diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(api_diagnostics["api_attempt_count"], 1)
        self.assertEqual(api_diagnostics["api_timeout_count"], 1)
        self.assertEqual(api_diagnostics["api_attempt_index"], 1)
        self.assertIsNotNone(api_diagnostics["api_first_attempt_ms"])
        self.assertIsNone(api_diagnostics["api_retry_attempt_ms"])
        self.assertEqual(api_diagnostics["api_error_type"], "timeout")
        self.assertEqual(api_diagnostics["api_error_message_class"], "read_timeout")

    def test_records_final_timeout_error_diagnostics(self):
        import urllib.error

        e = self._engine()
        side_effect = [urllib.error.URLError("timed out"), urllib.error.URLError("timed out")]

        with patch("urllib.request.urlopen", side_effect=side_effect) as urlopen, \
                patch("time.sleep") as sleep:
            result = e.translate("hello", "system", False)

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(
            get_last_engine_diagnostics(),
            {"engine": "nvidia", "retry_count": 1, "retry_reason": "timeout"},
        )
        api_diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(api_diagnostics["api_attempt_count"], 2)
        self.assertEqual(api_diagnostics["api_timeout_count"], 2)
        self.assertEqual(api_diagnostics["api_attempt_index"], 2)
        self.assertIsNotNone(api_diagnostics["api_first_attempt_ms"])
        self.assertIsNotNone(api_diagnostics["api_retry_attempt_ms"])
        self.assertEqual(api_diagnostics["api_error_type"], "timeout")
        self.assertEqual(api_diagnostics["api_error_message_class"], "read_timeout")
        self.assertEqual(api_diagnostics["timeout_config_ms"], 10000)

    def test_retries_once_after_non_timeout_network_error(self):
        import urllib.error

        e = self._engine()
        side_effect = [urllib.error.URLError("connection reset"), self._response("你好")]

        with patch("urllib.request.urlopen", side_effect=side_effect) as urlopen, \
                patch("time.sleep") as sleep:
            result = e.translate("안녕하세요", "system", False)

        self.assertEqual(result, "你好")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(
            get_last_engine_diagnostics(),
            {"engine": "nvidia", "retry_count": 1, "retry_reason": "network"},
        )

    def test_live_mode_does_not_retry_network_error(self):
        import urllib.error

        e = self._engine()
        e._retry_transient_errors = False
        side_effect = [urllib.error.URLError("connection reset"), self._response("你好")]

        with patch("urllib.request.urlopen", side_effect=side_effect) as urlopen, \
                patch("time.sleep") as sleep:
            result = e.translate("안녕하세요", "system", False)

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(
            get_last_engine_diagnostics(),
            {"engine": "nvidia", "retry_count": 0, "retry_reason": ""},
        )
        api_diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(api_diagnostics["api_attempt_count"], 1)
        self.assertEqual(api_diagnostics["api_timeout_count"], 0)
        self.assertEqual(api_diagnostics["api_error_type"], "connection_error")

    def test_does_not_retry_rate_limit(self):
        import urllib.error

        e = self._engine()
        error = urllib.error.HTTPError(
            url="https://integrate.api.nvidia.com/v1/chat/completions",
            code=429,
            msg="rate limit",
            hdrs=None,
            fp=None,
        )

        with patch("urllib.request.urlopen", side_effect=error) as urlopen, \
                patch("time.sleep") as sleep:
            result = e.translate("안녕하세요", "system", False)

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(
            get_last_engine_diagnostics(),
            {"engine": "nvidia", "retry_count": 0, "retry_reason": ""},
        )
        api_diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(api_diagnostics["api_attempt_count"], 1)
        self.assertEqual(api_diagnostics["api_timeout_count"], 0)
        self.assertEqual(api_diagnostics["api_error_type"], "api_error")
        self.assertEqual(api_diagnostics["api_error_message_class"], "rate_limit")

    def test_does_not_retry_http_server_error(self):
        import urllib.error

        e = self._engine()
        error = urllib.error.HTTPError(
            url="https://integrate.api.nvidia.com/v1/chat/completions",
            code=500,
            msg="server error",
            hdrs=None,
            fp=None,
        )

        with patch("urllib.request.urlopen", side_effect=error) as urlopen, \
                patch("time.sleep") as sleep:
            result = e.translate("안녕하세요", "system", False)

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(
            get_last_engine_diagnostics(),
            {"engine": "nvidia", "retry_count": 0, "retry_reason": ""},
        )
        api_diagnostics = get_last_engine_api_diagnostics()
        self.assertEqual(api_diagnostics["api_attempt_count"], 1)
        self.assertEqual(api_diagnostics["api_timeout_count"], 0)
        self.assertEqual(api_diagnostics["api_error_type"], "api_error")
        self.assertEqual(api_diagnostics["api_error_message_class"], "http_5xx")


class TestRuntimeRetryAttribution(unittest.TestCase):
    def test_production_worker_uses_event_snapshot_and_profile_after_switch(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()
        observed = []

        class _ObservingTranslator:
            def __init__(self, shared_state=None):
                pass

            def translate_event(self, text, incomplete=False, *, repetition_evidence=None):
                observed.append((effective_activity_value(), effective_profile_id()))
                return TranslationOutcome(
                    source_text=text,
                    target_text="固定場景",
                    status="success",
                    result_source="api",
                    cache_status="miss",
                    incomplete=False,
                    engine="fake",
                    model="fake-model",
                )

        snapshot = capture_activity_snapshot("StarCraft", source="manual")
        event = SentenceEvent(
            text="현재 문장",
            profile_id="url",
            sentence_id="sentence-000001",
            enqueued_at_monotonic=time.monotonic(),
            activity_snapshot=snapshot,
        )
        original_activity = translator_module.cfg.translation.current_activity
        original_profile = translator_module.cfg.translation.streamer_profile
        object.__setattr__(translator_module.cfg.translation, "current_activity", "Hades")
        object.__setattr__(
            translator_module.cfg.translation, "streamer_profile", "hades_chxxnnx"
        )
        try:
            with patch.object(translator_module, "Translator", _ObservingTranslator), \
                    patch.object(translator_module, "runtime_events") as events:
                thread = translator_module.start(sentence_q, subtitle_q, stop)
                sentence_q.put(event)
                self.assertEqual(subtitle_q.get(timeout=3), "固定場景")
                deadline = time.monotonic() + 3
                while not events.emit.called and time.monotonic() < deadline:
                    stop.wait(0.005)
                stop.set()
                thread.join(timeout=2)
        finally:
            object.__setattr__(translator_module.cfg.translation, "current_activity", original_activity)
            object.__setattr__(
                translator_module.cfg.translation, "streamer_profile", original_profile
            )

        self.assertEqual(observed, [("StarCraft", "url")])
        fields = events.emit.call_args.kwargs
        self.assertEqual(fields["sentence_id"], "sentence-000001")
        self.assertEqual(fields["activity_id"], "starcraft")
        self.assertTrue(fields["activity_bound_snapshot_used"])
        self.assertFalse(fields["activity_snapshot_fallback_used"])
        self.assertEqual(fields["profile_id"], "url")
        self.assertTrue(fields["activity_capsule_applied"])

    def _emit_outcome_with_stale_nvidia_diagnostics(
        self,
        outcome: TranslationOutcome,
        api_diagnostics: dict | None = None,
        translator_cls=None,
    ) -> dict:
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        class _FakeTranslator:
            def __init__(self, shared_state=None):
                pass

            def translate_event(
                self, text: str, incomplete: bool = False, *, repetition_evidence=None
            ) -> TranslationOutcome:
                return outcome

        stale_diagnostics = {
            "engine": "nvidia",
            "retry_count": 1,
            "retry_reason": "timeout",
        }
        api_diagnostics = api_diagnostics or {
            "engine": "nvidia",
            "api_attempt_count": 0,
        }
        translator_cls = translator_cls or _FakeTranslator
        with patch.object(translator_module, "Translator", translator_cls), \
                patch.object(translator_module, "get_last_engine_diagnostics", return_value=stale_diagnostics), \
                patch.object(translator_module, "get_last_engine_api_diagnostics", return_value=api_diagnostics), \
                patch.object(translator_module, "runtime_events") as events:
            thread = translator_module.start(sentence_q, subtitle_q, stop)
            sentence_q.put({"text": outcome.source_text, "incomplete": outcome.incomplete})
            if outcome.target_text:
                self.assertEqual(subtitle_q.get(timeout=3), outcome.target_text)
            deadline = time.monotonic() + 3
            while not events.emit.called and time.monotonic() < deadline:
                stop.wait(0.005)
            stop.set()
            thread.join(timeout=2)

        events.emit.assert_called_once()
        return events.emit.call_args.kwargs

    def test_translation_event_records_normalized_activity_metadata(self):
        outcome = TranslationOutcome(
            source_text="안녕하세요",
            target_text="你好",
            status="success",
            result_source="api",
            cache_status="miss",
            incomplete=False,
            engine="fake",
            model="fake-model",
        )
        original = translator_module.cfg.translation.current_activity
        object.__setattr__(
            translator_module.cfg.translation,
            "current_activity",
            "  StarCraft   ladder  " + ("x" * 100),
        )
        try:
            fields = self._emit_outcome_with_stale_nvidia_diagnostics(outcome)
        finally:
            object.__setattr__(
                translator_module.cfg.translation,
                "current_activity",
                original,
            )

        self.assertTrue(fields["current_activity"].startswith("StarCraft ladder "))
        self.assertNotIn("\n", fields["current_activity"])
        self.assertLessEqual(len(fields["current_activity"]), 80)

    def test_translation_worker_binds_one_activity_snapshot_for_runtime(self):
        outcome = TranslationOutcome(
            source_text="오늘 방송을 시작합니다",
            target_text="今天開始直播",
            status="success",
            result_source="api",
            cache_status="miss",
            incomplete=False,
            engine="fake",
            model="fake-model",
        )
        original = translator_module.cfg.translation.current_activity
        observed = []

        class _SwitchingTranslator:
            def __init__(self, shared_state=None):
                pass

            def translate_event(
                self, text: str, incomplete: bool = False, *, repetition_evidence=None
            ) -> TranslationOutcome:
                from modules.activity_context import effective_activity_value

                observed.append(effective_activity_value())
                object.__setattr__(
                    translator_module.cfg.translation,
                    "current_activity",
                    "Hades",
                )
                observed.append(
                    effective_activity_value(
                        translator_module.cfg.translation.current_activity
                    )
                )
                return outcome

        object.__setattr__(
            translator_module.cfg.translation,
            "current_activity",
            "StarCraft",
        )
        try:
            fields = self._emit_outcome_with_stale_nvidia_diagnostics(
                outcome,
                translator_cls=_SwitchingTranslator,
            )
        finally:
            object.__setattr__(
                translator_module.cfg.translation,
                "current_activity",
                original,
            )

        self.assertEqual(observed, ["StarCraft", "StarCraft"])
        self.assertEqual(fields["current_activity"], "StarCraft")
        self.assertEqual(fields["activity_id"], "starcraft")
        self.assertEqual(fields["activity_source"], "manual")
        self.assertEqual(fields["activity_context_schema_version"], 3)

    def test_translation_worker_uses_published_auto_only_when_enabled(self):
        from modules.activity_context import (
            AutomaticActivityPublication,
            activity_publication_store,
        )

        outcome = TranslationOutcome(
            source_text="자동 활동 테스트",
            target_text="自動活動測試",
            status="success",
            result_source="api",
            cache_status="miss",
            incomplete=False,
            engine="fake",
            model="fake-model",
        )
        original_manual = translator_module.cfg.translation.current_activity
        original_enabled = (
            translator_module.cfg.scene.publish_translation_activity
        )
        activity_publication_store.replace(
            AutomaticActivityPublication(
                activity_id="minecraft",
                display_label="Minecraft",
                confirmed_at_utc="2026-07-26T00:00:00+00:00",
                fresh_until_monotonic=time.monotonic() + 60,
                confidence=1.0,
                evidence_count=2,
                activity_kind="game",
            )
        )
        object.__setattr__(
            translator_module.cfg.translation,
            "current_activity",
            "",
        )
        object.__setattr__(
            translator_module.cfg.scene,
            "publish_translation_activity",
            True,
        )
        try:
            fields = self._emit_outcome_with_stale_nvidia_diagnostics(outcome)
        finally:
            object.__setattr__(
                translator_module.cfg.scene,
                "publish_translation_activity",
                original_enabled,
            )
            object.__setattr__(
                translator_module.cfg.translation,
                "current_activity",
                original_manual,
            )
            activity_publication_store.replace(None)

        self.assertEqual(fields["current_activity"], "Minecraft")
        self.assertEqual(fields["activity_id"], "minecraft")
        self.assertEqual(fields["activity_source"], "automatic")
        self.assertEqual(fields["activity_context_schema_version"], 3)
        self.assertEqual(fields["activity_kind"], "game")

    def test_translation_workers_share_translator_state(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()

        class _SharedFakeTranslator:
            instances: list["_SharedFakeTranslator"] = []
            calls: list[str] = []
            lock = threading.Lock()

            def __init__(self, shared_state=None):
                self.shared_state = shared_state
                with self.__class__.lock:
                    self.__class__.instances.append(self)

            def translate_event(
                self, text: str, incomplete: bool = False, *, repetition_evidence=None
            ) -> TranslationOutcome:
                with self.__class__.lock:
                    self.__class__.calls.append(text)
                if text == "first":
                    first_started.set()
                    second_started.wait(timeout=3)
                    release_first.wait(timeout=3)
                else:
                    second_started.set()
                return TranslationOutcome(
                    source_text=text,
                    target_text=f"out-{text}",
                    status="success",
                    result_source="api",
                    cache_status="miss",
                    incomplete=incomplete,
                    engine="fake",
                    model="fake-model",
                )

        with patch.object(translator_module, "Translator", _SharedFakeTranslator), \
                patch.object(translator_module, "runtime_events") as events:
            thread = translator_module.start(sentence_q, subtitle_q, stop)
            try:
                sentence_q.put({"text": "first", "incomplete": False})
                self.assertTrue(first_started.wait(timeout=3))
                sentence_q.put({"text": "second", "incomplete": False})
                self.assertTrue(second_started.wait(timeout=3))
                release_first.set()

                deadline = time.monotonic() + 3
                while events.emit.call_count < 2 and time.monotonic() < deadline:
                    stop.wait(0.005)
            finally:
                stop.set()
                thread.join(timeout=2)

        self.assertGreaterEqual(len(_SharedFakeTranslator.instances), 2)
        shared_state_ids = {id(instance.shared_state) for instance in _SharedFakeTranslator.instances}
        self.assertEqual(len(shared_state_ids), 1)
        self.assertIsNotNone(_SharedFakeTranslator.instances[0].shared_state)
        self.assertCountEqual(_SharedFakeTranslator.calls, ["first", "second"])
        self.assertEqual(events.emit.call_count, 2)

    def test_success_state_commits_in_sentence_sequence_order(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()
        first_started = threading.Event()
        second_finished = threading.Event()
        release_first = threading.Event()
        committed = []

        class _OutOfOrderTranslator:
            shared = None

            def __init__(self, shared_state=None):
                self.__class__.shared = shared_state
                self._last_input = ""

            def translate_event(
                self, text: str, incomplete: bool = False, *, repetition_evidence=None
            ) -> TranslationOutcome:
                self._last_input = text
                if text == "first":
                    first_started.set()
                    second_finished.wait(timeout=3)
                    release_first.wait(timeout=3)
                else:
                    second_finished.set()
                return TranslationOutcome(
                    source_text=text,
                    target_text=f"out-{text}",
                    status="success",
                    result_source="api",
                    cache_status="miss",
                    incomplete=incomplete,
                    engine="fake",
                    model="fake-model",
                    deferred_success=lambda value=text: committed.append(value),
                )

        with patch.object(translator_module, "Translator", _OutOfOrderTranslator), \
                patch.object(translator_module, "runtime_events") as events:
            thread = translator_module.start(sentence_q, subtitle_q, stop)
            try:
                sentence_q.put({"text": "first", "incomplete": False})
                self.assertTrue(first_started.wait(timeout=3))
                sentence_q.put({"text": "second", "incomplete": False})
                self.assertTrue(second_finished.wait(timeout=3))
                self.assertEqual(committed, [])
                release_first.set()
                deadline = time.monotonic() + 3
                while len(committed) < 2 and time.monotonic() < deadline:
                    stop.wait(0.005)
            finally:
                stop.set()
                thread.join(timeout=2)

        self.assertEqual(committed, ["first", "second"])
        self.assertEqual(events.emit.call_count, 2)
        self.assertEqual(_OutOfOrderTranslator.shared.policy.last_input, "second")

    def test_inflight_duplicate_can_succeed_when_first_attempt_fails(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()
        first_started = threading.Event()
        second_finished = threading.Event()
        calls = 0

        class _DuplicateTranslator:
            shared = None

            def __init__(self, shared_state=None):
                self.shared_state = shared_state
                self.__class__.shared = shared_state
                self._last_input = ""

            def translate_event(
                self, text: str, incomplete: bool = False, *, repetition_evidence=None
            ) -> TranslationOutcome:
                nonlocal calls
                with self.shared_state.lock:
                    reason = self.shared_state.policy.rejection_reason(text)
                    prepared = self.shared_state.policy.prepare_input(
                        text,
                        initial_rejection_reason=reason,
                    )
                    self._last_input = self.shared_state.policy.last_input
                if prepared is None:
                    return TranslationOutcome(
                        source_text=text,
                        target_text=None,
                        status="filtered",
                        result_source="policy",
                        cache_status="skipped",
                        incomplete=incomplete,
                        filter_reason=reason or "unknown",
                    )
                calls += 1
                if calls == 1:
                    first_started.set()
                    second_finished.wait(timeout=3)
                    with self.shared_state.lock:
                        if self.shared_state.policy.last_input == self._last_input:
                            self.shared_state.policy.reset_last_input()
                        self._last_input = ""
                    return TranslationOutcome(
                        source_text=text,
                        target_text=None,
                        status="failed",
                        result_source="none",
                        cache_status="miss",
                        incomplete=incomplete,
                    )
                second_finished.set()
                return TranslationOutcome(
                    source_text=text,
                    target_text="retry-success",
                    status="success",
                    result_source="api",
                    cache_status="miss",
                    incomplete=incomplete,
                    engine="fake",
                    model="fake-model",
                )

        with patch.object(translator_module, "Translator", _DuplicateTranslator):
            thread = translator_module.start(sentence_q, subtitle_q, stop)
            try:
                item = {"text": "same source", "incomplete": False}
                sentence_q.put(item)
                self.assertTrue(first_started.wait(timeout=3))
                sentence_q.put(item.copy())
                result = subtitle_q.get(timeout=3)
            finally:
                stop.set()
                thread.join(timeout=2)

        self.assertEqual(result, "retry-success")
        self.assertEqual(calls, 2)
        self.assertEqual(_DuplicateTranslator.shared.policy.last_input, "same source")
        self.assertEqual(
            _DuplicateTranslator.shared.policy.rejection_reason("same source"),
            "duplicate",
        )

    def test_worker_exception_after_prepare_input_allows_identical_retry(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()
        first_failed = threading.Event()
        calls = 0

        class _RaisingTranslator:
            shared = None

            def __init__(self, shared_state=None):
                self.shared_state = shared_state
                self.__class__.shared = shared_state
                self._last_input = ""

            def translate_event(
                self, text: str, incomplete: bool = False, *, repetition_evidence=None
            ) -> TranslationOutcome:
                nonlocal calls
                with self.shared_state.lock:
                    reason = self.shared_state.policy.rejection_reason(text)
                    prepared = self.shared_state.policy.prepare_input(
                        text,
                        initial_rejection_reason=reason,
                    )
                    self._last_input = self.shared_state.policy.last_input
                if prepared is None:
                    return TranslationOutcome(
                        source_text=text,
                        target_text=None,
                        status="filtered",
                        result_source="policy",
                        cache_status="skipped",
                        incomplete=incomplete,
                        filter_reason=reason or "unknown",
                    )
                calls += 1
                if calls == 1:
                    first_failed.set()
                    raise RuntimeError("unexpected provider failure")
                return TranslationOutcome(
                    source_text=text,
                    target_text="retry-success",
                    status="success",
                    result_source="api",
                    cache_status="miss",
                    incomplete=incomplete,
                    engine="fake",
                    model="fake-model",
                )

        with patch.object(translator_module, "Translator", _RaisingTranslator), \
                patch.object(translator_module, "runtime_events") as events:
            thread = translator_module.start(sentence_q, subtitle_q, stop)
            try:
                item = {"text": "same source", "incomplete": False}
                sentence_q.put(item)
                self.assertTrue(first_failed.wait(timeout=3))
                deadline = time.monotonic() + 3
                while events.emit.call_count < 1 and time.monotonic() < deadline:
                    stop.wait(0.005)
                sentence_q.put(item.copy())
                result = subtitle_q.get(timeout=3)
            finally:
                stop.set()
                thread.join(timeout=2)

        self.assertEqual(result, "retry-success")
        self.assertEqual(calls, 2)

    def test_actual_api_and_memory_hit_defer_context_in_sequence_order(self):
        t = _make_translator()
        t._defer_success_record = True
        t._engines[0].translate.return_value = "第一筆翻譯"
        first = t.translate_event("first source text")

        cached_source = "cached source text"
        system_prompt = t._build_system_prompt()
        prompt_ver = t._prompt_version_for_engine(t._engines[0], system_prompt)
        t._memory.cache_store(
            cached_source,
            False,
            "第二筆快取翻譯",
            prompt_ver,
            t._engines[0],
        )
        second = t.translate_event(cached_source)

        self.assertEqual(t._memory.context(), [])
        self.assertIsNotNone(first.deferred_success)
        self.assertIsNotNone(second.deferred_success)
        first.deferred_success()
        second.deferred_success()

        self.assertEqual(
            t._memory.context(),
            [
                ("first source text", "第一筆翻譯"),
                (cached_source, "第二筆快取翻譯"),
            ],
        )
        t._memory._history_writer.assert_called_once_with(
            "first source text",
            "第一筆翻譯",
        )

    def test_actual_api_and_slang_defer_history_in_sequence_order(self):
        t = _make_translator()
        t._defer_success_record = True
        t._engines[0].translate.return_value = "第一筆翻譯"

        first = t.translate_event("first source text")
        second = t.translate_event("대박")

        self.assertEqual(t._memory.context(), [])
        self.assertEqual(t._memory._history_writer.call_count, 0)
        first.deferred_success()
        second.deferred_success()

        self.assertEqual(
            t._memory.context(),
            [("first source text", "第一筆翻譯"), ("대박", "太狂了")],
        )
        self.assertEqual(
            t._memory._history_writer.call_args_list,
            [
                unittest.mock.call("first source text", "第一筆翻譯"),
                unittest.mock.call("대박", "太狂了"),
            ],
        )

    def test_stale_translation_skips_subtitle_display(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()
        orig_delay = getattr(translator_module.cfg.translation, "max_subtitle_output_delay_ms", 30000)

        class _SlowFakeTranslator:
            def __init__(self, shared_state=None):
                pass

            def translate_event(
                self, text: str, incomplete: bool = False, *, repetition_evidence=None
            ) -> TranslationOutcome:
                time.sleep(0.05)
                return TranslationOutcome(
                    source_text=text,
                    target_text=f"out-{text}",
                    status="success",
                    result_source="api",
                    cache_status="miss",
                    incomplete=incomplete,
                    engine="fake",
                    model="fake-model",
                )

        object.__setattr__(translator_module.cfg.translation, "max_subtitle_output_delay_ms", 1)
        try:
            with patch.object(translator_module, "Translator", _SlowFakeTranslator), \
                    patch.object(translator_module, "runtime_events") as events:
                thread = translator_module.start(sentence_q, subtitle_q, stop)
                sentence_q.put({"text": "late", "incomplete": False})
                deadline = time.monotonic() + 3
                while not events.emit.called and time.monotonic() < deadline:
                    stop.wait(0.005)
                stop.set()
                thread.join(timeout=2)
        finally:
            object.__setattr__(
                translator_module.cfg.translation,
                "max_subtitle_output_delay_ms",
                orig_delay,
            )

        self.assertTrue(subtitle_q.empty())
        events.emit.assert_called_once()
        self.assertFalse(events.emit.call_args.kwargs["subtitle_emitted"])
        self.assertEqual(events.emit.call_args.kwargs["subtitle_suppressed_reason"], "stale_output_delay")
        self.assertGreater(events.emit.call_args.kwargs["output_delay_ms"], 1)

    def test_final_publication_rejects_unexpected_script_before_memory_commit(self):
        for target_text, expected_reason in (
            ("오아真的嗎？", "unexpected_hangul"),
            ("それ真的嗎？", "unexpected_japanese"),
        ):
            with self.subTest(target_text=target_text):
                sentence_q = queue.Queue()
                subtitle_q = queue.Queue()
                stop = threading.Event()
                committed = MagicMock()

                class _UnsafeTranslator:
                    def __init__(self, shared_state=None):
                        pass

                    def translate_event(
                        self, text: str, incomplete: bool = False, *, repetition_evidence=None
                    ) -> TranslationOutcome:
                        return TranslationOutcome(
                            source_text=text,
                            target_text=target_text,
                            status="success",
                            result_source="api",
                            cache_status="miss",
                            incomplete=incomplete,
                            engine="fake",
                            model="fake-model",
                            deferred_success=committed,
                        )

                with patch.object(
                    translator_module, "Translator", _UnsafeTranslator
                ), patch.object(translator_module, "runtime_events") as events:
                    thread = translator_module.start(sentence_q, subtitle_q, stop)
                    try:
                        sentence_q.put({"text": "우와 진짜요?", "incomplete": False})
                        deadline = time.monotonic() + 3
                        while not events.emit.called and time.monotonic() < deadline:
                            stop.wait(0.005)
                    finally:
                        stop.set()
                        thread.join(timeout=2)

                self.assertTrue(subtitle_q.empty())
                committed.assert_not_called()
                event = events.emit.call_args.kwargs
                self.assertEqual(event["status"], "failed")
                self.assertIsNone(event["target_text"])
                self.assertEqual(event["filter_reason"], expected_reason)
                self.assertFalse(event["subtitle_emitted"])
                self.assertEqual(
                    event["subtitle_suppressed_reason"], "final_script_invariant"
                )

    def test_final_publication_allows_explicit_source_grounded_hangul(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()
        committed = MagicMock()

        class _ApprovedTranslator:
            def __init__(self, shared_state=None):
                pass

            def translate_event(
                self, text: str, incomplete: bool = False, *, repetition_evidence=None
            ) -> TranslationOutcome:
                return TranslationOutcome(
                    source_text=text,
                    target_text="모카來了。",
                    status="success",
                    result_source="api",
                    cache_status="miss",
                    incomplete=incomplete,
                    engine="fake",
                    model="fake-model",
                    unknown_name_approved_terms=("모카",),
                    deferred_success=committed,
                )

        with patch.object(
            translator_module, "Translator", _ApprovedTranslator
        ), patch.object(translator_module, "runtime_events") as events:
            thread = translator_module.start(sentence_q, subtitle_q, stop)
            try:
                sentence_q.put({"text": "모카가 왔어요.", "incomplete": False})
                deadline = time.monotonic() + 3
                while not events.emit.called and time.monotonic() < deadline:
                    stop.wait(0.005)
            finally:
                stop.set()
                thread.join(timeout=2)

        self.assertEqual(subtitle_q.get_nowait(), "모카來了。")
        committed.assert_called_once_with()
        self.assertTrue(events.emit.call_args.kwargs["subtitle_emitted"])

    def test_all_provider_canonical_rejection_outcome_emits_no_subtitle(self):
        sentence_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()

        class _CanonicalRejectedTranslator:
            def __init__(self, shared_state=None):
                pass

            def translate_event(
                self, text: str, incomplete: bool = False, *, repetition_evidence=None
            ) -> TranslationOutcome:
                return TranslationOutcome(
                    source_text=text,
                    target_text=None,
                    status="failed",
                    result_source="none",
                    cache_status="miss",
                    incomplete=incomplete,
                    filter_reason="canonical_obligation_missing",
                )

        with patch.object(
            translator_module, "Translator", _CanonicalRejectedTranslator
        ), patch.object(translator_module, "runtime_events") as events:
            thread = translator_module.start(sentence_q, subtitle_q, stop)
            try:
                sentence_q.put({"text": "모카가 왔어", "incomplete": False})
                deadline = time.monotonic() + 3
                while not events.emit.called and time.monotonic() < deadline:
                    stop.wait(0.005)
            finally:
                stop.set()
                thread.join(timeout=2)

        self.assertTrue(subtitle_q.empty())
        events.emit.assert_called_once()
        self.assertFalse(events.emit.call_args.kwargs["subtitle_emitted"])

    def test_memory_hit_ignores_stale_nvidia_retry_diagnostics(self):
        event = self._emit_outcome_with_stale_nvidia_diagnostics(
            TranslationOutcome(
                source_text="안녕하세요",
                target_text="你好",
                status="success",
                result_source="memory_hit",
                cache_status="memory_hit",
                incomplete=False,
                engine="nvidia",
                model="nvidia-test",
            )
        )

        self.assertEqual(event["retry_count"], 0)
        self.assertEqual(event["retry_reason"], "")

    def test_slang_hit_ignores_stale_nvidia_retry_diagnostics(self):
        event = self._emit_outcome_with_stale_nvidia_diagnostics(
            TranslationOutcome(
                source_text="ㅋㅋㅋ",
                target_text="哈哈哈",
                status="success",
                result_source="slang",
                cache_status="skipped",
                incomplete=False,
                engine="nvidia",
                model="nvidia-test",
            )
        )

        self.assertEqual(event["retry_count"], 0)
        self.assertEqual(event["retry_reason"], "")

    def test_api_result_keeps_nvidia_retry_diagnostics(self):
        event = self._emit_outcome_with_stale_nvidia_diagnostics(
            TranslationOutcome(
                source_text="안녕하세요",
                target_text="你好",
                status="success",
                result_source="api",
                cache_status="miss",
                incomplete=False,
                engine="nvidia",
                model="nvidia-test",
            )
        )

        self.assertEqual(event["retry_count"], 1)
        self.assertEqual(event["retry_reason"], "timeout")

    def test_api_result_emits_nvidia_api_diagnostics(self):
        event = self._emit_outcome_with_stale_nvidia_diagnostics(
            TranslationOutcome(
                source_text="hello",
                target_text="translated",
                status="success",
                result_source="api",
                cache_status="miss",
                incomplete=False,
                engine="nvidia",
                model="nvidia-test",
            ),
            api_diagnostics={
                "engine": "nvidia",
                "api_attempt_count": 2,
                "api_timeout_count": 1,
                "api_total_wall_ms": 20500.0,
                "api_final_attempt_ms": 9950.0,
                "api_first_attempt_ms": 10000.0,
                "api_retry_attempt_ms": 9950.0,
                "retry_sleep_ms": 500.0,
                "timeout_config_ms": 10000.0,
                "api_attempt_timeout_ms": 10000.0,
                "api_attempt_index": 2,
                "api_inflight_count_at_start": 1,
                "source_text_char_count": 5,
                "prompt_char_count": 42,
                "request_body_char_count": 1234,
                "message_count": 4,
                "context_item_count": 1,
                "api_error_type": None,
                "api_error_message_class": None,
                "api_cost_usd": 0.00012345,
            },
        )

        self.assertEqual(event["api_attempt_count"], 2)
        self.assertEqual(event["api_timeout_count"], 1)
        self.assertEqual(event["api_total_wall_ms"], 20500.0)
        self.assertEqual(event["api_final_attempt_ms"], 9950.0)
        self.assertEqual(event["api_first_attempt_ms"], 10000.0)
        self.assertEqual(event["api_retry_attempt_ms"], 9950.0)
        self.assertEqual(event["retry_sleep_ms"], 500.0)
        self.assertEqual(event["timeout_config_ms"], 10000.0)
        self.assertEqual(event["api_attempt_timeout_ms"], 10000.0)
        self.assertEqual(event["api_attempt_index"], 2)
        self.assertEqual(event["api_inflight_count_at_start"], 1)
        self.assertEqual(event["source_text_char_count"], 5)
        self.assertEqual(event["prompt_char_count"], 42)
        self.assertEqual(event["request_body_char_count"], 1234)
        self.assertEqual(event["message_count"], 4)
        self.assertEqual(event["context_item_count"], 1)
        self.assertIsNone(event["api_error_type"])
        self.assertEqual(event["api_cost_usd"], 0.00012345)
        self.assertIsNone(event["api_error_message_class"])


# ---------------------------------------------------------------------------
# History file
# ---------------------------------------------------------------------------

class TestWriteHistory(unittest.TestCase):

    def test_creates_file_with_ko_and_zh(self):
        import modules.translator as tmod
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tmod, "_LOG_DIR", __import__("pathlib").Path(tmpdir)):
                _write_history("안녕하세요", "你好")
            files = list(__import__("pathlib").Path(tmpdir).glob("translations_*.txt"))
            self.assertEqual(len(files), 1)
            content = files[0].read_text(encoding="utf-8")
            self.assertIn("안녕하세요", content)
            self.assertIn("你好", content)

    def test_appends_multiple_entries(self):
        import modules.translator as tmod
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tmod, "_LOG_DIR", __import__("pathlib").Path(tmpdir)):
                _write_history("첫 번째", "第一")
                _write_history("두 번째", "第二")
            files = list(__import__("pathlib").Path(tmpdir).glob("translations_*.txt"))
            content = files[0].read_text(encoding="utf-8")
            self.assertIn("첫 번째", content)
            self.assertIn("두 번째", content)


# ---------------------------------------------------------------------------
# Pause behaviour
# ---------------------------------------------------------------------------

class TestTranslatorThreadPause(unittest.TestCase):

    def test_pause_prevents_translation(self):
        from modules.translator import start as translator_start
        sentence_q: queue.Queue = queue.Queue()
        subtitle_q: queue.Queue = queue.Queue()
        stop = threading.Event()
        pause = threading.Event()
        pause.set()   # start paused

        with patch("anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = _claude_resp("你好")
            translator_start(sentence_q, subtitle_q, stop, pause_event=pause)
            sentence_q.put({"text": "안녕하세요", "incomplete": False})
            time.sleep(0.5)
            stop.set()

        self.assertTrue(subtitle_q.empty(), "No output expected while paused")


# ---------------------------------------------------------------------------
# Fallback probe logic
# ---------------------------------------------------------------------------

class TestPreserveAsIsAcceptance(unittest.TestCase):

    def test_accepts_narrow_machine_readable_shapes(self):
        accepted = (
            "A.I.N.D.S.",
            "https://example.com/watch?v=42",
            "example.com/path",
            "viewer_42",
            "@streamer_name",
            "@x",
            "hello@example.com",
            "2026-07-25",
            "SOOP",
        )

        for source in accepted:
            with self.subTest(source=source):
                self.assertTrue(_is_legitimate_preserve_as_is(source))
                self.assertFalse(_looks_untranslated(source, source))

    def test_rejects_ambiguous_words_and_ordinary_sentences(self):
        rejected = (
            "Hello",
            "HELLO",
            "hello-world",
            "오늘 정말 재미있었어요",
            "I'll be there for you.",
        )

        for source in rejected:
            with self.subTest(source=source):
                self.assertFalse(_is_legitimate_preserve_as_is(source))
                self.assertTrue(_looks_untranslated(source, source))

    def test_accepts_only_profile_authorized_canonical_terms(self):
        with _active_translation_profile("url"):
            for source in (
                "모카",
                "Wish Me Love",
                "조금 더 가까이",
                "Sandbox Network",
            ):
                with self.subTest(source=source):
                    self.assertFalse(_looks_untranslated(source, source))
            self.assertTrue(_looks_untranslated("유아렐", "유아렐"))
            self.assertTrue(_looks_untranslated("Again", "Again"))

        with _active_translation_profile("stellive_hina"):
            self.assertFalse(_looks_untranslated("해둥이", "해둥이"))
            self.assertTrue(_looks_untranslated("해둥", "해둥"))
            self.assertTrue(_looks_untranslated("Haedungi", "Haedungi"))

    def test_profile_terms_fail_closed_when_profile_is_disabled(self):
        with _active_translation_profile("url", use_profile=False):
            self.assertTrue(_looks_untranslated("모카", "모카"))
            self.assertTrue(_looks_untranslated("Wish Me Love", "Wish Me Love"))


class TestFallbackProbe(unittest.TestCase):
    def test_primary_and_fallback_receive_same_selected_history_cohort(self):
        from dataclasses import replace

        t = _make_translator()
        primary = _mock_engine("nvidia", None)
        fallback = _mock_engine("groq", "成功")
        t._engines = [primary, fallback]
        snapshot = replace(
            capture_activity_snapshot("League of Legends", source="manual"),
            cohort_epoch=7,
        )
        cohort = ("hades_chxxnnx", "league_of_legends", 7)
        t._memory.record_recent_context("前句", "先前", False, cohort)
        t._memory.record_recent_context(
            "聊天", "聊天內容", False, ("hades_chxxnnx", "chatting", 6)
        )

        with bind_profile_id("hades_chxxnnx"), bind_activity_snapshot(snapshot):
            self.assertEqual(t.translate("현재 문장"), "成功")

        self.assertEqual(primary.translate.call_args.args[3], [("前句", "先前")])
        self.assertEqual(fallback.translate.call_args.args[3], [("前句", "先前")])


    def test_dotted_acronym_identical_output_stops_at_primary(self):
        t = _make_translator()
        t._engines = [
            _mock_engine("nvidia", "A.I.N.D.S."),
            _mock_engine("deepl", "A.I.N.D.S."),
            _mock_engine("groq", "A.I.N.D.S."),
            _mock_engine("openrouter", "A.I.N.D.S."),
        ]

        result = t.translate("A.I.N.D.S.")

        self.assertEqual(result, "A.I.N.D.S.")
        t._engines[0].translate.assert_called_once()
        for fallback in t._engines[1:]:
            fallback.translate.assert_not_called()
        self.assertEqual(t._active_idx, 0)

    def test_user_translation_path_does_not_probe_primary_after_recovery(self):
        t = _make_translator()
        t._active_idx = 1                            # currently on fallback
        t._engines[0].translate.return_value = "你好"  # primary recovered
        t._engines[1].translate.return_value = "fallback"

        result = t.translate("안녕하세요")
        self.assertEqual(result, "fallback")
        self.assertEqual(t._active_idx, 1, "User translation path should stay on fallback until background probe")
        t._engines[0].translate.assert_not_called()

    def test_user_translation_path_stays_on_fallback_without_probe(self):
        t = _make_translator()
        t._active_idx = 1
        t._engines[0].translate.return_value = None   # primary still down
        t._engines[1].translate.return_value = "fallback result"

        result = t.translate("안녕하세요")
        self.assertEqual(result, "fallback result")
        self.assertEqual(t._active_idx, 1, "Should stay on fallback if probe fails")
        t._engines[0].translate.assert_not_called()

    def test_background_probe_thread_restores_primary(self):
        fallback_events = []
        shared = translator_module._new_translator_shared_state(
            fallback_event_sink=lambda action, **fields: fallback_events.append(
                {"action": action, **fields}
            )
        )
        shared.memory.record_direct_memory("이전 문장", "先前句子", False)
        shared.fallback.active_idx = 1
        stop = threading.Event()
        engines = [_mock_engine("primary", "你好"), _mock_engine("fallback", "fallback")]

        original_cooldown = (
            translator_module.cfg.translation.circuit_recovery_cooldown_sec
        )
        original_threshold = (
            translator_module.cfg.translation.circuit_recovery_success_threshold
        )
        try:
            object.__setattr__(
                translator_module.cfg.translation,
                "circuit_recovery_cooldown_sec",
                0.01,
            )
            object.__setattr__(
                translator_module.cfg.translation,
                "circuit_recovery_success_threshold",
                2,
            )
            with _live_backend("nvidia"), patch.object(
                translator_module,
                "_build_engine_chain",
                return_value=engines,
            ):
                thread = translator_module._start_fallback_probe_thread(
                    shared,
                    stop,
                    interval_seconds=0.01,
                )
                deadline = time.monotonic() + 1.0
                while shared.fallback.active_idx != 0 and time.monotonic() < deadline:
                    stop.wait(0.005)
                stop.set()
                thread.join(timeout=1)
        finally:
            object.__setattr__(
                translator_module.cfg.translation,
                "circuit_recovery_cooldown_sec",
                original_cooldown,
            )
            object.__setattr__(
                translator_module.cfg.translation,
                "circuit_recovery_success_threshold",
                original_threshold,
            )

        self.assertEqual(shared.fallback.active_idx, 0)
        self.assertGreaterEqual(engines[0].translate.call_count, 2)
        for call in engines[0].translate.call_args_list:
            self.assertEqual(call.args[3], [])
        self.assertEqual(
            [event["action"] for event in fallback_events],
            ["probe_succeeded", "probe_succeeded", "circuit_closed"],
        )
        self.assertTrue(all(event["probe_history_items"] == 0 for event in fallback_events))

    def test_background_probe_thread_respects_cooldown(self):
        fallback_events = []
        shared = translator_module._new_translator_shared_state(
            fallback_event_sink=lambda action, **fields: fallback_events.append(
                {"action": action, **fields}
            )
        )
        shared.fallback.active_idx = 1
        shared.fallback.primary_cooldown_until = time.monotonic() + 60.0
        stop = threading.Event()
        engines = [_mock_engine("primary", "雿末"), _mock_engine("fallback", "fallback")]

        with _live_backend("nvidia"), patch.object(
            translator_module,
            "_build_engine_chain",
            return_value=engines,
        ):
            thread = translator_module._start_fallback_probe_thread(
                shared,
                stop,
                interval_seconds=0.01,
            )
            stop.wait(0.05)
            stop.set()
            thread.join(timeout=1)

        self.assertEqual(shared.fallback.active_idx, 1)
        engines[0].translate.assert_not_called()
        self.assertTrue(fallback_events)
        self.assertTrue(
            all(event["action"] == "probe_cooldown_skipped" for event in fallback_events)
        )

    def test_live_hard_switch_emits_committed_circuit_open_event(self):
        fallback_events = []
        shared = translator_module._new_translator_shared_state(
            fallback_event_sink=lambda action, **fields: fallback_events.append(
                {"action": action, **fields}
            )
        )
        engines = [_mock_engine("nvidia", None), _mock_engine("deepl", "哈囉")]

        _set_provider_failure(engines[0])

        with _translation_mode("live"), _live_backend("nvidia"), patch.object(
            translator_module,
            "_build_engine_chain",
            return_value=engines,
        ):
            translator = Translator(shared_state=shared)
            result = translator.translate("안녕하세요")

        self.assertEqual(result, "哈囉")
        self.assertEqual(shared.fallback.active_idx, 1)
        self.assertEqual(len(fallback_events), 1)
        event = fallback_events[0]
        self.assertEqual(event["action"], "circuit_opened")
        self.assertEqual(event["primary_engine"], "nvidia")
        self.assertEqual(event["from_engine"], "nvidia")
        self.assertEqual(event["active_engine"], "deepl")
        self.assertEqual(event["failure_status"], "empty")
        self.assertEqual(event["failure_scope"], "provider")
        self.assertEqual(event["api_error_type"], "timeout")
        self.assertEqual(event["api_error_message_class"], "read_timeout")
        self.assertGreater(event["cooldown_remaining_ms"], 0)

    def test_live_openrouter_chain_uses_provider_neutral_circuit(self):
        fallback_events = []
        shared = translator_module._new_translator_shared_state(
            fallback_event_sink=lambda action, **fields: fallback_events.append(
                {"action": action, **fields}
            )
        )
        engines = [
            _mock_engine("openrouter", None),
            _mock_engine("deepl", "fallback"),
        ]
        _set_provider_failure(engines[0])

        with _translation_mode("live"), _live_backend("anthropic"), patch.object(
            translator_module,
            "_build_engine_chain",
            return_value=engines,
        ):
            translator = Translator(shared_state=shared)
            result = translator.translate("provider neutral source")

        self.assertEqual(result, "fallback")
        self.assertEqual(shared.fallback.active_idx, 1)
        self.assertEqual(len(fallback_events), 1)
        event = fallback_events[0]
        self.assertEqual(event["action"], "circuit_opened")
        self.assertEqual(event["primary_engine"], "openrouter")
        self.assertEqual(
            event["primary_route"],
            "openrouter:openrouter-test-model",
        )
        self.assertEqual(event["active_route"], "deepl:deepl-test-model")
        self.assertTrue(
            translator_module._translation_circuit_breaker_enabled()
        )

    def test_live_content_rejection_does_not_emit_circuit_open_event(self):
        fallback_events = []
        shared = translator_module._new_translator_shared_state(
            fallback_event_sink=lambda action, **fields: fallback_events.append(
                {"action": action, **fields}
            )
        )
        engines = [_mock_engine("nvidia", "source"), _mock_engine("deepl", "fallback")]

        with _translation_mode("live"), _live_backend("nvidia"), patch.object(
            translator_module,
            "_build_engine_chain",
            return_value=engines,
        ):
            translator = Translator(shared_state=shared)
            result = translator.translate("source")

        self.assertEqual(result, "fallback")
        self.assertEqual(shared.fallback.active_idx, 0)
        self.assertEqual(shared.fallback.consecutive_primary_failures, 0)
        self.assertEqual(fallback_events, [])

    def test_runtime_fallback_event_uses_dedicated_event_type(self):
        with patch.object(translator_module.runtime_events, "emit") as emit:
            translator_module._emit_fallback_runtime_event(
                "probe_failed",
                probe_status="empty",
            )

        emit.assert_called_once()
        args, kwargs = emit.call_args
        self.assertEqual(args, ("translation_fallback",))
        self.assertEqual(kwargs["action"], "probe_failed")
        self.assertEqual(kwargs["probe_status"], "empty")
        self.assertEqual(kwargs["translation_mode"], translator_module.cfg.translation.translation_mode)

    def test_single_failure_uses_fallback_without_switching(self):
        with _translation_mode("clip"):
            t = _make_translator()
            t._engines[0].translate.return_value = None
            t._engines[1].translate.return_value = "哈囉"

            result = t.translate("안녕하세요")
        self.assertEqual(result, "哈囉")
        self.assertEqual(t._active_idx, 0, "single failure should not hard-switch primary")
        self.assertEqual(t._consecutive_primary_failures, 1)

    def test_live_single_failure_hard_switches_to_fallback(self):
        with _translation_mode("live"):
            t = _make_translator()
            _set_provider_failure(t._engines[0])
            t._engines[1].translate.return_value = "哈囉"

            result = t.translate("안녕하세요")

        self.assertEqual(result, "哈囉")
        self.assertEqual(t._active_idx, 1, "live mode should hard-switch after one primary miss")
        self.assertEqual(t._consecutive_primary_failures, 0)

    def test_hard_switch_after_threshold_failures(self):
        from modules.translator import _FALLBACK_THRESHOLD
        with _translation_mode("clip"):
            t = _make_translator()
            t._engines[0].translate.return_value = None
            t._engines[1].translate.return_value = "哈囉"

            # Use distinct inputs — duplicate-suppression policy would block identical consecutive calls
            for i in range(_FALLBACK_THRESHOLD):
                t.translate(f"안녕하세요 {i}")

        self.assertEqual(t._active_idx, 1, "should hard-switch after threshold consecutive failures")
        self.assertEqual(t._consecutive_primary_failures, 0, "counter resets after hard switch")

    def test_success_resets_failure_counter(self):
        t = _make_translator()
        t._consecutive_primary_failures = 2
        t._engines[0].translate.return_value = "你好"

        result = t.translate("안녕하세요")
        self.assertEqual(result, "你好")
        self.assertEqual(t._consecutive_primary_failures, 0, "success should reset failure counter")
        self.assertEqual(t._active_idx, 0, "primary should remain active")

    def test_concurrent_state_merge_does_not_regress_failures(self):
        shared = translator_module.FallbackState(
            active_idx=0,
            consecutive_primary_failures=2,
        )
        before = translator_module.FallbackState(
            active_idx=0,
            consecutive_primary_failures=0,
        )
        after = translator_module.FallbackState(
            active_idx=0,
            consecutive_primary_failures=0,
        )

        translator_module._merge_fallback_state(shared, before, after)

        self.assertEqual(shared.active_idx, 0)
        self.assertEqual(shared.consecutive_primary_failures, 2)

    def test_concurrent_state_merge_preserves_hard_switch(self):
        shared = translator_module.FallbackState(
            active_idx=1,
            consecutive_primary_failures=0,
        )
        before = translator_module.FallbackState(
            active_idx=0,
            consecutive_primary_failures=0,
        )
        after = translator_module.FallbackState(
            active_idx=0,
            consecutive_primary_failures=0,
        )

        translator_module._merge_fallback_state(shared, before, after)

        self.assertEqual(shared.active_idx, 1)
        self.assertEqual(shared.consecutive_primary_failures, 0)

    def test_stale_worker_cannot_advance_past_newer_shared_engine(self):
        shared = translator_module.FallbackState(
            active_idx=1,
            consecutive_primary_failures=0,
        )
        before = translator_module.FallbackState(
            active_idx=0,
            consecutive_primary_failures=0,
        )
        after = translator_module.FallbackState(
            active_idx=2,
            consecutive_primary_failures=0,
            primary_cooldown_until=time.monotonic() + 60.0,
        )

        translator_module._merge_fallback_state(shared, before, after)

        self.assertEqual(shared.active_idx, 1)
        self.assertEqual(shared.primary_cooldown_until, 0.0)


# ---------------------------------------------------------------------------
# Cache, slang, and short-input optimisations
# ---------------------------------------------------------------------------

class TestTranslateOptimizations(unittest.TestCase):

    def test_short_input_returns_none(self):
        t = _make_translator()
        self.assertIsNone(t.translate("a"))
        self.assertIsNone(t.translate(" "))
        self.assertIsNone(t.translate(""))

    def test_short_input_does_not_call_api(self):
        t = _make_translator()
        t.translate("a")
        for engine in t._engines:
            engine.translate.assert_not_called()

    def test_slang_returns_direct_without_api(self):
        from config import cfg
        t = _make_translator()
        ko, zh = next(iter(cfg.translation.slang.items()))
        result = t.translate(ko)
        self.assertEqual(result, zh)
        for engine in t._engines:
            engine.translate.assert_not_called()

    def test_translate_event_reports_api_metadata(self):
        t = _make_translator()
        t._engines[0].translate.return_value = "你好"

        outcome = t.translate_event("안녕하세요")

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.result_source, "api")
        # live mode default: DB cache layer disabled -> lookup reports "skipped"
        self.assertEqual(outcome.cache_status, "skipped")
        self.assertEqual(outcome.engine, "gemini")
        self.assertEqual(outcome.model, "gemini-test-model")
        self.assertEqual(outcome.target_text, "你好")

    def test_translate_event_reports_filtered_reason(self):
        t = _make_translator()

        outcome = t.translate_event("a")

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "policy")
        self.assertEqual(outcome.filter_reason, "too_short")
        for engine in t._engines:
            engine.translate.assert_not_called()

    def test_translate_event_uses_repetition_evidence_to_allow_emotional_speech(self):
        t = _make_translator()
        text = "어? 살려줘. 살려줘. 살려줘. 살려줘. 살려줘."

        outcome = t.translate_event(
            text,
            repetition_evidence=RepetitionEvidence(
                min_avg_logprob=-0.455,
                max_no_speech_prob=0.013,
                cut_reason="silence_complete",
                forced=False,
                incomplete=False,
            ),
        )

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.target_text, "你好")
        t._engines[0].translate.assert_called_once()

    def test_translate_event_incomplete_argument_overrides_stale_repetition_evidence(self):
        t = _make_translator()
        text = "어? 살려줘. 살려줘. 살려줘. 살려줘. 살려줘."

        outcome = t.translate_event(
            text,
            incomplete=True,
            repetition_evidence=RepetitionEvidence(
                min_avg_logprob=-0.455,
                max_no_speech_prob=0.013,
                cut_reason="silence_complete",
                forced=False,
                incomplete=False,
            ),
        )

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.filter_reason, "stt_garbage")
        for engine in t._engines:
            engine.translate.assert_not_called()

    def test_meta_garbage_engine_output_is_filtered(self):
        t = _make_translator()
        t._engines[0].translate.return_value = "（無法理解的STT亂碼，無明確語義）"
        t._record_success = MagicMock()

        outcome = t.translate_event("오늘 방송 진짜 재미있었어요")

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "post_policy")
        self.assertEqual(outcome.filter_reason, "meta_garbage_output")
        self.assertIsNone(outcome.target_text)
        t._record_success.assert_not_called()

        t._engines[0].translate.return_value = "valid retry"
        retry = t.translate_event("?月? 諻拖 鴔? ?禺站???渥?")
        self.assertEqual(retry.status, "success")
        self.assertEqual(retry.target_text, "valid retry")
        self.assertEqual(t._engines[0].translate.call_count, 2)

    def test_cached_meta_garbage_output_is_filtered(self):
        t = _make_translator()
        engine = t._active_engine()
        prompt_ver = t._get_prompt_version_hash()
        source = "오늘 방송 진짜 재미있었어요"
        t._memory.cache_store(
            source,
            False,
            "（無法理解的STT亂碼，無明確語義）",
            prompt_ver,
            engine,
        )

        outcome = t.translate_event(source)

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "post_policy")
        self.assertEqual(outcome.cache_status, "memory_hit")
        self.assertEqual(outcome.filter_reason, "meta_garbage_output")
        self.assertIsNone(t._memory.cache_lookup(source, False, prompt_ver, engine))
        self.assertEqual(list(t._memory.recent), [])
        for engine in t._engines:
            engine.translate.assert_not_called()

        t._engines[0].translate.return_value = "valid retry"
        retry = t.translate_event(source)
        self.assertEqual(retry.status, "success")
        self.assertEqual(retry.target_text, "valid retry")
        t._engines[0].translate.assert_called_once()

    def test_source_aware_corrections_fix_runtime_term_misfires(self):
        source = (
            "마가뜨는 게 정확히 말하는 거. 붕 뜨는 시간. 개복치같은 스타일. "
            "하데스. 끼윤이랑 예난이. 철구형. 갓 신내림 받은 무당이 더 신발 좋은 거 아시죠? 거의 만신이십니다."
        )
        result = (
            "瑪加特才是精確說的。飄起來的時間。鯛魚燒風格。哈迪斯。"
            "끼윤和藝蘭。鐵球哥。剛受神降的巫女不是更懂鞋嗎？幾乎都滿了，滿了。"
        )

        corrected = _apply_source_aware_corrections(source, result)

        self.assertEqual(
            corrected,
            "冷場才是精確說的。空掉的時間。玻璃心風格。HADES。"
            "Kkiyun和Yenan。Chulgu哥。剛受神降的巫女不是神力更強嗎？幾乎是大神巫。",
        )

    def test_source_aware_corrections_fix_current_hades_hot_terms(self):
        cases = (
            (
                "메이플은 좀 아기자기하네?",
                "《仙境傳說》確實有點萌萌的。",
                "《楓之谷》確實有點萌萌的。",
            ),
            (
                "메이플은 좀 아기자기하네?",
                "MapleStory確實有點萌萌的。",
                "楓之谷確實有點萌萌的。",
            ),
            (
                "프린세스 메이커 갓겜임.",
                "《公主製造》真是神作。",
                "《美少女夢工場》真是神作。",
            ),
            (
                "포스터가 피맛이 너무 느껴져서",
                "海報實在太有酒味了。",
                "海報實在太有血味了。",
            ),
            (
                "창나면 일단 사과부터 하면",
                "如果開戰，先道歉。",
                "如果出事，先道歉。",
            ),
            (
                "다 죽여버릴 것 같은 그런 느낌",
                "這有種讓人想死的感覺。",
                "這有種殺氣很重的感覺。",
            ),
        )

        for source, target, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(_apply_source_aware_corrections(source, target), expected)

    def test_stellive_hina_current_stream_terms_are_corrected(self):
        cases = (
            ("해둥이 금지", "海洞禁止", "해둥이禁止"),
            ("해둥아 너를 처음 본 순간부터", "海洞啊，從第一次見到你", "해둥아，從第一次見到你"),
            ("유니 선배 생일이구나", "優妮前輩生日啊", "Yuni前輩生日啊"),
            ("그냥 유니가 1기생이다", "Yuni是個日記生", "Yuni是個1期生"),
            ("히나유키 히나가 시켰다고 할게", "希拉尤基·Hina叫的", "Shirayuki Hina叫的"),
            ("메이플 하고 싶다", "想玩 Maple", "想玩 楓之谷"),
            ("투니버스 메들리 보고 들어왔어요", "看了Touriverus Madeline才進來的", "看了투니버스 메들리才進來的"),
        )

        with _active_translation_profile("stellive_hina"):
            for source, target, expected in cases:
                with self.subTest(source=source, target=target):
                    self.assertEqual(_apply_source_aware_corrections(source, target), expected)

    def test_stellive_hina_current_stream_terms_are_profile_gated(self):
        cases = (
            ("해둥이 금지", "海洞禁止"),
            ("유니 선배 생일이구나", "優妮前輩生日啊"),
            ("히나유키 히나가 시켰다고 할게", "希拉尤基·Hina叫的"),
            ("투니버스 메들리 보고 들어왔어요", "看了Touriverus Madeline才進來的"),
        )

        for profile_id in ("", "hades_chxxnnx", "mwmeu"):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    for source, target in cases:
                        self.assertEqual(_apply_source_aware_corrections(source, target), target)

    def test_source_aware_corrections_do_not_restore_stock_streamer_phrase(self):
        source = "내일 서버 설명회 때 한번 오시면 되겠습니다. 구독과 좋아요는 저에게 아주 큰 힘이 됩니다."
        target = "明天伺服器說明會時來一趟就好。"

        self.assertEqual(
            _apply_source_aware_corrections(source, target),
            target,
        )

    def test_mwmeu_name_rendering_fixes_runtime_variants(self):
        cases = (
            ("이비가 찾은 거예요", "伊比姐姐找到了", "이비姐姐找到了"),
            ("이비 언니랑 같이 해요", "李比姐姐一起玩", "이비姐姐一起玩"),
            ("수아가 답변했어요", "數亞姐姐回答了", "수아姐姐回答了"),
            ("리츠랑 초은이가 앉았어요", "利茨和初雲坐下了", "리츠和초은坐下了"),
            ("리츠와 아이들이요", "Rits與小孩們", "리츠與小孩們"),
            ("리츠가 많이 힘들었어", "リツ好像很累", "리츠好像很累"),
            ("리츠 언니가 했어요", "Ritz姐姐做了", "리츠姐姐做了"),
            ("지한 언니가 말했어요", "志安姐姐說了", "지한姐姐說了"),
            ("지한이가 왔어요", "Z-Han來了", "지한來了"),
            ("웬즈들이 가까이서 봤어요", "wenz們近距離看到了", "WENs們近距離看到了"),
        )

        with _active_translation_profile("mwmeu"):
            for source, target, expected in cases:
                with self.subTest(source=source, target=target):
                    self.assertEqual(_apply_source_aware_corrections(source, target), expected)

    def test_mwmeu_chiikawa_rendering_fixes_runtime_variants(self):
        source = "치이카와랑 하치와레랑 모몽가가 나왔어요"
        target = "千川和哈奇瓦還有毛毛蟲都出來了"

        with _active_translation_profile("mwmeu"):
            self.assertEqual(
                _apply_source_aware_corrections(source, target),
                "Chiikawa和Hachiware還有Momonga都出來了",
            )

    def test_mwmeu_japanese_phrase_near_miss_is_corrected(self):
        source = "다이저고 데스. 아리가또 고자이마스. 이러는 거야."
        target = "一進去就直接說：「Daisuki desu！Arigatou gozaimasu！」"

        with _active_translation_profile("mwmeu"):
            self.assertEqual(
                _apply_source_aware_corrections(source, target),
                "一進去就直接說：「大丈夫です！Arigatou gozaimasu！」",
            )

    def test_mwmeu_current_stream_hot_terms_are_corrected(self):
        cases = (
            ("오버쿡드 2 할게요", "Oma-kooks 立刻投！投！", "《胡鬧廚房2》"),
            ("어금니 같아요", "像牙齦", "像臼齒"),
            ("짬밥순이 아니었어?", "不是按餃子順序來的嗎？", "不是按資歷順來的嗎？"),
            ("땡글즈 플러스 이제", "Tanggulz Plus怡潔", "땡글즈 Plus 이제"),
            ("겟머츠 아니고 땡글즈예요", "不是GetMuts，是Tanggulz", "不是겟머츠，是땡글즈"),
            ("띠빵뽕 버스기사", "叮糖餅司機", "띠빵뽕司機"),
            ("신호등즈가 맞나?", "信號燈們對嗎？", "信號燈즈對嗎？"),
            ("리츠 선배는 했어요", "リツ生趴伊做到了", "리츠前輩做到了"),
        )

        with _active_translation_profile("mwmeu"):
            for source, target, expected in cases:
                with self.subTest(source=source, target=target):
                    self.assertEqual(_apply_source_aware_corrections(source, target), expected)

    def test_mwmeu_name_rendering_is_profile_gated(self):
        source = "리츠랑 초은이가 앉았어요"
        target = "利茨和初雲坐下了"

        with _active_translation_profile("hades_chxxnnx"):
            self.assertEqual(_apply_source_aware_corrections(source, target), target)
        with _active_translation_profile("mwmeu", use_profile=False):
            self.assertEqual(_apply_source_aware_corrections(source, target), target)

    def test_lilpa_name_rendering_fixes_runtime_variants(self):
        cases = (
            ("릴파님 출발 감사합니다", "前輩、릴파님，出發感謝您！", "前輩、Lilpa님，出發感謝您！"),
            ("릴파 사랑해", "莉爾帕，我愛你", "Lilpa，我愛你"),
            ("릴파가 왔어요", "莉爾法來了", "Lilpa來了"),
            ("릴파 선배님", "莉帕前輩", "Lilpa前輩"),
        )

        with _active_translation_profile("isegye_lilpa"):
            for source, target, expected in cases:
                with self.subTest(source=source, target=target):
                    once = _apply_source_aware_corrections(source, target)
                    twice = _apply_source_aware_corrections(source, once)
                    self.assertEqual(once, expected)
                    self.assertEqual(twice, once)

    def test_lilpa_name_rendering_remains_source_and_profile_gated(self):
        target = "莉爾法來了"
        with _active_translation_profile("isegye_lilpa"):
            self.assertEqual(
                _apply_source_aware_corrections("오늘 방송 재미있다", target),
                target,
            )
        with _active_translation_profile("url"):
            self.assertEqual(
                _apply_source_aware_corrections("릴파가 왔어요", target),
                target,
            )
        with _active_translation_profile("isegye_lilpa", use_profile=False):
            self.assertEqual(
                _apply_source_aware_corrections("릴파가 왔어요", target),
                target,
            )

    def test_streamer_name_rendering_boundary_positive_cases(self):
        cases = (
            ("챈나가 멤버 섭외", "快叫醒-chan", "快叫醒Chaenna"),
            ("챈나님 오늘 와요", "謝謝-chan", "謝謝Chaenna"),
            ("성태는 지금 와요", "Sungtae哥來了", "KimSungtae來了"),
            ("성태형 불러요", "Sungtae老師來了", "KimSungtae來了"),
            ("봉준이 왔어요", "Bongjun來了", "Kim Bongjun來了"),
            ("봉준님 불러요", "奉主來了", "Kim Bongjun來了"),
            ("김봉준이 말했어요", "奉俊說了", "Kim Bongjun說了"),
            ("키마는 대기 중", "Kima待機中", "Kyma待機中"),
            ("큐마는 대기 중", "큐마待機中", "Kyma待機中"),
            ("솜주먹 바보님", "桑拳頭笨蛋", "Sompunch笨蛋"),
            ("솜주먹 언니 와요", "拳頭姐姐來了", "Sompunch姐姐來了"),
            ("김띵귤 기강 잡아라", "金叮菊管管紀律", "Singgyul管管紀律"),
            ("띵귤이 왔어요", "TINGGYUL來了", "Singgyul來了"),
            ("띵띵이도 친구 많아", "Singgyul朋友很多", "띵띵이朋友很多"),
            ("김챗나 방", "金chat的房間", "Chaenna的房間"),
            ("챈나가 왔어요", "Chxxnnx來了", "Chaenna來了"),
            ("챈나가 왔어요", "CHXXNNX來了", "Chaenna來了"),
            ("찬나미들 천재야", "我們-chan娜們才是天才", "我們Chaenna粉才是天才"),
            ("찬나미들 천재야", "我們-chan娜才是天才", "我們Chaenna粉才是天才"),
            ("찬나미들 천재야", "我們Chaenna們才是天才", "我們Chaenna粉才是天才"),
            ("찬나미들 천재야", "我們Chaenna们才是天才", "我們Chaenna粉才是天才"),
            ("고세구가 왔어요", "高世久來了", "Gosegu來了"),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target, expected in cases:
                with self.subTest(source=source, target=target):
                    self.assertEqual(_apply_source_aware_corrections(source, target), expected)

    def test_streamer_name_rendering_hangul_self_forms(self):
        cases = (
            ("챈나가 멤버 섭외", "챈나好可愛", "Chaenna好可愛"),
            ("봉준이 왔어요", "봉준來了", "Kim Bongjun來了"),
            ("김봉준이 말했어요", "김봉준說了", "Kim Bongjun說了"),
            ("성태는 지금 와요", "성태來了", "KimSungtae來了"),
            ("키마는 대기 중", "키마待機中", "Kyma待機中"),
            ("솜펀치 언니 와요", "솜펀치姐姐來了", "Sompunch姐姐來了"),
            ("띵귤이 왔어요", "띵귤來了", "Singgyul來了"),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target, expected in cases:
                with self.subTest(source=source, target=target):
                    self.assertEqual(_apply_source_aware_corrections(source, target), expected)

    def test_streamer_name_rendering_boundary_negative_cases(self):
        cases = (
            ("김챈나가 왔어요", "快叫醒-chan"),
            ("성태권도 이야기", "Sungtae哥"),
            ("가성태님이 왔어요", "Sungtae老師"),
            ("박봉준이 왔어요", "Bongjun"),
            ("챈나님abc", "-chan"),
            ("오늘 정상적인 한국어 문장입니다", "Bongjun Sungtae 高世久"),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target in cases:
                with self.subTest(source=source, target=target):
                    self.assertEqual(_apply_source_aware_corrections(source, target), target)

    def test_streamer_name_rendering_is_source_gated(self):
        with _active_translation_profile("hades_chxxnnx"):
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "-chan"), "-chan")
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "Bongjun"), "Bongjun")
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "高世久"), "高世久")
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "챈나好可愛"), "챈나好可愛")
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "봉준來了"), "봉준來了")
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "김봉준說了"), "김봉준說了")
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "성태來了"), "성태來了")
            self.assertEqual(_apply_source_aware_corrections("오늘 방송 재미있다", "키마待機中"), "키마待機中")

    def test_streamer_name_rendering_is_profile_gated(self):
        hades_self_form_cases = (
            ("챈나가 왔어요", "챈나"),
            ("봉준이 왔어요", "봉준"),
            ("김봉준이 말했어요", "김봉준"),
            ("성태는 왔어요", "성태"),
            ("키마는 왔어요", "키마"),
        )

        for profile_id in ("", "stellive_hina", "isegye_lilpa"):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    self.assertEqual(
                        _apply_source_aware_corrections("챈나가 왔어요", "-chan"),
                        "-chan",
                    )
                    self.assertEqual(
                        _apply_source_aware_corrections("봉준이 왔어요", "Bongjun"),
                        "Bongjun",
                    )
                    for source, target in hades_self_form_cases:
                        self.assertEqual(_apply_source_aware_corrections(source, target), target)

        with _active_translation_profile("hades_chxxnnx", use_profile=False):
            self.assertEqual(_apply_source_aware_corrections("성태는 왔어요", "Sungtae"), "Sungtae")
            for source, target in hades_self_form_cases:
                self.assertEqual(_apply_source_aware_corrections(source, target), target)

        with _active_translation_profile("stellive_hina", use_profile=False):
            self.assertEqual(_apply_source_aware_corrections("고세구가 왔어요", "高世久"), "Gosegu")

    def test_bare_hina_existing_entry_is_unchanged(self):
        with _active_translation_profile("hades_chxxnnx"):
            self.assertEqual(_apply_source_aware_corrections("히나", "希娜"), "Hina")

    def test_streamer_name_rendering_is_idempotent(self):
        cases = (
            ("챈나가 왔어요", "-chan", "Chaenna"),
            ("봉준이 왔어요", "Bongjun", "Kim Bongjun"),
            ("성태는 왔어요", "Sungtae哥", "KimSungtae"),
            ("키마는 왔어요", "Kima", "Kyma"),
            ("고세구가 왔어요", "高世久", "Gosegu"),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target, expected in cases:
                with self.subTest(source=source, target=target):
                    once = _apply_source_aware_corrections(source, target)
                    twice = _apply_source_aware_corrections(source, once)
                    self.assertEqual(once, expected)
                    self.assertEqual(twice, once)

            self.assertEqual(_apply_source_aware_corrections("봉준이 왔어요", "Kim Bongjun"), "Kim Bongjun")
            self.assertEqual(_apply_source_aware_corrections("성태는 왔어요", "KimSungtae"), "KimSungtae")
            self.assertEqual(_apply_source_aware_corrections("챈나가 왔어요", "Chaenna好可愛"), "Chaenna好可愛")
            self.assertEqual(_apply_source_aware_corrections("키마는 왔어요", "Kyma待機中"), "Kyma待機中")

    def test_streamer_name_rendering_self_forms_are_idempotent(self):
        cases = (
            ("챈나가 왔어요", "챈나好可愛", "Chaenna好可愛"),
            ("봉준이 왔어요", "봉준來了", "Kim Bongjun來了"),
            ("김봉준이 말했어요", "김봉준說了", "Kim Bongjun說了"),
            ("성태는 왔어요", "성태來了", "KimSungtae來了"),
            ("키마는 왔어요", "키마待機中", "Kyma待機中"),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target, expected in cases:
                with self.subTest(source=source, target=target):
                    once = _apply_source_aware_corrections(source, target)
                    twice = _apply_source_aware_corrections(source, once)
                    self.assertEqual(once, expected)
                    self.assertEqual(twice, once)

    def test_streamer_name_rendering_fixes_mixed_canonical_and_wrong_forms(self):
        cases = (
            (
                "챈나가 왔어요",
                "-chan ... Chaenna ... -chan",
                "Chaenna ... Chaenna ... Chaenna",
                ("-chan", "-Chan", "챈나"),
            ),
            (
                "봉준이 왔어요",
                "Bongjun ... Kim Bongjun",
                "Kim Bongjun ... Kim Bongjun",
                ("Kim Kim Bongjun", "Kim BongjunKim Bongjun"),
            ),
            (
                "성태는 왔어요",
                "Sungtae哥 ... KimSungtae",
                "KimSungtae ... KimSungtae",
                ("KimKimSungtae", "KimSungtaeKimSungtae"),
            ),
            (
                "챈나가 왔어요",
                "챈나 ... Chaenna ... 챈나",
                "Chaenna ... Chaenna ... Chaenna",
                ("챈나", "ChaennaChaenna"),
            ),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target, expected, forbidden_fragments in cases:
                with self.subTest(source=source, target=target):
                    once = _apply_source_aware_corrections(source, target)
                    twice = _apply_source_aware_corrections(source, once)
                    self.assertEqual(once, expected)
                    self.assertEqual(twice, once)
                    for fragment in forbidden_fragments:
                        self.assertNotIn(fragment, once)

    def test_streamer_name_rendering_already_canonical_only_is_stable(self):
        cases = (
            ("챈나가 왔어요", "Chaenna"),
            ("봉준이 왔어요", "Kim Bongjun"),
            ("성태는 왔어요", "KimSungtae"),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target in cases:
                with self.subTest(source=source, target=target):
                    once = _apply_source_aware_corrections(source, target)
                    twice = _apply_source_aware_corrections(source, once)
                    self.assertEqual(once, target)
                    self.assertEqual(twice, once)

    def test_streamer_name_rendering_repeated_correction_has_no_artifacts(self):
        cases = (
            (
                "챈나가 왔어요",
                "Chaenna好可愛",
                "Chaenna好可愛",
                ("ChaennaChaenna",),
            ),
            (
                "봉준이 왔어요",
                "Kim Bongjun是個人",
                "Kim Bongjun是個人",
                ("Kim Kim Bongjun", "Kim BongjunKim Bongjun"),
            ),
            (
                "봉준이 왔어요",
                "Bongjun + Kim Bongjun + Bongjun",
                "Kim Bongjun + Kim Bongjun + Kim Bongjun",
                ("Kim Kim Bongjun", "Kim BongjunKim Bongjun"),
            ),
            (
                "성태는 왔어요",
                "KimSungtae是老師",
                "KimSungtae是老師",
                ("KimKimSungtae", "KimSungtaeKimSungtae"),
            ),
            (
                "성태는 왔어요",
                "Sungtae哥 + KimSungtae",
                "KimSungtae + KimSungtae",
                ("KimKimSungtae", "KimSungtaeKimSungtae"),
            ),
        )

        with _active_translation_profile("hades_chxxnnx"):
            for source, target, expected, forbidden_fragments in cases:
                with self.subTest(source=source, target=target):
                    once = _apply_source_aware_corrections(source, target)
                    twice = _apply_source_aware_corrections(source, once)
                    self.assertEqual(once, expected)
                    self.assertEqual(twice, once)
                    for fragment in forbidden_fragments:
                        self.assertNotIn(fragment, once)

    def test_streamer_name_rendering_mixed_forms_remain_source_and_profile_gated(self):
        with _active_translation_profile("hades_chxxnnx"):
            target = "-chan ... Chaenna ... -chan"
            self.assertEqual(
                _apply_source_aware_corrections("오늘 방송 재미있다", target),
                target,
            )

        for profile_id in ("", "stellive_hina", "isegye_lilpa"):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    target = "Bongjun ... Kim Bongjun"
                    self.assertEqual(
                        _apply_source_aware_corrections("봉준이 왔어요", target),
                        target,
                    )

        with _active_translation_profile("hades_chxxnnx", use_profile=False):
            target = "Sungtae哥 ... KimSungtae"
            self.assertEqual(
                _apply_source_aware_corrections("성태는 왔어요", target),
                target,
            )

    def test_streamer_name_rendering_cache_round_trip_does_not_double_apply(self):
        t = _make_translator()
        source = "봉준이 왔어요"
        with _active_translation_profile("hades_chxxnnx"):
            t._engines[0].translate.return_value = "Bongjun"

            first = t.translate_event(source)
            t._policy_state().reset_last_input()
            second = t.translate_event(source)

        self.assertEqual(first.result_source, "api")
        self.assertEqual(first.target_text, "Kim Bongjun")
        self.assertEqual(second.result_source, "memory_hit")
        self.assertEqual(second.target_text, "Kim Bongjun")
        self.assertEqual(t._engines[0].translate.call_count, 1)

    def test_irise_canonical_rendering_covers_api_and_memory_hit_paths(self):
        t = _make_translator()
        source = "키리씨가 왔어요"
        with _active_translation_profile("irise"):
            t._engines[0].translate.return_value = "基里來了。"

            first = t.translate_event(source)
            t._policy_state().reset_last_input()
            second = t.translate_event(source)

        self.assertEqual(first.result_source, "api")
        self.assertEqual(first.target_text, "KIIRI來了。")
        self.assertEqual(second.result_source, "memory_hit")
        self.assertEqual(second.target_text, "KIIRI來了。")
        self.assertEqual(t._engines[0].translate.call_count, 1)

    def test_translation_memory_honors_context_window_config(self):
        from config import cfg

        original = cfg.translation.context_window
        object.__setattr__(cfg.translation, "context_window", 10)
        try:
            memory = _new_translation_memory()
        finally:
            object.__setattr__(cfg.translation, "context_window", original)

        self.assertEqual(memory.recent.maxlen, 10)

    def test_prompt_version_uses_effective_engine_prompt(self):
        from config import cfg

        t = _make_translator()
        groq = _mock_engine("groq")
        original_compact = cfg.translation.groq_translation_compact_prompt
        original_profile = cfg.translation.use_profile
        object.__setattr__(cfg.translation, "groq_translation_compact_prompt", True)
        object.__setattr__(cfg.translation, "use_profile", False)
        try:
            base_hash = t._prompt_version("FULL PRIMARY PROMPT")
            groq_hash = t._prompt_version_for_engine(groq, "FULL PRIMARY PROMPT")
        finally:
            object.__setattr__(cfg.translation, "groq_translation_compact_prompt", original_compact)
            object.__setattr__(cfg.translation, "use_profile", original_profile)

        self.assertNotEqual(base_hash, groq_hash)

    def test_token_usage_is_omitted_when_usage_engine_differs_from_outcome(self):
        from modules.translation_engines import _log_token_usage, reset_last_token_usage

        reset_last_token_usage()
        _log_token_usage("nvidia", {"prompt_tokens": 99, "completion_tokens": 1})
        outcome = TranslationOutcome(
            source_text="source",
            target_text="target",
            status="success",
            result_source="api",
            cache_status="miss",
            incomplete=False,
            engine="groq",
            model="fallback-model",
        )

        self.assertEqual(_token_usage_for_outcome(outcome), {})

    def test_hot_path_rejection_reason_is_computed_once(self):
        t = _make_translator()
        t._engines[0].translate.return_value = "你好"
        original = t._policy.rejection_reason
        t._policy.rejection_reason = MagicMock(side_effect=original)

        outcome = t.translate_event("안녕하세요")

        self.assertEqual(outcome.status, "success")
        self.assertEqual(t._policy.rejection_reason.call_count, 1)

    def test_profile_prompt_version_changes_between_profiles(self):
        t = _make_translator()

        with _active_translation_profile("hades_chxxnnx"):
            hades_prompt_version = t._get_prompt_version_hash()
        with _active_translation_profile("stellive_hina"):
            stellive_prompt_version = t._get_prompt_version_hash()

        self.assertNotEqual(hades_prompt_version, stellive_prompt_version)

    def test_meta_garbage_detector_matches_explanatory_output(self):
        self.assertTrue(_looks_like_meta_garbage_output("（無法理解的STT亂碼，無明確語義）"))
        self.assertTrue(_looks_like_meta_garbage_output("（無意義詞，省略）"))
        self.assertFalse(_looks_like_meta_garbage_output("這句我無法理解你的心情。"))

    def test_placeholder_echo_is_meta_garbage(self):
        # Audit §15.4: 17x "（留空）" shipped as subtitles on 2026-07-11.
        for output in (
            "（空）", "(空)", "[空]。", "（留空）", "(留空)", "（留空）。", "[留空]", "（空白）",
            "（無輸出）", "（无输出）", "（零個字元）", "無輸出", "无输出。",
            "空字串", "沒有輸出", "（無翻譯）",
            "（輸出零個字元）",
            "（語義破碎，無法連貫翻譯，輸出零個字元）",
            "（商業廣告混入，直接輸出零個字元）",
            "啊，這句完全破碎，無法理解，輸出零個字元",
        ):
            self.assertTrue(_looks_like_meta_garbage_output(output), output)

    def test_legitimate_uses_of_placeholder_words_pass(self):
        # Bare 留空/空白 can be real translations (비워 둬 → 留空, 공백 → 空白),
        # and sentences merely containing the words are never placeholders.
        for output in (
            "空", "留空", "空白",
            "請把名字欄留空",
            "欄位顯示（空）",
            "畫面一片空白",
            "這裡沒有輸出孔",
            "這個函式會輸出零個字元",
        ):
            self.assertFalse(_looks_like_meta_garbage_output(output), output)

    def test_placeholder_engine_output_is_filtered_not_cached(self):
        t = _make_translator()
        t._engines[0].translate.return_value = "（空）"

        with patch.object(t, "_record_success") as record_success:
            outcome = t.translate_event("Another word")

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "post_policy")
        self.assertEqual(outcome.filter_reason, "meta_garbage_output")
        self.assertIsNone(outcome.target_text)
        record_success.assert_not_called()
        for fallback in t._engines[1:]:
            fallback.translate.assert_not_called()
        # A later identical input must not resurrect the placeholder from cache.
        t._engines[0].translate.return_value = "另一個詞"
        second = t.translate_event("Another word 2")
        self.assertEqual(second.target_text, "另一個詞")

    def test_single_word_explanatory_output_is_filtered(self):
        t = _make_translator()
        t._engines[0].translate.return_value = "（無意義詞，省略）"

        outcome = t.translate_event("글랜스")

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "post_policy")
        self.assertEqual(outcome.filter_reason, "meta_garbage_output")
        self.assertIsNone(outcome.target_text)

    def test_cache_hit_on_second_call(self):
        t = _make_translator()
        t._engines[0].translate.return_value = "你好"
        t.translate("안녕하세요")
        t.translate("안녕하세요")   # second call — should hit cache
        total_calls = sum(e.translate.call_count for e in t._engines)
        self.assertEqual(total_calls, 1, "API should be called only once; second call hits cache")

    def test_cache_evicts_when_full(self):
        from modules.translator import _CACHE_MAX_SIZE
        t = _make_translator()
        engine = t._active_engine()
        prompt_ver = t._get_prompt_version_hash()
        for i in range(_CACHE_MAX_SIZE):
            t._memory.cache[
                (f"key{i}", False, prompt_ver, engine.engine_name, engine.model_name)
            ] = f"val{i}"
        t._memory.cache_store("overflow", False, "x", prompt_ver, engine)
        self.assertLessEqual(len(t._memory.cache), _CACHE_MAX_SIZE)
        self.assertIn(
            ("overflow", False, prompt_ver, engine.engine_name, engine.model_name),
            t._memory.cache,
        )

    def test_incomplete_lookup_skips_db(self):
        t = _make_translator()
        t._memory.db_lookup = MagicMock(return_value="DB result")
        lookup = t._lookup_existing_translation_event(
            "불완전한 문장", True, t._get_prompt_version_hash()
        )
        self.assertIsNone(lookup.result)
        self.assertEqual(lookup.source, "skipped")
        t._memory.db_lookup.assert_not_called()


class TestMaxTranslateCharsGuard(unittest.TestCase):
    """#6 — oversized inputs must be rejected before reaching any engine."""

    def _make_with_cap(self, cap: int = 10) -> Translator:
        t = _make_translator()
        # Replace the auto-built policy with one whose cap we control.
        from modules.translation_policy import TranslationPolicy
        from config import cfg
        t._policy = TranslationPolicy(
            slang=cfg.translation.slang,
            min_translate_chars=2,
            max_translate_chars=cap,
        )
        return t

    def test_too_long_skips_engine_call(self):
        t = self._make_with_cap(cap=10)
        outcome = t.translate_event("x" * 50, incomplete=False)
        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.filter_reason, "too_long")
        self.assertIsNone(outcome.target_text)
        for e in t._engines:
            e.translate.assert_not_called()

    def test_within_cap_reaches_engine(self):
        t = self._make_with_cap(cap=100)
        outcome = t.translate_event("안녕하세요", incomplete=False)
        # Engine returns "你好" by default per _mock_engine
        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.target_text, "你好")
        self.assertEqual(outcome.filter_reason, "")


class TestSttTemplateGarbageGuard(unittest.TestCase):
    """#8 — STT template hallucinations must be rejected before any engine,
    memory lookup or DB write."""

    _TEMPLATE = "시청해주셔서 감사합니다."

    def test_stt_template_garbage_skips_engine_call(self):
        t = _make_translator()

        outcome = t.translate_event(self._TEMPLATE, incomplete=False)

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "policy")
        self.assertEqual(outcome.filter_reason, "stt_template_garbage")
        self.assertIsNone(outcome.target_text)
        for e in t._engines:
            e.translate.assert_not_called()

    def test_subscribe_cta_template_skips_engine_call(self):
        t = _make_translator()

        outcome = t.translate_event("구독과 좋아요 부탁드립니다!", incomplete=False)

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "policy")
        self.assertEqual(outcome.filter_reason, "stt_template_garbage")
        self.assertIsNone(outcome.target_text)
        for e in t._engines:
            e.translate.assert_not_called()

    def test_stt_template_garbage_not_written_to_memory_or_db(self):
        t = _make_translator()
        t._record_success = MagicMock()

        t.translate_event(self._TEMPLATE, incomplete=False)

        t._record_success.assert_not_called()

    def test_stt_template_garbage_outcome_exposes_filter_reason(self):
        # runtime_events writes whatever `filter_reason` the outcome carries
        # via as_event_fields (same path as the `too_long` precedent) —
        # verify it is observable in the emitted event payload.
        t = _make_translator()

        outcome = t.translate_event(self._TEMPLATE, incomplete=False)
        event = outcome.as_event_fields(0.0, {})

        self.assertEqual(event["status"], "filtered")
        self.assertEqual(event["filter_reason"], "stt_template_garbage")


class TestSttTemplateFragmentSanitizer(unittest.TestCase):
    """#9 — boundary template fragments are stripped before translation."""

    _RAW = "시청해주셔서 감사합니다. 엄청나게 그렇잖아."
    _SANITIZED = "엄청나게 그렇잖아."

    def test_sanitizer_engine_receives_sanitized_text(self):
        t = _make_translator()

        t.translate_event(self._RAW, incomplete=False)

        self.assertEqual(t._engines[0].translate.call_args.args[0], self._SANITIZED)
        t._engines[1].translate.assert_not_called()

    def test_sanitizer_db_stores_sanitized_key(self):
        t = _make_translator()
        t._record_success = MagicMock()

        t.translate_event(self._RAW, incomplete=False)

        t._record_success.assert_called_once()
        self.assertEqual(t._record_success.call_args.args[0], self._SANITIZED)

    def test_sanitizer_outcome_source_text_is_original(self):
        t = _make_translator()

        outcome = t.translate_event(self._RAW, incomplete=False)

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.source_text, self._RAW)
        self.assertEqual(t._engines[0].translate.call_args.args[0], self._SANITIZED)

    def test_sanitized_to_empty_has_expected_filter_reason(self):
        t = _make_translator()

        outcome = t.translate_event("시청해주셔서 감사합니다. abcde!", incomplete=False)

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "policy")
        self.assertEqual(outcome.filter_reason, "stt_sanitized_empty")
        for e in t._engines:
            e.translate.assert_not_called()

    def test_hard_template_tail_engine_receives_sanitized_text(self):
        t = _make_translator()
        raw = (
            "입주비는 안 받습니다. 저희 스폰서분들도 너무 감사하게도 좀 많이 붙어가지고. "
            "자막 제공 및 자막 제공 및 광고를 포함하고 있습니다."
        )
        sanitized = "입주비는 안 받습니다. 저희 스폰서분들도 너무 감사하게도 좀 많이 붙어가지고"

        outcome = t.translate_event(raw, incomplete=False)

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.source_text, raw)
        self.assertEqual(t._engines[0].translate.call_args.args[0], sanitized)

    def test_subscribe_cta_prefix_engine_receives_sanitized_text(self):
        with _active_translation_profile("hades_chxxnnx"):
            t = _make_translator()
            raw = "구독과 좋아요 부탁 어? 진짜? 카페에 챗나룩 서버 포스터 누가 큐티 버전으로 올려주셨다고요?"
            sanitized = "어? 진짜? 카페에 챈나룩 서버 포스터 누가 큐티 버전으로 올려주셨다고요?"

            outcome = t.translate_event(raw, incomplete=False)

            self.assertEqual(outcome.status, "success")
            self.assertEqual(outcome.source_text, raw)
            self.assertEqual(t._engines[0].translate.call_args.args[0], sanitized)

    def test_subscribe_topic_prefix_engine_receives_sanitized_text(self):
        t = _make_translator()
        raw = "구독과 좋아요는 저 이제 2집 녹음하러 가거든요? 여러분들?"
        sanitized = "저 이제 2집 녹음하러 가거든요? 여러분들?"

        outcome = t.translate_event(raw, incomplete=False)

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.source_text, raw)
        self.assertEqual(t._engines[0].translate.call_args.args[0], sanitized)

    def test_hard_template_prefix_engine_receives_sanitized_text(self):
        t = _make_translator()
        raw = "자막 제공 및 자막 제공 및 광고를 포함하고 있습니다. 우와! 너무 예쁘던데?"
        sanitized = "우와! 너무 예쁘던데?"

        outcome = t.translate_event(raw, incomplete=False)

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.source_text, raw)
        self.assertEqual(t._engines[0].translate.call_args.args[0], sanitized)


class TestSttLowValueFragmentGuard(unittest.TestCase):
    def test_low_value_fragment_skips_engine_call(self):
        t = _make_translator()

        outcome = t.translate_event("도도리코 소라에 타받세 도개가 사라지게 된 날", incomplete=False)

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "policy")
        self.assertEqual(outcome.filter_reason, "stt_low_value_fragment")
        self.assertIsNone(outcome.target_text)
        for e in t._engines:
            e.translate.assert_not_called()

    def test_low_value_tail_engine_receives_sanitized_text(self):
        t = _make_translator()
        raw = "그걸 직접 만들 수 있다고? 너무 기대돼 망간부 바카스탕 골라요"
        sanitized = "그걸 직접 만들 수 있다고? 너무 기대돼"

        outcome = t.translate_event(raw, incomplete=False)

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.source_text, raw)
        self.assertEqual(t._engines[0].translate.call_args.args[0], sanitized)


class TestSttSongFragmentGuard(unittest.TestCase):
    def test_song_fragment_skips_engine_call(self):
        t = _make_translator()

        outcome = t.translate_event(
            "쓰읍... 락이라면서 띵시렁띵시렁 흐흐흐 나는 아름다운 남의 날개를",
            incomplete=False,
        )

        self.assertEqual(outcome.status, "filtered")
        self.assertEqual(outcome.result_source, "policy")
        self.assertEqual(outcome.filter_reason, "stt_song_fragment")
        self.assertIsNone(outcome.target_text)
        for e in t._engines:
            e.translate.assert_not_called()


class TestSourceNormBeforeMatching(unittest.TestCase):
    def test_stellive_hina_profile_normalizes_runtime_variants(self):
        raw = "히나유키 히나랑 해동이 일기생이 투리버스 메들린 얘기했어"

        with _active_translation_profile("stellive_hina"):
            self.assertEqual(
                _normalize_source_before_matching(raw),
                "시라유키 히나랑 해둥이 1기생이 투니버스 메들리 얘기했어",
            )

    def test_stellive_hina_norm_is_profile_and_use_profile_gated(self):
        raw = "히나유키 히나랑 해동이 일기생"

        for profile_id in ("", "hades_chxxnnx", "mwmeu", "isegye_lilpa"):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    self.assertEqual(_normalize_source_before_matching(raw), raw)

        with _active_translation_profile("stellive_hina", use_profile=False):
            self.assertEqual(_normalize_source_before_matching(raw), raw)

    def test_hades_profile_normalizes_mixed_script(self):
        with _active_translation_profile("hades_chxxnnx"):
            self.assertEqual(_normalize_source_before_matching("服주 화이팅"), "섭주 화이팅")
            self.assertEqual(_normalize_source_before_matching("서버 服주입니다"), "서버 섭주입니다")
            self.assertEqual(_normalize_source_before_matching("服주"), "섭주")

    def test_hades_use_profile_false_no_change(self):
        with _active_translation_profile("hades_chxxnnx", use_profile=False):
            self.assertEqual(_normalize_source_before_matching("服주 화이팅"), "服주 화이팅")

    def test_other_profiles_no_change(self):
        for profile_id in ("stellive_hina", "isegye_lilpa", ""):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    self.assertEqual(
                        _normalize_source_before_matching("服주 화이팅"), "服주 화이팅"
                    )

    def test_seop_jeong_unchanged_in_all_profiles(self):
        for profile_id in ("hades_chxxnnx", "stellive_hina", "isegye_lilpa", ""):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    self.assertEqual(
                        _normalize_source_before_matching("섭정 있는 거야?"),
                        "섭정 있는 거야?",
                    )

    def test_existing_slang_variants_pass_through(self):
        with _active_translation_profile("hades_chxxnnx"):
            self.assertEqual(_normalize_source_before_matching("섭쥬 화이팅"), "섭쥬 화이팅")
            self.assertEqual(_normalize_source_before_matching("썹주 화이팅"), "썹주 화이팅")
            self.assertEqual(_normalize_source_before_matching("SUBJU"), "SUBJU")
            self.assertEqual(_normalize_source_before_matching("服主"), "服主")

    def test_normalization_is_idempotent(self):
        with _active_translation_profile("hades_chxxnnx"):
            once = _normalize_source_before_matching("服주 화이팅")
            twice = _normalize_source_before_matching(once)
            self.assertEqual(once, "섭주 화이팅")
            self.assertEqual(twice, once)

    def test_hades_runtime_name_variants_normalized(self):
        cases = [
            ("김띵귤 왔어", "띵귤 왔어"),
            ("김챗나 방", "챈나 방"),
            ("김챔나 방", "챈나 방"),
            ("챗나야 빨리 와", "챈나야 빨리 와"),
            ("주먹이 왔어", "솜펀치 왔어"),
            ("주먹 언니 와요", "솜펀치 언니 와요"),
            ("팅귤이 왔어", "띵귤이 왔어"),
            ("틴귤아 와", "띵귤아 와"),
            ("큐마는 대기 중", "키마는 대기 중"),
            ("채엔나 방", "챈나 방"),
            ("차엔나 방", "챈나 방"),
        ]
        with _active_translation_profile("hades_chxxnnx"):
            for raw, expected in cases:
                with self.subTest(raw=raw):
                    once = _normalize_source_before_matching(raw)
                    self.assertEqual(once, expected)
                    self.assertEqual(_normalize_source_before_matching(once), once)

    def test_hades_채나_family_normalized(self):
        cases = [
            ("채나", "챈나"),
            ("채나야", "챈나야"),
            ("채나님", "챈나님"),
            ("채나로", "챈나로"),
            ("채나롱", "챈나롱"),
            ("채나룬", "챈나룬"),
            ("천사채나", "천사챈나"),
        ]
        with _active_translation_profile("hades_chxxnnx"):
            for raw, expected in cases:
                with self.subTest(raw=raw):
                    self.assertEqual(_normalize_source_before_matching(raw), expected)

    def test_채나_compound_no_double_replace(self):
        with _active_translation_profile("hades_chxxnnx"):
            self.assertEqual(_normalize_source_before_matching("천사채나"), "천사챈나")

    def test_채나_norm_is_profile_gated(self):
        for profile_id in ("stellive_hina", "isegye_lilpa", ""):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    self.assertEqual(_normalize_source_before_matching("채나"), "채나")

    def test_채나_norm_is_use_profile_gated(self):
        with _active_translation_profile("hades_chxxnnx", use_profile=False):
            self.assertEqual(_normalize_source_before_matching("채나"), "채나")

    def test_mwmeu_profile_normalizes_runtime_name_and_fandom_variants(self):
        raw = "이변이한테 초운이 집에 가야 되고 조은아 엔즈들이 기다려. 이츠가 소아가 지안 언니랑 왔어."

        with _active_translation_profile("mwmeu"):
            self.assertEqual(
                _normalize_source_before_matching(raw),
                "이비한테 초은이 집에 가야 되고 초은아 웬즈들이 기다려. 리츠가 수아가 지한 언니랑 왔어.",
            )

    def test_mwmeu_profile_normalizes_current_stream_game_variants(self):
        raw = (
            "오마쿡스 바로 투 하자. 플러스 인제 생빠이니까 토화기 들고 "
            "명예 소변관 해. 나는 이 가나디아인데."
        )

        with _active_translation_profile("mwmeu"):
            self.assertEqual(
                _normalize_source_before_matching(raw),
                "오버쿡드 2 하자. 플러스 이제 선배니까 소화기 들고 "
                "명예 소방관 해. 나는 이 강아지인데.",
            )

    def test_mwmeu_profile_normalizes_chiikawa_runtime_variants(self):
        raw = "시에가파크 갔다가 시가와 굿즈랑 하치와래랑 모몽가를 봤어."

        with _active_translation_profile("mwmeu"):
            self.assertEqual(
                _normalize_source_before_matching(raw),
                "치이카와파크 갔다가 치이카와 굿즈랑 하치와레랑 모몽가를 봤어.",
            )

    def test_mwmeu_norm_is_profile_and_use_profile_gated(self):
        raw = "이변이한테 초운이랑 시에가파크 얘기했어"

        for profile_id in ("hades_chxxnnx", "stellive_hina", "isegye_lilpa", ""):
            with self.subTest(profile_id=profile_id):
                with _active_translation_profile(profile_id):
                    self.assertEqual(_normalize_source_before_matching(raw), raw)

        with _active_translation_profile("mwmeu", use_profile=False):
            self.assertEqual(_normalize_source_before_matching(raw), raw)


class TestSourceNormIntegration(unittest.TestCase):
    def test_hades_lol_engine_receives_canonical_game_name(self):
        with _active_translation_profile("hades_chxxnnx"):
            t = _make_translator()
            t._engines[0].translate.return_value = "真的沒有治療《英雄聯盟》成癮的方法嗎？"

            outcome = t.translate_event("아니 롤 중독 치료하는 건 없나?")

            self.assertEqual(outcome.source_text, "아니 롤 중독 치료하는 건 없나?")
            self.assertEqual(outcome.target_text, "真的沒有治療《英雄聯盟》成癮的方法嗎？")
            call_text = t._engines[0].translate.call_args[0][0]
            self.assertEqual(call_text, "아니 LoL 중독 치료하는 건 없나?")

    def test_higedan_boundary_alias_engine_receives_canonical_source(self):
        with _active_translation_profile("isegye_lilpa"):
            t = _make_translator()
            t._engines[0].translate.return_value = "Official髭男dism的現場演出很棒"

            outcome = t.translate_event("희계단분들이 공연했어요")

            self.assertEqual(outcome.source_text, "희계단분들이 공연했어요")
            self.assertEqual(outcome.target_text, "Official髭男dism的現場演出很棒")
            call_text = t._engines[0].translate.call_args[0][0]
            self.assertEqual(call_text, "히게단분들이 공연했어요")

    def test_standalone_hanja_hangul_hits_slang(self):
        """translate_event("服주") under HADES → slang hit, engine not called, raw source preserved."""
        with _active_translation_profile("hades_chxxnnx"):
            t = _make_translator()
            outcome = t.translate_event("服주")
            self.assertEqual(outcome.status, "success")
            self.assertEqual(outcome.result_source, "slang")
            self.assertEqual(outcome.target_text, "服主")
            self.assertEqual(outcome.source_text, "服주")
            for e in t._engines:
                e.translate.assert_not_called()

    def test_mid_sentence_engine_receives_normalized_text(self):
        """translate_event mid-sentence → engine called with 섭주, source_text stays raw."""
        with _active_translation_profile("hades_chxxnnx"):
            t = _make_translator()
            t._engines[0].translate.return_value = "伺服器 服主 加油"
            outcome = t.translate_event("서버 服주 화이팅")
            self.assertEqual(outcome.source_text, "서버 服주 화이팅")
            t._engines[0].translate.assert_called_once()
            call_text = t._engines[0].translate.call_args[0][0]
            self.assertEqual(call_text, "서버 섭주 화이팅")

    def test_wrong_profile_engine_receives_raw_text(self):
        """Wrong profile → no normalization → engine sees raw 服주."""
        with _active_translation_profile("stellive_hina"):
            t = _make_translator()
            t.translate_event("服주 화이팅")
            call_text = t._engines[0].translate.call_args[0][0]
            self.assertIn("服주", call_text)

    def test_use_profile_false_engine_receives_raw_text(self):
        """use_profile=False → no normalization → engine sees raw 服주."""
        with _active_translation_profile("hades_chxxnnx", use_profile=False):
            t = _make_translator()
            t.translate_event("服주 화이팅")
            call_text = t._engines[0].translate.call_args[0][0]
            self.assertIn("服주", call_text)

    def test_seop_jeong_engine_receives_unchanged_source(self):
        """섭정 under HADES → engine receives unchanged Korean source."""
        with _active_translation_profile("hades_chxxnnx"):
            t = _make_translator()
            t.translate_event("섭정 있는 거야?")
            call_text = t._engines[0].translate.call_args[0][0]
            self.assertEqual(call_text, "섭정 있는 거야?")

    def test_mwmeu_runtime_variants_engine_receives_normalized_text(self):
        with _active_translation_profile("mwmeu"):
            t = _make_translator()
            t._engines[0].translate.return_value = "初雲和千川"

            outcome = t.translate_event("초운이 시에가파크 갔어")

            self.assertEqual(outcome.source_text, "초운이 시에가파크 갔어")
            self.assertEqual(outcome.target_text, "초은和Chiikawa")
            call_text = t._engines[0].translate.call_args[0][0]
            self.assertEqual(call_text, "초은이 치이카와파크 갔어")

    def test_mwmeu_current_stream_variants_engine_receives_normalized_text(self):
        with _active_translation_profile("mwmeu"):
            t = _make_translator()
            t._engines[0].translate.return_value = "Oma-kooks 立刻投！投！"

            outcome = t.translate_event("오마쿡스 바로 투 할게")

            self.assertEqual(outcome.source_text, "오마쿡스 바로 투 할게")
            self.assertEqual(outcome.target_text, "《胡鬧廚房2》")
            call_text = t._engines[0].translate.call_args[0][0]
            self.assertEqual(call_text, "오버쿡드 2 할게")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# H1 regression: a malformed queue item must not stall the in-order emit loop
# ---------------------------------------------------------------------------

class TestMalformedItemDoesNotStallEmit(unittest.TestCase):

    def test_bad_item_then_good_item_still_emits(self):
        from modules.translator import start as translator_start

        class _Boom:
            def __str__(self):
                raise RuntimeError("malformed pipeline item")

        sentence_q: queue.Queue = queue.Queue()
        subtitle_q: queue.Queue = queue.Queue()
        stop = threading.Event()
        engine = _mock_engine("primary", "\u4f60\u597d")

        with patch.object(translator_module, "_build_engine_chain", return_value=[engine]), \
                patch.object(translator_module, "runtime_events") as events:
            translator_start(sentence_q, subtitle_q, stop)
            sentence_q.put(_Boom())   # seq 0 — translate_item raises on sentence_text
            sentence_q.put({"text": "\uc548\ub155\ud558\uc138\uc694 \uc624\ub298 \ubc29\uc1a1 \uc2dc\uc791\ud569\ub2c8\ub2e4", "incomplete": False})

            deadline = time.monotonic() + 5.0
            result = None
            while time.monotonic() < deadline:
                try:
                    result = subtitle_q.get(timeout=0.1)
                    break
                except queue.Empty:
                    continue
            stop.set()

        self.assertEqual(result, "\u4f60\u597d", "good item after malformed item must still emit")
        emitted = [call.kwargs for call in events.emit.call_args_list]
        self.assertGreaterEqual(len(emitted), 2)
        self.assertTrue(all(event["translation_mode"] == "live" for event in emitted))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# P2: DB cache layer gating by translation mode
# ---------------------------------------------------------------------------

class TestDbCacheGating(unittest.TestCase):
    """Live mode skips the SQLite cache (0.45% measured hit rate); clip keeps it."""

    def _translator_with_db_spies(self):
        t = _make_translator()
        t._engines[0].translate.return_value = "你好"
        t._memory.db_lookup = MagicMock(return_value=None)
        t._memory.db_store = MagicMock()
        return t

    def _set_mode(self, mode):
        from config import cfg
        original = cfg.translation.translation_mode
        object.__setattr__(cfg.translation, "translation_mode", mode)
        return original

    def test_live_mode_skips_db_lookup_and_store(self):
        original = self._set_mode("live")
        try:
            t = self._translator_with_db_spies()
            outcome = t.translate_event("안녕하세요 방송 시작합니다")
            self.assertEqual(outcome.status, "success")
            t._memory.db_lookup.assert_not_called()
            t._memory.db_store.assert_not_called()
        finally:
            self._set_mode(original)

    def test_clip_mode_uses_db_cache(self):
        original = self._set_mode("clip")
        try:
            t = self._translator_with_db_spies()
            outcome = t.translate_event("안녕하세요 방송 시작합니다")
            self.assertEqual(outcome.status, "success")
            t._memory.db_lookup.assert_called_once()
            t._memory.db_store.assert_called_once()
        finally:
            self._set_mode(original)

    def test_live_mode_flag_reenables_db_cache(self):
        from config import cfg
        original = self._set_mode("live")
        original_flag = cfg.database.live_db_cache
        object.__setattr__(cfg.database, "live_db_cache", True)
        try:
            t = self._translator_with_db_spies()
            t.translate_event("안녕하세요 방송 시작합니다")
            t._memory.db_lookup.assert_called_once()
            t._memory.db_store.assert_called_once()
        finally:
            object.__setattr__(cfg.database, "live_db_cache", original_flag)
            self._set_mode(original)


class TestProvisionalPromotion(unittest.TestCase):
    def test_route_off_defensively_rejects_queued_provisional_work(self):
        sentence_q = queue.Queue()
        provisional_q = queue.Queue()
        subtitle_q = queue.Queue()
        stop = threading.Event()
        request = ProvisionalRequest(
            provisional_id="provisional:route-off",
            text="queued before emergency off",
            incomplete=True,
            profile_id="url",
            source_utterance_ids=("utt-route-off",),
            evidence_source_utterance_ids=("utt-route-off",),
            activity_snapshot=capture_activity_snapshot("chatting", source="manual"),
            requested_at_monotonic=time.monotonic(),
            first_stt_ready_at_monotonic=time.monotonic(),
        )
        original_route = translator_module.cfg.translation.deepseek_route
        object.__setattr__(translator_module.cfg.translation, "deepseek_route", "off")
        deepseek_cls = MagicMock()
        try:
            with patch.object(translator_module, "DeepSeekTranslationEngine", deepseek_cls):
                thread = translator_module.start(
                    sentence_q,
                    subtitle_q,
                    stop,
                    provisional_queue=provisional_q,
                )
                provisional_q.put(request)
                deadline = time.monotonic() + 2
                while not provisional_q.empty() and time.monotonic() < deadline:
                    stop.wait(0.005)
                stop.wait(0.05)
                stop.set()
                thread.join(timeout=2)
        finally:
            object.__setattr__(
                translator_module.cfg.translation,
                "deepseek_route",
                original_route,
            )

        deepseek_cls.assert_not_called()
        self.assertTrue(subtitle_q.empty())

    def _candidate(
        self,
        translator: Translator,
        source: str,
        target: str,
        *,
        source_ids=("utt-preview",),
        evidence_ids=("utt-preview",),
        incomplete=True,
    ) -> ProvisionalCandidate:
        snapshot = translator_module.bound_activity_snapshot()
        self.assertIsNotNone(snapshot)
        cohort = translator._history_cohort()
        history = translator._memory_state().context(cohort)
        system_prompt = translator._build_system_prompt()
        obligations = translator_module._resolve_active_canonical_obligations(source)
        known_source_spans = tuple(
            span
            for obligation in obligations
            for span in obligation.source_spans
        )
        escrow = resolve_unknown_name_escrow(
            source,
            known_source_spans=known_source_spans,
        )
        semantic_terminology = resolve_semantic_terminology(escrow.provider_source)
        messages = build_effective_deepseek_messages(
            semantic_terminology.provider_source, system_prompt, incomplete, history
        )
        return ProvisionalCandidate(
            provisional_id="provisional:utt-preview",
            raw_target=target,
            display_target=target,
            fingerprint=provisional_fingerprint(
                prepared_source=source,
                source_utterance_ids=source_ids,
                evidence_source_utterance_ids=evidence_ids,
                profile_id=cohort[0],
                activity_cache_identity=snapshot.cache_identity,
                history_cohort=cohort,
                messages=messages,
                incomplete=incomplete,
            ),
            engine="deepseek",
            model="deepseek-v4-flash",
            requested_at_monotonic=1.0,
            completed_at_monotonic=2.0,
            usage={},
            diagnostics={},
        )

    def test_unchanged_exact_fingerprint_promotes_without_provider_call(self):
        translator = _make_translator()
        engine = _route_engine("deepseek", "不應呼叫")
        translator._engines = [engine]
        source = "오늘 방송 재미있어요"
        snapshot = capture_activity_snapshot("chatting", source="manual")

        with _active_translation_profile("url"), bind_activity_snapshot(snapshot):
            candidate = self._candidate(translator, source, "今天直播很有趣")
            outcome = translator.translate_event(
                source,
                True,
                provisional_candidate=candidate,
                source_utterance_ids=("utt-preview",),
                evidence_source_utterance_ids=("utt-preview",),
            )

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.target_text, "今天直播很有趣")
        self.assertEqual(outcome.result_source, "provisional_promotion")
        engine.translate_messages.assert_not_called()
        self.assertTrue(translator._last_provisional_trace["promotion_passed"])

    def test_changed_evidence_identity_discards_preview_and_translates_normally(self):
        translator = _make_translator()
        engine = _route_engine("deepseek", "最終翻譯")
        translator._engines = [engine]
        source = "오늘 방송 재미있어요"
        snapshot = capture_activity_snapshot("chatting", source="manual")

        with _active_translation_profile("url"), bind_activity_snapshot(snapshot):
            candidate = self._candidate(translator, source, "暫定翻譯")
            outcome = translator.translate_event(
                source,
                True,
                provisional_candidate=candidate,
                source_utterance_ids=("utt-changed",),
                evidence_source_utterance_ids=("utt-changed",),
            )

        self.assertEqual(outcome.target_text, "最終翻譯")
        engine.translate_messages.assert_called_once()
        self.assertTrue(translator._last_provisional_trace["fingerprint_mismatch"])
        self.assertTrue(translator._last_provisional_trace["final_retranslation"])

    def test_promotion_still_enforces_canonical_obligations(self):
        translator = _make_translator()
        engine = _route_engine("deepseek", "모카來了")
        translator._engines = [engine]
        source = "모카가 왔어요"
        snapshot = capture_activity_snapshot("chatting", source="manual")

        with _active_translation_profile("url"), bind_activity_snapshot(snapshot):
            candidate = self._candidate(translator, source, "她來了")
            outcome = translator.translate_event(
                source,
                True,
                provisional_candidate=candidate,
                source_utterance_ids=("utt-preview",),
                evidence_source_utterance_ids=("utt-preview",),
            )

        self.assertEqual(outcome.target_text, "모카來了")
        engine.translate_messages.assert_called_once()
        self.assertEqual(
            translator._last_provisional_trace["guard_rejection"],
            "canonical_obligation_missing",
        )
        self.assertTrue(translator._last_provisional_trace["final_retranslation"])

    def test_promoted_raw_candidate_receives_normal_final_corrections(self):
        translator = _make_translator()
        engine = _route_engine("deepseek", "不應呼叫")
        translator._engines = [engine]
        source = "모카가 왔어요"
        snapshot = capture_activity_snapshot("chatting", source="manual")

        with _active_translation_profile("url"), bind_activity_snapshot(snapshot):
            candidate = self._candidate(
                translator, source, "\u6469\u5361\u4f86\u4e86"
            )
            outcome = translator.translate_event(
                source,
                True,
                provisional_candidate=candidate,
                source_utterance_ids=("utt-preview",),
                evidence_source_utterance_ids=("utt-preview",),
            )

        self.assertEqual(outcome.result_source, "provisional_promotion")
        self.assertEqual(outcome.target_text, "\ubaa8\uce74\u4f86\u4e86")
        engine.translate_messages.assert_not_called()

    def test_unknown_name_placeholder_promotes_and_restores_exact_hangul(self):
        translator = _make_translator()
        engine = _route_engine("deepseek", "不應呼叫")
        translator._engines = [engine]
        source = "모찌한테 가야 돼"
        snapshot = capture_activity_snapshot("chatting", source="manual")

        with _active_translation_profile("hades_chxxnnx"), bind_activity_snapshot(snapshot):
            candidate = self._candidate(
                translator,
                source,
                "得去找__LT_UNK_1__。",
            )
            outcome = translator.translate_event(
                source,
                True,
                provisional_candidate=candidate,
                source_utterance_ids=("utt-preview",),
                evidence_source_utterance_ids=("utt-preview",),
            )

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.result_source, "provisional_promotion")
        self.assertEqual(outcome.target_text, "得去找모찌。")
        engine.translate_messages.assert_not_called()

    def test_semantic_terminology_placeholder_promotes_and_restores(self):
        translator = _make_translator()
        engine = _route_engine("deepseek", "不應呼叫")
        translator._engines = [engine]
        source = "내가 좀 사패가 되는 것 같아"
        snapshot = capture_activity_snapshot("chatting", source="manual")

        with _active_translation_profile("url"), bind_activity_snapshot(snapshot):
            candidate = self._candidate(
                translator,
                source,
                "我好像變得很__LT_SEM_1__了",
            )
            outcome = translator.translate_event(
                source,
                True,
                provisional_candidate=candidate,
                source_utterance_ids=("utt-preview",),
                evidence_source_utterance_ids=("utt-preview",),
            )

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.result_source, "provisional_promotion")
        self.assertEqual(outcome.target_text, "我好像變得很反社會人格了")
        engine.translate_messages.assert_not_called()


class TestSemanticTerminologyIntegration(unittest.TestCase):
    def test_final_terminology_rejection_allows_identical_input_retry(self):
        translator = _make_translator()
        engine = _route_engine("deepseek", "我變成__LT_SEM_1__了")
        translator._engines = [engine]
        source = "내가 좀 사패가 되는 것 같아"

        with patch.object(
            translator_module.SemanticTerminologyEscrow,
            "evaluate_final",
            return_value=(False, "semantic_terminology_cardinality_mismatch"),
        ):
            first = translator.translate_event(source)
            second = translator.translate_event(source)

        self.assertEqual(first.status, "failed")
        self.assertEqual(second.status, "failed")
        self.assertEqual(engine.translate_messages.call_count, 2)

    def test_confirmed_semantics_are_restored_before_publication(self):
        cases = (
            ("내가 좀 사패가 되는 것 같아", "我好像變得很__LT_SEM_1__了", "反社會人格"),
            ("닉, 닉값 하시면 안 되나요?", "不可以__LT_SEM_1__嗎？", "名副其實"),
            ("준회가 짬 때린 것 같으면 연락해", "如果俊會像是__LT_SEM_1__就聯絡我", "把事情丟給別人"),
        )
        for source, provider_output, expected in cases:
            translator = _make_translator()
            engine = _route_engine("deepseek", provider_output)
            translator._engines = [engine]

            outcome = translator.translate_event(source)

            self.assertEqual(outcome.status, "success")
            self.assertIn(expected, outcome.target_text)
            sent = engine.translate_messages.call_args.args[0][-1][1]
            self.assertIn("__LT_SEM_1__", sent)
            self.assertNotIn(expected, sent)

    def test_semantics_share_frozen_mapping_across_fallback(self):
        translator = _make_translator()
        primary = _route_engine("deepseek", "我變成小廢物了")
        fallback = _route_engine("openrouter", "我變成__LT_SEM_1__了")
        translator._engines = [primary, fallback]

        outcome = translator.translate_event("我看了之後有點 사패가 되는 것 같아")

        self.assertEqual(outcome.target_text, "我變成反社會人格了")
        self.assertEqual(outcome.engine, "openrouter")
        self.assertIn(
            "__LT_SEM_1__", fallback.translate_messages.call_args.args[0][-1][1]
        )

    def test_action_direction_anchor_survives_name_rejection_and_fallback(self):
        translator = _make_translator()
        primary = _route_engine(
            "deepseek",
            "打賢님按右鍵後__LT_SEM_1__即可。",
        )
        fallback = _route_engine(
            "openrouter",
            "塔賢按右鍵後__LT_SEM_1__即可。",
        )
        translator._engines = [primary, fallback]

        outcome = translator.translate_event(
            "타현님 우클릭 누르셔서 증폭 풀어주시면 풀어주시면 됩니다."
        )

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.engine, "openrouter")
        self.assertEqual(outcome.target_text, "塔賢按右鍵後解除增幅即可。")
        for engine in (primary, fallback):
            sent = engine.translate_messages.call_args.args[0][-1][1]
            self.assertIn("__LT_SEM_1__", sent)
            self.assertNotIn("증폭 풀어주시면", sent)

    def test_known_canonical_unknown_name_and_terminology_are_independent(self):
        translator = _make_translator()
        engine = _route_engine(
            "deepseek", "모카和__LT_UNK_1__都很__LT_SEM_1__"
        )
        translator._engines = [engine]

        with _active_translation_profile("url"):
            outcome = translator.translate_event("모카와 모찌한테 좀 사패가 됐다고 했어")

        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.target_text, "모카和모찌都很反社會人格")

class TestDaemonWorkerPool(unittest.TestCase):
    def test_running_provider_call_cannot_keep_process_alive(self):
        started = threading.Event()
        release = threading.Event()

        def blocked_call():
            started.set()
            release.wait()

        pool = translator_module._DaemonWorkerPool(
            1,
            thread_name_prefix="TranslationWorkerTest",
        )
        future = pool.submit(blocked_call)
        self.assertTrue(started.wait(timeout=1.0))
        try:
            before = time.monotonic()
            pool.shutdown(wait=False, cancel_futures=False)
            elapsed = time.monotonic() - before

            self.assertLess(elapsed, 0.5)
            self.assertTrue(all(thread.daemon for thread in pool._threads))
            self.assertFalse(future.cancel(), "a running future remains non-cancellable")
        finally:
            release.set()
            for thread in pool._threads:
                thread.join(timeout=1.0)
