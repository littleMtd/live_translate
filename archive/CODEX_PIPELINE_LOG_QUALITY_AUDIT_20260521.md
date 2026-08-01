# Codex Pipeline-aware LOG-first Quality Audit — 2026-05-21

## A. Repo state
- git status summary: branch `main`; working tree was already dirty before this audit.
- staged state: no staged changes (`git diff --cached --stat` was empty).
- changed/untracked notes:
  - `config.py` modified: local profile change from `mwmeu` to `hades_chxxnnx`. This looks like local runtime config and is relevant only if the app is run from this worktree now.
  - Untracked/ignored runtime evidence exists under `logs/` and `audio/`.
  - Other untracked files already present: `.pytest-labels/`, `.pytest-tmp/`, `.pytest-tmpgroq-fallback/`, `.pytest-tmptask15-validation/`, `AGENTS.md`, and several optimization markdown files. I did not read or rely on the optimization docs for this audit.

## B. Brief pipeline map
- VAD/segmentation: `modules/audio_capture.py` captures audio frames, applies WebRTC VAD, accumulates speech until silence or max speech duration, then enqueues audio chunks.
- STT: `modules/stt.py` consumes audio chunks, calls Groq/Whisper or SenseVoice, and emits `stt` runtime events with engine/model/status/reason/audio seconds/latency/profile metadata. Successful STT text is not logged in `stt` events; downstream `translation.source_text` is the observable post-STT/post-sentence-buffer source.
- source text / sentence splitting: `modules/sentence_buffer.py` and `modules/sentence_splitter.py` buffer STT text and emit `SentenceEvent` records with `text`, `incomplete`, STT metadata, and dependency marker context.
- translation: `modules/translator.py` runs policy filtering, source normalization, exact slang lookup, profile prompt construction, memory/db cache lookup, engine fallback, source-aware corrections, meta-output filtering, and runtime event logging.
- cache: translation memory and DB cache are represented in runtime fields `result_source` and `cache_status` (`memory_hit`, `db_hit`, `miss`, `skipped`, etc.).
- target post-processing: source-aware correction rules in `modules/translator.py` replace known bad target forms only when source aliases are detected.
- emit/output: translator workers assign `sequence_id`, complete in parallel, then emit in-order through `next_emit_seq`; runtime events include `subtitle_emitted`, `subtitle_suppressed_reason`, `queue_wait_ms`, `output_delay_ms`, and `predecessor_stall_ms`.
- log locations: primary evidence is `logs/runtime_events_*.jsonl`; human-readable translations are in `logs/translations_*.txt`; captured audio exists under `audio/`.

## C. Logs / runtime evidence inspected
- files found:
  - `logs/runtime_events_20260516.jsonl` through `logs/runtime_events_20260521.jsonl` (ignored/untracked runtime logs).
  - `logs/translations_20260514.txt` through `logs/translations_20260521.txt`.
  - `logs/live_translate.db`, `logs/live_translate_config.json`, and model benchmark JSON files.
  - `audio/LGcLBC9_RUk.*` and `audio/WgzD8Qiq-ac.*`.
- files inspected:
  - All six `runtime_events_20260516.jsonl` through `runtime_events_20260521.jsonl` for status/engine/result/cache/quality summaries.
  - Focused line-level inspection of `runtime_events_20260519.jsonl` and `runtime_events_20260520.jsonl` because they contain the latest non-mock production translation events.
  - `runtime_events_20260521.jsonl` was inspected but treated as low value for quality because all 24 translation events used `engine=mock`.
  - `translations_20260519.txt`, `translations_20260520.txt`, and `logs/live_translate_config.json` were searched for sensitive terms.
- run_ids / timestamps if available:
  - Latest production-quality HADES evidence is mainly from `run_id=20260519T123011Z-77304`, `20260519T153511Z-104776`, `20260520T052405Z-62828`, and `20260520T141444Z-37304`.
  - Runtime windows inspected: `2026-05-16T10:56:30Z` through `2026-05-21T14:15:34Z`.
