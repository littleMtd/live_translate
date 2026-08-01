# Codex Independent Quality Audit — 2026-05-21

## A. Repo evidence checked

- Git status summary: `live_translate` is its own git repo on branch `main`. Pre-report dirty state: `M config.py`, `M main.py`, `M tests/test_main.py`. After this audit, `CODEX_QUALITY_AUDIT_20260521.md` is the only file I added.
- Staged state: none. `git diff --cached --name-status` returned empty.
- Changed files summary:
  - `config.py`: listen-mode STT config fields added, and default `streamer_profile` changed from `mwmeu` to `hades_chxxnnx`.
  - `main.py`: `--listen` mode and listen-mode config override added.
  - `tests/test_main.py`: listen-mode config test added.
- Untracked summary: `.pytest-*` temp dirs, `AGENTS.md`, `OPTIMIZATION*.md`, and several Claude/Codex pipeline audit markdown files. I did not read `.claude/` or `CLAUDE*.md` contents.
- Files inspected:
  - `modules/translator.py`
  - `modules/translation_policy.py`
  - `modules/translation_memory.py`
  - `modules/translation_runtime.py`
  - `modules/translation_engines.py`
  - `modules/db.py`
  - `modules/translation_prompts.py`
  - `data/default_slang.json`
  - `data/streamer_profiles.json`
  - `data/translation_profiles.json`
  - `tests/test_translator.py`
  - `tests/test_translation_policy.py`
  - `tests/test_translation_prompts.py`
  - `tests/test_config.py`
  - `logs/runtime_events_20260521.jsonl`
  - `logs/runtime_events_20260522.jsonl`
  - `logs/runtime_events_20260523.jsonl`
  - `logs/runtime_events_20260524.jsonl`
  - `logs/translations_20260523.txt`
- Grep/search terms used:
  - `wrong_forms`, `source_aliases`, `_SOURCE_NORM`, `_KOREAN_NAME_SUFFIXES`, `_looks_untranslated`, `use_profile`, `prompt_version`
  - `服주`, `服쥬`, `섭주`, `섭쥬`, `썹주`, `SUBJU`, `섭쥬방`, `섭주방`, `썹주방`, `SUBJU방`
  - `챈나`, `채나`, `챗나`, `챗나룽`, `챗나룬`, `-chan`, `Chxxnnx`
  - `성태`, `性泰`, `Sungtae`, `김봉준`, `봉준`, `키마`, `큐마`, `띵귤`, `싱귤`, `솜주먹`, `솜펀치`
  - `마크`, `Minecraft`, `Mark`, `히나`, `희나`, `希娜`
  - old-doc hint search in `OPTIMIZATION*.md`: `마크`, `Minecraft`, `섭주`, `source-aware`, `wrong_forms`, `Chxxnnx`, `cache`, `prompt_version`

## B. Confirmed remaining issues

### Issue X1

- Title: Source-aware name correction misses honorific/case suffix chains and lets current HADES names leak.
- Trigger example: `챈나님이 ...` with target `-chan ...`; `성태님도 ...` with target `性泰`.
- Expected behavior: HADES profile should render `챈나` as `Chxxnnx` and `성태` as `KimSungtae` even when followed by common chains like `님이`, `님도`, `님은`, `라고`, `처럼`.
- Current behavior: `_source_alias_matches_at()` consumes the whole Hangul suffix run and requires exact membership in `_KOREAN_NAME_SUFFIXES`. `님` is allowed, but `님이` / `님도` are not. Runtime sample after shipped tasks:
  - `logs/runtime_events_20260523.jsonl:26`: source has `챈나님이`; target emits `-chan`.
  - `logs/runtime_events_20260523.jsonl:72`: source has `성태님도`; target emits `性泰`.
- Evidence path / grep result:
  - `modules/translator.py:124-157`: suffix allowlist contains single suffixes only.
  - `modules/translator.py:223-243`: suffix run must match one full allowlist item.
  - `modules/translator.py:168-185`: HADES rules cover `챈나` and `성태`, but `성태` wrong_forms omit `性泰`.
  - `tests/test_translator.py` covers `챈나님 오늘 와요`, but not `챈나님이` / `성태님도`.
