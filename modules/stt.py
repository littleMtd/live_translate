import io
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import soundfile as sf

from config import cfg
from modules.activity_context import normalize_activity
from utils.audio import rms as _rms, write_wav
from utils.logger import get_logger
from utils.metrics import metrics
from utils.pipeline import poll_queue, start_daemon_thread
from utils.queue_utils import put_latest
from utils.runtime_events import runtime_events
from utils.text_heuristics import SENSEVOICE_NOISE_TAGS, SENSEVOICE_TAG_RE
from modules.pipeline_events import AudioChunk, SegmentInfo, TranscriptionEvent
from modules.scene_stt_terms import terms_for_activity
from modules.streamer_profiles import (
    build_stt_glossary,
    common_stt_terms,
    profile_stt_terms,
)
from modules.profile_context import (
    ProfileSnapshot,
    build_registry_stt_glossary,
    profile_state,
)
from modules.stt_policy import (
    build_groq_prompt_budget,
    dedupe_transcript_overlap,
    is_hallucinated,
    normalize_prompt_text,
    segment_rejection_reason,
    segment_stats,
    should_reject_language,
)

log = get_logger("stt")

_NOISE_TAGS = SENSEVOICE_NOISE_TAGS
_TAG_RE = SENSEVOICE_TAG_RE

_CONSECUTIVE_NONE_WARN = 10   # warn after this many consecutive silent results
_SENSEVOICE_PROBE_EVERY = 50  # after this many Groq transcriptions, probe SenseVoice once
_GROQ_CONTEXT_CHARS = 120
_GROQ_PROMPT_MAX_CHARS = 896
_TIMESTAMP_DEDUPE_MARGIN_SEC = 0.3
_AUDIO_DUMP_ROOT = Path(__file__).resolve().parent.parent / "logs" / "audio_dump"
_ELEVENLABS_UNSUPPORTED_KEYTERM_CHARS = frozenset("<>{}[]\\")


@dataclass(frozen=True)
class _ContextSource:
    utterance_id: str
    engine: str
    avg_logprob: float | None
    no_speech_prob: float | None
    updated_at: float


@dataclass(frozen=True)
class _ContextProvenance:
    source_utterance_id: str
    age_ms: float
    text_len: int
    source_engine: str
    source_avg_logprob: float | None
    source_no_speech_prob: float | None


def _is_groq_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    body = getattr(exc, "body", None)
    message = f"{exc} {body}".lower()
    return "rate_limit_exceeded" in message


def _is_hallucinated(text: str, *, allow_japanese: bool = False) -> bool:
    return is_hallucinated(
        text,
        cfg.stt.max_japanese_chars,
        log,
        max_repeat_ratio=cfg.stt.max_repeat_ratio,
        allow_japanese=allow_japanese,
    )


def _normalize_prompt_text(text: str, max_chars: int | None = None) -> str:
    return normalize_prompt_text(text, max_chars)


def _audio_seconds(audio: np.ndarray) -> float:
    return round(len(audio) / max(1, cfg.audio.sample_rate), 3)


def _audio_chunk(item: np.ndarray | AudioChunk) -> AudioChunk:
    if isinstance(item, AudioChunk):
        return item
    return AudioChunk(audio=item)


