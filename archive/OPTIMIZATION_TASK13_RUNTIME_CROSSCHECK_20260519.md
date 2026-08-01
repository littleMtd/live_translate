
# Task #13 Runtime 效力交叉驗證 — 2026-05-19

> 性質：post-implementation runtime 驗證的**獨立交叉複核請求**（本地 review 文件，永不 push、永不 stage，與 `OPTIMIZATION_*.md` 同類）。
> 目的：兩方各自從 raw log 獨立得出結論，比對是否找到同一組問題。**請勿錨定本文件的數字**；請自行從 log 重新推導，數字/分類不一致即如實標出。
> 本文件**刻意不含**因果解讀、blocker 分類、下一步建議（那些是 Claude 的解讀，故意盲化讓你獨立形成判斷）。

## 1. Ground truth（原始資料指標，無解讀）

- Log：`logs/runtime_events_20260519.jsonl`
- 新 run（Task #13 實作後）：`run_id = 20260519T153511Z-104776`（≈15:35–15:49，success≈64）
- 舊 run（Task #13 實作前，對照組）：`run_id = 20260519T123011Z-77304`（success≈68）
- Glossary 規格來源：`OPTIMIZATION_QUALITY_AUDIT_20260519.md` §4A；plan：`OPTIMIZATION_ACTION_PLAN.md` §14.3–§14.6
- §4A 目標形（節錄）：챈나→`Chxxnnx`；마크(遊戲語境)→`Minecraft`；봉준/김봉준→`Kim Bongjun`；성태→`KimSungtae`；섭주/섭쥬/썹주/SUBJU→`服主`；섭쥬방→`服主房`
- 關注 Korean terms：`챈나, 챗나, 챗나룡, 챗나룸, 찬나, 츤나, 챗마, 마크, 봉준, 김봉준, 성태, 섭주, 섭쥬, 썹주, SUBJU, 섭쥬방`

## 2. 待驗 claims（falsifiable；逐項給 ✅ supported / ⚠️ partial / ❌ unsupported + 你自己數的數字 + evidence）

| # | Claim（待你獨立查證，勿照抄） |
|--|--|
| A | 마크→Minecraft 在新 run **有效**：新 run 含 `마크` 的 success，target 為 `Minecraft` 且不含 `Mark`；對照舊 run 同類為 `Mark*`。Claude 量到新 run 2/2 正確。 |
| B | STT mishear 變體（챗나/챗나룡/챗나룸/찬나/츤나/챗마）在新 run 出現次數 **顯著下降**：Claude 量到新 run = 0，舊 run 多次。 |
| C | 챈나→Chxxnnx **未生效**：新 run 含 `챈나` 的 success，target **未**出現 `Chxxnnx`、仍為 `-chan` 之類。Claude 量到 0/2。 |
| D | 봉준/성태 few-shot 規格 **未被遵循**：新 run `봉준`→`Bongjun`（非 `Kim Bongjun`）、`성태`→`Sungtae老師/Sungtae哥`（非 `KimSungtae`、且 老師/哥 不一致）。 |
| E | 섭주系在新 run **無資料**（該段主播未說），本 run 無法驗，須延後下一輪 runtime。 |

## 3. 重現方法（建議；你可用自己的方法，但須能獨立復現）

```
讀 logs/runtime_events_20260519.jsonl
篩 event_type=='translation'、status=='success'
分 run_id：新 20260519T153511Z-104776、舊 20260519T123011Z-77304
對 §1 每個 term：掃 source_text 命中者，比對 target_text
   - 마크：target 是否含 'Minecraft' 且不含 'Mark'
   - 챈나：target 是否含 'Chxxnnx'
   - 봉준/성태：target 是否為 §4A 目標形
   - mishear 變體：在 source 出現次數（新 vs 舊）
逐 claim 給 verdict + 你自己數的計數 + 具體 source/target 例
```

## 4. 你的輸出（請寫成可比對表，置於本文件 §5）

