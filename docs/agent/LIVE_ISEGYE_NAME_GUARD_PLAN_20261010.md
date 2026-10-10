# Live: stop dropping whole subtitles that keep 이세돌 names in Hangul (2026-10-10)

Author: Claude Code. Status: round 2 (revised after Codex round 1). Scope:
`data/translation_corrections.json` (three `isegye_lilpa` no-wrong-form `name_rendering_rules`),
one approval-path change in `modules/translator.py`, and tests. No prompt, registry or provider change.

## 1. Evidence

User report (2026-10-10): when the streamer gets excited, subtitles vanish for ~20 s.
Gap audit of October live runs (15 runs, `run_kind=live`): 140 gaps ≥ 15 s that contain ≥ 40 %
speech. Dominant cause per gap: subtitle emitted but late (41 — long VAD chunks plus pauses,
handled by `LIVE_LATENCY_TUNING_PLAN_20261010.md`), short bursts discarded by VAD (51),
**translation rejected by the output guard (40, almost all `unexpected_hangul`)**, STT filtered (7).
Separately, rare ElevenLabs hangs to the 15 s timeout stall the STT worker (3 times in run
`20261010T090328Z-2704`) — a follow-up, not this card.

In run `20261010T090328Z-2704` (비챤 stream, `isegye_lilpa`) 8 sentences published nothing; almost
every candidate was rejected for keeping a member/group name in Hangul (exceptions: one DeepSeek
`unactivated_entity_target`, one Groq empty reply):

| source | rejected candidate |
|---|---|
| 내가 비챤이라는 것도 … | 我會知道비챤這個名字… |
| 진짜 이세돌이라고 하면 … | 說到이세돌，大家一定都會講圍棋。 |
| 그럼 그룹 이름이 뭔데? 이, 이세계아이돌이야. | 那團體名稱是什麼？是이세계아이돌。 |
| 그래가지고 챠니 친구 세구. … | 所以챠니的朋友세구，結果有點好笑。 |

Cause (verified in code):
- The `isegye_lilpa` no-wrong-form rules already approve sourced 비챤/챠니/아이네/징버거/…땅
  (`test_isegye_fan_names_are_approved_only_when_sourced`), via
  `_source_activated_name_canonicals` → `source_alias_matches_at`. A name followed by Hangul
  only counts when the trailing syllables are exactly one of `korean_name_suffixes`; copula
  endings (이라는, 이라고, 이야, …) are missing, so `비챤이라는` does not activate 비챤.
- 세구 (standalone), 이세돌 and 이세계아이돌 have no such rule at all.
- `_choose_guard_rescue` (copied-source-name rescue) also failed: its particle stripping does
  not cover copula endings. Measured: extending it would add 1 sentence that this change already
  covers, so the rescue code is left alone.

User decisions (2026-10-10): 비챤/아이네/징버거 stay in Hangul (like 랑코/솜먕); nicknames and
group names (챠니, 세구, 버거땅, 이세돌, 이세계아이돌) stay as spoken, in Hangul.

## 2. Change (as revised in round 2, §5)

1. Copula endings 이라고, 이라는, 라고, 라는, 이야, 이지, 이고, 이라서, 이니까 count as a name
   boundary **only for approval-only rules** (`_APPROVAL_ONLY_NAME_SUFFIXES`, empty `wrong_forms`).
   The shared `korean_name_suffixes` table stays at 33 (round 1 extended it; Codex showed
   ordinary-word regressions).
2. `name_rendering_rules` += three `isegye_lilpa` no-wrong-form rules, same shape as the
   existing 비챤 row: 세구, 이세돌, 이세계아이돌 (51 → 54). Approval stays sentence-local and
   profile-scoped (`rule.scope in (profile, shared)`), and needs the name in this source.

Rejected alternative (tried first in a scratch worktree): registry `translation` blocks /
nickname entities. Registry mentions are not profile-filtered in `_source_activated_name_canonicals`,
so 이세돌 etc. would be approved on other profiles' streams; it also duplicated the existing rules.

## 3. Validation

