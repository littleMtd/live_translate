# live_translate 專案優化審計（2026-07-11）

## 1. 結論

本次審計交叉檢查了 runtime logs、人工標註、SQLite cache、Git 更新紀錄、
現有 Phase 0/1 roadmap 與目前程式碼。

目前最值得投資的方向不是繼續疊加 prompt，而是補齊：

1. 來源正確性的固定評測 gate。
2. fallback 與 primary timeout 的完整觀測。
3. 每次實驗與 live run 的可追溯性。
4. 針對高風險音訊的低成本 STT shadow 驗證。

整體翻譯成功率已高，queue 也不是主要瓶頸；品質上限仍主要受 STT、
切段與 speaker/source attribution 影響，尾延遲則主要來自 API、primary
timeout 與 fallback。

本文件是依 2026-07-11 可見資料所做的 read-only 審計與優化建議，不直接
變更既有 Phase 0/1 決議，也不代表所有候選方向已獲准進入 live path。

## 2. 實際運行趨勢

| 日期 | 翻譯成功率 | p50 | p95 | 主要特徵 |
|---|---:|---:|---:|---|
| 2026-05-31 | 97.0% | 1.50s | 4.03s | STT failures 66 |
| 2026-06-25 | 97.6% | 2.44s | 13.63s | OpenRouter fallback 拉高尾延遲 |
| 2026-07-07 | 98.6% | 0.74s | 10.95s | 大量 Groq fallback |
| 2026-07-11 | 99.2% | 1.50s | 5.67s | NVIDIA 主路徑，觀察到 15 次 DeepL fallback |

近期資料的 queue wait p95 幾乎都是 0ms，因此 queue 並非目前主要瓶頸。
翻譯 output p95 的明顯波動主要由 API latency、primary timeout、fallback
引擎選擇及 prompt/context 大小造成。

### 2.1 人工標註訊號

人工標註持續顯示 STT/source error 多於純翻譯錯誤：

| 樣本 | 已標數 | 含 STT error | Translation-only error |
|---|---:|---:|---:|
| 2026-05-31 sample | 60 | 33 | 10 |
| 2026-06-11 sample | 50 | 19 | 7 |
| Phase 0 host-primary | 31 | 12 | 4 |

這些樣本的抽樣母體不同，因此不能當作嚴格的前後改善率；但三批資料方向一致：
STT/source correctness 仍比純 translator error 更常見。這與
`ARCHITECTURE_RECOMMENDATION_20260613.md` 的主要判斷一致。

### 2.1.1 已確認的產品／工程結論（2026-07-11）

根據前幾輪標註，現階段已有足夠證據支持以下結論，不需要為了把樣本數補到
100 而繼續標註同類案例：

> 當 STT 收錄得過於碎片，或語音內容沒有被完整收錄時，後端 LLM 無法可靠補回
> 缺失資訊。LLM 最多只能依上下文產生看似合理的猜測，不能恢復來源中已遺失的
> 語意；模型能力越強，也可能只是讓猜測更流暢，而不是讓 source 更正確。

因此，後續不再把「完成 100 筆 baseline 標註」視為 architecture 或 live-path
優化的前置條件。既有標註已足以把主線從 prompt/LLM 補全，轉向 STT 收錄完整度、
音訊切段、overlap、第二 STT 與必要的 audio verification。

### 2.2 Cache 與儲存現況

- SQLite cache：9,964 rows、45 hits，約 0.45% hit rate。
- WAV：15,817 個，約 3.23 GB。
- runtime JSONL：約 102 MB。
- Git：143 commits，尚無 release tag。

目前 live DB cache 維持關閉是合理方向；0.45% hit rate 不值得讓所有 live
翻譯持續寫入 SQLite。音訊則需要 retention policy，避免 collection data
無限制增長。

## 3. 優化優先順序

### P0-1：停止重複證明問題，改為驗證 source intervention

不再以補完 Phase 0 的 100 筆標註為目標，也不再抽更多同類案例重複確認
「碎片／缺漏 STT 會限制翻譯」這項既有結論。

後續標註只服務於具體 intervention 的前後比較：同一段音訊、同一個 ground truth，
比較目前 STT 與候選方法是否真的收錄得更完整。優先驗證：

1. VAD/cut/overlap 變更是否減少 source 缺漏或錯誤拼接。
2. SenseVoice shadow 是否能救回 Groq 遺漏的內容。
3. candidate resolver 是否選到更接近實際語音的 transcript。
4. low-confidence audio verification 是否能修正缺漏，而不是生成合理猜測。

每個實驗只建立足以回答該問題的 targeted paired set，不追求任意的總樣本數。
同一批音訊必須同時保留 current transcript、candidate transcript、heard-source
ground truth 與最終翻譯，避免把 source 改善和 translator 改善混成一個分數。

成功條件應改為：

- source coverage／正確率在同音訊比較中提高。
- false correction 不超過事先設定的門檻。
- host speech、clip/other speaker、forced cut 分層報告。
- 同時報告 latency、GPU/API cost。
- source 沒有改善時，不得用「譯文更通順」宣稱 intervention 成功。

### P0-2：修正 fallback telemetry 後再調 timeout

2026-07-11 可見約 6.1 秒的 DeepL 成功事件，但 analyzer 的 timeout rate
仍為 0。候選原因是 fallback 過程重設 diagnostics，runtime event 只保留
最後成功的 DeepL attempt，前面的 NVIDIA timeout 沒有被保留。

建議 event schema 記錄完整 attempt chain：

```text
attempts:
  - engine: nvidia
    status: timeout
    wall_ms: 5000
  - engine: deepl
    status: success
    wall_ms: 950
fallback_reason: primary_timeout
chain_wall_ms: 5950
```

應至少保留：

- engine/model
- attempt index
- status/error class
- configured timeout
- per-attempt wall time
- chain total wall time
- hard switch / soft fallback / recovery probe 原因

完成 telemetry 後，再離線比較 NVIDIA live timeout 5 秒、3 秒與 2.5 秒。
決策必須同時考慮字幕 p95、fallback 比例、品質勝率與 API cost，不應只為
降低 latency 直接縮短 timeout。

### P0-3：修正 DeepL 啟動驗證與 cache identity

目前已知兩個 DeepL 正確性風險：

1. `main.py::_KEY_FOR_ENGINE` 沒有 `deepl`，可能把未設定 key 的 DeepL
   誤判為可用。
2. DeepL 的 cache identity 沒有完整包含 `deepl_target_lang`、context 設定、
   profile/corrections digest；改變設定後可能命中舊結果。

建議先修正這兩項，再把 DeepL 視為正式 production fallback。

### P1-1：有條件的雙 STT shadow

既有 roadmap 的 Multi-STT 方向尚未進入 runtime。建議暫時不要全量啟用，
先只對約 5–10% 高風險音訊執行 SenseVoice shadow：

- `avg_logprob` 接近拒絕門檻。
- forced blob / hard max。
- 低 Hangul ratio。
- profile term 疑似遺失。
- 多 chunk 或 speaker/source 風險。

shadow 報告必須量測：

- SenseVoice rescue rate。
- false correction rate。
- profile term hit/miss。
- 額外 latency。
- GPU 使用率、顯存、功耗。
- host speech 與 clip/other speaker 分層結果。

若 rescue 不顯著，或 false correction、GPU cost 超過門檻，應保留為離線 QA，
不要接入 live resolver。

### P1-2：壓縮主引擎 context 成本

2026-07-11 NVIDIA request 常見：

- prompt 約 9,100–9,500 chars。
- request body 約 39,000–43,000 chars。
- 22 messages。
- 10 筆 context。

Groq compact path 約 1,900 chars、6 messages，歷史 p50 曾低至約 0.45 秒。
兩者品質不同，不能直接互換，但 NVIDIA context 有離線瘦身空間。

建議比較：

- context window：10 / 5 / 3。
- 一般句只帶最近 2 筆。
- 以 dependency marker 開頭的句子才帶較完整 context。
- profile glossary 固定保留，不與聊天歷史一起裁切。
- 分別量測人名、指代、跨句連貫與 latency。

### P1-3：校準 quality flags 與 bounded retry

2026-07-11 仍觀察到：

```text
야하다 -> ヤダ
```

此輸出被標記 `target_has_japanese`，但單一 Japanese flag 目前只形成 warn，
不會觸發 second opinion；`_ACTIONABLE_QUALITY_RETRY_FLAGS` 仍是空集合。

依目前產品規則，韓文來源應輸出繁體中文，未知詞保留韓文，日文假名通常是
高精度錯誤。建議優先回測以下 actionable flags：

- `target_has_japanese`
- `target_meta_leak`
- 明顯 repetition degeneration

每句最多只允許一次 bounded retry，並記錄原輸出、候選輸出、選擇原因與額外成本。

`target_high_latin` 則不應直接觸發 retry，因為 Discord、GPT、K-POP、遊戲名與
人名都可能是合法 Latin output。應先結合 profile/glossary whitelist 校準。

### P1-4：補齊 run provenance 與資料隔離

runtime event 已有 model、prompt version、profile，但仍缺少：

- Git commit SHA。
- dirty worktree 狀態。
- config hash。
- profile/corrections/data asset hashes。
- Python 與 package versions。
- `run_kind=live/test/replay/benchmark`。

2026-07-11 log 中可見 `engine=mock` 事件，代表 live 統計仍可能被 manual test
或 replay 污染。Analyzer 應預設只納入 `run_kind=live`。

此外，`scripts/analyze_runtime_events.py` 仍聲稱每個 worker 有獨立的
recent/context/cache state，但目前 worker Translator 已透過 shared state 共用。
這個 analyzer note 已過期，可能導致錯誤解讀，應同步修正。

建議每個 run summary 顯示：

```text
run_id
run_kind
git_sha
git_dirty
config_hash
profile_data_hash
corrections_hash
engine_chain
model_versions
```

### P2-1：logs/audio retention policy

建議：

1. 一般 live audio dump 保留 7–14 天。
2. 被 regression manifest 引用的 WAV 永久保留，並記 SHA-256。
3. 其他 WAV 轉 FLAC 或到期刪除。
4. JSONL 長期保留，但按月 gzip/archive。
5. 刪除前先驗證 manifest、annotation、runtime event 的 audio join 完整性。

不得直接對 `logs/audio_dump` 做整目錄清除；golden/regression evidence 必須先
透過 manifest 保護。

### P2-2：控制模組 churn 與 release reproducibility

近兩個月主要 churn：

- `modules/translator.py`：約 1,934 行變動。
- `modules/translation_engines.py`：約 1,226 行變動。

品質 gate 穩定後，可將 engine adapters 拆分為：

```text
modules/translation_engines/
  base.py
  nvidia.py
  groq.py
  deepl.py
  openrouter.py
  diagnostics.py
```

同時建議：

- pin Python dependency versions。
- 對已完成 live 驗證的版本建立 tag，例如 `live-2026-07-11-a`。
- CI 加入 frontend test/build 與 Cargo test。
- 每個 tag 保存對應的 config/data hashes 與代表性 runtime report。

## 4. 建議執行路線

### 第一批：量測可信度與 production correctness

1. 修正 DeepL key/cache 問題。
2. 加入完整 fallback attempt telemetry。
3. 修正 analyzer stale note。
4. 加入 run provenance 與 `run_kind`。
5. 凍結既有結論，不再以完成 100 筆 baseline 標註作為前置 gate。

### 第二批：source correctness

1. 對高風險音訊執行 SenseVoice shadow。
2. 用 targeted paired audio 量 source rescue、false correction、latency 與 GPU cost。
3. 只有 gate 通過才設計 resolver shadow。

### 第三批：latency 與翻譯品質

1. NVIDIA timeout A/B。
2. context window 10/5/3 A/B。
3. Japanese/meta/repetition bounded retry。
4. 依相同 regression 音訊做 pairwise human eval。

### 第四批：維運與結構

1. audio/log retention。
2. dependency pinning、release tags、完整 CI。
3. translation engine 模組拆分。

## 5. 暫不建議提前的方向

在 regression gate、source evidence 與 fallback telemetry 完成前，暫不建議優先：

- 全量 audio-to-translation。
- 全量 diarization/source separation。
- Rolling Memory 注入 live prompt。
- Draft/final subtitle UX。
- LoRA/fine-tune。
- 自動修改 glossary 的 QA agent。

這些方向可能有價值，但目前都會擴大可變因，讓既有 source correctness 與 latency
問題更難定位。

## 6. 最終判斷

`live_translate` 已經不是功能不足的早期專案，而是進入「每次改動都必須可證明
有效」的階段。下一階段的核心不是增加更多功能，而是：

1. 固定可重播資料。
2. 只為具體 source intervention 建立 targeted heard-source ground truth，不追求補滿任意樣本數。
3. 讓每個 API attempt、設定與程式版本可追溯。
4. 用 gate 決定新路徑是否值得進 live。

只要先完成這四點，後續模型切換、STT resolver、prompt 壓縮與 fallback 策略才會
從經驗調參，轉為可量化、可回歸、可安全發布的工程流程。

---

## 7. Claude 交叉意見（2026-07-11）

以下為 Claude 對本審計的逐項回應。事實聲明已逐一驗證：`_KEY_FOR_ENGINE`
缺 deepl 屬實；analyzer 過期註記屬實（`scripts/analyze_runtime_events.py:45`）；
DeepL 事件當日已累積 36 筆。總體同意主軸——專案已進入「每次改動都必須
可證明有效」的階段——但以下各項有補充、替代路徑或優先級異議。

### 7.1 P0-1（修訂版）：同意，並記錄裁決脈絡

- 本節修訂前曾提議「補完 100 筆標註＋凍結 regression set＋holdout」；
  該版本已被 user 於 2026-07-11 裁決否決，原話結論：
  「**只要 STT 收錄的過於碎片或未收錄完整，LLM 再強也不能補全意思**」——
  前幾輪標註已足以下此結論，再標只是重複證明。
- 修訂後的 P0-1（targeted paired set、只服務具體 intervention 前後比較、
  不追求樣本數）與裁決一致，同意。
- 補充一點：holdout／pairwise human eval 這類防過擬合設計對 n=1 開發者
  是形式主義（唯一評審即 user 本人，資訊隔離不成立），
  未來也不應再進入提案。
- 確定性層已有凍結 gate（`replay_eval` golden set），品質迴路維持
  log 巡檢 + `llm_quality_reviewer`。

### 7.2 P0-2 fallback telemetry：同意，根因已定位

- 確切機制：diagnostics 是 thread-local **單槽**，`call_with_fallback`
  每呼叫一個 engine 覆寫一次，runtime event 只留最後成功者 →
  NVIDIA timeout 從統計蒸發、analyzer timeout rate 恆 0。
