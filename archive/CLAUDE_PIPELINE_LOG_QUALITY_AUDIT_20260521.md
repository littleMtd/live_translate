# Claude Code Pipeline-aware LOG-first Quality Audit — 2026-05-21

> Local review document — **never push, never stage**, same class as `OPTIMIZATION_*.md` and `CLAUDE_*_AUDIT_*.md`.
> Primary evidence: `logs/runtime_events_20260520.jsonl` (722 production translation events across 7 real runs / 4 profiles + 1 trivial 2-event run).
> `logs/runtime_events_20260521.jsonl` contains only mock-engine sanity runs (4 events × 24 starts) — discarded as evidence.
> Latest shipped commit at audit time: `11dabc8` (2026-05-21 22:17 +0800) — HADES source-side normalization `服주→섭주`. **No post-commit-11dabc8 production run exists yet.**

---

## 1. Repo state

- **git status summary**: branch `main`, 1 modified file (`config.py`), no staged changes, many untracked `OPTIMIZATION_*.md` / `CLAUDE_*.md` review docs (per memory rule, these stay local).
- **staged state**: empty.
- **dirty/untracked notes**:
  - `config.py`: local profile switch `streamer_profile = "mwmeu"` → `"hades_chxxnnx"` only. Unrelated to audit; matches user’s current viewing profile. Will not commit.
  - Untracked review docs (`OPTIMIZATION_ACTION_PLAN.md`, `OPTIMIZATION_QUALITY_AUDIT_20260519.md`, `OPTIMIZATION_TASK13_RUNTIME_CROSSCHECK_20260519.md`, etc.) all match the “review-only, never push” convention. Confirmed not staged.
  - Untracked `.pytest-tmp*/` scratch dirs from earlier Task #15/#16 work. Local-only.

---

## 2. Brief pipeline map