- runtime summary:
  - Non-mock translation events across runtime logs: 4,282.
  - Status: `success=3,987`, `filtered=244`, `failed=51`.
  - Engines: mostly `nvidia=3,809`, plus `claude=159`, `google_translate=57`, `gemini=14`, and `unknown=243`.
  - Result sources: `api=3,962`, `policy=242`, `memory_hit=16`, `db_hit=8`, `slang=1`, `post_policy=2`, `none=51`.
  - Quality flags: `empty_target=295`, `very_short_target=270`, `low_target_cjk=79`, `low_source_hangul=68`, `long_target_ratio=4`.
- grep/search terms used:
  - Sensitive terms: `마크`, `Minecraft`, `섭주`, `섭쥬`, `썹주`, `SUBJU`, `服주`, `服主`, `챈나`, `찬나`, `챗나`, `챗나룡`, `챗나룸`, `봉준`, `성태`, `하데스`, `응원과 사랑`, `큰 힘이 됩니다`, `단인`, `단위`.
  - Pipeline terms: `vad`, `VAD`, `stt`, `STT`, `whisper`, `groq`, `segment`, `segmentation`, `chunk`, `partial`, `final`, `emit`, `queue`, `latency`, `cache`, `retry`, `timeout`.

## D. Confirmed current runtime quality issues

### Issue X1
- Title: HADES Chxxnnx variants from STT are not normalized, causing `-chan`, `ChanRoom`, or raw Korean output.
- Log evidence:
  - file path: `logs/runtime_events_20260519.jsonl`
  - line/timestamp/run_id: line 1483, `2026-05-19T12:44:14.331870+00:00`, `run_id=20260519T123011Z-77304`
  - source/STT text: `안녕? 챗나룡은 고래 서버 안 해? 저는 챗나룡 서버를 준비해야 되기 때문에...`
  - actual output: `你好？-chan龍不搞鯨魚伺服器嗎？因為我得準備-chan龍伺服器……`
  - expected output: use the canonical HADES rendering, e.g. `Chxxnnx龍` or a deliberate `Chxxnnx` room/server form, not `-chan`.
  - additional evidence: line 1492 maps `챗나룸` to `ChanRoom`; line 1581 maps `채나야` to `-chan`; `logs/runtime_events_20260520.jsonl:443` leaves raw Korean plus `-chan` for `채나님`.
- Suspected root stage: STT plus source normalization / glossary. The bad source forms come from STT, but the downstream profile currently has no deterministic mitigation for recurring variants.
- Why this is still current: current source-aware rules only use `("챈나",)` as the Chxxnnx source alias. Current verification against log samples leaves `챗나룡`, `챗나룸`, and `채나` outputs unchanged.
- Current repo evidence:
  - `modules/translator.py:163-165` has Chxxnnx source aliases/wrong forms for `챈나` and `-chan`, but not `챗나룡`, `챗나룸`, `채나`, or `찬나`.
  - `data/streamer_profiles.json` and `data/translation_profiles.json` include canonical `챈나`, not these observed variants.
  - Existing tests focus on canonical `챈나` correction.
- Is translator.py the right fix target? yes, but only for narrow profile-gated source normalization/source-aware correction. The upstream root remains STT.
- Minimal fix idea: add profile-gated normalization or alias coverage for repeated, observed HADES variants only. Avoid broad fuzzy matching.
- Test idea: regression tests for `챗나룡`, `챗나룸`, `채나야`, and `채나님` showing `-chan`/`ChanRoom` is corrected only under `hades_chxxnnx`.
- Risk level: medium; false positives are possible if variants are over-broadened.
- Priority: P1

### Issue X2
- Title: Mid-sentence glossary terms are not deterministically enforced (`마크` and `SUBJU`).
- Log evidence:
  - file path: `logs/runtime_events_20260519.jsonl`
  - line/timestamp/run_id: line 1403, `2026-05-19T12:38:06.082686+00:00`, `run_id=20260519T123011Z-77304`
  - source/STT text: `오 좋겠다 마크 영상도 봐줘 요즘 마크 영상 좀 이제 막 보려고 하고 있긴 하죠 ...`
  - actual output: `... Mark的影片 ... Mark影片 ... 一直看Mark ...`
  - expected output: `Minecraft` for `마크` in the game/video context.
  - line/timestamp/run_id: line 1320, `2026-05-19T12:32:17.990173+00:00`, same run
  - source/STT text: `아니 근데 또 제가 제 역량으로 또 열심히 하면 SUBJU API도 있으니까`
  - actual output: `... 還有 SUBJU API 呢`
  - expected output: `... 還有 服主 API ...`