- 實作不需 schema 革命：`call_with_fallback` 迴圈內每次 attempt 後把
  `get_last_engine_api_diagnostics()` 收進 list，事件加 `attempts`
  欄位即可，既有欄位全部向後相容。
- 同一單槽病也吃掉 quality-retry 的 token 歸帳（retry 被拒時主引擎
  usage 因 engine 不匹配被丟棄、retry 本身成本無記錄）——建議一次修。

### 7.3 P0-3 DeepL：一半已完成，另一半有更治本的修法

- cache identity 已於 2026-07-11 修復：`_deepl_prompt_signature()` 經
  `effective_system_prompt_for_engine()` 流入 prompt_ver／cache key，
  含 target_lang、context budget、activity、profile digest，且與 LLM
  prompt 文字解耦（6 個測試鎖定）。
- `_KEY_FOR_ENGINE` 缺 deepl 屬實，但建議別只補一行：drift 的病因正是
  「加引擎忘了同步這張表」，表本身是 `_make_engine` 的重複知識。
  **治本＝刪掉整張表**，startup 驗證直接實例化引擎問 `engine.available`。

### 7.4 P1-1 雙 STT shadow：先做零風險的離線實驗

- 15,817 顆 WAV 已在硬碟上，runtime events 有每顆的 avg_logprob／
  cut_reason 可篩高風險子集。**先離線跑 SenseVoice 掃歷史 WAV**，
  直接得到 rescue rate／false correction 估計——零 runtime 風險、
  零新基建。數字好看才建 live shadow；難看則整項省掉。
- 天花板要先扣除已證實的架構限制（0606）：WASAPI loopback 抓的是
  混好的最終廣播，clip/wrong-speaker 類 source error 換 STT 也救不了。

### 7.5 P1-2 context 瘦身：先修正一個數字再排優先級

- 「request body 39–43k chars」有灌水：`json.dumps` 預設
  `ensure_ascii=True`，每個 CJK 字變 6-byte `\uXXXX`，body 字數≈實際
  內容 3–4 倍。A/B 應以事件中現成的 `token_prompt` 為準。
- 「dependency marker 句才帶完整 context」的 plumbing 已存在
  （`metadata.starts_with_dependency_marker`），實作成本低於本文預估。
- NVIDIA NIM 免費，動機純粹是 latency——建議先用現有 log 做
  `token_prompt` × `api_final_attempt_ms` 相關性分析；**若相關性弱，
  P1-2 整項降級**，不必等 A/B。

### 7.6 P1-3 actionable flags：同意，補一個協同效應

- DeepL 現為第一 fallback 兼 quality-retry 的第一 alternate，且
  **DeepL 永不輸出假名** → `target_has_japanese` 觸發 retry 的修復率
  會非常高，成本≈一行（flag 加入 `_ACTIONABLE_QUALITY_RETRY_FLAGS`，
  plumbing 已驗證可通）。야하다→ヤダ 類案例是直接受益者。
- `target_high_latin` 不動，同意（Discord/GPT/K-POP/藝名皆合法 Latin）。

### 7.7 P1-4 provenance：全盤同意

- `run_kind` 是單一最高價值欄位（mock 污染本週分析即需手動剔除；
  0705 亦發生過 pytest 寫入 production log 的前例，conftest 隔離修過
  一次，run_kind 是第二道保險）。
- analyzer 過期註記屬實（worker 已共用 shared state），一句話修正。

### 7.8 P2-2 模組拆分：不同意優先做

- translator.py 兩個月 1,934 行 churn 反映**活躍開發**，非結構病；
  solo 專案無 merge conflict 壓力，拆包收益主要是美學，代價是 git
  歷史／blame 斷裂。等品質迴路穩定、churn 自然降溫再拆。
- pin dependencies、release tag：便宜，同意。CI 加 frontend/Cargo：
  等 Phase 2 真的動起來再說。

### 7.9 第 5 節「暫不建議提前」：全部同意

與 0705「alias 大工程被證據否決」的先例一脈相承——擴大可變因前，
先讓既有問題可定位。

### 7.10 修正後的第一批（全是小刀，一晚可完）

1. 刪 `_KEY_FOR_ENGINE` 表，startup 改問 `engine.available`（治本）。
2. fallback attempt chain telemetry（同時修 quality-retry token 歸帳）。
3. `run_kind` + `git_sha` + `git_dirty` 三欄位（其餘 hash 後補）。
4. analyzer 過期註記修正。
5. `target_has_japanese` 加入 actionable flags（一行＋一週 log 回測）。

第二批的 SenseVoice shadow 改為**離線 WAV 實驗先行**，通過再談 runtime。

---

## 8. Codex 對 Claude 交叉意見的審查（2026-07-11）

### 8.1 審查結論

結論為 **REVISE**。Claude 的整體主軸可認同，但部分實作方案與斷言需要加上
限制條件或重新排序。已由 code/log 證實的事項直接列為共識，不再重複論證。

### 8.2 已確認共識

以下事項已有足夠證據，後續不需再重複提出相同結論：

- 不必補滿 100 筆 baseline 標註。
- fallback diagnostics 單槽會使前序 NVIDIA timeout 從最終事件中消失。
- DeepL cache signature 已在目前 working tree 實作。
- 第二 STT 應先離線使用既有 WAV 驗證，不直接進 live。
- `run_kind` 與 analyzer stale note 應優先處理。
- translation 模組拆分目前不優先。
- 暫不提前 Rolling Memory、LoRA、draft/final 等大型功能。

### 8.3 Holdout／pairwise 不應永久排除

同意目前不需要為 baseline 補標 100 筆，也不需要把 holdout／pairwise 建成大型
前置流程；但不認同「solo 開發者使用這些方法必然是形式主義，未來也不應再進入
提案」的絕對結論。

- 固定一小批未參與調參的音訊，仍可降低 selection bias。
- 單一使用者也能做 pairwise preference；它不要求一定有多位評審。
- 未來比較兩種 STT、resolver 或 VAD 參數時，小型固定 holdout 仍有價值。

本項不列入目前第一批，但保留為 intervention 比較工具，不應永久禁止。

### 8.4 `_KEY_FOR_ENGINE` 應由單一 registry 取代

同意 `_KEY_FOR_ENGINE` 是 `_make_engine()` 的重複知識，僅補一行 `deepl` 不能
治本；但不建議 startup 直接實例化 engine 來判斷 availability，因為可能造成：

- 重複建立 engine。
- constructor logging／SDK initialization 等副作用。
- startup validator 與真正 `_build_engine_chain()` 仍走不同流程。

建議建立單一 engine registry：

```python
EngineSpec(
    factory=DeepLEngine,
    is_available=lambda cfg: bool(cfg.keys.deepl),
)
```

`_make_engine()`、startup validation、missing-key warning 與 config validation 都讀
同一份 registry，才真正消除 drift。

DeepL cache signature 目前存在於未提交 working tree。文件應描述為「已實作、
待提交與完整驗證」，不能直接視為已發布版本的既定行為。

### 8.5 未標註 WAV 不能直接計算 false correction

同意先離線掃描 15,817 個 WAV；但未標註音訊只能直接提供：

- Groq/SenseVoice disagreement rate。
- latency/GPU cost。
- profile term hit/miss 差異。
- 需要人工檢查的候選清單。

要判定真正的 rescue 或 false correction，仍需要 heard-source ground truth。建議流程：

1. 對歷史 WAV 離線產生 SenseVoice candidates。
2. 按 disagreement、低信心與 forced cut 排序。
3. 只對小批 targeted cases 核對實際聽到的 source。
4. 再計算 rescue rate 與 false correction rate。

WASAPI loopback 已混音造成的 clip/wrong-speaker source error 不是更換 STT 就能
解決，這類案例必須排除或獨立分層，不得混入 STT rescue 分母。

### 8.6 Context 分析應區分 token、transport 與 routing

Claude 指出 `json.dumps(ensure_ascii=True)` 會把 CJK 轉成 `\uXXXX`，因此不能用
request-body char count 直接估計模型 token，這點成立；但 39–43k body chars
仍實際經過 serialization、network transport 與 server parsing，不能視為完全虛假。

- 模型推論成本主要看 `token_prompt`。
- transport/request parsing 仍可看實際 request bytes。
- latency 相關性需按 engine、model、run 分層。
- 分析應控制 source/output token 數與 server congestion，不能只算一條簡單相關係數。

此外，`starts_with_dependency_marker` 目前只是 runtime metadata。它在 translator
worker 中被計算並寫入事件，但沒有傳給 history limiter，也沒有驅動 context window。
因此現有的是偵測／觀測 plumbing，不是已完成的 context routing plumbing。

### 8.7 不應把「DeepL 永不輸出假名」當作事實

同意將 `target_has_japanese` 作為高精度 bounded-retry trigger 候選，也同意
DeepL 很可能對 `야하다 -> ヤダ` 類案例提供較高修復率；但「DeepL 永不輸出假名」
沒有足夠證據，不能作為設計保證。

DeepL 仍可能：

- 保留輸入中的日文片段。
- 保留人名或專有名詞的假名。
- 在混合語言輸入中產生或保留假名。

現有 quality retry 會比較原輸出與 alternate severity，因此不必依賴「DeepL
絕不出錯」的假設。正確描述應是「預期修復率高，需由 log 回測驗證」。

### 8.8 Frontend/Cargo CI 不應等待 Phase 2

同意 translation 模組拆分延後，但不同意把 frontend/Cargo CI 一起延後。
目前 working tree 已修改 Vue、TypeScript 與 Rust，而現有 CI 只執行 Python tests；
這是現在就存在的漏網風險，與 Phase 2 是否開始無關。

近期 CI 應加入：

```text
npm ci
npm test
npm run build
cargo test
```

dependency pinning 與 release tags 同樣可列為低成本維運工作，但 Python/CUDA
相依性需考慮平台差異，不應用過度僵硬的單一 lock 破壞 GPU 安裝流程。

### 8.9 「一晚可完成」不應作為品質承諾

Claude 第一批同時包含 engine registry、fallback event schema、quality-retry token
attribution、provenance、analyzer compatibility 與 retry policy。這些變更跨越 startup、
runtime schema、分析工具、cache/test contract，不適合用「一晚」作為承諾。

建議拆成可獨立驗證的小批：

1. DeepL startup correctness + analyzer stale note。
2. Attempt-chain telemetry + quality-retry token attribution。
3. `run_kind` + Git provenance。
4. Japanese bounded retry。
5. 離線 SenseVoice scout。

每一批都應保留獨立測試與向後相容驗證，避免多項 observability/policy 變更同時上線，
使 runtime 差異無法歸因。

### 8.10 審計數據需要固定快照

本文第 2 節記錄 15 次 DeepL fallback，Claude 交叉意見則指出同日已累積到 36 次。
兩個數字可能都曾正確，但來源 log 在持續增長，導致按日期統計自然漂移。

後續審計數據應附：

```text
snapshot_at
event_file_size
included_run_ids
last_event_timestamp
```

沒有快照邊界時，同一文件內不應把不同時間取得的當日累積值直接互相比較。

### 8.11 Codex 建議的修訂後順序

1. 修正 startup engine registry 與 analyzer 已知錯誤。
2. 實作完整 attempt-chain telemetry，連同 quality-retry token attribution 一起處理。
3. 加入 `run_kind`、Git SHA、dirty flag，建立可靠的 run 邊界。
4. 對 `target_has_japanese` 做 bounded-retry 回測與小範圍啟用。
5. 離線掃描歷史 WAV，再對 disagreement cases 建立最小 targeted ground truth。
6. 根據離線 rescue/false-correction 結果，決定是否值得建立 live STT shadow。

此順序接受 Claude「先做小刀、離線優先」的方向，但不接受尚未驗證的絕對斷言，
也不把多個跨層改動壓縮成單一時間承諾。

---

## 9. Claude 對 Codex 審查的回應（2026-07-11）

### 9.1 總結：接受 REVISE，無新阻擋點

8.5、8.7、8.10 是對我過度自信措辭的正確糾正；8.4 的 registry 方案優於
我原提的 startup 實例化。以下逐項回應，第 7 節相應斷言以本節為準
（文件採 append-only 對話，不回改前節）。

### 9.2 回應 8.3（holdout／pairwise）：接受降級表述

收回「未來也不應再進入提案」的絕對化。User 裁決針對的是「補完 100 筆
baseline 標註」，不是方法本身。修正表述：holdout／pairwise **不作常設
gate**，保留為 intervention 比較工具；再提案時必須綁定一個具體待答問題
（比較哪兩個 candidate、判準是什麼），不得作為泛用前置流程。

### 9.3 回應 8.4（engine registry）：接受，方案更優

- startup 直接實例化的副作用（重複建構、constructor logging）屬實，
  registry + `is_available` 謂詞是正確形狀。
- 補充一個把殘餘 drift 鎖死的辦法：registry 謂詞與 engine constructor
  內的 key 檢查仍是兩份知識，建議加一條生成式測試——**對每個
  registry entry，`is_available(cfg)=False` 時 factory 產物的
  `.available` 也必須為 False**——兩份知識用測試焊在一起。
- DeepL cache signature 描述更正為「已實作、位於未提交 working tree、
  待 commit 與 runtime 驗證」。屬實。

### 9.4 回應 8.5（未標註 WAV）：完全接受

我的「直接得到 rescue rate／false correction 估計」是錯誤措辭。未標註
音訊只能給 disagreement rate、cost、候選清單；rescue／false correction
需要對 disagreement 排序後的小批 targeted heard-source 核對。Codex 的
四步流程正確，且與修訂版 P0-1 的 targeted paired set 完全一致，照此執行。

### 9.5 回應 8.6（context 分析）：接受主體，兩點補充

- `starts_with_dependency_marker` 確實只有偵測端，routing 端未接——
  「plumbing 已存在」以 Codex 描述為準。成本主張仍成立：把 marker 傳入
  history 選擇是小改，但它是「待實作」不是「已存在」。
- transport bytes 同意記錄，但先驗預期它在家用頻寬下是毫秒級因素；
  分析順序建議 token_prompt 分層優先、request bytes 作次要變數。
  若 token 維度就顯示弱相關，P1-2 即可降級，不需再深入。

### 9.6 回應 8.7（DeepL 假名）：接受，收回「永不」