def _float_or_none(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _segment_infos(segments: list[dict]) -> tuple[SegmentInfo, ...]:
    infos: list[SegmentInfo] = []
    for segment in segments:
        get = segment.get if isinstance(segment, dict) else lambda name, default=None: getattr(segment, name, default)
        infos.append(
            SegmentInfo(
                start=_float_or_none(get("start")),
                end=_float_or_none(get("end")),
                text=str(get("text", "") or "").strip(),
                avg_logprob=_float_or_none(get("avg_logprob")),
                no_speech_prob=_float_or_none(get("no_speech_prob")),
            )
        )
    return tuple(infos)


def _segment_value(segment, name: str, default=None):
    if isinstance(segment, dict):
        return segment.get(name, default)
    return getattr(segment, name, default)


def _dedupe_segments_by_timestamp(
    text: str,
    segments: list,
    overlap_seconds: float,
    *,
    margin_seconds: float = _TIMESTAMP_DEDUPE_MARGIN_SEC,
) -> tuple[str, list, int, int]:
    cutoff = overlap_seconds - margin_seconds
    if cutoff <= 0 or not segments:
        return text, segments, 0, 0

    kept = []
    dropped = []
    for segment in segments:
        end = _float_or_none(_segment_value(segment, "end"))
        if end is not None and end <= cutoff:
            dropped.append(segment)
        else:
            kept.append(segment)

    if not dropped:
        return text, segments, 0, 0

    kept_texts = [str(_segment_value(segment, "text", "") or "").strip() for segment in kept]
    dropped_texts = [str(_segment_value(segment, "text", "") or "").strip() for segment in dropped]
    deduped_chars = sum(len(item) for item in dropped_texts)
    if kept:
        if any(kept_texts):
            return " ".join(item for item in kept_texts if item).strip(), kept, len(dropped), deduped_chars
        return "", kept, len(dropped), deduped_chars or len(text or "")
    return "", kept, len(dropped), deduped_chars or len(text or "")


def _dedupe_elevenlabs_words_by_timestamp(
    text: str,
    words: list,
    overlap_seconds: float,
) -> tuple[str, list, int, int, bool]:
    """Discard Scribe tokens wholly contained in capture's copied prefix.

    Unlike transcript-string overlap, this is anchored to the exact audio
    prefix copied into the new VAD chunk. When usable timestamps exist their
    result is authoritative, including the no-drop case: a speaker may
    legitimately repeat the preceding words after the copied prefix.
    """
    if overlap_seconds <= 0 or not words:
        return text, words, 0, 0, False

    timed_words = [
        word for word in words
        if _float_or_none(_segment_value(word, "end")) is not None
    ]
    if not timed_words:
        return text, words, 0, 0, False

    kept = []
    dropped = []
    for word in words:
        end = _float_or_none(_segment_value(word, "end"))
        if end is not None and end <= overlap_seconds:
            dropped.append(word)
        else:
            kept.append(word)

    if not dropped:
        return text, words, 0, 0, True

    dropped_chars = sum(
        len(str(_segment_value(word, "text", "") or "")) for word in dropped
    )
    kept_text = "".join(
        str(_segment_value(word, "text", "") or "") for word in kept
    ).strip()
    return kept_text, kept, len(dropped), dropped_chars, True


def _cfg_audio_bool(name: str, default: bool) -> bool:
    value = getattr(cfg.audio, name, default)
    return bool(value) if isinstance(value, bool) else default


def _cfg_audio_float(name: str, default: float) -> float:
    value = getattr(cfg.audio, name, default)
    return float(value) if isinstance(value, (int, float)) else default


def _cfg_stt_float(name: str, default: float) -> float:
    value = getattr(cfg.stt, name, default)
    return float(value) if isinstance(value, (int, float)) else default


def _cfg_stt_int(name: str, default: int) -> int:
    value = getattr(cfg.stt, name, default)
    return int(value) if isinstance(value, int) else default


def _cfg_stt_bool(name: str, default: bool) -> bool:
    value = getattr(cfg.stt, name, default)
    return bool(value) if isinstance(value, bool) else default


def _peak(audio: np.ndarray) -> float:
    return float(np.max(np.abs(audio))) if len(audio) else 0.0


def _normalize_audio_for_stt(audio: np.ndarray) -> tuple[np.ndarray, dict[str, float | bool]]:
    raw_rms = _rms(audio)
    raw_peak = _peak(audio)
    stats: dict[str, float | bool] = {
        "audio_rms": round(raw_rms, 6),
        "audio_peak": round(raw_peak, 6),
        "normalized_rms": round(raw_rms, 6),
        "normalized_peak": round(raw_peak, 6),
        "normalization_gain": 1.0,
        "normalization_limited": False,
    }
    if not _cfg_audio_bool("stt_normalize_enabled", False) or raw_rms <= 0:
        return audio, stats

    target_rms = max(0.0, _cfg_audio_float("stt_target_rms", 0.0))
    max_gain = max(1.0, _cfg_audio_float("stt_max_gain", 1.0))
    peak_limit = min(1.0, max(0.0, _cfg_audio_float("stt_peak_limit", 1.0)))
    if target_rms <= 0 or peak_limit <= 0:
        return audio, stats

    gain = min(max_gain, target_rms / raw_rms)
    normalized = audio.astype(np.float32, copy=True) * np.float32(gain)
    limited = False
    normalized_peak = _peak(normalized)
    if normalized_peak > peak_limit:
        normalized *= np.float32(peak_limit / normalized_peak)
        limited = True

    stats.update(
        {
            "normalized_rms": round(_rms(normalized), 6),
            "normalized_peak": round(_peak(normalized), 6),
            "normalization_gain": round(float(gain), 3),
            "normalization_limited": limited,
        }
    )
    return normalized.astype(np.float32, copy=False), stats


class STTEngine:
    def __init__(self):
        self._sense_voice = None
        self._groq_client = None
        self._groq_fallback_client = None
        self._elevenlabs_client = None
        self._use_elevenlabs = (cfg.stt.primary_engine == "elevenlabs")
        self._use_groq = (cfg.stt.primary_engine == "groq")
        self._consecutive_none = 0
        self._sv_fallback_counter = 0   # counts Groq calls since SenseVoice failure
        self._groq_rate_limited_until = 0.0
        self._groq_fallback_rate_limited_until = 0.0
        # Once the primary key hits 429, keep preferring the fallback key even
        # after primary cooldown. We switch back only if the fallback key also
        # rate-limits, which avoids burning primary quota during live capture.
        self._groq_prefer_fallback_key = False
        self._last_transcript: str = ""
        self._last_context_transcript: str = ""
        self._last_context_updated_at: float | None = None
        self._last_context_source: _ContextSource | None = None
        self._current_context_provenance: _ContextProvenance | None = None
        self._last_context_gate_reason: str = ""
        self._last_prompt_context_gated = False
        self._last_prompt_context_gate_reason = ""
        self._last_avg_logprob: float | None = None
        self._last_no_speech_prob: float | None = None
        self._last_sensevoice_error = False
        self._last_elevenlabs_error = False
        self._elevenlabs_retry_after = 0.0
        self._last_elevenlabs_keyterm_count = 0
        # Monotonic per-transcription counter; minted once per transcribe_event
        # call (single STT thread, so no lock needed) to correlate downstream events.
        self._utterance_seq = 0
        self._current_utterance_id = ""
        self._last_audio_seconds = 0.0
        self._last_segments: tuple[SegmentInfo, ...] = ()
        self._last_timestamp_deduped_segments = 0
        self._last_timestamp_deduped_chars = 0
        self._current_overlap_seconds = 0.0
        self._current_vad_cut_reason = ""
        self._last_prompt_budget = None
        self._last_detected_language = ""
        self._last_language_probability: float | None = None
        self._last_transcription_id = ""
        self._last_foreign_speech_allowed = False
        if self._use_elevenlabs:
            self._init_elevenlabs()
            # Groq remains a same-chunk fallback for provider failures.
            self._init_groq()
            self._init_groq_fallback()
        elif self._use_groq:
            self._init_groq()
            self._init_groq_fallback()
        else:
            self._load_sense_voice()

        self.available = (
            self._sense_voice is not None
            or self._elevenlabs_client is not None
            or self._groq_client is not None
            or self._groq_fallback_client is not None
        )
        if not self.available:
            log.error("STT unavailable: ElevenLabs, SenseVoice, and Groq all failed to initialize")

    def _init_elevenlabs(self):
        if not cfg.keys.elevenlabs:
            log.error("ELEVENLABS_API_KEY not set — ElevenLabs STT unavailable")
            return
        try:
            from elevenlabs.client import ElevenLabs
            self._elevenlabs_client = ElevenLabs(
                api_key=cfg.keys.elevenlabs,
                timeout=cfg.stt.elevenlabs_timeout,
            )
            log.info("ElevenLabs %s ready as primary STT", cfg.stt.elevenlabs_model)
        except Exception as e:
            log.error("Failed to init ElevenLabs client: %s", e)

    def _load_sense_voice(self):
        try:
            from funasr import AutoModel
            log.info("Loading SenseVoice-Small…")
            self._sense_voice = AutoModel(
                model=cfg.stt.sensevoice_model,
                trust_remote_code=True,
                device=cfg.stt.sensevoice_device,
            )
            log.info("SenseVoice-Small loaded")
        except Exception as e:
            log.error("Failed to load SenseVoice-Small: %s — will use Groq", e)
            self._use_groq = True
            self._init_groq()
            self._init_groq_fallback()

    def _init_groq(self):
        if not cfg.keys.groq:
            log.error("GROQ_API_KEY not set — STT unavailable")
            return
        try:
            from groq import Groq
            self._groq_client = Groq(
                api_key=cfg.keys.groq,
                max_retries=cfg.stt.groq_max_retries,
                timeout=cfg.stt.groq_timeout,
            )
            log.info(
                "Groq %s ready as STT fallback (timeout=%ss, max_retries=%s)",
                cfg.stt.groq_model,
                cfg.stt.groq_timeout,
                cfg.stt.groq_max_retries,
            )
        except Exception as e:
            log.error("Failed to init Groq client: %s", e)

    def _init_groq_fallback(self):
        if not cfg.keys.groq_fallback:
            return
        try:
            from groq import Groq
            self._groq_fallback_client = Groq(
                api_key=cfg.keys.groq_fallback,
                max_retries=cfg.stt.groq_max_retries,
                timeout=cfg.stt.groq_timeout,
            )
            log.info("Groq fallback key ready (will activate when primary is rate-limited)")
        except Exception as e:
            log.error("Failed to init Groq fallback client: %s", e)

    def transcribe(self, audio: np.ndarray | AudioChunk) -> str | None:
        event = self.transcribe_event(audio)
        return event.text if event else None

    def transcribe_event(self, audio: np.ndarray | AudioChunk) -> TranscriptionEvent | None:
        request_profile = profile_state.current()
        configured_profile = str(getattr(cfg, "active_streamer_profile", "") or "")
        if (
            request_profile.evidence_source == "source_default"
            and not request_profile.source_profile_id
            and configured_profile
        ):
            request_profile = profile_state.legacy_snapshot(
                configured_profile,
                translation_profile_applied=bool(cfg.translation.use_profile),
                stt_glossary_applied=bool(cfg.stt.use_profile_glossary),
            )
        previous_profile = getattr(self, "_current_profile_snapshot", None)
        if (
            previous_profile is not None
            and previous_profile.generation != request_profile.generation
        ):
            self.reset_stream_context("profile_changed")
        self._current_profile_snapshot = request_profile
        chunk = _audio_chunk(audio)
        audio = chunk.audio
        self._utterance_seq += 1
        self._current_utterance_id = f"utt-{self._utterance_seq}"
        self._last_audio_seconds = _audio_seconds(audio)
        self._current_overlap_seconds = float(chunk.overlap_seconds or 0.0)
        self._current_overlap_represented = self._overlap_matches_last_represented_audio(
            audio,
            self._current_overlap_seconds,
        )
        self._current_vad_cut_reason = str(chunk.vad_cut_reason or "")
        # Provenance is request-attempt scoped. Clearing it here prevents a
        # skipped/no-request path from inheriting the previous attempt.
        self._current_context_provenance = None
        if getattr(self, "_use_elevenlabs", False):
            result = self._transcribe_elevenlabs(audio)
            if result is not None:
                self._consecutive_none = 0
                self._last_transcript = result
                self._update_context_transcript(result, "elevenlabs")
                self._remember_represented_audio(audio)
                return self._event(result, "elevenlabs")
            if not self._last_elevenlabs_error:
                return None
            # Provider/network/quota failures retry this same chunk with Groq.
            # The configured primary remains ElevenLabs for the next chunk;
            # a bounded cooldown prevents an outage from being hammered.
            log.warning("ElevenLabs STT unavailable for this chunk — falling back to Groq")
            result = self._transcribe_groq(audio, _attempt_index=2)
            if result is not None:
                self._consecutive_none = 0
                self._last_transcript = result
                self._update_context_transcript(result, "groq")
                self._remember_represented_audio(audio)
                return self._event(result, "groq")
            self._consecutive_none += 1
            return None
        if not self._use_groq:
            self._last_segments = ()
            result = self._transcribe_sensevoice(audio)
            if result is not None:
                result = self._dedupe_current_overlap(result)
                if not result:
                    return None
                self._consecutive_none = 0
                self._last_transcript = result
                self._update_context_transcript(result, "sensevoice")
                self._remember_represented_audio(audio)
                return self._event(result, "sensevoice")
            if not self._last_sensevoice_error:
                return None
            # SenseVoice engine failed — fall through to Groq
            self._use_groq = True
            self._init_groq()
            self._init_groq_fallback()
        else:
            # Periodically probe SenseVoice recovery (mirrors translator fallback logic).
            # Only attempt if we actually loaded a SenseVoice model at some point.
            if self._sense_voice is not None:
                self._sv_fallback_counter += 1
                if self._sv_fallback_counter >= _SENSEVOICE_PROBE_EVERY:
                    self._sv_fallback_counter = 0
                    self._last_segments = ()
                    probe = self._transcribe_sensevoice(audio)
                    if probe is not None:
                        probe = self._dedupe_current_overlap(probe)
                        if not probe:
                            return None
                        log.info("SenseVoice recovered — switching back from Groq")
                        self._use_groq = False
                        self._consecutive_none = 0
                        self._last_transcript = probe
                        self._update_context_transcript(probe, "sensevoice")
                        self._remember_represented_audio(audio)
                        return self._event(probe, "sensevoice")

        result = self._transcribe_groq(audio)
        if result is not None:
            self._consecutive_none = 0
            self._last_transcript = result
            self._update_context_transcript(result, "groq")
            self._remember_represented_audio(audio)
            return self._event(result, "groq")
        else:
            self._consecutive_none += 1
            if self._consecutive_none == _CONSECUTIVE_NONE_WARN:
                log.warning(
                    "STT returned None %d times in a row — both engines may be down",
                    _CONSECUTIVE_NONE_WARN,
                )
        return None

    def _overlap_matches_last_represented_audio(
        self,
        audio: np.ndarray,
        overlap_seconds: float,
    ) -> bool:
        """Prove that this prefix came from the last successful STT chunk."""
        if overlap_seconds <= 0:
            return False
        previous = getattr(self, "_last_represented_audio", None)
        if previous is None:
            return False
        sample_count = int(round(overlap_seconds * max(1, cfg.audio.sample_rate)))
        if sample_count <= 0 or sample_count > len(audio) or sample_count > len(previous):
            return False
        return bool(np.array_equal(previous[-sample_count:], audio[:sample_count]))

    def _remember_represented_audio(self, audio: np.ndarray) -> None:
        self._last_represented_audio = np.asarray(audio).copy()

    def _elevenlabs_keyterms(self) -> list[str]:
        snapshot = getattr(self, "_current_profile_snapshot", None)
        if snapshot is None:
            snapshot = profile_state.legacy_snapshot(
                str(getattr(cfg, "active_streamer_profile", "") or ""),
                translation_profile_applied=bool(cfg.translation.use_profile),
                stt_glossary_applied=bool(cfg.stt.use_profile_glossary),
            )
        if not snapshot.stt_glossary_applied or not _cfg_stt_bool("use_profile_glossary", True):
            return []
        scene_terms = terms_for_activity(
            normalize_activity(getattr(cfg.translation, "current_activity", ""))
        )
        candidates = (
            *scene_terms,
            *(snapshot.registry or profile_state.registry).common_stt_terms,
            *(snapshot.registry or profile_state.registry).terms_for(snapshot.effective_profile_id),
        )
        limit = max(0, min(100, _cfg_stt_int("elevenlabs_max_keyterms", 100)))
        unique: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            term = str(candidate or "").strip()
            if (
                not term
                or term in seen
                or len(term) >= 50
                or len(term.split()) > 5
                or any(char in term for char in _ELEVENLABS_UNSUPPORTED_KEYTERM_CHARS)
            ):
                continue
            seen.add(term)
            unique.append(term)
            if len(unique) >= limit:
                break
        return unique

    def _transcribe_elevenlabs(self, audio: np.ndarray) -> str | None:
        started = time.monotonic()
        self._last_elevenlabs_error = False
        self._last_avg_logprob = None
        self._last_no_speech_prob = None
        self._last_segments = ()
        self._last_timestamp_deduped_segments = 0
        self._last_timestamp_deduped_chars = 0
        self._last_detected_language = ""
        self._last_language_probability = None
        self._last_transcription_id = ""
        self._last_foreign_speech_allowed = False
        self._last_elevenlabs_keyterm_count = 0

        if self._elevenlabs_client is None:
            self._last_elevenlabs_error = True
            self._emit_elevenlabs_runtime_event(
                audio=audio,
                started=started,
                status="failed",
                reason="no_client",
                request_sent=False,
                will_retry=self._groq_client is not None or self._groq_fallback_client is not None,
            )
            return None
        if started < self._elevenlabs_retry_after:
            self._last_elevenlabs_error = True
            self._emit_elevenlabs_runtime_event(
                audio=audio,
                started=started,
                status="skipped",
                reason="provider_cooldown",
                request_sent=False,
                will_retry=self._groq_client is not None or self._groq_fallback_client is not None,
            )
            return None

        request_audio, audio_stats = _normalize_audio_for_stt(audio)
        if _rms(request_audio) < cfg.audio.volume_threshold:
            self._emit_elevenlabs_runtime_event(
                audio=audio,
                started=started,
                status="skipped",
                reason="below_volume_threshold",
                request_sent=False,
                audio_stats=audio_stats,
            )
            return None

        request_sent = False
        try:
            buf = io.BytesIO()
            sf.write(buf, request_audio, cfg.audio.sample_rate, format="WAV", subtype="PCM_16")
            buf.seek(0)
            buf.name = "audio.wav"
            keyterms = self._elevenlabs_keyterms()
            self._last_elevenlabs_keyterm_count = len(keyterms)
            request_kwargs = {
                "file": buf,
                "model_id": cfg.stt.elevenlabs_model,
                "language_code": cfg.stt.language,
                "timestamps_granularity": "word",
                "tag_audio_events": False,
                "diarize": False,
            }
            if keyterms:
                request_kwargs["keyterms"] = keyterms
            request_sent = True
            resp = self._elevenlabs_client.speech_to_text.convert(**request_kwargs)
            text = str(getattr(resp, "text", "") or "").strip()
            words = list(getattr(resp, "words", None) or [])
            language_probability = _float_or_none(
                getattr(resp, "language_probability", None)
            )
            self._last_language_probability = (
                language_probability
                if language_probability is not None and np.isfinite(language_probability)
                else None
            )
            self._last_transcription_id = str(
                getattr(resp, "transcription_id", "") or ""
            )
            timestamp_dedupe_applied = False
            if (
                _cfg_stt_bool("dedupe_by_timestamp", True)
                and bool(getattr(self, "_current_overlap_represented", False))
            ):
                (
                    text,
                    words,
                    dropped_words,
                    dropped_chars,
                    timestamp_dedupe_applied,
                ) = _dedupe_elevenlabs_words_by_timestamp(
                    text,
                    words,
                    getattr(self, "_current_overlap_seconds", 0.0),
                )
                self._last_timestamp_deduped_segments = dropped_words
                self._last_timestamp_deduped_chars = dropped_chars
            self._last_segments = tuple(
                SegmentInfo(
                    start=_float_or_none(getattr(word, "start", None)),
                    end=_float_or_none(getattr(word, "end", None)),
                    text=str(getattr(word, "text", "") or ""),
                    logprob=(
                        value
                        if (value := _float_or_none(getattr(word, "logprob", None)))
                        is not None and np.isfinite(value)
                        else None
                    ),
                    word_type=str(getattr(word, "type", "") or ""),
                    speaker_id=str(getattr(word, "speaker_id", "") or ""),
                )
                for word in words
            )
            detected_lang = getattr(resp, "language_code", None)
            self._last_detected_language = str(detected_lang or "")
            allow_detected_japanese = (
                bool(getattr(cfg.translation, "translate_coherent_foreign_speech", False))
                and self._last_detected_language.lower() in ("ja", "japanese")
            )
            self._last_foreign_speech_allowed = allow_detected_japanese
            if should_reject_language(
                detected_lang,
                text,
                log,
                allow_japanese=allow_detected_japanese,
            ):
                self._clear_context_transcript("filtered_language")
                reason = "language"
            elif not text:
                self._clear_context_transcript("filtered_empty")
                reason = "empty"
            elif _is_hallucinated(text, allow_japanese=allow_detected_japanese):
                self._clear_context_transcript("filtered_hallucinated")
                reason = "hallucinated"
            else:
                # Exact transcript overlap remains a compatibility fallback for
                # responses without usable timestamps. Once Scribe supplied
                # timing evidence, even a no-drop result is trusted so an
                # intentional repetition after the copied prefix is preserved.
                if not timestamp_dedupe_applied:
                    text = self._dedupe_current_overlap(text)
                if not text:
                    self._clear_context_transcript("filtered_overlap_duplicate")
                    reason = "overlap_duplicate"
                else:
                    self._emit_elevenlabs_runtime_event(
                        audio=audio,
                        started=started,
                        status="success",
                        reason="",
                        request_sent=request_sent,
                        text=text,
                        audio_stats=audio_stats,
                    )
                    log.debug("ElevenLabs: %s", text)
                    return text

            self._emit_elevenlabs_runtime_event(
                audio=audio,
                started=started,
                status="filtered",
                reason=reason,
                request_sent=request_sent,
                text=text,
                audio_stats=audio_stats,
            )
            return None
        except Exception as e:
            self._last_elevenlabs_error = True
            self._elevenlabs_retry_after = time.monotonic() + max(
                0.0,
                _cfg_stt_float("elevenlabs_failure_cooldown_sec", 30.0),
            )
            log.error("ElevenLabs STT error: %s", e)
            self._emit_elevenlabs_runtime_event(
                audio=audio,
                started=started,
                status="failed",
                reason="rate_limited" if _is_groq_rate_limit_error(e) else "error",
                request_sent=request_sent,
                audio_stats=audio_stats,
                will_retry=self._groq_client is not None or self._groq_fallback_client is not None,
            )
            return None

    def _dedupe_current_overlap(self, text: str) -> str:
        """Remove exact text overlap only for a proven represented audio prefix."""
        if float(getattr(self, "_current_overlap_seconds", 0.0) or 0.0) <= 0:
            return text
        if not bool(getattr(self, "_current_overlap_represented", False)):
            return text
        return dedupe_transcript_overlap(self._last_transcript, text)

    def reset_stream_context(self, reason: str = "stream_reset") -> None:
        """Clear transcript state that must not cross a pause/discontinuity."""
        self._last_transcript = ""
        self._clear_context_transcript(reason)
        self._current_overlap_seconds = 0.0
        self._current_overlap_represented = False
        self._last_represented_audio = None
        self._current_vad_cut_reason = ""

    def _event(self, text: str, engine: str) -> TranscriptionEvent:
        snapshot = getattr(self, "_current_profile_snapshot", profile_state.current())
        return TranscriptionEvent(
            text=text,
            engine=engine,
            profile_id=snapshot.effective_profile_id,
            utterance_id=self._current_utterance_id,
            audio_seconds=self._last_audio_seconds,
            avg_logprob=self._last_avg_logprob,
            no_speech_prob=self._last_no_speech_prob,
            segments=getattr(self, "_last_segments", ()),
            overlap_seconds=getattr(self, "_current_overlap_seconds", 0.0),
            vad_cut_reason=getattr(self, "_current_vad_cut_reason", ""),
            language_probability=getattr(self, "_last_language_probability", None),
            transcription_id=getattr(self, "_last_transcription_id", ""),
            profile_snapshot=snapshot,
        )

    def _transcribe_sensevoice(self, audio: np.ndarray) -> str | None:
        self._last_avg_logprob = None
        self._last_no_speech_prob = None
        self._last_language_probability = None
        self._last_transcription_id = ""
        self._last_sensevoice_error = False
        try:
            res = self._sense_voice.generate(
                input=audio,
                cache={},
                language=cfg.stt.language,
                use_itn=True,
                batch_size_s=cfg.stt.batch_size_s,
            )
            text = res[0]["text"] if res else ""
            # Reject pure noise chunks (no speech tag present)
            if not any(tag in text for tag in ("<|Speech|>", "<|WITHITN|>", "<|withitn|>")):
                if any(tag in text for tag in _NOISE_TAGS):
                    return None
            # Strip ALL metadata tokens: <|ko|>, <|EMO_UNKNOWN|>, <|Speech|>, etc.
            text = _TAG_RE.sub("", text).strip()
            if not text:
                return None
            log.debug("SenseVoice: %s", text)
            return text
        except Exception as e:
            log.error("SenseVoice error: %s", e)
            self._last_sensevoice_error = True
            return None

    def _transcribe_groq(
        self,
        audio: np.ndarray,
        *,
        _retrying_other_key: bool = False,
        _attempt_index: int = 1,
    ) -> str | None:
        started = time.monotonic()
        # Diagnostic-only attempt metadata.  One TranscriptionEvent/utterance may
        # make at most one immediate cross-key retry; these fields make attempts
        # joinable without changing selection, retry, or cooldown behavior.
        self._current_groq_attempt_index = _attempt_index
        self._current_groq_key_role = "none"
        self._last_avg_logprob = None
        self._last_no_speech_prob = None
        self._last_segments = ()
        self._last_timestamp_deduped_segments = 0
        self._last_timestamp_deduped_chars = 0
        self._last_detected_language = ""
        self._last_language_probability = None
        self._last_transcription_id = ""
        self._last_foreign_speech_allowed = False
        primary_ready = self._groq_client is not None and started >= self._groq_rate_limited_until
        fallback_ready = (
            self._groq_fallback_client is not None
            and started >= self._groq_fallback_rate_limited_until
        )
        prefer_fallback = bool(getattr(self, "_groq_prefer_fallback_key", False))
        if prefer_fallback and fallback_ready:
            active_client = self._groq_fallback_client
            using_fallback = True
            log.debug("Using preferred Groq fallback key")
        elif primary_ready:
            active_client = self._groq_client
            using_fallback = False
        elif fallback_ready:
            active_client = self._groq_fallback_client
            using_fallback = True
            self._groq_prefer_fallback_key = True
            log.debug("Primary Groq key rate-limited; using fallback key")
        else:
            if self._groq_client is None and self._groq_fallback_client is None:
                reason = "no_client"
            else:
                reason = "rate_limit_cooldown"
                remaining = round(
                    min(
                        self._groq_rate_limited_until if self._groq_client else 0,
                        self._groq_fallback_rate_limited_until if self._groq_fallback_client else 0,
                    ) - started,
                    2,
                )
                log.debug("Both Groq keys rate-limited (%.2fs left on shorter cooldown)", remaining)
            # M7: chunks dropped during rate-limit cooldown were previously
            # only visible in per-event logs; surface the loss in the 60 s
            # metrics summary too.
            metrics.increment(f"stt.dropped.{reason}")
            self._emit_stt_runtime_event(
                audio=audio,
                started=started,
                status="skipped",
                reason=reason,
                request_sent=False,
            )
            return None
        request_audio, audio_stats = _normalize_audio_for_stt(audio)
        # L10 note: the gate intentionally measures the *normalized* audio.
        # With stt_normalize_enabled the gate is effectively looser than the
        # raw-audio gates in VAD/fixed capture — that is the point: quiet
        # speech that normalization can rescue should not be dropped here.
        gate_rms = _rms(request_audio)
        if gate_rms < cfg.audio.volume_threshold:
            self._last_avg_logprob = None
            self._last_no_speech_prob = None
            log.debug("Groq STT skipped: audio below volume threshold")
            self._emit_stt_runtime_event(
                audio=audio,
                started=started,
                status="skipped",
                reason="below_volume_threshold",
                request_sent=False,
                audio_stats=audio_stats,
            )
            return None
        request_sent = False
        try:
            buf = io.BytesIO()
            sf.write(buf, request_audio, cfg.audio.sample_rate, format="WAV", subtype="PCM_16")
            buf.seek(0)
            buf.name = "audio.wav"
            dynamic_prompt = self._build_groq_prompt()

            self._current_groq_key_role = "fallback" if using_fallback else "primary"
            request_sent = True
            resp = active_client.audio.transcriptions.create(
                model=cfg.stt.groq_model,
                file=buf,
                language=cfg.stt.language,
                prompt=dynamic_prompt,
                response_format="verbose_json",
                temperature=0.0,
            )

            # Language sanity — only reject if clearly Japanese (the dominant hallucination lang)
            detected_lang = getattr(resp, "language", None)
            self._last_detected_language = str(detected_lang or "")
            allow_detected_japanese = (
                bool(getattr(cfg.translation, "translate_coherent_foreign_speech", False))
                and self._last_detected_language.lower() in ("ja", "japanese")
            )
            self._last_foreign_speech_allowed = allow_detected_japanese
            text = (getattr(resp, "text", "") or "").strip()
            segments = getattr(resp, "segments", None) or []
            if (
                _cfg_stt_bool("dedupe_by_timestamp", True)
                and bool(getattr(self, "_current_overlap_represented", False))
            ):
                text, segments, dropped_segments, dropped_chars = _dedupe_segments_by_timestamp(
                    text,
                    list(segments),
                    getattr(self, "_current_overlap_seconds", 0.0),
                )
                self._last_timestamp_deduped_segments = dropped_segments
                self._last_timestamp_deduped_chars = dropped_chars
            if should_reject_language(
                detected_lang,
                text,
                log,
                allow_japanese=allow_detected_japanese,
            ):
                self._last_avg_logprob = None
                self._last_no_speech_prob = None
                self._clear_context_transcript("filtered_language")
                self._emit_stt_runtime_event(
                    audio=audio,
                    started=started,
                    status="filtered",
                    reason="language",
                    request_sent=request_sent,
                    text=text,
                    audio_stats=audio_stats,
                )
                return None

            # Confidence filtering via segment metadata
            self._last_segments = _segment_infos(segments)
            reason, stats = segment_rejection_reason(
                segments,
                text=text,
                no_speech_threshold=cfg.stt.no_speech_threshold,
                avg_logprob_threshold=cfg.stt.avg_logprob_threshold,
                logger=log,
            )
            if reason is not None:
                self._last_avg_logprob = None
                self._last_no_speech_prob = None
                self._clear_context_transcript(f"filtered_{reason or 'segments'}")
                self._emit_stt_runtime_event(
                    audio=audio,
                    started=started,
                    status="filtered",
                    reason=reason or "segments",
                    request_sent=request_sent,
                    text=text,
                    avg_logprob=stats.logprob if stats else None,
                    no_speech_prob=stats.no_speech if stats else None,
                    audio_stats=audio_stats,
                )
                return None

            if not text:
                self._last_avg_logprob = None
                self._last_no_speech_prob = None
                self._clear_context_transcript("filtered_empty")
                self._emit_stt_runtime_event(
                    audio=audio,
                    started=started,
                    status="filtered",
                    reason="empty",
                    request_sent=request_sent,
                    text=text,
                    audio_stats=audio_stats,
                )
                return None
            # A response explicitly detected as Japanese may contain kana and
            # is still subject to the segment confidence/repetition filters.
            # Kana in a Korean-detected response remains a hallucination.
            if _is_hallucinated(text, allow_japanese=allow_detected_japanese):
                self._last_avg_logprob = None
                self._last_no_speech_prob = None
                self._clear_context_transcript("filtered_hallucinated")
                self._emit_stt_runtime_event(
                    audio=audio,
                    started=started,
                    status="filtered",
                    reason="hallucinated",
                    request_sent=request_sent,
                    text=text,
                    audio_stats=audio_stats,
                )
                return None
            text = self._dedupe_current_overlap(text)
            if not text:
                self._clear_context_transcript("filtered_overlap_duplicate")
                self._emit_stt_runtime_event(
                    audio=audio,
                    started=started,
                    status="filtered",
                    reason="overlap_duplicate",
                    request_sent=request_sent,
                    text="",
                    avg_logprob=stats.logprob if stats else None,
                    no_speech_prob=stats.no_speech if stats else None,
                    audio_stats=audio_stats,
                )
                return None
            log.debug("Groq: %s", text)
            # L9: reuse `stats` from segment_rejection_reason above —
            # segments were already converted/aggregated once.
            self._last_avg_logprob = stats.logprob if stats else None
            self._last_no_speech_prob = stats.no_speech if stats else None
            self._emit_stt_runtime_event(
                audio=audio,
                started=started,
                status="success",
                reason="",
                request_sent=request_sent,
                text=text,
                avg_logprob=stats.logprob if stats else None,
                no_speech_prob=stats.no_speech if stats else None,
                audio_stats=audio_stats,
            )
            return text
        except Exception as e:
            self._last_avg_logprob = None
            self._last_no_speech_prob = None
            if _is_groq_rate_limit_error(e):
                reason = "rate_limited"
                cooldown_end = time.monotonic() + max(0.0, cfg.stt.groq_rate_limit_cooldown_sec)
                if using_fallback:
                    self._groq_fallback_rate_limited_until = cooldown_end
                    self._groq_prefer_fallback_key = False
                    if (
                        not _retrying_other_key
                        and self._groq_client is not None
                        and time.monotonic() >= self._groq_rate_limited_until
                    ):
                        log.warning(
                            "Groq STT fallback key rate limited; retrying same chunk with primary key: %s",
                            e,
                        )
                        self._emit_stt_runtime_event(
                            audio=audio,
                            started=started,
                            status="failed",
                            reason=reason,
                            request_sent=request_sent,
                            audio_stats=audio_stats,
                            will_retry=True,
                        )
                        return self._transcribe_groq(
                            audio,
                            _retrying_other_key=True,
                            _attempt_index=_attempt_index + 1,
                        )
                    log.warning("Groq STT fallback key rate limited; dropping chunk: %s", e)
                else:
                    self._groq_rate_limited_until = cooldown_end
                    if self._groq_fallback_client is not None:
                        self._groq_prefer_fallback_key = True
                    if (
                        not _retrying_other_key
                        and self._groq_fallback_client is not None
                        and time.monotonic() >= self._groq_fallback_rate_limited_until
                    ):
                        log.warning(
                            "Groq STT primary key rate limited; retrying same chunk with fallback key: %s",
                            e,
                        )
                        self._emit_stt_runtime_event(
                            audio=audio,
                            started=started,
                            status="failed",
                            reason=reason,
                            request_sent=request_sent,
                            audio_stats=audio_stats,
                            will_retry=True,
                        )
                        return self._transcribe_groq(
                            audio,
                            _retrying_other_key=True,
                            _attempt_index=_attempt_index + 1,
                        )
                    log.warning("Groq STT rate limited; dropping chunk without fallback retry: %s", e)
            else:
                log.error("Groq STT error: %s", e)
                reason = "error"
            self._emit_stt_runtime_event(
                audio=audio,
                started=started,
                status="failed",
                reason=reason,
                request_sent=request_sent,
                audio_stats=audio_stats,
            )
            return None

    def _build_groq_prompt(self) -> str | None:
        snapshot = getattr(self, "_current_profile_snapshot", None)
        if snapshot is None:
            snapshot = profile_state.legacy_snapshot(
                str(getattr(cfg, "active_streamer_profile", "") or ""),
                translation_profile_applied=bool(cfg.translation.use_profile),
                stt_glossary_applied=bool(cfg.stt.use_profile_glossary),
            )
        context_transcript = self._context_transcript_for_prompt()
        # Manual activity-keyed hot vocabulary. The automatic scene resolver
        # is record-only and never activates STT terms.
        # what game is on screen; bias the recognizer toward its terms so
        # mishears are prevented at the source (메가태화←메가진화 class).
        scene_terms = terms_for_activity(
            normalize_activity(getattr(cfg.translation, "current_activity", "")))
        budget = build_groq_prompt_budget(
            seed_prompt=cfg.stt.groq_prompt,
            use_profile_glossary=(cfg.stt.use_profile_glossary and snapshot.stt_glossary_applied),
            active_profile=snapshot.effective_profile_id,
            last_transcript=context_transcript,
            glossary_builder=(
                (lambda profile_id: build_stt_glossary(
                    profile_id, extra_terms=scene_terms
                ))
                if snapshot.evidence_source == "legacy_fallback"
                else (lambda profile_id: build_registry_stt_glossary(
                    snapshot.registry or profile_state.registry,
                    profile_id,
                    extra_terms=scene_terms,
                ))
            ),
            max_context_chars=_GROQ_CONTEXT_CHARS,
            max_prompt_chars=_GROQ_PROMPT_MAX_CHARS,
        )
        self._last_prompt_budget = budget
        self._current_context_provenance = None
        source = getattr(self, "_last_context_source", None)
        if budget.context_included and source is not None:
            # Freeze what this attempt actually selected before the request.
            # Later response filtering may clear future context eligibility,
            # but must not rewrite attribution for the request already sent.
            context_payload = normalize_prompt_text(
                context_transcript,
                _GROQ_CONTEXT_CHARS,
            )
            self._current_context_provenance = _ContextProvenance(
                source_utterance_id=source.utterance_id,
                age_ms=round(max(0.0, time.monotonic() - source.updated_at) * 1000, 2),
                text_len=len(context_payload),
                source_engine=source.engine,
                source_avg_logprob=source.avg_logprob,
                source_no_speech_prob=source.no_speech_prob,
            )
        return budget.prompt

    def _context_transcript_for_prompt(self) -> str:
        self._last_prompt_context_gated = False
        self._last_prompt_context_gate_reason = ""
        context = getattr(self, "_last_context_transcript", "")
        if not context:
            if getattr(self, "_last_transcript", ""):
                self._last_prompt_context_gated = True
                self._last_prompt_context_gate_reason = (
                    getattr(self, "_last_context_gate_reason", "") or "not_context_eligible"
                )
            return ""

        max_age = _cfg_stt_float("context_max_age_sec", 30.0)
        updated_at = getattr(self, "_last_context_updated_at", None)
        if max_age > 0 and updated_at is not None and time.monotonic() - updated_at > max_age:
            self._clear_context_transcript("expired")
            if getattr(self, "_last_transcript", ""):
                self._last_prompt_context_gated = True
                self._last_prompt_context_gate_reason = "expired"
            return ""

        return context

    def _clear_context_transcript(self, reason: str) -> None:
        self._last_context_transcript = ""
        self._last_context_updated_at = None
        self._last_context_source = None
        self._last_context_gate_reason = reason

    def _update_context_transcript(self, text: str, engine: str) -> None:
        compact = "".join((text or "").split())
        min_chars = _cfg_stt_int("context_min_chars", 4)
        if len(compact) < min_chars:
            self._clear_context_transcript("too_short")
            return

        if engine == "groq":
            avg_logprob = getattr(self, "_last_avg_logprob", None)
            no_speech_prob = getattr(self, "_last_no_speech_prob", None)
            if avg_logprob is None or no_speech_prob is None:
                self._clear_context_transcript("missing_confidence")
                return
            if avg_logprob < _cfg_stt_float("context_avg_logprob_threshold", -0.7):
                self._clear_context_transcript("avg_logprob")
                return
            if no_speech_prob > _cfg_stt_float("context_no_speech_threshold", 0.3):
                self._clear_context_transcript("no_speech_prob")
                return

        updated_at = time.monotonic()
        self._last_context_transcript = text
        self._last_context_updated_at = updated_at
        self._last_context_source = _ContextSource(
            utterance_id=str(getattr(self, "_current_utterance_id", "")),
            engine=engine,
            avg_logprob=(
                getattr(self, "_last_avg_logprob", None) if engine == "groq" else None
            ),
            no_speech_prob=(
                getattr(self, "_last_no_speech_prob", None) if engine == "groq" else None
            ),
            updated_at=updated_at,
        )
        self._last_context_gate_reason = ""

    def _emit_stt_runtime_event(
        self,
        *,
        audio: np.ndarray,
        started: float,
        status: str,
        reason: str,
        request_sent: bool,
        text: str = "",
        avg_logprob: float | None = None,
        no_speech_prob: float | None = None,
        audio_stats: dict[str, float | bool] | None = None,
        will_retry: bool = False,
    ) -> None:
        audio_stats = audio_stats or {}
        # Prompt budget only reflects this request; on skipped paths no prompt
        # was built, so the (possibly stale) budget is intentionally omitted.
        budget = getattr(self, "_last_prompt_budget", None) if request_sent else None
        provenance = (
            getattr(self, "_current_context_provenance", None)
            if request_sent and budget is not None and budget.context_included
            else None
        )
        runtime_events.emit(
            "stt",
            utterance_id=getattr(self, "_current_utterance_id", ""),
            engine="groq",
            model=cfg.stt.groq_model,
            status=status,
            reason=reason,
            request_sent=request_sent,
            attempt_index=int(getattr(self, "_current_groq_attempt_index", 1)),
            key_role=str(getattr(self, "_current_groq_key_role", "none")),
            will_retry=bool(will_retry),
            audio_seconds=_audio_seconds(audio),
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            text_len=len(text or ""),
            **getattr(self, "_current_profile_snapshot", profile_state.current()).as_metadata(),
            avg_logprob=avg_logprob,
            no_speech_prob=no_speech_prob,
            segment_count=len(getattr(self, "_last_segments", ())),
            timestamp_deduped_segments=int(getattr(self, "_last_timestamp_deduped_segments", 0)),
            timestamp_deduped_chars=int(getattr(self, "_last_timestamp_deduped_chars", 0)),
            overlap_seconds=round(getattr(self, "_current_overlap_seconds", 0.0), 3),
            vad_cut_reason=getattr(self, "_current_vad_cut_reason", ""),
            audio_rms=audio_stats.get("audio_rms"),
            audio_peak=audio_stats.get("audio_peak"),
            normalized_rms=audio_stats.get("normalized_rms"),
            normalized_peak=audio_stats.get("normalized_peak"),
            normalization_gain=audio_stats.get("normalization_gain"),
            normalization_limited=audio_stats.get("normalization_limited"),
            prompt_bytes=budget.prompt_bytes if budget else None,
            prompt_max_bytes=budget.max_prompt_bytes if budget else None,
            glossary_present=budget.glossary_present if budget else None,
            glossary_truncated=budget.glossary_truncated if budget else None,
            context_present=budget.context_present if budget else None,
            context_included=budget.context_included if budget else None,
            context_source_utterance_id=(
                provenance.source_utterance_id if provenance else None
            ),
            context_age_ms=provenance.age_ms if provenance else None,
            context_text_len=provenance.text_len if provenance else None,
            context_source_engine=provenance.source_engine if provenance else None,
            context_source_avg_logprob=(
                provenance.source_avg_logprob if provenance else None
            ),
            context_source_no_speech_prob=(
                provenance.source_no_speech_prob if provenance else None
            ),
            context_gated=bool(getattr(self, "_last_prompt_context_gated", False)) if request_sent else False,
            context_gate_reason=getattr(self, "_last_prompt_context_gate_reason", "") if request_sent else "",
            detected_language=str(getattr(self, "_last_detected_language", "")) if request_sent else "",
            foreign_speech_allowed=bool(getattr(self, "_last_foreign_speech_allowed", False)) if request_sent else False,
        )

    def _emit_elevenlabs_runtime_event(
        self,
        *,
        audio: np.ndarray,
        started: float,
        status: str,
        reason: str,
        request_sent: bool,
        text: str = "",
        audio_stats: dict[str, float | bool] | None = None,
        will_retry: bool = False,
    ) -> None:
        audio_stats = audio_stats or {}
        word_logprobs = [
            float(segment.logprob)
            for segment in getattr(self, "_last_segments", ())
            if segment.logprob is not None and np.isfinite(segment.logprob)
        ]
        word_type_counts: dict[str, int] = {}
        for segment in getattr(self, "_last_segments", ()):
            word_type = str(segment.word_type or "")
            if word_type:
                word_type_counts[word_type] = word_type_counts.get(word_type, 0) + 1
        runtime_events.emit(
            "stt",
            utterance_id=getattr(self, "_current_utterance_id", ""),
            engine="elevenlabs",
            model=cfg.stt.elevenlabs_model,
            status=status,
            reason=reason,
            request_sent=request_sent,
            attempt_index=1,
            key_role="primary",
            will_retry=bool(will_retry),
            audio_seconds=_audio_seconds(audio),
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            text_len=len(text or ""),
            **getattr(self, "_current_profile_snapshot", profile_state.current()).as_metadata(),
            avg_logprob=None,
            no_speech_prob=None,
            segment_count=len(getattr(self, "_last_segments", ())),
            timestamp_deduped_segments=int(getattr(self, "_last_timestamp_deduped_segments", 0)),
            timestamp_deduped_chars=int(getattr(self, "_last_timestamp_deduped_chars", 0)),
            overlap_seconds=round(getattr(self, "_current_overlap_seconds", 0.0), 3),
            vad_cut_reason=getattr(self, "_current_vad_cut_reason", ""),
            audio_rms=audio_stats.get("audio_rms"),
            audio_peak=audio_stats.get("audio_peak"),
            normalized_rms=audio_stats.get("normalized_rms"),
            normalized_peak=audio_stats.get("normalized_peak"),
            normalization_gain=audio_stats.get("normalization_gain"),
            normalization_limited=audio_stats.get("normalization_limited"),
            keyterm_count=int(getattr(self, "_last_elevenlabs_keyterm_count", 0)),
            language_probability=(
                getattr(self, "_last_language_probability", None)
                if request_sent else None
            ),
            word_count=len(getattr(self, "_last_segments", ())),
            min_word_logprob=min(word_logprobs) if word_logprobs else None,
            mean_word_logprob=(
                round(sum(word_logprobs) / len(word_logprobs), 6)
                if word_logprobs else None
            ),
            word_type_counts=word_type_counts,
            detected_language=(
                str(getattr(self, "_last_detected_language", "")) if request_sent else ""
            ),
            foreign_speech_allowed=(
                bool(getattr(self, "_last_foreign_speech_allowed", False))
                if request_sent
                else False
            ),
        )


def start(audio_queue: queue.Queue, text_queue: queue.Queue,
          stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    def run_pipeline():
        engine = STTEngine()
        if not engine.available:
            # Both SenseVoice and Groq failed to load. Don't sit and spin
            # consuming audio chunks that we can never transcribe — signal
            # shutdown so main.py can tear down the rest of the pipeline.
            log.error("STT thread aborting: no engine available")
            stop_event.set()
            return
        # Collection-mode audio dump: one session dir per run so per-run
        # utterance ids (utt-1, utt-2…) don't collide across restarts. Keyed by
        # runtime_events.run_id (timestamp-pid) so two restarts in the same
        # second land in different dirs AND the dir matches the run_id stamped
        # on every runtime event, making audio joinable from the logs.
        dump_dir: Path | None = None
        if cfg.stt.dump_audio:
            dump_dir = _AUDIO_DUMP_ROOT / runtime_events.run_id
            log.info("STT audio dump enabled → %s", dump_dir)
        was_paused = False
        while not stop_event.is_set():
            if pause_event and pause_event.is_set():
                if not was_paused:
                    engine.reset_stream_context("pipeline_paused")
                    was_paused = True
                stop_event.wait(0.05)
                continue
            if was_paused:
                # Drop anything that raced with the UI's queue drain and make
                # the first resumed request independent of pre-pause context.
                while True:
                    try:
                        audio_queue.get_nowait()
                    except queue.Empty:
                        break
                engine.reset_stream_context("pipeline_resumed")
                was_paused = False
            has_audio, audio_item = poll_queue(audio_queue, stop_event, pause_event)
            if not has_audio:
                continue

            chunk = _audio_chunk(audio_item)
            audio = chunk.audio
            started = time.monotonic()
            event = engine.transcribe_event(chunk)
            metrics.observe_latency("stt", time.monotonic() - started)
            if event:
                metrics.increment("stt.success")
                if dump_dir is not None and event.utterance_id:
                    try:
                        write_wav(dump_dir / f"{event.utterance_id}.wav", audio, cfg.audio.sample_rate)
                    except Exception as exc:  # never let dumping break transcription
                        log.warning("Audio dump failed for %s: %s", event.utterance_id, exc)
                put_latest(text_queue, event, log, "text_queue", "tokens")
            else:
                metrics.increment("stt.none")
            metrics.log_summary_if_due()

        log.info("STT stopped")

    def run():
        try:
            run_pipeline()
        except Exception as exc:
            # A dead STT consumer otherwise leaves capture/UI running forever
            # while audio queues accumulate and no subtitles can be produced.
            log.error("STT worker aborted: %s", exc, exc_info=True)
            stop_event.set()

    return start_daemon_thread("STT", run)


if __name__ == "__main__":
    import time, sounddevice as sd

    engine = STTEngine()
    print(f"Recording {cfg.audio.chunk_seconds}s…")
    audio = sd.rec(
        int(cfg.audio.sample_rate * cfg.audio.chunk_seconds),
        samplerate=cfg.audio.sample_rate,
        channels=1,
        dtype="float32",
    )
    sd.wait()
    result = engine.transcribe(audio[:, 0])
    print("Transcription:", result)