- Existing related logic: source-aware corrections are profile-gated by `_name_rendering_rule_enabled()`, then applied after API/cache via `_apply_source_aware_corrections()`.
- Minimal fix idea: make suffix matching accept safe chains of honorific + case/topic particles, and add observed `性泰` as a HADES-only wrong form for `성태`.
- Test idea: add positive cases for `챈나님이`, `성태님도`, `봉준님은`, `챈나라고`, `키마처럼`; keep negative cases like `김챈나`, `성태권도`, `가성태님`.
- Risk level: Medium. Suffix expansion can overmatch if it becomes too broad.
- Priority: P1

### Issue X2

- Title: Current `챗나*` STT variants are still outside the Chxxnnx normalization/correction path.
- Trigger example: `챗나룽이?`, `챗나룬이 ... 서버`.
- Expected behavior: current HADES/Chxxnnx server-name variants should not surface as `-chanlung` / `-chanrun`.
- Current behavior: source normalization only covers `채나` family plus `服주`; it does not cover `챗나룽`, `챗나룬`, or related observed variants. Runtime sample after shipped tasks:
  - `logs/runtime_events_20260523.jsonl:72`: `챗나룽이?` -> `-chanlung?`
  - `logs/runtime_events_20260523.jsonl:1035`: `챗나룬이 ... 서버` -> `-chanrun...`
- Evidence path / grep result:
  - `modules/translator.py:109-122`: `_SOURCE_NORM_BY_PROFILE` has `채나* -> 챈나*` and `服주 -> 섭주`, but no `챗나*`.
  - `modules/translator.py:168-173`: Chxxnnx source alias is only `챈나`.
  - `tests/test_translator.py:1381-1408`: tests cover `채나` family only.
- Existing related logic: source normalization runs before slang/cache/API and before source-aware correction, so missing variants affect both engine input and post-correction eligibility.
- Minimal fix idea: add only runtime-observed, profile-gated `챗나룽/챗나룬` handling after deciding the canonical target form for these server-name compounds.
- Test idea: add HADES-only tests for `챗나룽`, `챗나룬`; explicitly assert other profiles and `use_profile=False` stay unchanged.
- Risk level: Medium. The exact desired rendering of server-name compounds needs a narrow rule, not a broad `챗나 -> 챈나` sweep.
- Priority: P1

### Issue X3

- Title: HADES member glossary is internally inconsistent across STT terms, translation profiles, examples, and post-corrections.
- Trigger example: `큐마 왔어요`, `솜펀치 언니`, `싱귤이 노래`.
- Expected behavior: HADES member names should have one consistent source alias set and target rendering: `Sompunch`, `Yeon Chorok`, `Singgyul`, `Kyma`, etc.
- Current behavior:
  - `data/streamer_profiles.json` STT terms use `솜주먹`, `띵귤`, `키마`.
  - `data/translation_profiles.json` fixed glossary maps `솜주먹 -> Sompunch`, `띵귤 -> Singgyul`, `키마 -> Kyma`.
  - The same profile examples use `솜펀치`, `큐마`, `싱귤`, and their outputs preserve Korean surface forms like `큐마來了` / `싱귤唱歌`.
  - `modules/translator.py` has post-correction for `키마` only, not `큐마`, `솜주먹/솜펀치`, `띵귤/싱귤`, or `연초록`.
- Evidence path / grep result:
  - `data/streamer_profiles.json:51-55`
  - `data/translation_profiles.json:5` and `data/translation_profiles.json:11`
  - `modules/translator.py:187-191`
  - `tests/test_translation_prompts.py` asserts profile contains `Sompunch`, `Yeon Chorok`, `Singgyul`, `Kyma`, but does not assert examples or correction rules align.
- Existing related logic: only selected names have `_NameRenderingRule`; the profile prompt is expected to carry the rest.
- Minimal fix idea: align HADES aliases and examples first; then add source-aware corrections only for names with runtime evidence or high confidence aliases.
- Test idea: profile text tests for source alias consistency; source-aware tests for `큐마`, `싱귤`, `솜펀치` only after target canonical forms are confirmed.
- Risk level: Medium. Some variants may be real alternate names; fixing without alias policy can erase useful Korean branding.
- Priority: P1