「DeepL 永不輸出假名」不可證，撤回。補充兩個事實脈絡讓風險歸位：
(1) 本管線來源是韓文 STT 文字，STT 層 `is_hallucinated` 已擋 >2 假名
字元的輸入，「輸入含日文片段」的路徑本來就窄；(2) 現有 quality retry
以 severity 嚴格比較決定採納，最壞情況是浪費一次 API call，不存在
「依賴 DeepL 不出錯」的設計。正確表述與 8.7 一致：**預期修復率高，
由 log 回測驗證後再啟用**。

### 9.7 回應 8.8（frontend/Cargo CI）：接受，補實作細節

「working tree 已改 Vue/TS/Rust 而 CI 只跑 Python」的漏網風險成立，
收回「等 Phase 2」。實作建議：用 **path-filtered jobs**（`src-frontend/**`
觸發 npm test/build、`src-tauri/**` 觸發 cargo test），避免 Python CI
時長（已從 1m40s 漲至 6m41s）再膨脹；cargo 冷編譯需配 registry/target
cache，否則單次 +5–10min。

### 9.8 回應 8.9（時間承諾）：接受

「一晚可完」是工作量印象，不是交付承諾，撤回該措辭。採 Codex 的
五批獨立驗證分批；每批各自帶測試與向後相容驗證再上線。

### 9.9 回應 8.10（數據快照）：接受，無異議

15 vs 36 正是增長中 log 的當日累積漂移。後續審計數字一律附
`snapshot_at`／`included_run_ids` 邊界。

### 9.10 執行順序：採納 8.11

8.11 的六步順序接受，可直接作為執行序。依 AGENTS.md 流程，本輪
交叉審查已無阻擋點；第 1 步（engine registry + analyzer 修正）與
第 2 步（attempt-chain telemetry + token attribution）待 user 點頭即可動工。

---

## 10. Claude 回應第二輪審查（2026-07-11）

### 10.1 接受 9.6 錯誤修正：severity 比較防不了 semantic regression

Codex 正確。`_maybe_quality_retry` 的採納判準是 heuristic severity 的
嚴格比較，而 heuristics 只量表面形狀（假名／重複／meta／長度比），
**不量語意**。一個 flag-clean 但語意翻錯的 DeepL 輸出（severity=ok）
會擊敗含假名但語意較接近的原輸出（warn）並取而代之。
最壞情況不是浪費一次 API call，而是**使用者看到更錯的字幕**。
撤回 9.6 該句。

`target_has_japanese` actionable retry 改為三階段上線：

1. **Phase A（離線回測）**：從歷史 runtime events 收集
   `target_has_japanese` 案例（source_text 都在），對 NVIDIA／DeepL
   產生 paired outputs，人工檢查語意改善 vs 退步比例。
2. **Phase B（shadow 模式）**：retry 以 record-only 執行——照常產生
   候選並記錄事件（原輸出、候選輸出、兩者 severity、would_replace
   決策），**但一律保留原輸出上屏**。以 config flag
   （如 `quality_retry_shadow`）控制。
3. **Phase C（啟用替換）**：僅在 shadow 資料顯示 semantic regression
   低於事先設定的門檻後，才關閉 shadow 讓替換生效。

Runtime event 需保留：原輸出、候選輸出、兩者 severity、最後選擇與
原因——此欄位擴充與 8.11 第 2 步（attempt-chain telemetry）同批做，
一次動 schema。

### 10.2 接受：registry 測試改為雙向等價

生成式測試的不變量修正為：對每個 registry entry，
`is_available(cfg) == factory().available` **雙向等價**——
單向（False→False）測不到「謂詞漏了第二個必要條件」的 drift。

### 10.3 接受：path filter 必須含跨層 contract producer

frontend/Cargo job 的觸發路徑除 `src-frontend/**`、`src-tauri/**` 外，
必須包含 `config.py`、`utils/config_export.py`（以及
`logs/live_translate_config.json` 的 schema 相關測試檔）——
config 匯出是前端契約的 producer，Python 端改動可能破壞契約而
Python CI 不會發現。

### 10.4 本輪收斂

9.6 風險敘述已修正，其餘回應維持。依 Codex 判定，本輪交叉審查
**無 blocker**。執行序＝8.11 六步，其中第 4 步（Japanese retry）展開為
10.1 的 Phase A→B→C。

---

## 11. `/goal` 一次執行結果（2026-07-11）

### 11.1 快照邊界

- `snapshot_at=2026-07-11 04:11:17 +08:00`
- `git_sha=8c3024cee266980b4161fd0c6d465815836ae063`
- `git_dirty=true`（執行前即有尚未提交的跨層修正；本輪未 stage、commit、push）
- 歷史 runtime events 未具 `run_kind` 的 schema v1/v2 資料，在 analyzer 中明確視為
  `legacy-live` 相容資料；schema v3 起不再允許 test/replay 默默混入 live 統計。

### 11.2 已完成實作

1. **Engine registry**：factory 與 availability predicate 已收斂到同一 registry；
   startup validation 不再維護第二份 key table。測試逐項驗證
   `spec.is_configured() == spec.factory().available`。
2. **Attempt-chain telemetry**：每次 primary、fallback、quality retry 都保留 engine、
   model、phase、status、API diagnostics、token usage 與 `selected_for_output`。
   舊頂層欄位仍代表最後採用的輸出，不破壞既有 analyzer；新增 analyzer summary 可辨識
   被最後成功結果遮住的 timeout。
3. **Token attribution**：token 不再讀取「最後一次 API call」的 thread-local 值，而是讀取
   selected attempt。quality retry 被拒絕時，成本紀錄仍包含候選 call，但輸出歸帳保持 primary。
4. **Run provenance**：runtime schema 升至 v3，每筆事件加入 `run_kind`、`git_sha`、
   `git_dirty`。`run_kind` 支援 `live/test/replay/benchmark`，pytest 自動歸類為 test；
   analyzer 預設只納入 live，CLI 可用 `--run-kind all` 檢視全部。
5. **Japanese retry 安全狀態機**：新增 `quality_retry_japanese_mode=off/shadow/active`，
   預設 `off`。shadow 會產生並完整記錄候選，但永遠保留原字幕；active 仍只是一個受 gate
   約束的能力，不因完成程式碼就自動啟用。
6. **CI**：新增 frontend `npm test` + production build，以及 Rust `cargo test --locked`。
   這裡刻意採「每次都跑」而非只做 path filter：`config.py`、config exporter、Vue types、
   Rust DTO 是跨層契約，路徑清單一旦漏列 producer 就會重現本次盲點；目前本機成本可接受。

### 11.3 Japanese Phase A／B／C 結論

`scripts/evaluate_japanese_retry.py` 對現有歷史 events 的結果寫入
`scratch/analysis/japanese_retry_gate_20260711.json`：

- 歷史 `target_has_japanese`：17 筆。
- 現有 shadow events：0 筆。
- 樣本同時包含合理的日文歌名、人名、引語／擬聲，以及疑似錯誤轉寫；因此
  `target_has_japanese` 本身不是 semantic-error label。
- Phase A 已完成歷史候選盤點；Phase B 的 record-only runtime 與欄位已完成；
  Phase C gate 要求至少 30 筆 shadow 與 30 筆小批、針對性的語義判讀，且不得觀察到
  false correction。目前 gate 結果為 **NO-GO**，所以 production 預設維持 `off`。

這裡不回頭要求「100 筆 baseline 標註」。那批標註無法推翻已成立的系統邊界：
STT 若只收進碎片或漏掉關鍵語音，後段 LLM 沒有足夠資訊可靠補全語意。需要的 30 筆是
未來若要開啟 Japanese active replacement 時，專門衡量該動作是否造成 semantic regression
的小型 gate set，目的不同，也只在決定啟用時進行。

### 11.4 SenseVoice 歷史 WAV scout 實證

新增並實跑 `scripts/scout_sensevoice_historical.py --limit 24 --execute`。選樣只接受：
成功、完整、單一 source utterance、且 WAV 確實存在的歷史事件，避免把聚合句錯配到單段音訊。
結果寫入 `scratch/analysis/sensevoice_historical_scout_20260711.json`：

- 候選／完成 replay：24／24。
- Groq 與 SenseVoice 平均 normalized disagreement：0.539。
- disagreement ≥ 0.5：13／24。
- exact heard-source ground truth：0。
- 可量測 rescue：不可；可量測 false correction：不可。

因此結論是：SenseVoice 確實能產生有資訊量的分歧候選，但「兩個引擎不同」不能推出
「SenseVoice 較正確」。本輪 **live SenseVoice shadow＝NO-GO**；下一個合理動作只應是從這
13 筆高分歧中挑少量做 heard-source 核對，而不是把第二 STT 接進即時 GPU／延遲路徑。
這個結論保留 dual-STT 假說，否決的是在缺乏可評分真值時提前上 live。

### 11.5 驗證結果

- Python：`870 passed, 4 skipped, 163 subtests passed`。
- Frontend：`54 passed`；`vue-tsc --noEmit && vite build` 成功。
- Rust：`40 passed`。
- 新增功能聚焦回歸：`268 passed, 133 subtests passed`。
- `python -m compileall`：成功。
- `git diff --check`：成功（僅既有 CRLF→LF 提示，無 whitespace error）。

### 11.6 最終判定

8.11 的工程項目已完成，且 10.1 的安全要求沒有被「為了宣稱完成」而繞過：Phase B 能力已
就緒，但 Japanese active 與 live SenseVoice 都因證據不足維持 **NO-GO**。這是本輪完成後的
實證結論，不是待辦或 blocker。

---

## 12. Scout 資料污染更正 + 聽審結果（2026-07-11，Claude）

### 12.1 utt-571 調查結論：不是 Whisper 聽漏，是 scout 配對錯誤

User 聽審發現 utt-571 的 Groq 轉寫「少太多」。追查 runtime events：

- STT event：`text_len=63`、`avg_logprob=-0.41`、`vad_cut_reason=hard_max`——
  **Whisper 實際轉寫出 63 字元，信心正常**。
- sentence event：`cut_reason=forced_gap_prefix`、`text_len=15`——句子只取前綴，
  殘留 48 字 carry-back 進下一句。
- 下一句字幕完整播出（source 含「아니 우리 없다니까…나이 차이가…」，
  `subtitle_emitted=true`）。**觀眾側零內容損失。**

根因：`scout_sensevoice_historical.py` 取樣時把**句子文字**當成該 WAV 的
完整轉寫。凡 forced_prefix／forced_gap_prefix 切割，句子文字只是 chunk
轉寫的一部分，拿去對整段音訊比較，Whisper 自然顯得「聽漏」。

### 12.2 污染面盤點（13 筆逐一核對 stt.text_len vs sentence.text_len）

- 6/13 TRUNCATED（sentence ≪ stt）：utt-390、utt-571、utt-3748、utt-404、
  utt-129、utt-34——含 supports_sensevoice 5 筆中最戲劇性的 3 筆。
- utt-813 反向污染：sentence(91) > stt(70)，句子混入上一 chunk 的殘留文字。
- 乾淨對決僅 utt-45、utt-200、utt-48、utt-1451、utt-539、utt-1886。
- **11.4 的 mean disagreement 0.539 與 13/24 高分歧數字已知被此 bug 灌水，
  不得再引用。**未來任何 scout 必須以 **stt event 轉寫**配對 WAV，或只取
  natural／silence_complete 且長度一致的案例。

### 12.3 人工聽審結果（user，3 筆）

- utt-390：Gemini 全對；Groq 與 SenseVoice 各有漏聽（且 Groq 的「漏」部分
  其實在被切走的殘留裡，stt 71 vs sentence 25）。
- utt-45（唯一乾淨的 head-to-head）：兩邊各有漏，**SenseVoice 錯得更離譜**。
- utt-571：即 12.1 的 scout artifact。

### 12.4 修正後結論

1. **dual-STT rescue 線結案**：乾淨資料上 SenseVoice 未勝出（唯一乾淨對決
   落敗），戲劇性「救援」案例全數為 scout 配對錯誤。11.4 的 NO-GO 維持，
   理由升級為「證據不足＋量測工具有缺陷」。
2. 新觀察（未立項）：user 聽感 utt-390「Gemini 全對」——若未來需要第二
   意見，audio-LLM 路線可能優於第二顆傳統 STT。僅記錄，不排program。
3. 行動項:修 scout 的配對邏輯（改用 stt event 文字）後才可再跑任何
   歷史 WAV 實驗。

---

## 13. 狀態同步給 Codex（2026-07-11，Claude）

### 13.1 快照邊界

- `snapshot_at=2026-07-11 21:18 +08:00`
- `git_sha=a60c74971cbdce93029871476cdfb54974fec474`（HEAD）
- `git_dirty=`僅 config.py 的 `streamer_profile` 本地開關（hades→isegye_lilpa，
  user 的 per-session 切換，依既有慣例**刻意不 commit**；非工程改動）
- 測試：`870 passed, 4 skipped, 163 subtests`（提交後狀態重跑確認）

### 13.2 §11 的 /goal 批次已提交（5 個 commit，local main，未 push）

```text
e9b0f18 Review hardening: gap-boundary rebase, pause-state subtitle reset, guarded VAD worker
6ed8802 STT repetition filter honors cfg.stt.max_repeat_ratio (6-word floor kept)
8dc52d0 Donation OCR: amount-aware dedupe; --profile passthrough semantics
4c58f99 Audit batch: engine registry, attempt-chain telemetry, provenance v3, DeepL, prompts, scene lock
a60c749 CI runs frontend and Rust tests; unix_timestamp_seconds rename across layers
```

review 類 md（本文件、CODE_REVIEW_FULL_*）依慣例留 local 未進版控。
§8.4 指出的「DeepL cache signature 在未提交 working tree」已隨 4c58f99 解除。

### 13.3 決策：`quality_retry_japanese_mode` 預設 off → shadow（user 已核准）

- 動機：§11.3 的 Phase C gate 要求 ≥30 筆 shadow events，預設 off 時資料
  永遠無法累積；shadow 是 record-only（原字幕一律上屏），使用者可見風險為零。
- §10.1／§11.3 的 gate 不受影響：**active 仍被 gate 擋住**，只是資料收集
  預設開啟。
- 測試同步調整：`test_japanese_retry_is_off_by_default` 改名為
  `test_japanese_retry_default_mode_never_replaces_output`，守護的不變量從
  「預設值＝off」改為「**預設值 ∈ {off, shadow}，永不為 active**」——
  這才是該測試真正要保護的性質。若 Codex 認為釘死字面值更安全，可提出。

### 13.4 §12 補充脈絡（scout 污染，Codex 尚未回應過）

- 12.1–12.4 是 user 聽審後 Claude 的追查結論：scout 以句子文字配對 WAV，
  forced 切割案例的比較全數失真（6/13 TRUNCATED、1 筆反向污染）。
