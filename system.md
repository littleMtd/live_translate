# System Architecture

This document owns the current backend/runtime architecture contract. Code is authoritative if this summary falls behind. Detailed routing, validation, and historical decisions belong in `docs/agent/` and dated evidence documents.

## Product and live graph

`live_translate` turns Korean livestream audio into ordered Traditional Chinese subtitles on Windows.

```text
capability-checked sounddevice input
  -> ElevenLabs Scribe v2 batch STT
       -> Groq same-chunk fallback; SenseVoice only when explicitly configured
  -> SentenceBuffer / sentence_splitter
       -> optional one-shot provisional request
  -> two translation workers
  -> in-order completion coordinator
  -> tkinter subtitle overlay
```

`stop_event` and `pause_event` control the graph. Startup failures propagate to shutdown. Audio/text/subtitle queues prefer freshness; the sentence queue drops only its oldest item. Translation may complete concurrently, but publication remains ordered by run and sequence.

## Translation correctness pipeline

```text
TranslationPolicy sanitize/filter
  -> deterministic source normalization
  -> registry-backed reviewed entity activation/capsule/canonical obligations
  -> unknown-name escrow excluding known canonical spans
  -> semantic terminology escrow
  -> immutable effective provider request/messages
  -> DeepSeek Flash -> Groq
  -> deterministic restore and source-aware corrections
  -> canonical/name/terminology occurrence invariants
  -> Hangul/Kana/meta/content guards
  -> final fail-closed result
  -> cache/history/event commit
  -> ordered publication
```

The same frozen escrow mapping and correctness finalization apply across fallbacks. Reviewed source aliases activate entities once through the global registry; that request-local result owns the prompt capsule, resolved spans, canonical obligations, and source-gated target repair across profiles. Legacy repair-only name rules may normalize a target but cannot create publication obligations. Known canonical obligations always precede unknown-name detection. Unknown names authorize only exact restored source-grounded Hangul spans; there is no global Hangul allowance. Semantic terminology is a separate exact/narrow deterministic owner.

Primary, fallback, cache, and exact provisional candidates use one structured adjudication boundary for deterministic restore/correction, request protection, canonical/entity provenance, and script safety. Finalization runs that same adjudicator authoritatively before recording history or publishing. A provisional candidate is one-shot and may be promoted only when DeepSeek is the selected backend and its fingerprint exactly matches the final source/profile/activity/context contract; mismatch follows the normal final route. Shutdown closes provisional publication before workers are released, and stale final results are rejected before cache/history commit. Provisional output never weakens final ordering or invariants.

Translation profile identity is an optional request-shaping hint. `source_profile` records the configured selection, but Auto mode starts neutral and never treats that setting as confirmed content identity. Exact reviewed member-name markers from the hot-reloaded profile registry are candidate evidence but cannot independently authorize a cross-profile mutation. A profile hint switch requires two distinct agreeing frames in the same validated window generation plus profile-level corroboration from group branding or persistent non-member visual evidence; a multi-member roster is not owner corroboration, and markers from different profile families conflict. A strong marker for an already-confirmed hint may refresh that confirmation immediately. Profile vision uses bounded JSON output, precise schema/semantic rejection, and one serialization-only retry. Sampling is adaptive: seeking/recovery wakes at five seconds, stable confirmation backs off to fifteen seconds, and a rolling attempt cap plus provider-error cooldown bounds traffic. Unknown, conflict, provider, capture, and window-lifecycle failures retire pending evidence and trigger revalidation but cannot replace a confirmed hint with the configured source; only a newly confirmed reviewed identity or an explicit lifecycle/control change may change it. Manual mode remains a complete hint hard lock. STT glossary, profile prompt, sentence boundaries, provisional fingerprints, and request identity consume one immutable `ProfileSnapshot`; its generation retires stale concurrent work, while its cache identity contains only request-shaping state. Conversation history is owned by the live translator session and activity epoch, so profile observation churn cannot split the history cohort.

Downstream compatibility paths may consume the current typed snapshot when an old event omitted one, but they never reconstruct a manual profile from `cfg.active_streamer_profile` or an event's legacy `profile_id`. This keeps the configured source as metadata in Auto mode and prevents it from silently retaking request ownership.

### Provider routing

The ordinary live backend is `deepseek`. Persisted dashboard values using the old
`anthropic` backend name are normalized to `deepseek` when loaded.

- `deepseek_route="primary"` (default): DeepSeek `deepseek-v4-flash` -> Groq `openai/gpt-oss-120b`.
- `LIVE_TRANSLATE_DEEPSEEK_ROUTE=off`: Groq only. This is the operational emergency rollback.
- `nvidia`: NVIDIA primary plus the fixed Groq fallback.
- `ollama`: local Ollama only.

The dashboard no longer exposes provider ordering. Provider failure and content rejection are distinct: retryable provider failures may advance persistent circuit state; content rejection falls back only for the current sentence and must not damage provider health. Routes have stable `provider:model` identities, per-adapter timeouts, a shared live deadline, circuit breaker, and bounded recovery probe.

Quality retry, Japanese translation shadow/active, DeepSeek record-only shadow/model comparison, and `source_fuzzy_shadow` are retired and have no live producers. Historical analyzers may parse old fields. Current source normalization and Hangul/Kana publication guards remain active.

## STT and sentence ownership