- Offline re-adjudication (`_adjudicate_translation_candidate`, `publication=True`, per-event
  profile bound) of every October live candidate: 2702 candidates (emitted `target_text` + raw
  text of every rejected attempt). HEAD reproduces the live outcome for 2679/2702.
  With this change: **15 candidates flip rejected → accepted, 7 previously lost sentences gain an
  acceptable candidate, 0 candidates flip accepted → rejected.**
- Tests: pinned rule count updated (54, in two files); fan-name test extended with
  세구/이세돌/이세계아이돌; new test: copula endings approve the sourced name on `isegye_lilpa`,
  the same text is still rejected on `url`, and `고세구` does not activate standalone `세구`.
  Full suite in the worktree at round 1: 1683 passed, 4 skipped (current results: §5).

## 4. Risks

- Copula endings could let an approval-only alias match a longer word that merely starts with it. Aliases still require a non-Hangul left boundary and the whole trailing
  run must equal one suffix; the re-adjudication found no accepted→rejected or suspicious flips.
- 세구 as a standalone alias: `고세구` is protected by the left-boundary rule (tested).
- Remaining lost sentences (≈ 84 of 91 in the replay) are long-tail viewer nicknames and
  mishearings; the existing rescue already covers 31 of them going forward.

## 5. Review log

### Codex review round 1 (2026-10-10): REVISE (plan and diff)

Blocker: the shared `korean_name_suffixes` table also drives registry mentions (not
profile-filtered, can create required obligations) and target repairs. Verified regressions:
`목화라는 식물로 옷을 만들어` on `isegye_lilpa` activated `url_moka` and was rejected for a missing
canonical; `이 커피는 모카라고 불러` on `url` had 摩卡 repaired to 모카. Non-blocking: the shared
table also feeds boundary source normalization (`릴파라고` → `Lilpa라고`); the plan over-stated
"every candidate" (one DeepSeek attempt was `unactivated_entity_target`, one Groq attempt empty);
validation lacked ordinary-word negatives.

### Claude Code revision (round 2)

- **Shared suffix table unchanged** (still 33). The copula endings live in a new
  `_APPROVAL_ONLY_NAME_SUFFIXES = _KOREAN_NAME_SUFFIXES | {이라고, 이라는, 라고, 라는, 이야, 이지,
  이고, 이라서, 이니까}` in `modules/translator.py`, used only by `_source_activated_name_canonicals`
  and only for rules with empty `wrong_forms` (approval-only: never a repair, never an obligation).
  Registry mentions, repairs, obligations and source normalization keep the shared table.
  `_source_has_name_alias` gained a `korean_name_suffixes` keyword defaulting to the shared table,
  so its other caller (repair gating, ~L1010) is unchanged.
- Both callers of `_source_activated_name_canonicals` (publication `approved_terms` and
  `_quality_telemetry_approved_terms`) only widen Hangul approval of a span that is
  literally in this source.
- Three rules (세구, 이세돌, 이세계아이돌) unchanged from round 1.
- Tests: Codex's two negatives added (`목화라는` accepted without obligation on `isegye_lilpa`;
  `모카라고` leaves 摩卡 untouched on `url`); copula positives, `url` isolation and `고세구` boundary
  tests kept. Full suite in the worktree: 1684 passed, 4 skipped.
- Re-adjudication of the same 2702 October candidates: 15 rejected → accepted, 7 lost sentences
  recovered, 0 accepted → rejected, **0 candidate outputs changed** (no repair side effects).
- §1 wording corrected: of the 8 lost sentences, the failures were the name-boundary or missing-rule
  `unexpected_hangul` case except one DeepSeek `unactivated_entity_target` (Gosegu for 세구) and one
  Groq empty reply.

### Codex re-review round 2 (2026-10-10): YES

Round 1 blocker resolved, no new blocker. Verified in memory: the shared table stays at 33 for
registry activation/obligation (`translator.py:531`) and repair (`:1023`); the new endings apply
only to non-registry, empty-`wrong_forms`, profile-matching rules (`:704–738`); both ordinary-word
negatives, all 9 endings and profile isolation pass. Non-blocking wording fixes applied above.