### Issue X4

- Title: `희나` source variant bypasses existing Hina correction and current logs still output `希娜`.
- Trigger example: `희나님 일본 여행 좋아함?`
- Expected behavior: in the known VTuber-name context, `희나` should normalize/correct to `Hina` or the agreed canonical Hina rendering, not Chinese `希娜`.
- Current behavior: source-aware replacement only checks source term `히나`; exact slang only covers full `시라유키 히나`. Runtime sample after shipped tasks:
  - `logs/runtime_events_20260523.jsonl:1700`: `희나님` -> `希娜姐`
  - `logs/runtime_events_20260523.jsonl:2157`: `희나랑` -> `希娜`
- Evidence path / grep result:
  - `data/default_slang.json:68`: full-name only `시라유키 히나 -> Shirayuki Hina`.
  - `modules/translator.py` source-aware replacements include `히나`, not `희나`.
  - `logs/runtime_events_20260523.jsonl:1700`, `:2157`
- Existing related logic: `("히나",) -> ("希娜", "Hina")` style correction is shared, not profile-specific, but source term must match.
- Minimal fix idea: add a narrow source-aware source alias for `희나` only if runtime confirms this is consistently Hina in current streams; avoid broad global source normalization until false positives are checked.
- Test idea: `_apply_source_aware_corrections("희나님 ...", "希娜姐...") == "Hina姐..."`; add non-Hina negative if known.
- Risk level: Medium. `희나` can be a real Korean name spelling, so this needs context gating or runtime sample confidence.
- Priority: P2

### Issue X5

- Title: `섭주` sibling variants are still incomplete in source normalization and exact slang.
- Trigger example: `服쥬`, `服쥬방`, `섭주방`, `썹주방`, `SUBJU방`.
- Expected behavior: sibling forms of shipped `섭주/섭쥬/썹주/SUBJU -> 服主` and `섭쥬방 -> 服主房` should deterministically map or at least be explicitly non-goals.
- Current behavior:
  - `_SOURCE_NORM_BY_PROFILE` only maps `服주 -> 섭주`.
  - Exact slang contains `섭쥬방 -> 服主房`, but not `섭주방`, `썹주방`, or `SUBJU방`.
  - Local deterministic check: `TranslationPolicy.slang_result("섭주방")`, `"썹주방"`, and `"SUBJU방"` return `None`; `섭쥬방` returns `服主房`.
- Evidence path / grep result:
  - `modules/translator.py:109-122`
  - `data/default_slang.json:70-74`
  - `tests/test_translator.py:1341-1378` covers `服주` but not `服쥬`.
  - `tests/test_translation_policy.py:41-44` covers exact `섭쥬방` but not sibling room variants.
- Existing related logic: source normalization is HADES-profile gated; exact slang is global and exact-string only.
- Minimal fix idea: add only clear sibling forms with deterministic target (`服쥬 -> 섭쥬`, `服쥬방 -> 섭쥬방`, and room exact keys if accepted).
- Test idea: profile-gated source normalization tests for `服쥬`; exact slang tests for accepted room variants.
- Risk level: Low for `服쥬`; Medium for adding many room variants globally if they are rare or ambiguous.
- Priority: P2

### Issue X6

- Title: Post-output quality guard accepts short Korean leftovers and Simplified Chinese output.
- Trigger example: primary engine returns `안녕` for Korean source; primary engine returns `这是门` for `zh-TW` target.
- Expected behavior: untranslated Korean fragments and Simplified Chinese should be rejected or sent to fallback unless explicitly preserved names/terms.
- Current behavior: `_looks_untranslated()` returns `False` for any non-empty output shorter than 6 chars unless exactly equal to source, and only checks Japanese script after the Hangul ratio check. It has no Simplified Chinese check.
- Evidence path / grep result:
  - `modules/translator.py:324-343`
  - Deterministic probe: `_looks_untranslated("안녕", "이건 문이야") == False`; `_looks_untranslated("这是门", "이건 문이야") == False`.