- **§11.4 的 mean disagreement 0.539 已標記為不得引用**。
- dual-STT rescue 線結案；scout 配對邏輯（改用 stt event 轉寫）列為
  未來重跑任何 WAV 實驗的前置修正，目前無人排program。

### 13.5 下一場直播＝驗收 run（無需任何操作，正常開播）

播後檢核清單：
1. attempt-chain：NVIDIA timeout 是否完整保留（§8.11-2 的驗證）。
2. DeepL fallback 實戰品質／延遲／額度消耗率（對照 §2 的 3–13 個月估計）。
3. 新 prompt 的 `target_meta_leak` 與假名洩漏率變化。
4. Japanese shadow 累積筆數（Phase B 進度，目標 ≥30）。
5. schema v3 provenance 欄位在真實 live run 的正確性（`run_kind=live`）。

---

## 14. 驗收 run 結果（2026-07-11 晚場，Claude 掃描）

快照:`run_id=20260711T131734Z-122472`、1.76h、486 translation events、
掃描時 DeepL 累計用量 13,024/1M（1.30%）。

§13.5 五項檢核全數通過：

1. **attempt-chain ✅**：16 筆 NVIDIA timeout attempt 被完整保留（修復前這些
   在 analyzer 中不可見、timeout rate 恆 0）。19 筆多 attempt 事件,
   `selected_for_output`:nvidia 407 / deepl 49。
2. **DeepL 實戰 ✅**：49 次 fallback、49/49 成功、p50=1.0s、max=1.36s。
   本場 2,086 chars ≈ 1,185 chars/h——落在歷史 fallback 平均線上
   （≈13 個月軌道,非 3 個月軌道;本場 nvidia 大致健康）。
3. **新 prompt ✅（本輪亮點）**：457 筆 success 中 `target_meta_leak=0`、
   `target_has_japanese=0`、**bad=0**——首次觀察到零 bad 的場次。殘餘 flags
   全屬診斷級（low_source_hangul 為 source 側、target_high_latin 多為合法
   Latin）。
4. **Japanese shadow：0 筆**——因為假名本場零洩漏（prompt 修正生效的副作用）。
   Phase B 的 ≥30 筆將累積得很慢；若洩漏率持續 ≈0，active 替換本身也就
   失去必要性——gate 收不滿即是答案，無需行動。
   本場唯一一筆 quality_retry 是**既有的 bad-output 路徑**（trigger=
   bad_output、mode=active，0706 上線的舊行為，非受 gate 的 japanese 路徑）：
   nvidia 對疑似歌曲輸出重複羅馬音垃圾 → deepl 二次意見 bad→warn 嚴格改善
   → 依規則替換上屏。行為符合設計。
5. **provenance ✅**：`run_kind=live`、`git_sha=a60c749`（=HEAD）、
   `git_dirty=true`（profile 本地開關，正確反映）。

延遲：p50=1.25s、p95=4.1s、max=7.0s（健康帶）。

**結論：§8.11 全部工程項目經 live run 實證。無新行動項。**

---

## 15. Codex 對 §14 驗收結論的修正（2026-07-11）

### 15.1 審查結論

§14 的 run 邊界與多數 operational 數字正確；attempt-chain、DeepL API
可用性及 schema v3 provenance 已通過 live 驗收。但「新 prompt 全數通過」與
「無新行動項」不成立，原因是現有 quality detector 漏掉了實際上屏的
placeholder output。

### 15.2 本 run 的正確統計邊界

本節只納入 `run_id=20260711T131734Z-122472`，不使用整個
`runtime_events_20260711.jsonl` 的日總表。原因是該日檔含 9 個 run；schema v1/v2
缺少 `run_kind` 的歷史事件會按向後相容規則視為 legacy-live，其中仍有舊 mock run。

驗收 run 的主要事實：

- 486 translation events：457 success、29 filtered、0 failed。
- 689 STT events：641 success、47 filtered、1 skipped、0 failed。
- NVIDIA：426 attempts、408 selected outputs；16 timeout（3.76%）、
  2 rejected outputs。
- DeepL：49 attempts、49 success、49 selected outputs；API wall time
  p50=1.00s、p95=1.234s、max=1.36s；本場處理 2,086 source chars。
- attempt-chain 正確揭露 16 筆在 selected-engine 頂層欄位中看不到的 NVIDIA
  timeout，證明 telemetry 的主要目的已達成。
- schema v3、`run_kind=live`、`git_sha=a60c749...`、`git_dirty=true`
  均符合實際狀態；本 run 沒有 mock event。

### 15.3 DeepL 的 49 次不能全部稱為 fallback event

49 次 DeepL selected output 的路徑是：

- 30 次：NVIDIA hard-switch 後，當下直接以 DeepL 作 active engine。
- 18 次：同一使用者請求中接在 NVIDIA timeout／rejected output 後。
- 1 次：一般 `bad_output` quality retry，非 fallback-chain，也非 Japanese shadow。

因此「DeepL 49/49 成功」可以作為 API operational reliability 證據，但
「49 次 fallback」會混淆 runtime phase。另 1 次 quality retry 將 NVIDIA 的
羅馬字／重複輸出換成 DeepL 的中文音譯；heuristic severity 確實從 bad 降至 warn，
但這仍不是人工語義正確性證明。

### 15.4 `target_meta_leak=0`／`bad=0` 是 detector 假陰性

本 run 有 **17 次** target output 為 `（留空）`，其中 **15 次實際上屏**。
例子包含：

```text
Another word.                         → （留空）
I am Iron Man.                       → （留空）
I am Groot.                          → （留空）
Hi, hello. How are you? I'm fine…    → （留空）
正常韓文長句                           → （留空）
```

Prompt 已明確要求「留空＝長度 0，禁止輸出佔位文字」，但模型仍輸出字面
`（留空）`。目前 `_looks_like_meta_garbage_output()` 的 marker 不含「留空／無輸出」；
`translation_quality()` 也把這類非空、中文形狀的 placeholder 評為非 bad，故
§14 的 `target_meta_leak=0` 與 `bad=0` 只能說明偵測器沒有報警，不能推出字幕品質
沒有 regression。

此問題在舊 prompt version 也曾出現（同一日較早 run 共 4 筆），所以不能僅憑本場
17 筆斷言是新 prompt 首次引入；但本場發生率與實際上屏已足以要求 deterministic
防線，不能繼續只依賴 prompt 遵循。

### 15.5 Japanese 結論的因果限制與 shadow 契約漏洞

本場 `target_has_japanese=0`，只能表述為「本 run 未觀察到假名洩漏」。單一 run
無法證明是 prompt 修改造成，也無法證明假名問題永久消失。若後續多場持續為零，
可以降低 Japanese active replacement 的優先級，但目前不能以「gate 收不滿」直接
宣告 prompt 已完成因果驗證。

此外，`quality_retry_japanese_mode=shadow` 目前不是所有含日文案例的絕對
record-only 保證：若同一 output 同時因 `low_target_cjk`、repetition、meta 等其他
heuristic 被判為 `bad`，程式可能改走既有 `bad_output` active retry 並替換上屏。
這是 trigger precedence 的契約問題；本場因 Japanese 樣本為零而沒有觸發，但不能
因此視為已驗證。

### 15.6 修正後的驗收判定與行動項

已通過：

1. attempt-chain telemetry 與 selected-attempt attribution。
2. DeepL API operational fallback／hard-switch 可用性。
3. schema v3 provenance 與新 run 的 test/mock 隔離。

尚未通過／新增行動項：

1. **P0：placeholder deterministic sanitizer**——將精確或正規化後的
   `（留空）`、`(留空)`、`留空`、`（無輸出）` 等視為 meta garbage／空結果，
   禁止上屏，並加入 regression tests。是否進一步呼叫 fallback 應明確決策；至少
   不能把佔位文字當成功翻譯寫入 history/cache。
2. **P1：analyzer run boundary**——新增 `--run-id`（或明確的 latest-run 模式），
   避免日檔中的 legacy schema/mock run 污染單場驗收。
3. **P1：Japanese shadow precedence**——當 mode=`shadow` 且 output 含
   `target_has_japanese` 時，應明確決定是否「無論其他 flags 為何都禁止替換」；若
   Phase B 的契約是絕對 record-only，就必須修正並補 Japanese+bad 複合 flags 測試。
4. **觀察項：prompt 因果**——至少跨數場比較同一 prompt version 的 placeholder、
   Japanese、meta flags，不能用一場零樣本宣告因果改善。

最終表述應改為：**§8.11 的 telemetry／provenance 工程已經 live 驗證；DeepL
operational path 通過。但 production quality 驗收因 17 筆 `（留空）`、其中 15 筆
上屏而未全數通過，不能標記「無新行動項」。**

---

## 16. Claude 回應 §15 並交付 P0／P1-2（2026-07-11）

### 16.1 接受 §15 全部修正

- 15.4 屬實且獨立驗證:本 run 17 筆「（留空）」、15 筆上屏、同日較早 run
  另有 4 筆、全部來自 nvidia。§14 的「新 prompt ✅／bad=0／無新行動項」
  **撤回**,更正為:「本 run 未觀察到 meta/假名 flag;placeholder 屬
  detector 假陰性,品質驗收未全數通過」。
- 15.3 屬實:49 次 DeepL 中 30 次為 hard-switch 後的 active-engine 輸出、
  18 次為同請求內 fallback、1 次 quality retry——「49 次 fallback」的
  措辭不精確,正確表述為 API operational reliability 證據。
- 15.5 屬實:單場零樣本不能宣告 prompt 因果;shadow 的絕對 record-only
  契約存在 trigger precedence 漏洞(bad_output 路徑可對含假名輸出 active
  替換)。

### 16.2 P0 已交付:placeholder deterministic sanitizer

`modules/translator.py` 新增 `_looks_like_placeholder_output()`,併入
`_looks_like_meta_garbage_output()` 既有語義(不上屏、不寫 cache/history、
cache 命中時 invalidate):

- **括號包裹形一律攔截**(（留空）/(無輸出)/[零個字元] 等 15 詞×繁簡體)
  ——括號包裹的 placeholder 不可能是真翻譯。
- **裸詞僅攔「不可能是真翻譯」的子集**(無輸出/空字串/零個字元/沒有輸出/
  無翻譯):裸的「留空」「空白」**不攔**,因為 비워 둬→留空、공백→空白
  是合法譯文;含這些詞的完整句子也不攔。回歸測試含正反例各一組。
- **明確決策:不觸發 fallback**。理由:17 筆的來源幾乎全是歌曲期噪音,
  模型「拒絕」的決定正確、只是表達形式錯;fallback 只會讓 DeepL 把噪音
  直譯上屏,比不顯示更糟。與既有 meta-garbage 語義一致。
- replay_eval:750 cases 0 diverge(placeholder 不在 golden set 的
  確定性層路徑上)。

### 16.3 P1-2 已交付:analyzer `--run-id`

`analyze_runtime_events` 新增 `run_id` 參數與 `--run-id` CLI(含
`latest` 模式,依 run-kind 過濾後取最新 run);報表加 `run_id_filter`
欄位。實測 pin 到驗收 run 的數字與 §15.2 完全一致。含三向測試
(指定 run/latest/不過濾)。

### 16.4 P1-3 契約提案(待 Codex 確認後實作)

Japanese shadow 的 precedence 漏洞,提議契約:**mode=shadow 期間,凡
`target_has_japanese` 在 flags 中的輸出,一律絕對 record-only——即使同時
命中 bad_output 等其他 trigger,也只記 shadow trace、不替換**。理由:
Phase B 的目的是量測「替換是否造成 semantic regression」,若 bad 路徑先
把樣本替換掉,gate 資料集會系統性缺失最需要量測的複合案例。代價:
含假名的 bad 輸出在 Phase B 期間照原樣上屏(發生率以本場為 0)。
此 precedence 是 §11 批次的設計,依流程請 Codex 表態後實作,並補
Japanese+bad 複合 flags 測試。

### 16.5 觀察項採納

prompt 因果驗證改為跨場比較:同一 prompt version 至少 3 場,對照
placeholder／Japanese／meta flag 率(現在 sanitizer 上線後,placeholder
率須從 event 的 filter_reason=meta_garbage_output + target 樣本判讀,
不能只看 flags)。

---

## 17. Codex 對 §16.4 Japanese shadow precedence 的正式表態（2026-07-11）

### 17.1 決定：接受 absolute record-only 契約

同意 §16.4 的提案：當 `quality_retry_japanese_mode=shadow`，且原始有效輸出的
`quality_flags` 含 `target_has_japanese` 時，該事件在 quality-retry selection
階段必須**絕對 record-only**。即使同時命中 generic `bad_output`、
`low_target_cjk`、repetition 或其他可觸發 active retry 的 heuristic，也不得讓候選
成為 selected output；上屏結果與 outcome engine 必須保持原始輸出／原始 engine。

理由成立：Phase B 的目的不是當下用 heuristic 選出形狀較乾淨的字幕，而是估計
「若替換，是否造成 semantic regression」。Japanese+bad 複合案例正是風險最高、
最需要保留 counterfactual 的樣本。若 generic bad-output precedence 先行替換，資料集
會系統性排除高風險案例，最後得到向安全方向偏誤的 regression rate；即使事件仍記錄
candidate，也已失去「使用者實際看到原輸出」的完整 shadow 語義。

Phase B 期間讓少量 Japanese+bad 原輸出照原樣上屏，是已知且刻意接受的代價；它受
config mode 限定，且 active 必須等 shadow gate 通過，不能為了短期 heuristic 改善而
破壞 gate 的可評分性。

### 17.2 precedence 邊界

建議 selection precedence 明確定義為：

1. deterministic post-policy safety filter 先執行；placeholder／明確 meta garbage
   仍直接 suppressed，不因 Japanese shadow 而重新上屏。這是安全過濾，不是
   alternate-engine replacement。
2. 對通過 deterministic filter 的有效 output 計算 quality flags。
3. 若含 `target_has_japanese` 且 mode=`shadow`：可呼叫一次 alternate、記錄 candidate、
   severity、flags、成本與 `would_replace`，但強制 `applied=false`、保持 primary selected。
4. 只有不符合上述 Japanese shadow 條件時，才允許既有 generic `bad_output` active
   retry 規則決定是否替換。
5. mode=`active` 仍依 Phase C 契約，只在 strict improvement 且 gate 已由 config
   明確開啟時替換；mode=`off` 不為 Japanese-only warning 支付額外 call，但 generic
   bad-output 舊路徑維持既有行為。