- Suspected root stage: source normalization / glossary plus translation engine / prompt. The glossary exists, but exact slang lookup does not enforce terms inside longer sentences.
- Why this is still current: current tests confirm exact slang hits, and explicitly show non-exact phrases such as `마크 서버` do not hit `slang_result`. Runtime logs show the engine sometimes honors the prompt (`Minecraft` appears in other lines) and sometimes does not.
- Current repo evidence:
  - `data/default_slang.json:69-74` includes `마크 -> Minecraft` and `SUBJU -> 服主`.
  - `data/translation_profiles.json` has HADES prompt guidance for both terms.
  - `tests/test_translation_policy.py:41-44` covers exact slang and asserts `slang_result("마크 서버")` is `None`.
  - `tests/test_config.py:66-71` verifies the glossary entries exist, not that mid-sentence enforcement works.
- Is translator.py the right fix target? yes/unclear. A deterministic source/target term enforcement layer could live near translator/policy, but the problem should not be described as an engine-only mistranslation.
- Minimal fix idea: token-aware, profile-aware mid-sentence glossary enforcement for high-value terms that are unambiguous in context; start with `SUBJU` and `마크` only.
- Test idea: cases for `마크 영상`, `마크 서버`, `SUBJU API`, and non-Minecraft uses if any are known.
- Risk level: medium; `마크` can mean a person named Mark outside game context, so context gating matters.
- Priority: P1

### Issue X3
- Title: Source-aware target correction can create mixed-script artifacts such as `金Kim Bongjun`.
- Log evidence:
  - file path: `logs/runtime_events_20260519.jsonl`
  - line/timestamp/run_id: line 1457, `2026-05-19T12:42:06.218122+00:00`, `run_id=20260519T123011Z-77304`
  - source/STT text: `... 봉준, 김봉준. ... 고세구 ...`
  - actual output: `... 봉준、金Bongjun ... 高世久 ...`
  - expected output: `Kim Bongjun` and `Gosegu`, without raw Korean or `金Bongjun`.
- Suspected root stage: target-side post-processing.
- Why this is still current: current source-aware correction fixes some parts but would turn `金Bongjun` into `金Kim Bongjun`, which is also wrong.
- Current repo evidence:
  - `modules/translator.py:169-171` maps `봉준/김봉준/Bongjun` to `Kim Bongjun`.
  - `_replace_wrong_name_forms` applies a regex over alternatives without guarding against mixed-script prefixes such as `金Bongjun`.
  - Current verification on the line 1457 event produces `Kim Bongjun、金Kim Bongjun、...、Gosegu...`.
- Is translator.py the right fix target? yes.
- Minimal fix idea: make target-form replacement compound-safe, with explicit handling for `金Bongjun`/`金 Bongjun` and negative tests for already-canonical forms.
- Test idea: source `봉준, 김봉준` with target `봉준、金Bongjun、Bongjun、Kim Bongjun`; expected all valid mentions normalize to `Kim Bongjun` without `金Kim Bongjun`.
- Risk level: medium; target-side regex changes can over-replace if boundary rules are too loose.
- Priority: P1

### Issue X4
- Title: Stacked Korean suffixes block source-aware name correction for `성태님한테도`.
- Log evidence:
  - file path: `logs/runtime_events_20260519.jsonl`
  - line/timestamp/run_id: line 1465, `2026-05-19T12:42:42.321872+00:00`, `run_id=20260519T123011Z-77304`
  - source/STT text: `성태님한테도 연락해보려고 하긴 했었는데 성태님은 어떻게 연락해야 되는지 몰라가지고 ...`
  - actual output: `本來也想聯絡Sungtae哥，但不知道該怎麼聯繫他，所以決定寫訊息。`
  - expected output: use canonical `KimSungtae`.
  - additional evidence: line 1665 repeats the same `성태님한테도` pattern and leaves `Sungtae哥`.