- `modules/audio_capture.py`: endpoint checks, stream readiness, normalization, VAD, overlap/adaptive chunking, discontinuity resets.
- `modules/stt.py`: ElevenLabs/Groq/SenseVoice adapters, same-chunk fallback, context provenance, audio dump, STT events. ElevenLabs overlap removal uses Scribe word timing only when the copied audio prefix exactly matches the last successfully represented chunk; otherwise it preserves the prefix. Scribe language probability and provider-native word log-probability/type metadata are retained for measurement, but do not select or rewrite source text.
- `modules/stt_policy.py`: confidence/language rejection, hallucination/repetition checks, overlap dedupe, Groq prompt budget.
- `modules/sentence_buffer.py`: completeness/cut decisions, including bounded holds for unpunctuated Korean embedded-question/subordinate tails. Safe ordinary full stops complete a sentence, while URL/version/decimal/ellipsis dots do not; the existing force-cut remains authoritative.
- `modules/sentence_splitter.py`: queue orchestration, bounded merge, provisional submission, sentence events.

`semantic_early_cut_mode` supports only `off|shadow`; the frozen T20 gate was NO-GO, so shadow cannot alter sentence timing. Provisional subtitles are a separate active feature.

Selective secondary-ASR source replacement is not active. Retained replay does not yet establish a safe trigger or reconciliation rule: SenseVoice is fast enough to collect evidence but is not truth, faster-whisper is outside the live latency envelope, and ASR majority voting fails known context-supported cases. Groq remains provider-failure fallback rather than a semantic-evidence route.

## State, persistence, and telemetry

Translation workers share one immutable live-session identity, bounded `ConversationHistory`, translation cache, policy, and fallback state. History and cache are separate owners: a cache lookup cannot mutate conversation context, and only a successfully adjudicated publication commit may add an entry. The in-memory LRU is active. SQLite schema v2 provides optional prompt-versioned persistence; live DB cache is disabled by default, while clip mode may use it.

Runtime JSONL uses schema v6 at `logs/runtime_events_YYYYMMDD.jsonl`. `stt_request_contract` records the exact provider prompt/keyterms, their provenance, profile/activity identity, and hashed data inputs. `translation_request_contract` records exact messages, selected history, protected source spans, canonical obligations, profile/activity identity, and policy/data hashes. STT results and translation attempts reference those immutable contracts by ID. Candidate adjudication retains raw, protection-restored, and corrected text plus every failed invariant and its owning layer. These additive events let an exported runtime bundle reconstruct the causal request and publication path without guessing from the final subtitle.

`scripts/export_chatgpt_bundle.py` creates the portable one-run evidence unit.
`scripts/analyze_forensics_bundle.py` validates bundle/file/component hashes and
builds evidence-only audio-to-publication chains. It reports missing, orphaned,
ambiguous, or unverifiable lineage as findings rather than reconstructing it
from current code. Blind Phase 1 is performed in the designated ChatGPT Project
through computer use and follows `docs/agent/BLIND_PHASE1_WORKFLOW.md`.

After normal pipeline teardown joins its workers, Python emits a terminal
`runtime_lifecycle` event and automatically creates a default ChatGPT bundle
without WAV copies under `scratch/chatgpt_bundles/`. Dashboard-launched runs use
a launcher-provided stable run ID; because dashboard Stop/close force-terminates
the child, Tauri exports that exact persisted run after termination. Automatic
export is fail-soft and never changes the runtime's original exit status.

`logs/translations_YYYYMMDD.txt` is human-readable history. Optional STT WAV evidence is written under `logs/audio_dump/<run_id>/` only when `LIVE_TRANSLATE_DUMP_AUDIO=1`.

## Configuration and optional surfaces

- `config.py`: runtime defaults and validation.
- `.env`: secrets and operational environment switches.
- `logs/live_translate_config.json`: allowlisted dashboard overrides applied on restart.
- `data/streamer_profiles.json`: profile identities/STT glossary.
- `data/translation_profiles.json`: bounded prompt profiles.
- `data/translation_corrections.json`: deterministic normalization, corrections, and canonical tables.
- `data/replay_eval_snapshot.jsonl`: frozen deterministic replay baseline.

The implemented Tauri v2/Vue 3 dashboard exposes configuration, cache, process, and system-stat commands; API keys never enter its JSON bridge. Scene context is translation-only and never becomes spoken source text. `donation_ocr/` remains a separate OCR prototype/process.

## Validation

```powershell
.\live-subtitle-env\Scripts\python.exe -m pytest tests -q
.\live-subtitle-env\Scripts\python.exe scripts\replay_eval.py run --snapshot data\replay_eval_snapshot.jsonl
.\live-subtitle-env\Scripts\python.exe scripts\evaluate_translation_prompt_benchmark.py --production-runtime-baseline
```

The 75-case scorer is offline-only. Specialized validation and mutation boundaries are owned by `docs/agent/VALIDATION.md`; maintained commands are owned by `docs/agent/TOOL_INVENTORY.md`.

## Intentional queue asymmetry

| Queue | Strategy | Reason |
|---|---|---|
| `audio_queue` | keep newest | stale audio increases live delay |
| `text_queue` | keep newest | stale fragments have lost live value |
| `sentence_queue` | drop oldest only | assembled sentences retain more value |
| `subtitle_queue` | keep newest | overlay displays the newest subtitle |

Do not unify these policies without an explicit behavior change and ordering/backpressure validation.