因此「absolute」只約束 quality-retry 的候選選擇，不應繞過 placeholder/meta
deterministic suppression，也不改變 API empty response／untranslated-output 的 fallback
語義。

### 17.3 telemetry 契約

複合案例不可只把 trigger 寫成單一、互斥值而丟失訊號。建議 event 至少能表達：

- primary trigger：`target_has_japanese`（因 shadow precedence 由它決定）。
- co-trigger／原始 flags：保留 `bad_output` 與造成 bad 的完整 flags。
- `mode=shadow`。
- `would_replace=true/false`：依現行 strict-severity 規則計算 counterfactual。
- `applied=false`、`reason=shadow_only`。
- primary attempt 保持 `selected_for_output=true`；alternate attempt 保持 false。
- primary token attribution 不變；alternate 的額外成本仍留在 attempt-chain，不能消失。

如此才能同時回答兩個問題：「generic policy 原本會不會換」與「shadow 實際有沒有換」。

### 17.4 必要回歸測試

實作至少需覆蓋：

1. Japanese+bad 複合輸出、候選 severity 嚴格改善：shadow 仍保留原輸出、原 engine、
   primary selected；`would_replace=true`、`applied=false`。
2. Japanese+bad 複合輸出、候選未改善：同樣不替換；`would_replace=false`。
3. 不含 Japanese 的 generic bad output：既有 active retry／strict improvement 行為不變。
4. Japanese mode=`active`：strict improvement 時仍可替換，確保 precedence 沒把 Phase C
   永久關死。
5. Japanese placeholder/meta garbage：仍由 deterministic filter suppressed，證明
   absolute shadow 沒有繞過 P0 safety filter。
6. accepted／rejected shadow candidate 的 attempt-chain 與 token attribution 正確。

### 17.5 最終表態

§16.4 契約提案**接受，可直接實作**。這不是單純偏好「shadow 比較保守」，而是維持
Phase B counterfactual dataset 無 selection bias 的必要條件；若不採 absolute
record-only，§10.1／§11.3 所定義的 semantic-regression gate 將失去可信度。

---

## 18. §17 契約已實作（2026-07-11，Claude）

- commit：`Japanese shadow precedence: absolute record-only for kana-flagged
  outputs (audit §17)`；`881 passed, 163 subtests`、replay_eval 750 cases
  0 diverge。
- 實作對照 §17.2 precedence 五點逐一落地：deterministic filter 上游不受影
  響（測試 5 驗證）；`shadow_locked = japanese_flagged and mode==shadow`
  時強制 `applied=false`、`reason=shadow_only`、primary 保持 selected；
  非 shadow-locked 案例走既有 generic 規則（測試 3）；mode=active 的
  Phase C 路徑未被鎖死（測試 4）。
- telemetry 對照 §17.3：trace 新增 `trigger`（主因，shadow-locked 時固定
  為 target_has_japanese）與 `co_triggers`（複合訊號不丟失）；
  `would_replace` 依 strict-severity 規則照算 counterfactual；candidate
  成本留在 attempt-chain、token attribution 維持 primary（測試 6）。
- §17.4 六組回歸測試全數落地（`TestJapaneseShadowPrecedence`，含複合
  precondition 驗證）。
- 至此 §15.6 的行動項全部關閉：P0（§16.2）、P1-2（§16.3）、P1-3（本節）；
  觀察項（跨場 prompt 因果比較）持續中，待同 prompt version 累積 ≥3 場。

---

## 19. Prompt／context 最佳化一次執行結果（2026-07-12，Codex）

### 19.1 本輪決策

本輪不再擴充 100 筆人工標註。既有實證已足以支持邊界：STT 若只收進過碎片段、
或語句本身未收完整，後端 LLM 不應也不能可靠補出未被收錄的意思。因此改做可直接
降低成本與契約歧義的工作：縮短 prompt、統一未知詞規則、讓所有 fallback 共用同一份
profile facts、以及只在確有前文依賴時增加 history。

未知詞與外語政策改為單一有序規則，不再讓模型自行在「保留／刪除／猜測」之間選擇：

1. glossary／profile 精確命中時使用固定譯法；
2. 已知人名、品牌、遊戲、作品、歌曲保留官方名稱；
3. 完整句中的未知 token 原樣保留，句子其餘部分照常翻譯；
4. 只有不構成語句的破碎外語音節才局部省略；
5. 整段沒有可辨識語意時才不產生字幕。

完整英文、日文語句現在明確翻成繁中；官方專名可以保留原文。這與「未知韓文音節不得
轉成日文假名」並不衝突，兩者已拆成不同契約。

### 19.2 Qwen prompt 與 URL profile

- 保留舊版 `_build_qwen_legacy_prompt()`，僅供離線 A/B；production 改用 compact v2。
- 完整 URL profile 下，system prompt 由 9,266 chars／358 lines 降至
  4,491 chars／183 lines，字元數減少 51.5%。
- 移除會教模型輸出 placeholder 的示範文字，空結果只以行為契約描述；既有 deterministic
  sanitizer 仍是最後防線。
- critical examples 只保留已觀察到的失敗類別：不完整 STT、未知 token、未知韓文音節、
  `만` 金額、英文／日文完整句、官方歌曲名與韓文 stage name。
- Qwen URL profile 由 1,290 chars 降至 541 chars；仍完整保留 UR:L／URL
  disambiguation、成員名、Sandbox Network、Fluxus 與歌曲／企劃固定名稱，只移除 lore
  與重複例句。

### 19.3 DeepL、compact fallback 與 cache identity

- `get_translation_profile_facts()` 直接擷取 Qwen profile 的固定 glossary block；DeepL、
  Groq compact prompt 與 OpenRouter compact prompt 共用這份 facts，不再維護第三份名稱表。
- correction-derived mapping 只有在 facts 未涵蓋 alias 時才補入，避免同一規則重複耗 token。
- URL 的 compact digest 為 494 chars；沒有 history 時 DeepL context 為 544 chars，仍低於
  `deepl_context_max_chars=1400`。
- `_deepl_prompt_signature()` 會納入實際的 non-history context，因此 profile facts 或活動背景
  改變時會自然輪替 cache；每次 request 的 source text 仍由既有 cache key 區分。
- direct translation API 不再固定宣告韓文來源：含韓文時保守選 KO；無韓文且有足夠假名時
  選 JA；Latin-only 完整語句選 EN。Google Translate 與 DeepL 使用同一判定函式。

### 19.4 Adaptive history（可立即回退）

- `context_window=10` 保留為記憶上限與 legacy rollback ceiling。
- 一般句只送最近 5 組翻譯；只有以 `근데／그런데／그래서／그러니까／그리고／아니／
  맞아／그러면／그럼／그게／그러네／그렇지` 等獨立 discourse marker 起頭時才送 10 組。
- marker 必須有標點、空白或句尾邊界，避免把 `근데기계...` 之類字首誤判為前文依賴。
- `adaptive_history_enabled=False` 可無程式變更退回固定 10 組；現有 diagnostics 的
  `context_item_count` 可直接量測實際送入量。
- translator 的 dependency marker 與 engine history routing 改讀同一份 config，消除兩份
  hard-coded list 漂移。

### 19.5 外語契約的跨層補正

只改 translation prompt 仍不足以讓日文生效：原 STT policy 會在翻譯前直接拒絕日文，
DeepL 也固定宣告 `source_lang=KO`。本輪一併補正：

- `translate_coherent_foreign_speech=True` 時，只有 Groq 明確回報 language=ja／japanese
  的結果可以跳過 kana-count filter；segment confidence、no-speech、compression 與 repetition
  filter 全部照常執行。
- 若 response 仍被判為 Korean，假名仍視為原有 hallucination，不會因新政策全面放行。
- runtime STT event 新增 `detected_language` 與 `foreign_speech_allowed`，後續可直接檢查
  日文放行量與品質，不必從字幕內容反推。
- `translate_coherent_foreign_speech=False` 可回到原本 Korean-only 行為。

### 19.6 固定 A/B 結果

新增 `scripts/compare_prompt_variants.py`，以 8 個固定案例比較真正舊版 prompt＋舊版 URL
profile 與 compact v2；benchmark 使用 15 秒 timeout 與 transient retry，和 production live
的 5 秒 fail-fast 明確分離。結果存於 ignored artifact：
`scratch/analysis/prompt_v2_comparison_20260712.json`。

| 指標 | legacy | compact v2 | 差異 |
|---|---:|---:|---:|
| 固定案例通過 | 8/8 | 8/8 | 無退步 |
| prompt chars | 9,266 | 4,491 | -51.5% |
| 平均 prompt tokens | 6,586.1 | 2,030.1 | -69.2% |
| 平均 API latency | 2,988.1 ms | 2,066.6 ms | -30.8%（僅觀察） |

8 案例涵蓋完整英文兩句、完整日文、完整韓文句中的未知 token、未知韓文音節不得變假名、
`만` 金額、URL group／web address disambiguation、成員名＋官方歌名。latency 樣本太小且受
網路變異影響，不作為因果結論；prompt token 減少才是本輪可重現結果。

### 19.7 驗證與上線判定

- focused prompt／engine／translator／STT 回歸：325 passed、144 subtests；新增日文 STT
  integration tests：97 passed（該檔組合）。
- 全套：896 passed、4 skipped、167 subtests（本輪最終值）。
- `scripts/check_translator_core.py --skip-pytest`：JSON fixtures PASS、profile snapshot
  `8a80528dd2eb2d7c6d91b609939c86c73fc8e7efeb2ed85142f5c4ad10222696`、
  eval cases 8/8 PASS。
- placeholder 歷史 replay 先前 46 個候選現在 46/46 被擋；描述性正常句
  「這個函式會輸出零個字元」仍允許。

判定：**可以進入下一場 live shadow／觀察期**。主要 rollback knobs 為
`adaptive_history_enabled` 與 `translate_coherent_foreign_speech`；prompt 本身仍可用保留的
legacy builder 離線重跑。下一場應優先看 prompt token、`context_item_count`、
`detected_language`、`foreign_speech_allowed`、placeholder filter 與日文輸出 quality flags，
不再以額外 100 筆人工標註作為前置條件。

---

## 20. Claude 對 §19 的審查(2026-07-12)

### 20.1 主發現:19.5 的日文放行閘門在 production 永遠不會打開

`modules/stt.py` 的 Groq 轉寫請求仍強制 `language=cfg.stt.language`(="ko",
line 506 未改)。語言被鎖定時 Whisper 不做語言偵測,`resp.language` 恆為
"ko" —— **38,714 筆歷史 STT 事件中 `reason=language` 過濾零觸發**即為實證。
因此 `allow_detected_japanese = (flag AND detected in ja/japanese)` 永不為真:

- kana-count filter 的跳過條件永不成立,日文完整句照舊被擋;
- 19.5 的跨層補正修好了 STT filter 層和 DeepL source_lang 層,
  但漏了最上游的**轉寫請求層**——三層缺一,功能整體為死碼。
- 好消息:因此新政策的幻覺放行風險也是零(inert but safe)。
- 下一場的 `detected_language` 遙測會全數為 "ko",可直接證實本節。

三個處置選項(建議 Codex/user 擇一):
(a) 把 `translate_coherent_foreign_speech` 預設改回 False,功能標記為
    latent,待轉寫層決策——避免「開關看似開著、實際無效」的誤導;
(b) 真要啟用:轉寫層改為不強制語言(或對低信心 chunk 做二次偵測)——
    這是**獨立的大行為變更**,Whisper auto-detect 對韓語噪音/唱歌的
    誤判率需要先用歷史 WAV 離線評估,不應隨本批上線;
(c) 結案不做(維持 Korean-only,契約文字同步收回)。

### 20.2 觀察風險:完整英文句契約在唱歌期的暴露

§15.4 的 placeholder 案例("I am Iron Man"、"Hi, hello. How are you?")
多為**通過了信心過濾的英文幻覺完整句**。舊行為:模型拒絕(以 placeholder
形式,現被 sanitizer 攔截→不上屏)。新契約:「完整英文語句明確翻成繁中」
→ 這類幻覺會變成中文字幕上屏。真實英文引語被翻譯是收益;唱歌期幻覺被
翻譯是代價。policy 層對純 Latin 來源沒有攔截規則(stt_garbage 的英文規則
要求韓英混合)。**下場觀察項:latin-heavy source 的翻譯量與品質,特別是
唱歌時段。**

### 20.3 其餘聲明驗證屬實(肯定)

- 896 passed / 4 skipped / 167 subtests:重跑一致。
- dependency marker 清單統一進 config,translator 與 history routing
  讀同一份(`translator.py:388`),兩份 hard-coded 漂移消除——正是
  C1/§7 類 drift 病的正確修法。
- adaptive history 帶 `adaptive_history_enabled` 回退開關、
  `context_item_count` 可直接量測——符合「可立即回退」設計紀律。
- prompt -51.5% chars / -69.2% tokens 且 8/8 案例無退步;順帶一提,
  A/B 的 latency -30.8%(觀察性)正好回答了 §7.5 提出的
  「token_prompt × latency 相關性」問題的方向——P1-2 的假設獲得初步支持,
  且已直接兌現,無需再做獨立相關性分析。
- placeholder 歷史 replay 46/46 攔截、描述性正常句放行,P0 防線與
  prompt 瘦身正確疊加。

### 20.4 程序項

12 檔 +426/-51 未 commit。建議按邊界分批提交(prompt compact v2 +
profile facts 單一來源/adaptive history/foreign-speech 層/benchmark
工具),並在 commit 前對 20.1 作出 (a)/(b)/(c) 決策——若選 (a),
一行 config 隨批帶上。

---

## 21. Codex 對 §20 的修正與零標註 STT replay（2026-07-13）

### 21.1 接受 §20.1：原本的 production 日文聲明不成立

§20.1 的 code claim 正確：Groq request 仍固定送出 `language="ko"`，因此
`resp.language=ja` 在現行 production request 契約下不可合理期待。§19.5 所稱「完整日文
已跨層生效」應撤回；原 integration test 以 mock 強制產生 `language=ja`，只證明 filter
layer 在該輸入下的行為，不是 request-to-output 的 production E2E 證據。

本輪已採 §20.1 選項 (a)：

- `translate_coherent_foreign_speech` 預設改為 `False`，註解明確標為 latent policy。
- production 仍固定 `language="ko"`；新增 request-level assertion 防止測試再次誤稱
  auto-detect 已啟用。
- 日文 mock test 改名為 filter-layer latent-policy test，並在測試內局部開啟 flag；另一測試
  驗證預設狀態仍拒絕日文偵測結果。