每 claim 一列：`claim | 你的 verdict | 你數到的計數 | 你引的 source→target 例 | 與 Claude §2 數字是否一致`
另列：你是否發現 §2 未涵蓋、但同屬 Task #13 效力範圍的其他 term / 模式（new observation，標明）。

## 5. Codex 獨立查證結果

### §5 Codex cross-check

重導方法：讀 `logs/runtime_events_20260519.jsonl`，篩 `event_type == "translation"` 且 `status == "success"`，比較舊 run `20260519T123011Z-77304` 與新 run `20260519T153511Z-104776`。新 run 共 72 translation events，其中 64 success / 7 filtered / 1 failed；profile 全為 `hades_chxxnnx`，success 事件中使用 prompt version `39e9c0bd`。

| Claim | Codex result | Re-derived evidence | Compare with §1–§4 numbers | Notes |
|---|---|---|---|---|
| A. `마크 → Minecraft` glossary 是否有效 | ✅ supported | 新 run：source 含 `마크` 的 success 為 2 rows / 2 occurrences，兩筆 target 均含 `Minecraft` 且不含 `Mark`。樣本：seq 21 `마크팬` → `Minecraft粉絲`；seq 35 `마크 서버` → `Minecraft伺服器`。舊 run：3 rows / 4 occurrences，target 均為 `Mark*`。 | 與文件「新 run 2/2 正確」一致；我補算舊 run 為 3 rows / 4 occurrences，皆 Mark。 | 有效性限於本 run 中的 Minecraft 語境；未觀察到 `마크` 人名語境。 |
| B. STT mishear 變體是否下降 | ✅ supported | 舊 run mishear success：6 rows；occurrences：`챗나` 3、`챗나룡` 2、`챗나룸` 1、`찬나` 2、`츤나` 1、`챗마` 1。新 run：listed mishear variants 0 rows / 0 occurrences。 | 與文件「新 run = 0，舊 run 多次」一致。 | 支持「觀測上下降」；不單獨證明原因一定是 `stt_terms`，因 runtime 內容不可完全控制。 |
| C. `챈나 → Chxxnnx` 是否仍未穩定生效 | ✅ supported | 新 run source 含 canonical `챈나` 的 success 為 2 rows / 2 occurrences，target `Chxxnnx` 為 0/2，且兩筆都輸出 `-chan`。樣本：seq 13 `챈나 깨워라` → `快叫醒-chan`；seq 44 `챈나가 멤버 섭외` → `因為-chan選成員...`。 | 與文件「0/2」一致。 | 這表示 source 已聽對時，profile few-shot 仍不足以保證 Qwen 遵守 `Chxxnnx`。 |
| D. `봉준 / 성태` 是否仍未遵守 §4A 目標形 | ✅ supported | 新 run `봉준/김봉준` success：1 row，target `Bongjun`，未出現 `Kim Bongjun`。新 run `성태` success：2 rows / 3 occurrences，target 為 `Sungtae老師`、`Sungtae哥`，未出現 `KimSungtae`，且稱呼風格不一致。 | 與文件描述一致。 | 同 row 也含 Isegye 名稱，見 new observations。 |
| E. `섭주` 是否缺乏 runtime 資料 | ⚠️ partially supported | 對文件列出的 exact variants（`섭주/섭쥬/썹주/SUBJU/섭쥬방`），新 run success 為 0 rows / 0 occurrences，所以 exact variant 效力無法驗證。額外掃描發現 2 筆 target 含 `服主` 但 source 不含 listed variants：seq 14 `섭정 시간` → `服主時間`；seq 28 `서브주 API` → `服主API`。 | 與文件「本 run 無 `섭주` 系資料」在 strict listed-variant 定義下大致一致；但我額外找到 `서브주`/`섭정` 相關或疑似相關樣本，因此不完全同意「完全無可觀察訊號」。 | Exact variants 仍是 insufficient evidence；`서브주` 可能是未列入的 SUBJU STT 變體，`섭정` 可能是 false positive 或 STT 誤聽，需後續判斷。 |