- Suspected root stage: target-side post-processing, specifically source-aware source alias detection.
- Why this is still current: current source alias matching accepts a single suffix such as `님` or `한테`, but it treats the combined suffix `님한테도` as one unknown suffix.
- Current repo evidence:
  - `modules/translator.py:175-177` defines `성태` aliases and wrong forms.
  - `_KOREAN_NAME_SUFFIXES` contains individual suffixes (`님`, `한테`, `도`) but `_source_alias_matches_at` reads all following Hangul as one suffix and requires an exact set match.
  - Current verification on line 1465 leaves `Sungtae哥` unchanged.
- Is translator.py the right fix target? yes.
- Minimal fix idea: allow validated stacked Korean particles/honorifics after name aliases, or add a small set of observed compound suffixes.
- Test idea: `성태님한테도`, `성태님은`, `성태형한테`, and negative cases like `가성태님`.
- Risk level: medium; suffix expansion must preserve existing false-positive protections.
- Priority: P1

### Issue X5
- Title: Valid or partially valid Korean sources sometimes produce empty failed translation output.
- Log evidence:
  - file path: `logs/runtime_events_20260519.jsonl`
  - line/timestamp/run_id: line 1606, `2026-05-19T15:40:12.155644+00:00`, `run_id=20260519T153511Z-104776`
  - source/STT text: `오 좋겠다. 마크 영상도 봐줘. 요즘 마크 영상 좀 이제 막 보려고 하고 있긴 하죠.`
  - actual output: `target_text=null`, `status=failed`, `engine=nvidia`, `latency_ms=20734.0`, `retry_count=0`, `subtitle_emitted=false`
  - expected output: a normal Chinese subtitle preserving `Minecraft`.
  - additional evidence: `logs/runtime_events_20260520.jsonl:1206` fails on an incomplete but ordinary fragment; `logs/runtime_events_20260520.jsonl:1556` and `:1569` fail on Japanese-trip context.
- Suspected root stage: translation engine / prompt / fallback / timeout. Some examples are sentence-fragment sensitive, but line 1606 is a valid complete utterance.
- Why this is still current: no shipped glossary/name task appears to address engine failures or fallback behavior, and current runtime event logic still records failed/null output with suppressed subtitle.
- Current repo evidence:
  - Translation events have `result_source=none`, `cache_status=miss/skipped`, and `quality_flags=["empty_target","very_short_target"]`.
  - `modules/translator.py` emits failed outcomes when no engine returns usable text.
- Is translator.py the right fix target? unclear. The fix target is probably engine/fallback configuration and failure instrumentation; translator.py is relevant only where it orchestrates fallback and logs failures.
- Minimal fix idea: add provider failure reason logging, targeted retry/fallback on null output, and a policy for incomplete fragments so short partials do not consume long engine timeouts.
- Test idea: fake primary engine returns empty/raises/timeout-like result and secondary engine succeeds; verify subtitle is emitted and runtime event records retry/fallback metadata.
- Risk level: medium/high; fallback changes affect latency and cost.
- Priority: P1

## E. Upstream VAD/STT quality issues

### Upstream Issue E1
- Log evidence: `logs/runtime_events_20260519.jsonl:1348`, `:1352`, and `:1355` translate `시청자 여러분의 응원과 사랑은 저에게 아주 큰 힘이 됩니다...` as real speech. `logs/runtime_events_20260520.jsonl:399`, `:714`, `:716`, and `:1707` show related stock outro/subscription templates being filtered.
- Why this is likely VAD/STT, not translator: the phrases are boilerplate STT hallucination/outro text, not streamer content. The translator correctly translates what it receives when policy does not reject it.
- Better next action:
  - logging improvement: log sanitized/rejected template family and keep a short source sample for filtered STT template events.
  - STT prompt/model/config: check if Groq/Whisper produces this template under low/no-speech conditions.
  - segment merge/split policy: avoid sending low-confidence silent tails as real segments.
  - needs more samples: gather fresh post-task logs to see if this variant still appears.

### Upstream Issue E2
- Log evidence: recurring STT/source variants around Chxxnnx: `챗나룡`, `챗나룸`, `채나`, `츤나`, and similar forms in `logs/runtime_events_20260519.jsonl` and `20260520.jsonl`.
- Why this is likely VAD/STT, not translator: the source text itself is not the canonical streamer name. Translation can mitigate known variants, but the first failure is recognition of a proper noun.
- Better next action:
  - STT prompt/model/config: add HADES proper noun prompt/context if supported by the STT path.
  - logging improvement: include STT text in successful `stt` runtime events or add a privacy-safe sample mode so source mutations can be attributed before sentence splitting.
  - needs more samples: collect fresh HADES runs after any STT context change.