- Existing related logic: `call_with_fallback()` treats `result and not looks_untranslated(result, text)` as a successful engine result, so this guard directly decides whether fallback is attempted.
- Minimal fix idea: add a conservative post-output script policy: reject short Hangul unless it is an allowed preserved term/name for the active profile; detect common Simplified-only characters or convert via a dedicated zh converter before accepting.
- Test idea: primary engine returns `안녕`, fallback returns `你好`; assert fallback is used. Primary returns `这是门`; assert not accepted as `zh-TW`.
- Risk level: Medium. Some Korean fandom terms are intentionally preserved, so an allowlist/context check is needed.
- Priority: P1

## C. Hypotheses needing runtime samples

### Hypothesis H1

- Hypothesis: Global exact `마크 -> Minecraft` may overfire outside Minecraft contexts.
- Why suspected: `data/default_slang.json:69` makes `마크` a global exact slang hit, while HADES profile wording says `마크 in Minecraft/game/server context`.
- Required evidence: runtime samples where source `마크` refers to a person/brand/other non-Minecraft referent.
- Why not implement yet: current HADES runtime samples are Minecraft server context and show this mapping is beneficial there.

### Hypothesis H2

- Hypothesis: Google Translate fallback remains a high-risk quality path for profile-bound names.
- Why suspected: `GoogleTranslateEngine` ignores system prompt, streamer profile, and history. Older logs show outputs like `哈迪斯`/`希娜`; current config uses `nvidia` with empty `engine_chain`, so this may be dormant.
- Required evidence: current post-shipped runtime rows with `engine=google_translate` under HADES/Stellive/Isegye.
- Why not implement yet: no current 20260523/20260524 Google fallback sample was found in this audit pass.

### Hypothesis H3

- Hypothesis: Normalized-source cache keys can hide raw-source distinctions after source normalization rules change.
- Why suspected: source normalization runs before cache/DB lookup, while `TranslationOutcome.source_text` preserves raw text. `服주` and `섭주` converge to the same cache key under HADES.
- Required evidence: cache hit where two distinct raw source forms should have different translations or diagnostics.
- Why not implement yet: for `服주 -> 섭주`, convergence appears intentional and beneficial.

### Hypothesis H4

- Hypothesis: `use_profile=False` may not mean "no profile-specific glossary behavior" to callers.
- Why suspected: global `default_slang` still contains `마크/섭주/SUBJU`, and shared source-aware rule for `고세구` is always enabled.
- Required evidence: runtime or test expectation that `use_profile=False` should suppress all streamer/glossary special cases, not just appended profile text.
- Why not implement yet: current code and tests intentionally keep some global slang active when profiles are disabled.

## D. Old-doc items that are fixed or stale

### Old Item D1

- Old item: `마크` mistranslates as `Mark`.
- Current repo evidence: `data/default_slang.json:69` has `마크 -> Minecraft`; `logs/runtime_events_20260523.jsonl` has Minecraft outputs for Minecraft-context `마크`.
- Status: fixed / partial. Fixed for current Minecraft context; possible non-Minecraft overfire remains H1.

### Old Item D2

- Old item: conflicting bare global person-name slang entries (`키마`, `봉준`, `성태`, `히나`) block profile rendering.
- Current repo evidence: `tests/test_config.py:73-78` asserts these bare keys are absent; `data/default_slang.json` no longer contains them.
- Status: fixed.

### Old Item D3

- Old item: `챈나 -> Chxxnnx` does not consistently apply.
- Current repo evidence: source-aware rules exist, wrong_forms coexistence is handled, and `채나*` source normalization exists. Current runtime still shows `챈나님이 -> -chan` and `챗나룬 -> -chanrun`.
- Status: partial.

### Old Item D4

- Old item: `섭주/섭쥬/썹주/SUBJU -> 服主` and `섭쥬방 -> 服主房`.
- Current repo evidence: exact slang keys exist for the listed base variants and `섭쥬방`; source norm covers `服주`. Sibling forms like `服쥬`, `섭주방`, `썹주방`, `SUBJU방` are not covered.
- Status: partial.

### Old Item D5