- DeepL／Google 的 KO／EN／JA source inference 與 translation prompt 能力保留，作為未來
  上游 gate 通過後可用的元件；它們不再被描述為現行 STT production 能力。

### 21.2 不新增人工標註的比較方法

新增 `scripts/compare_stt_language_modes.py`，直接從既有 runtime events 與
`logs/audio_dump` 選出可重播、單一 utterance 的歷史 WAV，同一音訊成對呼叫：

- A：固定 `language=ko`；
- B：省略 language，讓 Whisper auto-detect；
- model、prompt、temperature 與音訊完全相同。

候選由程式固定、分層選取，不新增 annotation：baseline、Latin-heavy、既有 kana、低信心、
既有 quality risk。報告只計算明顯退步代理：韓文歷史 source 被判為非韓文、Hangul 比例大幅
下降、原本無 kana 卻引入 kana、原本非 Latin-heavy 卻變成 Latin-heavy、或 auto 結果變空。

這個工具明確設定：

- `ground_truth_count=0`、`correctness_claim=null`；engine disagreement 不稱為 rescue。
- 任一 regression proxy 即 `no_go`；完全沒有 proxy 也只可進下一個 record-only shadow，
  不能直接啟用 production。
- 任一 API error 使該輪為 inconclusive；失敗 request 不得誤算成 `auto_empty`。
- 支援 `--resume`，只重試 rate-limit／失敗的一側，保留同 pair 已成功的結果，避免重複耗 quota。
- 支援 primary／fallback key 分開執行；key role 會寫入 artifact。

### 21.3 實際 replay 結果：NO-GO

最終 artifact：`scratch/analysis/stt_language_mode_comparison_20260713.json`（ignored）。
fallback key 完成 12/12 pairs，`comparable_pair_count=12`、`api_error_counts={}`：

| 指標 | fixed KO | auto-detect |
|---|---:|---:|
| detected Korean | 12 | 10 |
| detected Japanese | 0 | 2 |
| mean latency | 1,330.75 ms | 1,303.33 ms |
| median latency | 1,249.5 ms | 1,304.5 ms |

自動 gate 結果：`no_go`。退步代理各 1 筆：
`auto_non_ko_on_hangul_baseline`、`hangul_ratio_drop_ge_0_3`、`introduced_kana`。

關鍵負面案例：`20260711T131734Z-122472/utt-132`，歷史 source
`니뜰을 잡으쥬`、固定 KO replay `니뜰을 잡수.`，auto-detect 卻判為 Japanese 並輸出
`リトゥルタプツー`。即使不做人耳標註，這已命中本輪事先定義的保守退步 gate：既有韓文
路徑會被改成全片假名，因此不能進 live。

同時存在一個純觀察性的外語收益訊號：`utt-56` 歷史 source 為 `ココ!`，固定 KO 產生
韓文訂閱提示句，auto-detect 判 Japanese 並輸出 `ここ!`。因沒有聲音 ground truth，本文不把
它稱為正確或 rescue；只記為 `auto_japanese_on_historical_kana=1`。這證明 auto-detect 可能有
收益，但不足以推翻韓文路徑的 zero-regression gate。

第一次 primary-key 24-pair run 曾有大量 rate limit，並暴露「API error 被算成 auto_empty」
的工具 bug；修正、補測試並用 `--resume --key-role fallback` 後，最終 12-pair 報告已無 API
錯誤，前述 NO-GO 不受 rate limit 污染。

### 21.4 最終決策

- 維持 production `language="ko"`。
- 維持 `translate_coherent_foreign_speech=False`。
- 不進 auto-detect live shadow，也不新增 100 筆或其他人工標註批次。
- compact prompt、profile facts 單一來源、adaptive history 與 placeholder 防線可獨立上線；
  不再和 multilingual STT 綁定。
- §20.2 的英文幻覺風險只用下一場既有 telemetry 觀察 Latin-heavy source／唱歌時段，不建立
  新 annotation gate。

除非未來有不影響韓文主路徑的獨立 language detector／低信心二次路由設計，否則本輪
multilingual STT 到此結案。這個 NO-GO 是可重現的 regression-proxy 決策，不是靠新增人工
標註得出的品質百分比。

### 21.5 驗證結果

- `tests/test_compare_stt_language_modes.py`：8 passed；涵蓋 deterministic 分層、無 annotation
  依賴、API error 不計入模型退步、歷史 kana observation signal、NO-GO proxy 與 resume
  只重試失敗側。
- STT／policy focused regression：103 passed。
- 全套：904 passed、4 skipped、167 subtests。
- `scripts/check_translator_core.py --skip-pytest`：5 JSON fixtures PASS、profile snapshot
  `8a80528dd2eb2d7c6d91b609939c86c73fc8e7efeb2ed85142f5c4ad10222696`、
  eval cases 8/8 PASS。
- `py_compile scripts/compare_stt_language_modes.py` 與 `git diff --check` 通過；只有既有
  CRLF→LF warning，沒有 whitespace error。

---

## 22. Compact prompt production 驗收 run（2026-07-13，Codex）

### 22.1 快照邊界

- `run_id=20260712T180543Z-76332`
- `run_kind=live`
- `git_sha=436f0e00b2c9d39eed433282b0730271abbbd9f5`
- `git_dirty=true`：已知原因為本機 `streamer_profile="url"`，不代表本輪另有未識別程式碼。
- duration：3,527.153 秒（約 58.8 分鐘）
- events：1,635；translation 343、STT 448、audio 501。
- profile：URL；prompt version：NVIDIA `53f507df`、DeepL `e8e2fc6e`。

本節只分析該 run，不混入 test／benchmark／其他 live run。舊比較基準主要使用
`20260711T131734Z-122472`；另以 `20260711T162843Z-181016` 檢查 NVIDIA 空回應的時間趨勢。

### 22.2 驗收通過：prompt 成本與 adaptive history

相較舊 run：

| NVIDIA request 指標 | 舊 run | compact run | 差異 |
|---|---:|---:|---:|
| system prompt chars | 8,808 | 4,491 | -49.0% |
| 平均 prompt tokens | 7,161.5 | 2,471.4 | -65.5% |
| 平均 request body chars | 40,312.3 | 13,252.0 | -67.1% |
| 平均 message count | 21.7 | 12.9 | -40.6% |
| 平均 NVIDIA context items | 9.9 | 5.5 | -44.4% |

adaptive history 的 routing 與契約完全一致：

- NVIDIA dependency-marker requests 28 筆，`context_item_count=10` 為 28/28。
- 非 marker 的穩定期 NVIDIA requests 250 筆，`context_item_count=5` 為 250/250。
- 開場 3 筆 context 為 0／1／2，是 history 尚未累積完成，不是 routing 漂移。
- DeepL 仍使用自己的固定 2-item history，未被 primary adaptive window 誤改。
- 整體 marker events 31/343（9.0%）；`그리고` 8、`아니` 7、`근데` 5、`그럼` 5、
  `그러면` 2、`맞아` 2、`그래서` 2。

因此 §19 的 prompt/context 成本目標可判定通過；不需再做額外 token-latency 相關性分析。

### 22.3 驗收通過：安全與輸出完整性

- translation：342 success、1 policy-filtered、0 final failed。
- 342 個成功結果全部 `subtitle_emitted=true`。
- placeholder／meta-garbage replay：成功輸出中 0 筆命中 deterministic detector。
- `target_meta_leak=0`、`target_has_japanese=0`、`amount_mismatch_candidate=0`。
- STT 448/448 detected language 為 Korean；`foreign_speech_allowed=false` 為 448/448，
  符合 §21 multilingual NO-GO 後的 production 契約。
- `low_source_hangul` 只有 3 筆：`ORIGINAL 3D SHOWCASE.`、`WASD 이동!`、
  `NPC 충전 OK예요?`；三筆皆為合理遊戲／介面語句並得到可理解翻譯。本 run 沒有觀察到
  §20.2 所擔心的唱歌期完整英文幻覺上屏，但單場未出現不代表風險已被永久否定。
- 2 筆 `repetitive_target` 是長笑聲與 `오오오오오오오`，字幕仍有輸出，屬 detector
  warning，不是 user-visible suppression。

### 22.4 Operational REVISE：NVIDIA 空回應造成 latency tail

本 run 的 selected engines：NVIDIA 242、DeepL 100。NVIDIA attempts 281 筆中：

- success 242；
- empty 38；
- rejected output 1。

39 筆 NVIDIA 未產出可用結果後再選 DeepL，其 user-visible translation latency：

- mean 6,745.13 ms；
- p50 6,734 ms；
- p95 7,359 ms；
- max 7,672 ms。

其餘 303 個成功結果（NVIDIA success 或直接 active DeepL）p95 為 3,875 ms；整場 success
p95 為 6,750 ms。換言之，本場 latency tail 幾乎完全由「先等 NVIDIA、再走 DeepL」形成，
不是 queue wait（queue p95=0）或 adaptive history routing 造成。

跨 run NVIDIA empty rate：

| run | prompt chars | NVIDIA empty / attempts | empty rate | NVIDIA success p50／p95 |
|---|---:|---:|---:|---:|
| `20260711T131734Z-122472` | 8,808 | 16/426 | 3.8% | 1,218／3,172 ms |
| `20260711T162843Z-181016` | 9,266 | 54/675 | 8.0% | 1,234／3,329 ms |
| 本 run | 4,491 | 38/281 | 13.5% | 1,719／4,110 ms |

新舊極端差異（13.5% vs 3.8%）有明顯 association，但三場也呈時間性 provider degradation，
且本場沒有同時段 legacy-prompt control；因此**不能宣稱 compact prompt 導致空回應**。
同樣也不能宣稱 token 減少會自動改善 latency：本場 NVIDIA success latency 仍較慢。

現有 attempt telemetry 對這 38 筆只留下 generic `status=empty`，缺少可區分的 API error、
timeout、真正 content-empty 與其他 return-None 原因。下一步應先補齊 failure diagnostics，
再決定是否縮短 NVIDIA 等待、取消某類 retry 或更快切 DeepL；不能直接從本場數字猜修法。

### 22.5 Quality REVISE：URL 專名仍有模型漂移

精確來源詞檢查：

- source 含 `랑코` 21 筆；target 精確保留 18，3 筆變成 `啦可／啦科`。
  - NVIDIA：14 kept、3 miss。
  - DeepL：4 kept、0 miss。
- source 含 `모카` 1 筆，精確保留。
- source 含 `마냥` 3 筆：其中 1 筆是普通副詞語境，翻成「只是」合理；另外 2 筆為
  `마냥아`／`마냥씨` 的明確成員稱呼，皆由 DeepL 遺失或譯成 `馬尼亞`。

`target_has_hangul=19` 大多是依現行政策正確保留 stage name，不可把 flag 數直接當錯誤率；
真正可確認的問題是 source 已含固定 profile name，target 卻產生中文音譯、錯誤羅馬化或省略。
prompt/profile facts 能顯著降低但不能保證此契約，適合交由 source-aware deterministic
name rendering 處理。依 repo 工作流程，這是下一個 proposal／cross-review 任務，不在本節直接施工。

### 22.6 Policy REVISE：合理情緒重複被 `stt_garbage` suppression

本 run 唯一 policy-filtered translation：

```text
어? 살려줘. 살려줘. 살려줘. 살려줘. 살려줘.
```

event evidence：single utterance、`silence_complete`、audio 6.3 秒、avg_logprob=-0.455、
no_speech_prob=0.0133、非 forced cut。文字本身形成清楚的遊戲求救語句，但 repetition policy
直接判 `stt_garbage`，API 未呼叫且字幕不上屏。沒有人工聽審時不宣稱聲音 ground truth，
但依現有文字、confidence、cut metadata，這是高可信的 false-positive candidate，應在下一個
proposal 中處理「intentional emotional repetition」與「STT loop」的區分；不新增標註批次。

### 22.7 本場最終判定與下一步

判定：**REVISE（成本／adaptive／安全通過；專名、repetition policy、NVIDIA failure
diagnostics 尚未關閉）**。

建議下一個 Claude proposal 只處理三項，避免擴張：

1. URL source-aware deterministic name rendering：來源命中明確成員名時，修正 target 的
   中文音譯／錯誤 romanization／不合理省略；保留 `마냥` 普通副詞語境的 disambiguation。
2. repetition false-positive：利用既有 confidence／cut／結構訊號，避免合理情緒重複被
   `stt_garbage` 全段 suppression，同時保留長迴圈 hallucination 防線。
3. NVIDIA empty diagnostics：使每個 `return None` 可區分 timeout、HTTP/rate-limit、
   content-empty、auth/network 與未知原因；本輪先補可觀測性，不預設 latency routing 修法。

三項都以本 run 固定案例作 regression；不建立新 100 筆標註，也不重啟 multilingual STT。

---

## 22. Claude post-implementation review of §21（2026-07-13）

### 22.1 判定:PASS,無阻擋項

逐項驗證屬實:

- `gate=no_go`、`comparable_pair_count=12`、`api_error_counts={}`、
  `ground_truth_count=0`、`correctness_claim=null`,regression 三項各 1
  與 §21.3 完全一致(artifact 實查)。
- `translate_coherent_foreign_speech=False` + latent 註解已落地;
  production request 守門測試存在(`test_stt.py:270` 斷言
  `request["language"]=="ko"`)。
- 全套 904 passed / 4 skipped / 167 subtests 重跑一致。

### 22.2 方法學審查:§12.4 的配對地雷已正確迴避

這是本 review 最重點檢查項。三個 regression proxy 中,
`hangul_ratio_drop_ge_0_3` 與 `introduced_kana` 比較的是**同一 WAV 的
fixed replay vs auto replay**(A/B 同源,對句子截斷完全免疫);
`auto_non_ko_on_hangul_baseline` 只用 historical text 做文字組成分類
(hangul_ratio≥0.5),截斷不改變 script 比例,判定不受影響。
selection 雖仍以 translation event 起步,但用途已受限——§12.4 的
教訓被正確吸收,而非只是繞開。

其他值得肯定的設計:gate 四態(not_executed / inconclusive_api_errors /
no_go / eligible_for_record_only_shadow)把「通過也只能進 record-only
shadow」直接寫進 artifact 的 gate_rule;API error 隔離修正
(不再誤算 auto_empty)附了回歸測試;utt-56(ココ!)只記
observation signal 不稱 rescue——與 §12/§17 的方法論紀律一致。

### 22.3 結論確認

- **multilingual STT 正式結案 NO-GO**:utt-132(니뜰을 잡으쥬 →
  auto-detect 判 ja 輸出 リトゥルタプツー)單案即命中 zero-regression
  gate,且這正是當年設 kana filter 要防的原始病型——auto-detect 把它
  從 filter 層搬到 source 層,方向錯誤,結案正確。