### Upstream Issue E3
- Log evidence: failed or low-value incomplete fragments such as `logs/runtime_events_20260520.jsonl:1206` (`그래서 뭔가 학교인데도`, `incomplete=true`) and `:1556` (`그리고 제가 또 전에는 ... 스미마셍 같은 것도`, `incomplete=true`).
- Why this is likely VAD/STT, not translator: the source arrives as a dependent fragment with `starts_with_dependency_marker=true`; some failures are more about cut timing than Chinese translation rules.
- Better next action:
  - VAD tuning: inspect chunk duration and silence boundaries around these line numbers.
  - segment merge/split policy: merge dependency-marker fragments when the next segment arrives quickly.
  - logging improvement: add VAD segment/chunk id to STT and translation events so fragments can be traced across stages.

## F. Historical or likely-fixed log issues

### Fixed/likely stale F1
- Log evidence: `logs/runtime_events_20260519.jsonl:1577` and `logs/runtime_events_20260520.jsonl:300` show canonical `챈나` in source but `-chan` in output.
- Why stale/fixed: current source-aware correction changes canonical `챈나` + `-chan` to `Chxxnnx` in verification.
- Which shipped task likely fixed it: Task #15 and Task #16, especially Hangul self-form and canonical/wrong-form coexistence handling.
- Current repo evidence: `modules/translator.py:163-165` includes `챈나` in both source aliases and wrong forms, and `tests/test_translator.py` covers canonical Chxxnnx corrections.

### Fixed/likely stale F2
- Log evidence: historical concern around mixed source-side `服주`.
- Why stale/fixed: current HADES source normalization maps `服주` to `섭주` before slang/cache/engine matching.
- Which shipped task likely fixed it: Top-5 #4 narrow source-side normalization `服주→섭주` for HADES.
- Current repo evidence: `modules/translator.py:112`; tests around `tests/test_translator.py:1342-1404` cover profile-gated behavior, slang hit for exact `服주`, and mid-sentence engine input normalization.

### Fixed/likely stale F3
- Log evidence: older `runtime_events_20260516.jsonl` entries include meta/Q&A-style target output and explanatory STT comments.
- Why stale/fixed: newer policy and translator code include prompt-only-output guidance and meta garbage filtering; latest production logs show far fewer explicit meta-output artifacts.
- Which shipped task likely fixed it: source-aware correction and prompt/policy hardening work after early runtime captures.
- Current repo evidence: `_looks_like_meta_garbage_output` in `modules/translator.py` and `result_source=post_policy` event handling.

## G. Hypotheses needing reproduction or fresher logs

### Hypothesis G1
- Hypothesis: bad cached translations may persist after glossary/profile fixes.
- Log clue: real logs include only small cache hit counts (`memory_hit=16`, `db_hit=8`) and no clearly proven stale bad cache recurrence in the inspected samples.
- Evidence gap: no repeated bad output was traced from `db_hit` or `memory_hit` to a known fixed glossary term.
- What runtime sample would confirm it: same source text before and after a glossary fix, with later runtime event showing `cache_status=db_hit` or `memory_hit` and the old bad target.
- Why not implement yet: clearing or versioning cache without a confirmed stale-cache sample risks churn and masks root causes.

### Hypothesis G2
- Hypothesis: emit/order latency can make subtitles feel delayed even when order is technically correct.
- Log clue: runtime events include `predecessor_stall_ms`; older runs have occasional high stalls, but sequence IDs still appear in-order.
- Evidence gap: no captured display transcript showing duplicated, reordered, or user-visible stale subtitle output.
- What runtime sample would confirm it: paired runtime events plus captured display output around a high `predecessor_stall_ms` window.
- Why not implement yet: current evidence supports latency monitoring, not an emit/order correctness bug.