#### New observations

- **new supported observation**：HADES run 中提到 Isegye 名稱時仍未穩定官方羅馬名。seq 44 source 含 `고세구`, `주르륵`, `일파`，target 為 `高世久`, `朱魯魯`, `一派`，未出現 `Gosegu/Jururu/Lilpa`。這不在 §2 five claims 內，但屬 Task #13 專有名詞效力範圍。
- **new supported observation**：新 run 確實使用 post-Task #13 profile：profile 為 `hades_chxxnnx`，success 主要 prompt version 為 `39e9c0bd`。因此 `챈나/봉준/성태` 失敗不是「沒吃到新 prompt」造成。
- **new risk**：`服主` 可能出現過度泛化或未列 variant 問題。seq 14 `섭정 시간` → `服主時間` 可能是 false positive；seq 28 `서브주 API` → `服主API` 可能是 SUBJU 的新 STT 變體，但不在 §14 的 default_slang key 清單。
- **insufficient evidence**：未觀察到 `Kyma/Sompunch/Yeon Chorok/Singgyul/Shirayuki Hina` 等名稱在新 run 中被說出，無法驗證這些 mapping 的 runtime 效力。
- **insufficient evidence**：未觀察到 default_slang exact-match 導致的 `result_source="slang"` 命中；本 run 的 relevant rows 都走 `api`，所以無法驗證 exact-match path 的實際字幕效果。

#### Evidence gaps

- `섭주/섭쥬/썹주/SUBJU/섭쥬방` exact source variants 在新 run 中沒有出現；只能驗證「缺資料」，不能驗證 variants 是否正確輸出。
- 新舊 run 不是嚴格同一音訊片段，mishear 下降是 runtime observation，不足以單獨證明因果。
- HADES run 中出現 Isegye 名稱，但目前資料不足以判斷應由 HADES profile 覆蓋、全域覆蓋，或另開 cross-profile name handling。

#### Risk classification

- **Blocker**：無。這是 runtime validation finding，不是 commit-readiness review；未發現需要回滾 Task #13 commit 的證據。
- **Non-blocking risk**：profile few-shot 對人名官方羅馬名遵守度不足，已在 `챈나/봉준/성태` 被新 run 支持。
- **Non-blocking risk**：`服主` 可能對 `섭정` 或未列變體過度泛化，需更多樣本確認。
- **Post-runtime validation**：繼續收集含 exact `섭주/섭쥬/썹주/SUBJU/섭쥬방`、HADES 成員名、StelLive/Isegye 名的片段。
- **User decision needed**：是否另開 follow-up task 處理「source-aware target correction / profile rendering hardening」，以及 Isegye 等跨 profile 名稱是否應在 HADES profile 或全域層處理。

### §5 Conclusion

**⚠️ Runtime data partially supports Task #13; follow-up/user decision needed.**

1. 與文件量化結果：A/B/C/D 與 Claude 文件基本一致；E 在 strict listed-variant 定義下一致（0 exact hits），但我補充發現 `서브주`/`섭정` 兩筆 `服主` 相關樣本，所以標為 partially supported。
2. 我獨立發現相同主要問題：`마크` 有效；mishear variants 下降；`챈나`、`봉준`、`성태` 仍未遵守 §4A 目標形。
3. 需要 user decision：是否把「prompt 不足以穩定人名」升級為新 task；是否要處理 `서브주`/`섭정` 與跨 profile Isegye 名稱。
4. 建議另開 task：是，方向應是 source-aware target correction / profile rendering hardening；若後續 runtime 顯示 STT 變體仍高頻，再另開 source-norm / STT normalization。
5. 不建議修改或回滾本次 Task #13 commit；Task #13 的資料層改動對 `마크` 已有正向證據，且沒有 runtime blocker。