- **VAD / segmentation**: [`modules/audio_capture.py`](modules/audio_capture.py) (WASAPI loopback → VAD chunking) → [`modules/sentence_buffer.py`](modules/sentence_buffer.py) + [`modules/sentence_splitter.py`](modules/sentence_splitter.py). Force-cut rule: `force_cut_seconds`; punctuation-bounded complete-prefix split (`split_complete_prefix`, Task #10 B1). `_MIN_PREFIX_SIGNIFICANT=6`, `_MAX_TRIVIAL_RESIDUAL=3`. `incomplete=True` flag preserved through pipeline.
- **STT**: [`modules/stt.py`](modules/stt.py) — Groq `whisper-large-v3` primary; SenseVoice optional local engine. STT initial_prompt enriched with `cfg.active_streamer_profile`’s `stt_terms` from [`data/streamer_profiles.json`](data/streamer_profiles.json). [`modules/stt_policy.py`](modules/stt_policy.py) drops `no_speech`/low-logprob/hallucination outputs.
- **Translation**: [`modules/translator.py`](modules/translator.py) coordinator. Pipeline order:
  1. `policy.prepare_input()` → STT-template / song / garbage / low-value filtering
  2. `_normalize_source_before_matching()` → `_SOURCE_NORM_SHARED` ∪ `_SOURCE_NORM_BY_PROFILE[active_profile]` (HADES has `服주→섭주`)
  3. Slang exact-match (`policy.slang_result`) — `data/default_slang.json` (75 entries)
  4. Memory / DB lookup (`_lookup_existing_translation_event`)
  5. `_call_with_fallback` (`nvidia` / engine_chain)
  6. `_apply_source_aware_corrections()`:
     - `_SOURCE_AWARE_TARGET_REPLACEMENTS` (global, source-conditional Chinese-Hangul mistranslation patches)
     - `_NAME_RENDERING_RULES` (profile-scoped, Task #14/#15/#16) — currently only **`hades_chxxnnx`** has entries plus one shared `고세구→Gosegu`
  7. Meta-garbage post-filter (`_looks_like_meta_garbage_output`)
- **Cache**: [`modules/translation_memory.py`](modules/translation_memory.py) — in-memory LRU → SQLite write-through ([`modules/db.py`](modules/db.py)). Keyed by `(prepared_text, prompt_version, engine.model)`. `incomplete=True` translations bypass cache (`cache_status=skipped`).
- **Target post-processing**: same `_apply_source_aware_corrections` step above; also `_looks_untranslated` guard (Hangul ratio > 50% threshold) inside `call_with_fallback`.
- **Emit / output**: dedup suppress within `_DEDUP_SUBTITLE_SEC=5.0`; `put_latest(subtitle_queue,…)` drains all but latest. `_TRANSLATION_WORKERS=2` worker pool, `_MAX_PENDING_TRANSLATIONS=4` order-preserving emit (Task `ab5a86f`).
- **Log locations**: `logs/runtime_events_YYYYMMDD.jsonl` (per-translation + per-STT JSONL), `logs/translations_YYYYMMDD.txt` (human-readable Korean→Chinese), `logs/live_translate.db` (SQLite cache), `logs/stt_*.txt`, `logs/model_benchmark_*.json`. Filenames follow injected clock’s local timezone ([`utils/runtime_events.py`](utils/runtime_events.py)).

---

## 3. Logs / samples inspected

- **Files found** in `logs/`:
  - `runtime_events_20260516.jsonl` … `runtime_events_20260521.jsonl`
  - `translations_20260514.txt` … `translations_20260521.txt`
  - `stt_20260515_013530.txt`, `model_benchmark_2026051[8|0]_*.json`, `live_translate.db`, `live_translate_config.json`
- **Files inspected**:
  - **Primary**: `logs/runtime_events_20260520.jsonl` — 1,982 events (755 translation, 1,227 STT).
  - Skim: `runtime_events_20260521.jsonl` — 96 lines, all `engine=mock` test runs (excluded as non-evidence).
  - Code: [`modules/translator.py`](modules/translator.py), [`modules/translation_policy.py`](modules/translation_policy.py), [`modules/sentence_buffer.py`](modules/sentence_buffer.py), [`modules/pipeline_events.py`](modules/pipeline_events.py), [`modules/translation_prompts.py`](modules/translation_prompts.py) (referenced via grep), [`utils/runtime_events.py`](utils/runtime_events.py), [`data/default_slang.json`](data/default_slang.json), [`data/streamer_profiles.json`](data/streamer_profiles.json), [`data/translation_profiles.json`](data/translation_profiles.json).
- **Production run_ids extracted** (translation events with `engine=nvidia` + non-empty `profile_id`):

  | run_id | profile | n / succ / filt / fail | retry rows | quality_flags |
  |---|---|---|--:|---|
  | `20260520T044657Z-123192` | hades_chxxnnx | 83 / 82 / 1 / 0 | 1 | empty×1, very_short×1, low_target_cjk×3 |
  | `20260520T052405Z-62828` | hades_chxxnnx | 73 / 72 / 1 / 0 | 0 | low_target_cjk×2, empty×1, very_short×1 |
  | `20260520T053821Z-136712` | isegye_lilpa | 92 / 85 / 7 / 0 | 0 | empty×7, very_short×8, low_source_hangul×1 |
  | `20260520T055954Z-138140` | stellive_hina | 81 / 74 / 5 / 2 | 0 | empty×7, very_short×5, low_target_cjk×4 |
  | `20260520T101803Z-151060` | mwmeu | 183 / 182 / 0 / 1 | 9 | empty×1, very_short×1, low_target_cjk×1 |
  | `20260520T140152Z-17508` | mwmeu | 60 / 57 / 1 / 2 | 1 | empty×3, very_short×3 |
  | `20260520T141444Z-37304` | hades_chxxnnx | 148 / 144 / 4 / 0 | 0 | empty×4, very_short×4, low_target_cjk×1 |
  | `20260520T145640Z-38636` | hades_chxxnnx | 2 / 2 / 0 / 0 | 0 | — |

  Note: only `20260520T141444Z-37304` and `20260520T145640Z-38636` were captured **after** Task #15 commit (`5f0c0b2`, 2026-05-20 14:58 +0800). All other 5/20 runs are **pre-Task-15** for HADES-name behavior. **No run** is post-Task-16 (`a388e37`, 2026-05-21 00:51 +0800) or post-HADES-source-norm (`11dabc8`, 2026-05-21 22:17 +0800).
- **Search terms used**: 마크 Minecraft Mark 챈나 찬나 챗나 챗나룡 챗나룸 챗마 츤나 채나 채나로 채나롱 봉준 김봉준 Bongjun 성태 Sungtae KimSungtae 하데스 HADES 단인 단위 섭주 섭쥬 썹주 SUBJU 服주 服主 응원과 사랑 큰 힘이 나락 중박 섭종 썹종 글씨는 고세구 Gosegu 주르르 Jururu 릴파 Lilpa 주르륵 주루룰 일파 시라유키 히나 Hina Shirayuki 솜주먹 Sompunch 연초록 Chorok 띵귤 Singgyul 키마 Kyma 땡귤 렌트 대표 지효 민지 지상 리모컨 개극포 빡세 Chxxnnx -chan 짭봄주 손바람 키아 세나 솜창 글랜스 정유장 리츠 이비 수아 지안 초은; pipeline keywords vad VAD stt STT whisper groq segment chunk partial final emit queue latency cache retry timeout incomplete dependency_marker quality_flags.

---

## 4. Confirmed current runtime quality issues

Each issue below appears in a **post-Task-15 run** (`20260520T141444Z-37304` or `20260520T145640Z-38636`) **or** is structurally impossible to be fixed by Tasks #13–#16 + Top-5 #4 because the code path I traced does not cover it. Pre-Task-15 evidence is cited only as additional supporting frequency data.

### Issue C1 — STT mishear `채나` (missing ㄴ) bypasses HADES name canonicalization

- **Title**: HADES profile `_NAME_RENDERING_RULES` source_aliases only contain canonical `챈나`, so the high-frequency STT mishear `채나` (and clitic variants `채나롱`, `채나로`, `채나룬`, `채나야`, `채나님`) never triggers the wrong-form sweep; the engine often passes them through verbatim or partially substitutes `-chan`.
- **Log evidence**:
  - file path: `logs/runtime_events_20260520.jsonl`
  - line/timestamp/run_id: `20260520T141444Z-37304` (post-Task-15) @ 2026-05-20 14:14Z+
    - source `오늘은 그래서 소통하고 노래 부르다가 갔고 같은데? 천사채나 감사해요...` → target `今天就是聊了天、唱了歌就走了吧？謝謝天使-chan，感謝你。...`
  - Pre-Task-15 supporting frequency (same root cause): `20260520T044657Z-123192` and `20260520T052405Z-62828` together show 11 source sentences with `채나` (no `챈나`) and 2 mixed; targets contain raw `채나` 6 times and raw `-chan` 5 times.
  - actual output: contains `天使-chan` / `채나` (raw Hangul or `-chan` suffix).
  - expected output: `天使Chxxnnx` (or `Chxxnnx` consistently for vocative).
- **Suspected root stage**: **3. source normalization / glossary** (translator layer) and partly **2. STT** (Groq mishearing 챈 → 채). Translator-side normalization is the correct intervention because STT mishear is unreliable and the STT initial_prompt already lists `챈나` in [`data/streamer_profiles.json`](data/streamer_profiles.json) without fixing the issue.
- **Why this is still current**: Tracing [`modules/translator.py:160-191`](modules/translator.py#L160-L191) — `_NameRenderingRule(_HADES_PROFILE_ID, ("챈나",), ("챈나","-chan",...), "Chxxnnx")`. `_source_has_name_alias` (`modules/translator.py:238-247`) requires alias `챈나` to match in source. `채나` ≠ `챈나`, so the entire wrong-form sweep skips. `_SOURCE_NORM_BY_PROFILE[_HADES_PROFILE_ID]` (`modules/translator.py:110-114`) currently contains only `服주→섭주`; no `채나→챈나` mapping. Task #16 changed the canonical+wrong-form coexistence inside the sweep but did **not** widen what trips the sweep.
- **Current repo evidence**: confirmed via `Read` of `modules/translator.py` lines 109–235 and 256–278.
- **Is `translator.py` the right fix target?** **yes** — either widen `source_aliases` to include `채나` (and clitic-tolerant variants) or add `채나→챈나` to `_SOURCE_NORM_BY_PROFILE[hades_chxxnnx]`. The latter is preferred since it also normalizes the cache key and prevents 채나 from leaking into engine context.
- **Minimal fix idea**: add a small profile-scoped normalization map entry `{"채나": "챈나"}` after a word-boundary guard analogous to `_source_alias_matches_at` so non-name uses of `채나` (rare; phonetic ambiguity exists) are not blindly rewritten — or accept that risk since `채나` is documented as not a real Korean word and the audit (`OPTIMIZATION_QUALITY_AUDIT_20260519.md` §6) classifies it as “channa family STT split”.
- **Suggested tests**: extend `tests/test_translator.py` with:
  - `채나야 고마워` (HADES profile) → target contains `Chxxnnx`, not `-chan` / `채나`
  - `채나롱 서버` → target contains `Chxxnnx` (or canonical server rendering)
  - non-HADES profile with `채나` → unchanged (proves profile gating)
- **Risk / false positive concern**: medium. `채나` may rarely be an unrelated viewer name; the audit doc already accepts this trade-off in §6. Required guard: profile-scope.
- **Priority**: **P1** (highest visible recurring artifact; sustains hades_chxxnnx leak rate).

### Issue C2 — `isegye_lilpa` profile lacks post-processing name rules; engine renders names in Chinese (莉帕 / 莉朗 / 莉拉) despite profile prompt declaring `Lilpa` / `Gosegu` / `Jururu`

- **Title**: Few-shot glossary alone is insufficient to enforce the `Lilpa`/`Gosegu`/`Jururu` romanization; `_NAME_RENDERING_RULES` has no rule for `isegye_lilpa`, so engine output is shipped unchanged.
- **Log evidence**:
  - file path: `logs/runtime_events_20260520.jsonl`
  - run_id: `20260520T053821Z-136712` (isegye_lilpa, 92 events)
    - `릴파님 생일 축하합니다!` → `莉帕娘娘，生日快樂！` (expected: `Lilpa님, 生日快樂!` or `Lilpa, 生日快樂!`)
    - `미역국도 먹고. 와 우선 릴파 축하해줘서 고마워.` → `也喝了海帶湯。哇，首先謝謝你為莉帕慶生！`
    - `구독과 좋아요는 저에게 큰 힘이 됩니다. 감사합니다. 릴라님 감사합니다...` → `謝謝！莉拉님，謝謝！...莉拉님！...`
    - `1. 릴파 언니, ...릴랑이었어.` → `1. 莉帕姐姐, ...莉朗。`
  - 6 distinct sentences with source `릴파` rendered as `莉帕`/`莉朗`/`莉拉`; 0/6 rendered as `Lilpa`.
- **Suspected root stage**: **4. translation engine / prompt** (engine ignores Fixed Proper-Noun Glossary header) compounded by missing **5. target-side post-processing** to enforce canonical.
- **Why this is still current**: [`data/translation_profiles.json`](data/translation_profiles.json) `standard.isegye_lilpa` and `qwen.isegye_lilpa` both declare `고세구 -> Gosegu`, `주르르 -> Jururu`, `릴파 -> Lilpa`. The active runtime engine for these events was `nvidia` (Qwen model per `prompt_version=39e9c0bd` confirmed in code path), so the qwen profile WAS appended. The engine still output 莉帕. There is no `_NameRenderingRule` for `isegye_lilpa`, so no post-processing safety net. Untranslated-Korean leak rate for isegye_lilpa run: **15.2%** (14/92 targets contain Hangul).
- **Current repo evidence**: `Read` of `modules/translator.py:160-191` confirms only one shared rule (`고세구` shared) and four HADES rules. `Grep` for `isegye` / `lilpa` in `_NAME_RENDERING_RULES` returns nothing.
- **Is `translator.py` the right fix target?** **yes** — mirror the HADES `_NameRenderingRule` pattern for isegye_lilpa names. Risk: lower than HADES because there are no clitic-bearing canonical Hangul aliases to mishear (릴파, 주르르, 고세구 are stable).
- **Minimal fix idea**: append three `_NameRenderingRule(_ISEGYE_LILPA_PROFILE_ID, source_aliases, wrong_forms, canonical)` rules with the known Chinese mistranslations as wrong_forms (`莉帕`, `莉朗`, `莉拉`, `朱魯魯`, `高世久`, `朱魯魯爾`, `一帕`, `一派`).
- **Suggested tests**: add per-rule tests under `tests/test_translator.py` mirroring HADES tests.
- **Risk / false positive concern**: low. `莉帕` etc. are not standard Chinese words.
- **Priority**: **P1**.

### Issue C3 — `mwmeu` profile has the worst untranslated-Korean leak rate (17.7%, 43/243) because the profile prompt lacks a `[Fixed proper-noun glossary]` header and `_NAME_RENDERING_RULES` has no `mwmeu` entries

- **Title**: MW:MEU member names (`리츠`, `이비`, `수아`, `초은`, `지안` / `지한`) leak verbatim into Chinese targets.
- **Log evidence**:
  - file path: `logs/runtime_events_20260520.jsonl`
  - run_id: `20260520T101803Z-151060` (mwmeu, 183 events) and `20260520T140152Z-17508` (mwmeu, 60 events).
    - `미츠, 이비, 초은이 거의 비슷해요...` → `米茨、이비、초은幾乎都差不多呢...`
    - `리츠 면접을 왔을 때, 얘도 보자마자 오우 리츠다!` → `去面試리츠的時候，一看到她就心想：「喔！就是리츠！」`
    - `이비 언니 면접 때 되게 엘리베이터에서 이미 마주쳤어요.` → `面試이비姐姐時，我們在電梯裡早就碰過面了。`
    - `지안 언니도 일일 내로 연락 주겠다 하고` → `지안姐姐也說會親自聯絡`
    - `수아가 답변을 해줬는데` → `수아也回應了`
  - 17.7% of mwmeu targets retain at least one Hangul cluster.
- **Suspected root stage**: **3. source normalization / glossary** (profile prompt missing glossary header) and **4. engine** (engine has no fixed mapping to follow).
- **Why this is still current**: [`data/translation_profiles.json`](data/translation_profiles.json) `standard.mwmeu` and `qwen.mwmeu` both begin with `【MW:MEU 특화 범례】` — no `[Fixed proper-noun glossary]\n- X -> Y` header (every other profile has one). `_NAME_RENDERING_RULES` has no `mwmeu` entry. Compare HADES post-Task-15 untranslated-Korean rate: **5.9%** (3× lower).
- **Current repo evidence**: `Read` of `data/translation_profiles.json` (mwmeu sections lines 5–6 of `standard`, lines 11–12 of `qwen`). `Read` of `modules/translator.py:160-191`.
- **Is `translator.py` the right fix target?** **partially** — main fix is `data/translation_profiles.json` (data, not code). Secondary safety net is `_NAME_RENDERING_RULES`. Both are advisable, mirroring how HADES gets both layers.
- **Minimal fix idea**:
  1. Add `[Fixed proper-noun glossary]` block to `mwmeu` in `data/translation_profiles.json` (both `standard` and `qwen`) — `지한 -> Jihan`, `이비 -> Eebee`, `수아 -> Sua`, `리츠 -> Rits` (or `Litz`), `초은 -> Cho-eun`. **Romanization needs user decision** — defer to plan stage.
  2. Add `_NameRenderingRule` entries for each.
- **Suggested tests**: `tests/test_translator.py` per-name; `tests/test_translation_prompts.py` — profile glossary header presence.
- **Risk / false positive concern**: needs user romanization decisions; some names may have official rendering the user prefers. Do **not** ship without user sign-off on the canonical form.
- **Priority**: **P1** scope-wise, but **blocked on user decision** for canonical romanizations.

### Issue C4 — Pre-Task-16 `-chan` leak when source contains canonical `챈나` (LIKELY now fixed by Task #16, NOT YET RUNTIME-VALIDATED)

- **Title**: In pre-Task-15 hades runs, source `챈나야 고맙다. 그래서 나도 채나롱 서버 열심히 홍보했어 챈나야.` produced target `-chan，謝謝你。所以我也有好好幫Chxxnnx伺服器宣傳，-chan。` — mixed canonical correction and untouched `-chan`.
- **Log evidence**:
  - file path: `logs/runtime_events_20260520.jsonl`
  - run_id: `20260520T052405Z-62828` (pre-Task-15) — 2 such mixed outputs.
  - Codepoints verified: `U+002D` ASCII hyphen + `chan`, which is in current `wrong_forms`.
- **Suspected root stage**: was **5. target-side post-processing** — `_replace_wrong_name_forms` was performing partial rewrite leaving canonical-original-form `챈나` and one of the `-chan` variants unreplaced when both appeared.
- **Why I’m flagging this as “likely current” not “fixed”**: Task #15 (5/20 06:58 UTC, +Hangul self-form to wrong_forms) and Task #16 (5/21 16:51 UTC, canonical+wrong-form coexist) are both committed, but **no production run after Task #16 commit exists in `logs/`**. The pattern construction at [`modules/translator.py:260-262`](modules/translator.py#L260-L262) — `alternatives = sorted({rule.canonical, *rule.wrong_forms}, key=len, reverse=True)` — should now correctly unify `-chan` and canonical, but my LOG-first audit cannot confirm runtime efficacy.
- **Is `translator.py` the right fix target?** unchanged — the existing fix is already shipped.
- **Minimal fix idea**: no code change needed; capture one fresh HADES live run after `11dabc8` and re-grep `Chxxnnx` vs `-chan` counts.
- **Risk / false positive concern**: very low if Task #16 unit tests pass; observation just needs to confirm runtime behavior matches code.
- **Priority**: **P2** — block-of-evidence only; flag for post-implementation runtime validation, not a new fix.

### Issue C5 — Viewer/donor names and ad-hoc terms leak as untranslated Hangul (lower visibility, but present across all profiles)

- **Title**: Names like `키아` (subscriber count nickname), `손바람` (donor), `짭봄주` (alternate-name?), `세나`, `정유장`, `솜창` (slang/verb? singing), `하느초마`, `백인석`, `렌트` (rendered `倫特` inconsistently), `짭봄주`, `젠나룽` (server variant), `밀랑`, `릴라` (릴파 mishear?) leak verbatim or partially transliterated.
- **Log evidence**: 76 targets in real runs contain Hangul clusters (10.5% of 722); largest concentration in mwmeu (43) and hades (18). Selected samples:
  - `손바람님 ... 채나님도 ...` → `손바람님，... -chan也嗨到飛起來` (run 052405)
  - `짭봄주님 메일이 와서 영상을 딱 틀었는데` → `收到 짭봄주 的郵件，立刻打開影片` (run 052405)
  - `우리 키아가 700개. 챈나 귀여워.` → `我們的키아有700個，챈나好可愛。` (run 044657)
  - `네이징 된 마바.` → `已經變成奈津的馬巴` (run 141444; `네이징` is unclear — may be `nazaring`/STT noise)
  - `정유장이랑 정말로 사랑한다면` → `如果真的愛上正佑장` (run 044657)
- **Suspected root stage**: combination of **2. STT** (rare-word mishearing) and **4. engine** (no glossary, model gives up and copies the Hangul). Some are also **3. source normalization** candidates (e.g. `짭봄주` is likely a 김봉준 / 봉준 mishear; needs user confirmation).
- **Why this is still current**: these are individual viewer/donor names with no glossary entries — same pattern as the deferred `단위님`/`렌트님` from the prior audit (§4B).
- **Current repo evidence**: `default_slang.json` and the profile JSONs have no entries for any of these terms.
- **Is `translator.py` the right fix target?** **no, not in isolation.** Most of these are one-off viewer names — a translator-side fix list does not scale. Better options:
  - Tighten `_looks_untranslated` (raise sensitivity for very-low-frequency Hangul clusters in target).
  - User-decision queue for adding canonical renderings.
  - Generic post-processing: strip leftover `님` and transliterate single Hangul names — risky.
- **Minimal fix idea**: NONE without user input on each term. Recommendation: **collect candidate list** in a follow-up document, not a code change.
- **Suggested tests**: N/A until canonical renderings known.
- **Risk / false positive concern**: high if we blanket-replace.
- **Priority**: **P2 / Defer**. Track in a name-glossary candidate doc; ship one at a time as user confirms.

### Issue C6 — `구독과 좋아요는 저에게 큰 힘이 됩니다` (no 아주) sometimes mixes with real content and partly leaks

- **Title**: Variant without `아주` (“very”) of the YouTube template hallucination is sometimes preceded by a non-template head; sanitizer strips template tail but the head still translates with hangul-name suffixes preserved.
- **Log evidence**:
  - `20260520T053821Z-136712` (isegye_lilpa) — source `구독과 좋아요는 저에게 큰 힘이 됩니다. 감사합니다. 릴라님 감사합니다. 첫번째 방송이 성공하게! 감사합니다. 감사합니다. 릴라님! 불볕당 리턴즈 고마워.` → target `謝謝！莉拉님，謝謝！第一場直播成功了！謝謝！謝謝！莉拉님！不滅黨回歸，感謝！`
  - Pure-template variants (no real content) are correctly filtered as `stt_template_garbage` 6 times in same log.
- **Suspected root stage**: **3. source normalization / glossary** (template strip) and a side-effect of **C2** (isegye name rendering).
- **Why this is still current**: `is_stt_template_garbage` (`modules/translation_policy.py:196-252`) handles **dominant-template** cases. Mixed real+template falls through correctly (good), but the leading boilerplate `구독과 좋아요는 저에게 큰 힘이 됩니다.` was stripped — output begins with `謝謝！` — confirming `STT_TEMPLATE_STRIP_PHRASES` already covers this variant. So the **template strip itself is working**. The visible problem is purely **C2** (`릴라님` → `莉拉님` with `님` retained).
- **Current repo evidence**: targets show no leading `訂閱按讚` etc. — strip worked.
- **Is `translator.py` the right fix target?** **no** — this is a sub-symptom of C2. Resolving C2 (Isegye name rendering) and adding `님` post-process strip resolves the visible artifact.
- **Minimal fix idea**: covered by C2; optionally add a `님` suffix stripper for trailing Hangul name + `님` when remainder is non-Hangul (covered by `_NAME_RENDERING_RULES` once `릴파` rule is added).
- **Risk / false positive concern**: low.
- **Priority**: **subsumed by C2; do not split into a separate task**.

### Issue C7 — `_NAME_RENDERING_RULES` overlap with deprecated global `_SOURCE_AWARE_TARGET_REPLACEMENTS` for `히나` (stellive_hina) — neither is profile-scoped

- **Title**: `_SOURCE_AWARE_TARGET_REPLACEMENTS` at [`modules/translator.py:93`](modules/translator.py#L93) has `(("히나",), (("希娜","Hina"),))` — runs **globally** for any profile where source contains `히나`. Stellive profile rule cluster is otherwise empty.
- **Log evidence**: stellive_hina run has only 2 source events with `히나`, both filtered (empty target). Not visible-impacting in the captured runs, but the structure is fragile.
- **Suspected root stage**: **5. target-side post-processing** (potential cross-profile bleed).
- **Why this matters**: if a HADES stream guest mentions `히나`, the global rule rewrites `希娜→Hina` regardless of active profile. That may be desired, but the inconsistency between “Lilpa/Gosegu have nothing” and “Hina is global” is unintentional. The author probably intended profile-scoped.
- **Current repo evidence**: `_SOURCE_AWARE_TARGET_REPLACEMENTS` already mixes profile-agnostic mistranslation fixes (HADES, 끼윤, 예난, 철구) with what should be profile-scoped name rules — inconsistent.
- **Is `translator.py` the right fix target?** **yes** — clean up by migrating profile-specific names from `_SOURCE_AWARE_TARGET_REPLACEMENTS` into `_NAME_RENDERING_RULES` with proper scope.
- **Minimal fix idea**: as part of C2/C3, migrate `히나` (and `끼윤`, `예난`, `철구`, …) entries to the new rules where applicable. Defer if the user accepts the current ambiguity.
- **Risk / false positive concern**: low (mostly refactor).
- **Priority**: **P2** — quality-of-codebase, not a visible runtime regression.

### Issue C8 — Engine timeouts at ~13s p99 (1.5% of translations), 11 retries this day — upstream Nvidia/Qwen latency

- **Title**: `retry_count > 0` rows have `retry_reason=timeout`, `engine_latency_ms` 12.3–20.8 s (p99 13.3 s, max 20.8 s).
- **Log evidence**: 11 rows across runs 044657, 101803, 140152 — every retry is `timeout`.
- **Suspected root stage**: **upstream engine (Nvidia NIM-hosted Qwen3)** under load.
- **Is `translator.py` the right fix target?** **no** — already handled by `engine_chain` fallback and `nvidia.live_timeout` config (tuned in `db8c0cb`/`2b4c708`).
- **Minimal fix idea**: nothing; existing timeout/retry logic absorbed all 11 cases successfully (none became `failed`).
- **Priority**: **Not worth doing** as a translator task. Acceptable runtime.

---

## 5. Upstream VAD/STT quality issues

### U1 — STT mishear of canonical Korean names: `채나` ↔ `챈나`, `짭봄주` ↔ `김봉준`(?), `채나로`/`채나롱`/`채나룬`/`채나야`, `젠나룽`, `츤나`, `찬나`

- **Log evidence**: pre-Task-15 hades runs (044657, 052405) show `채나` family in 13 sources, `찬나` in 1, `챗나` / `챗나룡` / `챗나롱` in 1, `짭봄주(님)` in 2, `젠나룽` in 1. Post-Task-15 still shows `천사채나` and `채나` mishear (`20260520T141444Z-37304`).
- **Why this is likely VAD/STT, not translator**: the Korean source itself is wrong (missing ㄴ batchim, or character substitution). Translator can never reliably reverse-engineer mishears it didn’t see in training.
- **STT initial_prompt already includes `챈나`** in [`data/streamer_profiles.json`](data/streamer_profiles.json) HADES `stt_terms`; the bias doesn’t fully overcome whisper’s phonotactic preference.
- **Better next action**:
  - **logging improvement**: emit STT-stage `avg_logprob` / `no_speech_prob` already captured; aggregate them per term to see if `채나` mishears correlate with low confidence.
  - **STT prompt/model/config**: try lifting `chxxnnx` (latin) into the Groq prompt, or include `채나` as a known mishear marker in policy/normalization.
  - **segment merge/split policy**: not relevant — STT outputs whole sentences here.
  - **needs more samples**: yes, particularly across stream sessions to confirm `채나` rate is stable enough to justify a normalization entry (C1).
- **Translator-side mitigation (C1)**: still warranted as defense-in-depth — STT bias alone has not solved it.

### U2 — STT template hallucinations (`구독과 좋아요는 저에게 (아주) 큰 힘이 됩니다`, `시청해주셔서 감사합니다`, `자막 제공 및 ...`) at run-end / silence segments

- **Log evidence**: in 5/20 runs, 11 rows tagged `stt_template_garbage` and 1 tagged `meta_garbage_output`; correctly filtered. One mixed-with-real variant escaped to engine (run 053821), the template tail was strip-removed and only the real content translated — desired behavior.
- **Why this is upstream**: Groq Whisper hallucinates these phrases during silence/non-speech. Translator policy is already catching them.
- **Better next action**: **needs more samples** to find new variants. Current filter is healthy. No translator action required.

### U3 — `글랜스` single-token filtered as `meta_garbage_output`

- **Log evidence**: run 055954 (stellive_hina), source `글랜스`, target empty, `filter_reason=meta_garbage_output`, `result_source=post_policy`.
- **Why this is upstream-classify-misroute**: `_looks_like_meta_garbage_output` (`modules/translator.py:194-204`) keys on Chinese phrases like `無法理解`. A single-token Korean `글랜스` (possibly “Glance” brand or just STT noise) shouldn’t hit this filter directly — but the engine likely returned a meta-disclaimer Chinese phrase, which IS meta garbage. So the filter correctly suppressed an unhelpful engine response.
- **Better next action**: probably acceptable. If single-token Korean tends to provoke meta-disclaimers from Qwen, consider short-circuiting single-token inputs to slang lookup only.

### U4 — Forced incomplete cuts (99 events, all `incomplete=True` ⇒ cache_status=skipped)

- **Log evidence**: 99/722 (13.7%) are `incomplete=True`. 94% of those still get an API translation (94/99 went `result_source=api`); they bypass cache by design.
- **Why this is upstream-segmentation**: `SentenceBuffer.pop_ready` (`modules/sentence_buffer.py`) emits `incomplete=True` on `forced` cuts with no internal punctuation. Engine still translates; output `incomplete` propagates to runtime event.
- **Better next action**: **logging improvement** — capture `forced=True` reason in runtime events. Currently the event has `incomplete` but no field saying *why*. Useful for tuning force_cut_seconds.
- Not a translator fix.

---

## 6. Historical or likely-fixed log issues

### H1 — `마크 → Minecraft` glossary glossary success (Task #13)

- **Log evidence**: 1 source hit (`땡귤이 마크 프로게이머라 서버 열리면 ...`) → target `tinggyul是Minecraft職業選手...` (run 044657). Old audit (`OPTIMIZATION_QUALITY_AUDIT_20260519.md` §2) had 마크→Mark mistranslation.
- **Why fixed**: Task #13 shipped `마크: Minecraft` in [`data/default_slang.json`](data/default_slang.json) and few-shot in `hades_chxxnnx` profile.
- **Which shipped task**: #13 (`59fc0ab`).
- **Current repo evidence**: `Read data/default_slang.json:69`.

### H2 — `섭주 / 섭쥬 / 썹주 / SUBJU → 服主` glossary success (Task #13)

- **Log evidence**: 2 source hits with canonical `섭주` in run 044657, both rendered as `服主`. `섭쥬방→服主房` also entered glossary.
- **Why fixed**: Task #13 added all four variants to `default_slang.json` (lines 70–74).
- **Current repo evidence**: `data/default_slang.json:70-74`.

### H3 — Hangul self-form leak (`챈나` → `채나` in target) when source had canonical `챈나` — addressed by Task #15

- **Log evidence**: pre-Task-15 run `20260520T052405Z-62828` has `채나` in target for sources containing both `챈나` and `채나`. Post-Task-15 run `20260520T141444Z-37304` shows `Chxxnnx` consistently for canonical `챈나` source.
- **Why fixed**: Task #15 added `챈나` itself to `wrong_forms` (`modules/translator.py:164`), so even if engine outputs the Hangul, the sweep maps it to `Chxxnnx`.
- **Current repo evidence**: `Read modules/translator.py:160-166` (canonical present in both source_aliases and wrong_forms).

### H4 — Canonical-coexistence rewrite (Task #16)

- **Log evidence**: No post-Task-16 production run available. The only pre-Task-16 surfacing of this exact bug was in run 052405 (already partly covered in C4).
- **Why likely fixed (but unverified)**: code in `modules/translator.py:260` now includes `rule.canonical` in `alternatives` set, so pattern rewrites both directions to canonical.
- **Which shipped task**: #16 (`a388e37`).
- **Current repo evidence**: `Read modules/translator.py:256-262`.
- **Important caveat**: **runtime efficacy not yet validated by any captured log**. See §7 Hy1.

### H5 — `服주→섭주` HADES source-side normalization (Top-5 #4)

- **Log evidence**: zero occurrences of `服주` in source across the 5/20 logs. The audit doc Codex cross-check § already flagged this as **insufficient runtime evidence** even before this commit.
- **Why likely fixed (but unverified)**: code in `modules/translator.py:111-114` declares `{"服주": "섭주"}` under `_HADES_PROFILE_ID`.
- **Which shipped task**: `11dabc8`.
- **Current repo evidence**: `Read modules/translator.py:109-114`.
- **Important caveat**: **runtime efficacy not yet validated**. See §7 Hy2.

---

## 7. Hypotheses needing reproduction or fresher logs

### Hy1 — Task #16 actually neutralizes Hangul-self-form leak when canonical and wrong-form coexist

- **Hypothesis**: After Task #16 commit (`a388e37`), source `챈나야 ... 채나로 ...` should produce target with **only** `Chxxnnx` and no residual `채나`/`-chan`.
- **Log clue**: pre-Task-16 run 052405 shows mixed `채나` + `Chxxnnx`. No post-Task-16 production run exists.
- **Evidence gap**: zero post-Task-16 runtime data.
- **What runtime sample would confirm it**: one HADES live session capturing a sentence with mixed canonical+mishear, ideally same conditions as 052405.
- **Why not implement yet**: code change isn’t needed — just runtime validation. If Hy1 fails, that becomes a new task on top of #16.

### Hy2 — `服주→섭주` HADES source-side normalization works at runtime

- **Hypothesis**: After commit `11dabc8`, a HADES source containing `服주` (post-translation-engine-leaked Chinese-Hangul mix) is normalized back to `섭주` and rendered `服主`.
- **Log clue**: zero `服주` occurrences in 5/20 logs.
- **Evidence gap**: no observed mishear of this kind; the variant is rare.
- **What runtime sample would confirm it**: a HADES live segment where Whisper outputs `服주` (the precise STT pattern this normalization targets).
- **Why not implement yet**: no fix needed; just needs a triggering sample. Consider lowering normalization priority if `服주` is genuinely too rare to observe.

### Hy3 — `짭봄주` is an STT mishear of `김봉준` (or `잡몸주` / 봉주)

- **Hypothesis**: `짭봄주(님)` rendering verbatim in target is a 김봉준 mishear and could be added to `_NAME_RENDERING_RULES` source_aliases for HADES.
- **Log clue**: 2 hits, both with `님` honorific, context referring to a streamer who sent “mail with video” — plausibly Kim Bongjun (HADES producer).
- **Evidence gap**: user has not confirmed identity; could also be an unrelated streamer.
- **What runtime sample would confirm it**: user listens to the original audio for that segment OR confirms from streamer context.
- **Why not implement yet**: cannot ship a name mapping without user confirmation of identity.

### Hy4 — mwmeu profile’s 17.7% Hangul leak fully reverts once Fixed Proper-Noun Glossary header is added

- **Hypothesis**: Adding `[Fixed proper-noun glossary]\n- 지한 → Jihan\n- 이비 → Eebee...` to mwmeu profile prompt drops the leak rate to <5% (parity with HADES post-Task-15).
- **Log clue**: 43 leaks all are member names already enumerated in mwmeu profile but without `->` mapping.
- **Evidence gap**: user has not chosen canonical romanization for these names.
- **What runtime sample would confirm it**: a new mwmeu live session after the glossary header is added.
- **Why not implement yet**: blocked on user’s romanization decisions (see C3 priority).

### Hy5 — Cache hit rate (~0.4%) is structural to live streams, not a bug

- **Hypothesis**: live-stream sentences are nearly always unique, so the 604 misses + 115 skipped (incomplete) reflect reality, not a cache-key mismatch.
- **Log clue**: `db_hit=2`, `memory_hit=1`, both on “감사합니다”-class short phrases — consistent with repeat-content matching.
- **Evidence gap**: no comparison to a recorded-clip replay scenario.
- **What runtime sample would confirm it**: re-run the same JSONL’s source texts twice in a row in a test harness; expect ~100% hit on the second pass.
- **Why not implement yet**: would not be a fix — would be a measurement script. Defer until a cache-related complaint actually arises.

---

## 8. Top 5 recommended next tasks

Ranked by **runtime visibility × evidence strength × risk**. Strictly evidence-backed.

### T1 — Add `채나` STT-mishear normalization (or alias) under HADES profile

- **Exact scope**: in `modules/translator.py`, either (a) extend `_SOURCE_NORM_BY_PROFILE[_HADES_PROFILE_ID]` with `{"채나": "챈나"}` plus word-boundary guard, or (b) extend the HADES 챈나 `_NameRenderingRule.source_aliases` to include `채나` (plus other clitic-bearing combinations: `채나야`, `채나님`, `채나가`). The two options behave slightly differently — (a) feeds cache+engine the canonical form (better cache reuse + better few-shot adherence); (b) only fixes target rewriting after engine returns. Prefer (a).
- **Non-goals**: do **not** add `채나로`/`채나롱`/`채나룬` server-name variants in this task (those are server-name rewrites, separately discussed in Top-5 #4 of the prior audit and orthogonal to person-name canonical). do **not** modify other profiles. do **not** delete existing STT initial_prompt biasing.
- **Likely files touched**: `modules/translator.py` (≤15 lines), `tests/test_translator.py` (3–6 new cases).
- **Required tests**: `채나야 고마워` (HADES) → contains `Chxxnnx`; same source non-HADES → unchanged; `채나` mid-sentence with non-name context (negative case) → ideally unchanged (guard verifies).
- **Risk level**: **medium**. `채나` could rarely be a different word; word-boundary guard reduces this. Single shipped commit, easy to revert.
- **Why worth doing next**: highest-frequency residual hades issue, confirmed in post-Task-15 run, clearly within translator’s scope, no upstream-coordination needed.

### T2 — Add `_NameRenderingRule` entries for `isegye_lilpa` (릴파→Lilpa, 주르르→Jururu, 고세구→Gosegu, 비챤→VTuber-name-TBD)

- **Exact scope**: introduce `_ISEGYE_LILPA_PROFILE_ID = "isegye_lilpa"`; add 3–4 `_NameRenderingRule` entries with source_aliases (canonical Korean), wrong_forms (observed Chinese transliterations `莉帕`, `莉朗`, `莉拉`, `朱魯魯爾`, `高世久`, `一帕`, `一派`, etc. — gather from logs), canonical (`Lilpa`, `Jururu`, `Gosegu`).
- **Non-goals**: do not change profile prompt JSON (already declares the mapping correctly). do not introduce stellive_hina or mwmeu rules in this task. do not unify `_SOURCE_AWARE_TARGET_REPLACEMENTS` and `_NAME_RENDERING_RULES`.
- **Likely files touched**: `modules/translator.py`, `tests/test_translator.py`.
- **Required tests**: `릴파님 감사합니다` (isegye profile) → contains `Lilpa`, not `莉帕`/`莉朗`/`莉拉`; same source HADES profile → unchanged.
- **Risk level**: **low** (additive, profile-scoped).
- **Why worth doing next**: isegye runs already produce 15.2% Hangul leak; visible to user; tight pattern; no user decision blocked.

### T3 — Capture one post-Task-16 + post-`11dabc8` HADES live run; runtime-validate Hy1, Hy2, T1 (if shipped before validation)

- **Exact scope**: collect a real HADES session into `logs/runtime_events_YYYYMMDD.jsonl`, then diff observed targets containing `Chxxnnx`/`-chan`/`채나`/`服주` vs the prior audit’s expectations. Write findings into a new `OPTIMIZATION_TASK16_RUNTIME_VALIDATION_*.md` (local-only).
- **Non-goals**: no code change. no plan modifications.
- **Likely files touched**: none (run app, then write a local-only review doc).
- **Required tests**: none (this is observation, not unit-test work).
- **Risk level**: **none** (read-only).
- **Why worth doing next**: Tasks #15, #16, and `11dabc8` are unverified at runtime. Without this, future audits will keep flagging Hy1/Hy2 as “code looks right, no log proof”.

### T4 — Add mwmeu profile’s `[Fixed proper-noun glossary]` header (data/translation_profiles.json) + matching `_NameRenderingRule` entries — **PRECEDED by user decision on canonical romanizations for 지한, 이비, 수아, 리츠, 초은**

- **Exact scope**:
  1. User decision step: confirm canonical English/Latin form for each name (e.g. `지한 → Jihan`, `이비 → Eebee`, `수아 → Sua`, `리츠 → Litz`, `초은 → Cho-eun`). The official group spelling may exist; needs research.
  2. Add `[Fixed proper-noun glossary]` block to `standard.mwmeu` and `qwen.mwmeu` in `data/translation_profiles.json`.
  3. Add `_NameRenderingRule` entries for each.
- **Non-goals**: do not address mwmeu STT mishear normalization yet (no evidence; 지한↔지안 may be one STT mishear, but only 2 hits). do not touch HADES.
- **Likely files touched**: `data/translation_profiles.json`, `modules/translator.py`, `tests/test_translator.py`.
- **Required tests**: each name in mwmeu profile → canonical; outside mwmeu → unchanged.
- **Risk level**: **medium** — wrong canonical breaks downstream user expectation; user must sign off before code lands.
- **Why worth doing next**: mwmeu has the highest Hangul leak rate (17.7%); same pattern as isegye fix but blocked on a user-input step. Ranked T4 only because it has external dependencies.

### T5 — Migrate `_SOURCE_AWARE_TARGET_REPLACEMENTS` profile-specific entries (`히나`, `끼윤`, `예난`, `철구`) into `_NAME_RENDERING_RULES` with proper profile scope

- **Exact scope**: move name entries currently in the global tuple into rules tagged with the right `scope` (`stellive_hina` for `히나`; the others need user confirmation). Keep mistranslation-fix-only entries (`하데스→哈迪斯`, `마가 뜨→瑪加特`, `붕 뜨→飄起來`, `개복치→鯛魚燒`) global where they remain context-conditional.
- **Non-goals**: do not change behavior for entries that should remain global. do not introduce new names. do not refactor `_apply_source_aware_corrections` signature.
- **Likely files touched**: `modules/translator.py`, `tests/test_translator.py`.
- **Required tests**: profile gating tests (히나 in stellive → Hina; 히나 in HADES → unchanged).
- **Risk level**: **low** (refactor with test coverage). Could be combined with T2 to avoid two passes on the file.
- **Why worth doing next**: not visible-broken, but code consistency reduces future audit friction. Lowest priority but easy.

---

## 9. Non-goals

These appeared in earlier docs or are tempting but should **not** be next tasks:

1. **Do not** add server-name variants (`채나롱`, `채나로`, `채나룬`, `채나야`-as-server, `젠나룽`, `츤나`, `찬나`) to `_SOURCE_NORM_BY_PROFILE` blanket-wise. The prior audit (§4B item 4) explicitly noted these are STT splits of a server name; canonicalizing them all to `Chxxnnx` will conflate person name and server name. Defer until user decides on the server-name canonical form (which may be Korean, not Latin).
2. **Do not** add `짭봄주`, `세나`, `밀랑`, `릴라`, `짭봄주`, `손바람`, `키아` to any glossary yet — these are viewer/donor names or ambiguous; user has explicitly deferred this class (see prior audit §4B items 1, 2, 6c).
3. **Do not** tighten `_looks_untranslated` Hangul-ratio threshold (`_HANGUL_RATIO_THRESHOLD=0.50`) yet — the 76 Hangul-leaked targets in 5/20 logs include valid 님-suffixed names that are partially translated. A tighter threshold will start rejecting useful subtitles in favor of empty ones.
4. **Do not** invest in cache-tuning or hit-rate work (Hy5 is structural). Cache works as designed for live streams.
5. **Do not** push timeout/retry knobs (Issue C8). Already absorbed by existing fallback; 1.5% retry rate is acceptable; risk of changing knobs > benefit.
6. **Do not** modify `translator.py` to add slang/idiom rules (`나락`, `중박`, `섭종`) without going through the prior audit’s § 5/7 staging — those are prompt-side, not glossary-side.
7. **Do not** push or stage `CLAUDE_PIPELINE_LOG_QUALITY_AUDIT_20260521.md` (this document). Same convention as other `OPTIMIZATION_*.md` review docs — local-only.
8. **Do not** modify any source / test files as part of this audit. Discovery only.

---

> **Status**: Claude Code independent audit, 2026-05-21. Local-only, **not staged, not committed, not pushed**. Next concrete step requires user choice between (a) starting T1 plan, (b) starting T2 plan, or (c) collecting a post-Task-16 / post-`11dabc8` runtime log (T3) before any new code work.