### Hypothesis G3
- Hypothesis: VAD chunking is contributing to source cuts, but current logs do not expose enough cross-stage IDs.
- Log clue: translation events contain `incomplete=true`, dependency markers, and failed fragments, while STT events have audio metadata but no successful STT text and no shared VAD segment id.
- Evidence gap: no VAD segment id/chunk duration linked directly to each translation source.
- What runtime sample would confirm it: one run with VAD chunk id, audio seconds, STT text, sentence-buffer output, and translation output on the same event chain.
- Why not implement yet: without correlated logging, changing VAD thresholds would be guesswork.

## H. Top 5 recommended next tasks

### 1. Add narrow HADES source normalization for repeated Chxxnnx STT variants
- exact scope: profile-gated handling for observed variants such as `챗나룡`, `챗나룸`, `채나야`, and `채나님`.
- non-goals: no broad fuzzy matching for all near-`챈나` strings; no STT model swap.
- likely files touched: `modules/translator.py`, possibly `data/streamer_profiles.json` / profile data, `tests/test_translator.py`.
- required tests: profile-gated positive and negative cases, including non-HADES profile no-op.
- risk level: medium.
- why this is worth doing next: it has multiple recent runtime examples and current code verification shows no mitigation.

### 2. Add deterministic mid-sentence glossary enforcement for high-value terms
- exact scope: start with `SUBJU -> 服主` and `마크 -> Minecraft` in clear game/server/video contexts.
- non-goals: do not build a general glossary replacement system for every slang entry in this pass.
- likely files touched: `modules/translator.py`, `modules/translation_policy.py` or a small glossary enforcement helper, `tests/test_translation_policy.py`, `tests/test_translator.py`.
- required tests: `SUBJU API`, `마크 영상`, `마크 서버`, plus at least one non-context false-positive guard for `Mark`.
- risk level: medium.
- why this is worth doing next: the glossary entries are shipped but runtime shows engine/prompt enforcement remains inconsistent.

### 3. Make source-aware target correction compound-safe and suffix-stack-aware
- exact scope: prevent artifacts like `金Kim Bongjun`; allow source alias matches through stacked suffixes like `성태님한테도`.
- non-goals: no unrelated name list expansion.
- likely files touched: `modules/translator.py`, `tests/test_translator.py`.
- required tests: `金Bongjun`, already-canonical `Kim Bongjun`, `성태님한테도`, `성태님은`, and existing false-positive tests.
- risk level: medium.
- why this is worth doing next: current post-processing can both miss a correction and introduce a new visible artifact.

### 4. Extend STT template hallucination handling for `응원과 사랑` variants
- exact scope: classify repeated `시청자 여러분의 응원과 사랑은...큰 힘이 됩니다` as likely STT template garbage when isolated or repeated.
- non-goals: do not block real streamer thank-you messages broadly.
- likely files touched: `utils/text_heuristics.py`, `modules/translation_policy.py`, `tests/test_translation_policy.py`, possibly `tests/test_analyze_runtime_events.py`.
- required tests: isolated/repeated variant rejection, mixed real content preservation, sanitizer behavior.
- risk level: medium.
- why this is worth doing next: current policy catches adjacent stock templates but lets this observed variant through.

### 5. Improve failed translation fallback and failure logging
- exact scope: capture provider failure reason, retry/fallback on empty target for valid sources, and distinguish incomplete-fragment failures from engine failures.
- non-goals: no wholesale engine replacement.
- likely files touched: `modules/translator.py`, engine wrapper modules, runtime event tests.
- required tests: primary engine empty/timeout-like failure with secondary success; failed incomplete fragment metadata; subtitle emit/suppress assertions.
- risk level: medium/high.
- why this is worth doing next: recent production logs show real subtitles dropped with `target_text=null`, including at least one valid complete `마크 영상` utterance.

## I. Non-goals
- Do not blame all bad Chinese output on `translator.py`; several issues originate upstream in STT or segmentation.
- Do not implement broad fuzzy proper-noun matching without runtime evidence and false-positive tests.
- Do not use old optimization docs as authority for current quality state.
- Do not clear caches or add cache invalidation until a stale-cache runtime sample is confirmed.
- Do not tune VAD thresholds without correlated VAD/STT/translation event IDs.
- Do not make prompt-only changes for terms that runtime logs show need deterministic enforcement.
- Do not stage, commit, or push as part of this discovery phase.