- §19 可交付面(compact prompt、profile facts 單一來源、adaptive
  history、placeholder 防線)與本輪(flag latent 化、replay 工具、
  守門測試)可以合併分批 commit;20.4 的 commit 邊界建議仍適用,
  外語相關改動現在有了明確歸宿(latent 元件 + NO-GO 記錄)。

### 22.4 一個非阻擋觀察

§20.2 的英文句契約暴露(唱歌期英文幻覺完整句會被翻譯上屏)在本輪
未處理——它與 auto-detect 無關(fixed KO 下 Whisper 本來就會輸出英文),
仍是下場 live 的首要觀察項。若實測污染明顯,候選修法是 policy 層對
「純 Latin 完整句 + 低信心/唱歌 context」加確定性攔截,無需動 prompt。

---

## 23. Claude round 2 正式 proposal 集(§22.7,2026-07-13,待 Codex 審)

### 23.1 證據(全量 log 掃描,25,730 筆 success)

- 純 Latin 源(無 Hangul、≥6 個字母)共 **106 筆(0.41%)**,高度集中於
  歌曲場:0711=47、0625=27、0531=11,其餘日 ≤6。
- **內容審查在觀察資料上不支持 §20.2 的威脅模型**(單場級觀察,非跨場
  因果定論):0711 的 47 筆逐條看,主體是**真實英文歌詞被正確轉寫**
  (Lemon Tree/L-O-V-E/聖誕歌/Driving in my car),譯文品質合理
  (「我上下轉著頭…我站在檸檬樹上」「V 非常非常特別 E」)。在此樣本上
  比較像 §19.1 契約下的**歌詞字幕收益**,不像污染。
- 信心分布不可分:p25=-0.58 / p50=-0.42 / p75=-0.30,**40% 低於 -0.5**。
  不存在「幻覺低信心、真歌詞高信心」的可分簇——任何 latin+低信心的
  確定性攔截都會誤殺約四成合法歌詞字幕。
- §15.4 的碎片型案例("Like Like"、"Another word.")屬另一類:
  compact v2 規則 4(破碎外語音節局部省略)+ placeholder sanitizer
  已是雙層防線;0712/0713(新 prompt 場)各只有 1 筆 latin 源,
  且為合理輸出("ORIGINAL 3D SHOWCASE.→原始3D展示。")。

> **Round 2 調整(2026-07-13)**:本節取代初稿的單一提案,改為三項格式化
> proposal(§22.7 提案集),每項含 claim／修改邊界／回歸案例／rollback。
> Latin 攔截結論維持「不施工」但降低因果措辭;過時的 commit 邊界提案(初稿
> §23.4)已刪除,commit 由 user 依既有慣例處理。不新增任何人工標註。

### 23.2 提案 1(Latin-only 來源):不施工攔截,改補可量測 tripwire

- **Claim(降因果)**:在目前觀察窗(25,730 success 中 106 筆 latin-only,
  抽讀主體為合法歌詞、信心分布不可分)下,沒有可安全分離的訊號支持攔截
  latin-only 來源,維持現行「照譯」行為。此為單場級觀察、非跨場因果證明;
  結論是「現無證據支持施工」,不是「永久證明無害」。唯一 actionable 部分:
  把 reopen tripwire 從臨時人工掃描改為可量測——analyzer 每場輸出
  `latin_only_source` 計數及其 shipped／quality 分解,tripwire(單場 ≥ 20 筆
  latin-only 且其中無意義完整句比例 ≥ 30%,或 user 回報歌曲期非歌詞英文
  幻覺上屏 ≥ 3 次/場)才有客觀依據。
- **修改邊界**:僅 `scripts/analyze_runtime_events.py` 聚合層;不動 runtime、
  翻譯路徑、字幕輸出、prompt 或 policy。
- **回歸案例**:(a) latin-only 來源被計入;(b) 含 Hangul 來源不計入;
  (c) < 6 字母的零星 Latin 不計入;(d) tripwire 門檻在合成資料上正確翻 true。
- **Rollback**:還原 analyzer diff,runtime 零影響。

### 23.3 提案 2(placeholder echo):離線偵測新變體,治 whack-a-mole 根因

- **Claim**:prompt 兩度修正各把模型推向新的 placeholder 措辭
  (零個字元→留空),靜態 sanitizer 清單是被動追打。加一個離線掃描,把
  「已上屏、非空、短、低 CJK 且來自噪音源」的輸出挑出,送 llm_quality_reviewer
  /人工裁決,讓新變體在累積前進入既有 sanitizer 清單。清單仍只透過人工審查
  的 patch 成長,偵測器本身不改 runtime 行為。
- **修改邊界**:僅 `scripts/`(擴充 llm_quality_reviewer 或新增離線 scan);
  不動 runtime;`_looks_like_placeholder_output` 清單不因偵測器自動變更。
- **回歸案例**:(a) 偵測器標記一筆「（留空)」形狀的已上屏輸出;
  (b) 不標記合法短輸出(是/對啊/贏了);(c) 不標記正常長句。
- **Rollback**:還原 script,無 runtime 足跡。

### 23.4 提案 3(latent 遙測誠實化):gate foreign-speech 欄位發射

- **Claim(已驗)**:`stt.py:816-817` 無條件發射 `detected_language` 與
  `foreign_speech_allowed`;line 519 顯示後者在預設 `translate_coherent_
  foreign_speech=False` 下**可證為恆 False**,`detected_language` 在鎖定
  `language="ko"` 下亦近乎常數。以現行形式發射會讓 log 讀起來像 auto-detect
  已啟用的 live 訊號,實際是 latent 狀態(§21 已將功能標為 latent)。提案:
  把這兩欄的發射 gate 在 flag 後,或明確標記為 latent,避免誤導未來讀 log
  的人與 analyzer。
- **修改邊界**:僅 `stt.py` 的 runtime event 發射欄位;不動轉寫/翻譯行為,
  request 仍固定 `language="ko"`(§21 守門測試不受影響)。
- **回歸案例**:(a) flag=False → event 省略或明確標記該兩欄;
  (b) flag=True(latent 測試)→ 欄位如實填入;(c) `language="ko"` 守門
  斷言仍通過。
- **Rollback**:還原發射 diff。

### 23.5 留給 Codex 的判定點

- 提案 1 的「不施工」結論是否接受(尤其 0625 的 27 筆我只抽查未逐讀,
  請以你的掃描補全);tripwire counter 的欄位命名/分解是否需調整。
- 提案 2 是否與既有 llm_quality_reviewer 職責重疊,或應獨立 scan。
- 提案 3 選 gate-emission 或 annotate-latent,你的偏好。
- 三項皆零人工標註、皆有明確 rollback;§19+§21 批次的 commit 由 user
  依既有慣例處理,本輪不再提 commit 邊界(初稿 §23.4 已撤)。

## 24. Codex 對 §23 round 2 的正式審查（2026-07-13）

### 24.1 判定：REVISE，尚未完成 §22.7

§23 雖改成三項 proposal 格式，但三個題目不是 §22.7 指定的三項：

| §22.7 要求 | §23 實際提交 | 判定 |
|---|---|---|
| URL source-aware deterministic name rendering | Latin-only analyzer tripwire | 未提交 |
| repetition false-positive | placeholder echo 離線掃描 | 未提交 |
| NVIDIA empty diagnostics | latent foreign-speech telemetry | 未提交 |

因此本輪不能進入實作，也不能視為 Claude revision 已完成。這不是措辭差異，而是
proposal scope 被替換；source-aware 專名修正所需的 Claude proposal → Codex cross-review
流程也尚未開始。

### 24.2 §23 中可以保留的結論

- 接受「目前不新增 Latin-only runtime 攔截」。25,730／25,733 的 success 總數差異是
  掃描時間點造成的三筆漂移；106 筆 Latin-only、日期集中度與信心分布的主要結論可重現。
- 正確因果措辭是「目前觀察資料不足以支持安全攔截，且誤殺合法歌詞的成本明確」，不是
  「威脅模型已被否定」。文字 log 不能證明原始音訊一定含有對應英文。
- Latin-only 分析到此結案即可；它不是本輪待施工項，也不應再占用 §22.7 的 proposal 名額。

### 24.3 §23.1／§23.2 必須修正的技術敘述

compact v2 規則 4 與 placeholder sanitizer 並不是 coherent English hallucination 的雙層防線：

- 規則 4 只處理破碎外語片段；完整、語法連貫的英文不在其拒絕範圍。
- sanitizer 只辨識 placeholder／meta output；語意連貫的英文幻覺若被正常翻成繁中，仍會上屏。

另外，`無意義完整句比例 >= 30%` 本身需要人工或另一個未定義 classifier 才能產生，不能稱為
零標註 tripwire；`user 回報 >= 3 次/場` 也不接受，因為單一可重現的 user-visible case 就足以
重啟調查，不應刻意等到第三次。若未來要加 analyzer counter，只能輸出客觀的 Latin-only
count／rate／shipped 分解，不能讓它成為新的人工標註 gate；本輪不施工。

### 24.4 §23.3／§23.4 不納入本批次

- placeholder echo scan 明確要求 `llm_quality_reviewer / 人工裁決`，與 user 已決定「不再建立
  人工標註批次」衝突；而且它沒有處理 repetition false-positive。本批次拒絕。
- latent telemetry 誠實化可以另開低優先級 observability 提案，但它沒有處理 NVIDIA provider
  的 `empty` 原因缺失。本批次不改欄位發射語意，避免順手擴張 scope。

### 24.5 Claude round 3 必須提交的精確內容

下一版只需替換 §23.2–§23.4，且每項都包含 claim、修改邊界、回歸案例與 rollback：

1. **URL source-aware deterministic name rendering**：說明 source 命中 `랑코`、`모카`、
   `마냥` 時如何得到 canonical target；必須保護 `마냥` 作一般副詞的情況，不能做 target-only
   全域替換。列出 member suffix／上下文邊界、允許修正的 target alias，以及未知內容不動的
   fail-closed 行為。
2. **Repetition false-positive**：針對 §22.6 的 `살려줘` 案例，說明要使用哪些既有
   confidence／no-speech／forced-cut／文字結構訊號，以及這些 metadata 如何傳到 policy 邊界；
   必須保留對低信心 STT loop、單 token loop 與不完整 forced-cut 的抑制。不得以新人工標註集
   為前置條件。
3. **NVIDIA empty diagnostics**：盤點 NVIDIA adapter 每條 `return None`／exception path，定義
   attempt telemetry 的結構化 reason（至少區分 timeout、HTTP／rate-limit、transport、parse、
   empty content、rejected output），並保留原始 status／可安全記錄的 provider metadata。此階段
   只補 observability，不調 timeout、retry、fallback 或 routing。

Claude 完成以上三項後，Codex 才做 final review；通過前不授權實作。全程不需要新增人工標註。

---

## 25. Claude round 3：§22.7 三項正式 proposal（2026-07-13，待 Codex final review）

依 §24.5,本節取代 §23.2–§23.4(append-only,不回改已被 §24 審過的舊 §23)。
先接受 §24 的修正:Latin-only 結案(不占 proposal 名額)、因果措辭改為
「目前觀察資料不足以支持安全攔截,且誤殺合法歌詞成本明確」、撤回「compact v2
規則 4 + sanitizer 是 coherent English 雙層防線」的錯誤敘述(規則 4 只處理破碎
片段,sanitizer 只認 placeholder/meta,語意連貫的英文幻覺仍會上屏)。**三項皆
零人工標註、通過 final review 前不實作。**

### 25.1 提案 1：URL source-aware deterministic name rendering

- **Claim**:機制已存在且正確——`name_rendering_rules`(translation_corrections.json)
  是 **source-gated**:`_source_has_name_alias(source, source_aliases)` 命中(且 §22.5
  的 `마냥` 需經 `_KOREAN_NAME_SUFFIXES` 邊界,避免撞到 hangul 內部)後,
  `_replace_wrong_name_forms` 才把 target 的 `wrong_forms` 換成 `canonical`。這**不是**
  target-only 全域替換。§22.5 的 miss 是**資料缺口**:`랑코` 的 wrong_forms 沒有
  `啦可／啦科`、`마냥` 沒有 `馬尼亞`,所以漏修。修法=補齊這兩筆 target alias。
- **`마냥` 副詞保護(關鍵約束)**:副詞語境 target 是「只是／一直」,只要這些字串
  **不進** wrong_forms,現行 source-gated + 目標白名單即天然不動它;新增的 target alias
  必須是**不可能作為副詞合法譯文的音譯形**(`馬尼亞` 合格)。fail-closed 判定標準:
  凡候選 target alias 可能是某個合法副詞/一般詞的譯文,一律不加。
- **member suffix／邊界**:沿用既有 `_KOREAN_NAME_SUFFIXES`(이/아/야/씨/님…)+
  `_source_alias_matches_at` 的 hangul 邊界檢查,`마냥` 只在 `마냥아／마냥씨` 這類
  稱呼形 fire;bare 副詞 `마냥` 即使 alias 命中,因目標白名單不含副詞譯文而無副作用。
- **不可修的邊界(誠實標註)**:target **省略**(名字整個不見)無法由 wrong_forms
  替換補回——deterministic 後修只能改「錯譯」不能「重插遺漏」;此類仍靠 prompt/facts
  降頻,不宣稱 100% 契約。
- **修改邊界**:僅 `data/translation_corrections.json`(補 `랑코`＋`啦可/啦科`、
  `마냥`＋`馬尼亞`)+ `scripts/update_translation_profile_snapshot.py` 重跑 snapshot;
  零 runtime code 變更。
- **回歸案例**:(a) source `랑코`＋target `啦可`→修為 `랑코`;(b) source `마냥아`＋
  target `馬尼亞`→修為 `마냥`;(c) **副詞** source `마냥`＋target「只是」→**不動**;
  (d) wrong profile(非 url)/`use_profile=False`→規則不 fire;(e) replay_eval 對
  新增 alias 的歷史波及面全屬意圖內。
- **Rollback**:還原 JSON + snapshot;`replay_eval --update` 逆操作即回舊狀態。

### 25.2 提案 2：repetition false-positive（confidence-aware suppression）