- Old item: STT template hallucinations like `자막 제공` / `시청해주셔서 감사합니다` leak into translation.
- Current repo evidence: `modules/translation_policy.py` has template guards/sanitizers; `logs/runtime_events_20260523.jsonl:3` filters a hard template as `stt_garbage`.
- Status: fixed / partial. Hard-template filtering works; some benign remnants still produce very short outputs like `그러면 시청해주셔서 감사합니다. -> 然後`, which is acceptable but worth monitoring.

### Old Item D6

- Old item: prompt/profile cache schema needs prompt-version separation.
- Current repo evidence: `modules/db.py` schema includes `prompt_version`; `tests/test_translator.py` checks prompt version changes between profiles.
- Status: fixed for DB keying. Runtime cache analysis still has per-worker confounds but that is observability, not a direct translation defect.

## E. Top 5 recommended next tasks

### Task 1

- Exact scope: Harden source-aware name matching for safe suffix chains and add observed `성태 -> 性泰` wrong form.
- Non-goals: Do not broaden all Hangul suffixes; do not add unrelated HADES names in the same patch.
- Likely files touched: `modules/translator.py`, `tests/test_translator.py`.
- Required tests: `챈나님이`, `성태님도`, `봉준님은`, `챈나라고`; negative `성태권도`, `김챈나`, wrong profile, `use_profile=False`.
- Risk level: Medium.

### Task 2

- Exact scope: Add narrow HADES handling for runtime-observed `챗나룽/챗나룬` leaks after deciding canonical output for those server-name compounds.
- Non-goals: No broad `챗나/찬나/츤나` family sweep without samples.
- Likely files touched: `modules/translator.py`, `tests/test_translator.py`.
- Required tests: HADES profile normalizes/corrects `챗나룽`, `챗나룬`; other profiles and `use_profile=False` unchanged.
- Risk level: Medium.

### Task 3

- Exact scope: Align HADES member aliases across `streamer_profiles.json`, `translation_profiles.json`, examples, and source-aware correction coverage.
- Non-goals: Do not add global person-name slang; do not change unrelated profiles.
- Likely files touched: `data/streamer_profiles.json`, `data/translation_profiles.json`, `tests/test_translation_prompts.py`, optionally `modules/translator.py` / `tests/test_translator.py`.
- Required tests: standard and Qwen profiles use consistent aliases/targets; examples no longer contradict fixed glossary; source-aware tests only for accepted aliases.
- Risk level: Medium.

### Task 4

- Exact scope: Add conservative output script guard for short Hangul leftovers and Simplified Chinese in `zh-TW` output.
- Non-goals: Do not reject known preserved Korean fandom terms/names blindly; do not introduce a large Chinese conversion dependency without evaluating impact.
- Likely files touched: `modules/translator.py`, `tests/test_translation_runtime.py` or `tests/test_translator.py`.
- Required tests: primary short Korean output falls through to fallback; simplified-only characters are not accepted as final `zh-TW`; preserved approved Korean term remains allowed.
- Risk level: Medium.

### Task 5

- Exact scope: Complete low-risk `섭주` sibling normalization/exact keys for observed or mechanically obvious forms.
- Non-goals: Do not invent broad SUBJU variants without runtime evidence; do not make source norm global if it stays HADES-specific.
- Likely files touched: `modules/translator.py`, `data/default_slang.json`, `tests/test_translator.py`, `tests/test_translation_policy.py`, `tests/test_config.py`.
- Required tests: `服쥬`, `服쥬방`, accepted room variants; wrong profile and `use_profile=False` unchanged for source normalization.
- Risk level: Low to Medium.

## F. Non-goals

- Do not modify source/tests in this audit phase.
- Do not read or rely on Claude analysis files.
- Do not broad-normalize every `찬나/첸나/챗나` string without concrete runtime triggers and false-positive checks.
- Do not add bare person names back into `default_slang.json`.
- Do not treat old optimization docs as authoritative over current code/log evidence.
- Do not refactor the translation architecture or cache schema as part of narrow quality fixes.
- Do not make Google Translate fallback policy changes without current runtime samples proving it is active.
- Do not clean old DB/cache rows unless a separate cache hygiene task is explicitly requested.