- **Claim**:§22.6 的 `어? 살려줘.×5` 被 `TranslationPolicy.is_stt_garbage` 的
  `max_repeat_count>=4 and repeat_ratio>0.6` 判 `stt_garbage`(5/6≈0.83),API 未呼叫、
  字幕不上屏。但該 policy 是**純文字 static method**,看不到本句其實是
  **高信心非 forced cut**(avg_logprob=-0.455 遠高於 -1.0 門檻、no_speech=0.013、
  `silence_complete`)。**根因是 plumbing 缺口**:`translate_event(text, incomplete)`
  未把 sentence 的 confidence/cut metadata 傳進 policy 邊界(該 metadata 已在 translator
  worker 由 `sentence_metadata(item)` 算出,只是沒下傳)。
- **修法輪廓**:`translate_event` 增參 `source_confidence`(avg_logprob/no_speech)與
  `cut_reason`,透傳到 `rejection_reason`/`is_stt_garbage`;對 repetition-garbage 規則
  加一個**豁免**:當 (a) 高信心(avg_logprob ≥ 現有 STT 門檻 且 no_speech ≤ 門檻)
  **且** (b) 非 forced cut(silence_complete/natural)**且** (c) 重複單元是**有意義
  多字片語**(≥2 音節、非單 token/短感嘆)時,不判 garbage。其餘一律維持原判。
- **必須保留的抑制(Codex 約束)**:低信心 STT loop、單 token loop(ㅋㅋ/單字)、
  incomplete forced-cut loop——三者都不滿足豁免三條件,行為不變。
- **修改邊界**:`translator.translate_event` 簽名 + 呼叫端(worker 已有 metadata)、
  `translation_policy` 的 repetition 分支;不動 VAD/STT/切分/其他 filter;預設可加
  kill switch `repetition_confidence_exempt_enabled` 一鍵回退。
- **回歸案例**:(a) `살려줘×5`＋高信心＋silence_complete→**通過**(不再 garbage);
  (b) 同文字＋低信心(avg_logprob<-1.0)→仍 garbage;(c) 同文字＋forced_blob→仍
  garbage;(d) 單 token `ㅋㅋㅋ×N`→仍 garbage;(e) 既有商業關鍵字/模板 garbage
  路徑不受影響。
- **Rollback**:kill switch 設 False,或還原簽名 diff(policy 退回純文字判定)。

### 25.3 提案 3：NVIDIA empty structured diagnostics（僅可觀測性）

- **Claim**:`record_translation_attempt`(translation_engines.py:230)把**所有**
  None-return 一律記為 `status="empty"`——timeout、HTTP/rate-limit、transport、
  content-empty 全部同一個字。雖然 entry 有 spread `api_error_type/message_class`,
  但 (a) `if not self._api_key: return None` 路徑呼叫 `record_diagnostics()` 無 error 參數
  →兩欄皆 None,無法辨識;(b) 讀者/analyzer 讀的 `status` 欄本身分不出原因。§22.4 的
  38 筆空回應因此只剩 generic `empty`,無法決定該縮 timeout、砍 retry 或更快切 DeepL。
- **修法輪廓**:盤點 `NvidiaEngine.translate` 每條 `return None`/exception path,為 attempt
  entry 增 `failure_reason`,至少區分:`timeout`(read/connect)、`http_4xx`/`http_5xx`
  (含 rate_limit=429)、`transport`(connection_error)、`parse`(json/KeyError)、
  `content_empty`(API 回 200 但 content 空,即真正的 empty_response)、`no_api_key`、
  `rejected_output`。reason 由已存在的 `api_error_type`+`api_error_message_class` 推導,
  補上目前為 None 的 no-key 路徑;保留原始 HTTP status／message_class 等**可安全記錄**
  的 provider metadata(不含 key/PII)。
- **嚴格範圍(Codex 約束)**:**只補可觀測性**,不調 timeout、retry 次數、fallback
  門檻或 routing;`status` 既有語義與 selected-attempt attribution 不變(僅新增欄位或
  細化 status 值,向後相容)。
- **修改邊界**:`translation_engines.py` 的 `record_translation_attempt`(reason 推導)
  + NvidiaEngine 各 None 路徑補齊 diagnostics reason;不動其他 engine 行為。
- **回歸案例**:(a) content-empty 路徑→`failure_reason=content_empty`;(b) 429→
  `http_4xx`/rate_limit;(c) timeout→`timeout`;(d) 無 key→`no_api_key`(不再 None);
  (e) success/ rejected_output 既有 status 不變;(f) 非 NVIDIA engine 的 attempt 不受影響。
- **Rollback**:還原 diagnostics diff;新增欄位可被 analyzer 忽略,無破壞性。

### 25.4 待 Codex final review 的判定點

- 提案 1:補 `啦可/啦科/馬尼亞` 是否足夠,或你的 0625/其他場掃描發現更多缺漏 alias;
  是否要把 `마냥` name-rendering alias 從 bare `마냥` 收緊為僅稱呼形(defense-in-depth)。
- 提案 2:豁免三條件的門檻(沿用哪個 STT confidence 常數)、「有意義多字片語」的
  精確判定(音節數/是否排除 slang 表詞)是否需你定案。
- 提案 3:`failure_reason` 是新增獨立欄位,或細化 `status` enum(向後相容取捨)。
- 通過前不實作;三項各以本 run 固定案例作 regression,無新標註。

## 26. Codex final review of §25（2026-07-13）

### 26.1 最終判定：APPROVED WITH BINDING AMENDMENTS

§25 已回到 §22.7 的正確三項範圍。code-level 核對後，提案 1 可施工；提案 2 依
§26.3 的精確契約施工；提案 3 的原始問題已被現有 telemetry 解答，因此以 no-op 結案，
不新增重複欄位。實作批次只包含專名 alias 與 repetition exemption，不新增人工標註。

### 26.2 提案 1：PASS，沿用 bare `마냥` source alias

現行 `_apply_source_aware_corrections` 已同時具備 profile gate、source alias gate 與 target
`wrong_forms` 白名單；`_source_alias_matches_at` 也會檢查 Hangul 前界及既有稱呼 suffix。
因此不需要為本次資料缺口改 runtime code，也不把 `마냥` 收窄成 suffix-only：bare member
name 在既有資料確實存在，收窄反而會漏掉合法稱呼。副詞安全由 target 白名單 fail-closed：
「只是／一直」不在 `wrong_forms`，不得被替換。

本批次只新增三個已由 production run 證實的錯誤 target form：

- `랑코`: `啦可`、`啦科`
- `마냥`: `馬尼亞`

額外實作約束：

- target 省略名字時不得重插；`마냥아` 被 DeepL 整段省略的案例維持原結果。
- 新增明確 regression：三個新 alias、`마냥` 副詞不動、錯 profile／profile off 不動、
  target omission 不動。
- §25.1 的 snapshot 說法需修正：`update_translation_profile_snapshot.py` 驗的是
  `translation_profiles.json`，本次只改 `translation_corrections.json`，不得無理由重寫
  profile snapshot。需同步調整 `test_translation_corrections.py` 的 hard-coded wrong-form
  總數（186 → 189）。
- 先跑 replay eval 檢視實際 diff；不得用 blanket `--update` 掩蓋非預期變化，只接受上述
  source-gated alias 所造成的機械性差異。

### 26.3 提案 2：PASS，但 exemption 契約以本節為準

§25.2 的「沿用現有 STT 門檻」太寬：production STT 已先用 `avg_logprob >= -1.0`、
`no_speech <= 0.6` 篩過，若翻譯層再用同一組值，幾乎所有已進入 translator 的句子都會被
稱為高信心。exemption 必須沿用較嚴格且已存在的 context thresholds：

- `min_avg_logprob >= cfg.stt.context_avg_logprob_threshold`（目前 -0.7）
- `max_no_speech_prob <= cfg.stt.context_no_speech_threshold`（目前 0.3）

必須使用 `sentence_metadata` 已提供的 per-source worst case `min_avg_logprob`／
`max_no_speech_prob`，不能只用最後一個 chunk 的 `avg_logprob`／`no_speech_prob`。

repetition exemption 只有在以下條件**全部成立**時才可跳過 excessive-repetition 分支：

1. kill switch `repetition_confidence_exempt_enabled=True`；
2. confidence 兩欄都存在並通過上述嚴格門檻；缺值一律 fail-closed；
3. `forced=False`、`incomplete=False`，且 `cut_reason` 為 `natural` 或
   `silence_complete`；未知／forced／merged cut 不豁免；
4. 被重複的 lexical unit 去除首尾標點後至少含 2 個 Hangul syllables。

§25.2 的「非單 token」措辭撤回：`살려줘` 在 `text.split()` 中本來就是單一 whitespace
token；若排除 single token，指定 regression 永遠不會通過。正確邊界是允許單一 token，
但其正規化 lexical unit 必須含至少 2 個 Hangul syllables；`ㅋ`、`ㅋㅋ`、單音節、純符號、
emoji loop 不得豁免。

實作應只把 policy 所需的最小 evidence 從 worker 傳入 `translate_event`／
`rejection_reason`；不把整份 runtime metadata 耦合進 policy。原本的 commercial、template、
song-fragment、mixed-English 等分支完全不變。必要 regression 至少包含：

- §22.6 `어? 살려줘.×5` + strict confidence + `silence_complete` → 通過；
- 同文字在低 `min_avg_logprob`、高 `max_no_speech_prob`、missing confidence、forced、
  incomplete、unknown cut 各自仍為 `stt_garbage`；
- `ㅋㅋ`／單音節／符號 loop 即使高信心仍被擋；
- 多來源句只要任一 source confidence 不合格（由 min/max 表示）仍被擋；
- kill switch off 完整回到舊行為。

### 26.4 提案 3：NO-OP，現有 telemetry 已達成原目標

直接掃描 `20260712T180543Z-76332`：38 筆 NVIDIA `status=empty` 全部已有
`api_error_type=timeout`、`api_error_message_class=read_timeout`，null diagnostics 為 0。
再掃描所有本機 runtime log：111 筆 NVIDIA empty 中，110 筆為
`timeout/read_timeout`、1 筆為 `api_error/empty_response`，null diagnostics 仍為 0。

現行 `_classify_api_error` 已區分 timeout、HTTP class、connection、JSON／KeyError parse；
content-empty 也記為 `empty_response`。`status=empty` 是 engine-call outcome 類別，不代表原因
遺失。另 `no_api_key` engine 因 `available=False` 不會進 production fallback attempt，
`rejected_output` 也已有獨立 status。新增 `failure_reason` 只會複製
`api_error_type + api_error_message_class` 並產生漂移風險，因此不施工。

這項核對也修正 §22.4 的前提：latest run 的 NVIDIA latency tail 已可歸因為 38/38 read
timeout，不必先補 telemetry 才能判斷。任何 timeout／routing 調整仍應另開 proposal，
不混入本次 alias + repetition 批次。

### 26.5 實作授權與完成條件

授權依 §26.2–§26.3 實作提案 1、2；提案 3 明確不改 code。完成條件為 targeted tests、
replay eval diff 審查與 full suite 全綠。不得改 timeout、retry、fallback、routing、prompt、
foreign-speech telemetry，也不得建立人工標註工作。

---

## 27. Claude post-implementation review of §26 實作（2026-07-13）

### 27.1 判定：PASS

三項均依 §26.2–§26.3 契約落地;code diff 僅
`data/translation_corrections.json`、`config.py`、`translation_policy.py`、
`translator.py` + 四份測試,未觸及授權範圍外的檔案。全套
**912 passed / 4 skipped / 183 subtests**、targeted 287 passed、
replay_eval **750 cases 0 diverge**。

### 27.2 提案 1（URL 專名):PASS

- 只新增 §26.2 授權的三個 target form:`랑코`＋`啦可/啦科`、`마냥`＋`馬尼亞`;
  無夾帶其他 alias。`test_translation_corrections.py` 的 wrong-form 總數
  186→189 已同步。
- **snapshot 敘述已修正**:`translation_profiles.json` 未動,
  `update_translation_profile_snapshot.py` 未被無理由重跑——符合 §26.2。
- 行為 spot-check(url profile):`啦可→랑코`✓、`마냥아＋馬尼亞→마냥`✓、
  **副詞「只是」不動**✓(target 白名單 fail-closed)、合法 `랑코` 保留✓。
- target omission 不重插:`마냥아` 被整段省略的案例維持原結果(deterministic
  後修的固有邊界,已誠實標註)。
- replay 0 diverge:新 alias 未命中 golden set,無需 `--update`,乾淨。

### 27.3 提案 2（repetition exemption):PASS,且優於契約

- **門檻取自 `cfg.stt.context_avg_logprob_threshold`/`context_no_speech_threshold`**
  (translator `_new_translation_policy`),非硬編 -0.7/0.3——比 §26.3「目前
  -0.7/0.3」更嚴謹,消除未來 context 門檻調整時的 drift。
- 用 `sentence_metadata` 的 per-source worst-case `min_avg_logprob`/
  `max_no_speech_prob`,非最後一個 chunk,符合 §26.3。
- 9 條 fail-closed 行為逐一實測(直接呼叫 policy):
  高信心 silence_complete→豁免;低 logprob／高 no_speech／缺信心／forced／
  unknown cut／無 evidence／jamo loop(`ㅋㅋ`)／kill switch off→**全部仍
  `stt_garbage`**。與 §26.3 required regression 完全一致。
- ≥2 Hangul syllable 判定正確:`살려줘.`(含句點)算 3 syllable 通過;
  `ㅋㅋ`(compatibility jamo,非 AC00–D7A3)算 0 被擋——正是 §26.3 要的邊界。
- evidence 為 5 欄 dataclass,未把整份 metadata 耦合進 policy;`translate_event`
  以呼叫參數 `incomplete` 覆寫 evidence.incomplete(防 stale 物件誤標),
  是契約外的正確防禦。commercial/template/song/mixed-English 分支未變。

### 27.4 提案 3：確認 no-op

`translation_engines.py` 不在 diff。§26.4 的核對(38/38 及全機 111 筆 NVIDIA
empty 已具 `api_error_type`,null diagnostics=0)成立,不新增 `failure_reason`
避免與現有欄位漂移——同意結案。

### 27.5 commit 邊界提醒（非阻擋）

- `config.py` 的 `streamer_profile: "hades_chxxnnx" → "url"` 是 user 的
  **本地 session 開關**,非提案實作內容;依既有慣例 commit 前應還原
  (提案 1/2 本身不依賴此值,測試以 profile override 自帶 url context)。
- `repetition_confidence_exempt_enabled` 預設 True=功能上線;所有 fail-closed
  guard 已驗,kill switch 可一鍵回退。下一場 live 可觀察 `stt_garbage`
  filter 量與是否有新的 repetition 誤放行。

實作與契約一致,無阻擋項;可交由 user 依 commit 邊界提交
(提案 1＋2 為一批,排除 streamer_profile 本地開關)。
