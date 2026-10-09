# 離線字幕改用 Gemini 3.8 Flash 翻譯（任務計畫）

- 起草：Claude Code，2026-10-09（依 `CROSS_REVIEW_WORKFLOW.md`，本文件為提案，待交叉審查後實作）
- 任務類型：離線工具的翻譯供應商變更；只動 `modules/offline_subtitles.py`、`scripts/make_subtitles.py` 與其測試、文件。
  **即時路徑不變**（仍為 DeepSeek → Groq 後備）。
- 不呼叫付費 API 做測試；不提交

## 1. 使用者決定與依據

| ID | 內容 | 證據 |
|---|---|---|
| U1 | 離線字幕（cafe_fansite「下載＋字幕」使用的 `make_subtitles.py`）的翻譯改用 Gemini 3.8 Flash；即時字幕維持 DeepSeek | user decision，2026-10-09 |
| E1 | 75 句 production 基準（`data/translation_prompt_benchmark_20260822.json`），兩者吃相同的 production DeepSeek 訊息與參數、相同後處理：機器評分 DeepSeek 62／75、Gemini 69／75；Gemini 多過 8 句、少過 1 句（`tpb_lilpa_canonical_029`：譯文殘留「님」） | runtime：Claude Code 離線 A/B（腳本在 repo 外，結果未提交） |
| E2 | Gemini 多過的 8 句皆為實質語意：結巴的「계정」、「짬 때리다」、「사패」、STT 錯字「무송부」、`솜먕` 保留韓文、`아이즈원` 不套 IRISÉ 粉絲名等 | runtime：同上 |
| E3 | 延遲中位數：DeepSeek 0.86 s、Gemini 2.1–2.3 s（`thinkingLevel=low` 時思考 token 為 0；不設時 4.7 s）；`minimal` 不被接受 | runtime：同上與 10 句複測 |
| E4 | 金鑰：`.env` 的 `GEMINI_API_KEY`，付費 Tier 1（Prepay），回應標頭 `X-Gemini-Service-Tier: standard` | runtime；使用者提供的 AI Studio 截圖 |
| E5 | 官方價格（standard，至 2026-12-31）：輸入 US$0.75／1M、輸出 US$3.75／1M（含思考 token）；2027-01-01 起兩者加倍。Batch／Flex 半價、Priority 1.8 倍 | doc：https://ai.google.dev/gemini-api/docs/pricing（2026-10-09 查核） |
| E6 | live_translate 目前沒有任何 Gemini 設定或程式路徑（`config.py`、`modules/`、`utils/` 皆無 `gemini`） | code：repo 搜尋 |

## 2. 提案

### G1　Gemini 翻譯用戶端（只在離線工具內）

- 在 `modules/offline_subtitles.py` 加一個最小的 Gemini REST 用戶端（標準函式庫 `urllib`，不新增套件）：
  `POST https://generativelanguage.googleapis.com/v1beta/models/<model>:generateContent`，金鑰以 `x-goog-api-key` 標頭傳送。
- 不做成 `TranslationEngine`，不接進 `translation_engines` 的路由，避免即時路徑出現新的 provider（遵守「production 只用 DeepSeek 翻譯」）。
- 金鑰來源：`GEMINI_API_KEY`（環境變數或 `.env`，讀法比照現有金鑰）；只在選用 Gemini 時讀取。金鑰不得出現在 stdout、report、例外訊息或日誌。

### G2　請求內容與參數

- 訊息沿用現有離線 `messages_for(rows, facts, context)`（cue ID JSON 契約）；轉成 Gemini 格式：
  system → `systemInstruction`，其餘依序轉為 `contents`（`assistant` → `model`）。
- `generationConfig`：`temperature` 與 DeepSeek 離線請求相同；`maxOutputTokens` 沿用現有每批 token 預算計算；
  `thinkingConfig.thinkingLevel = "low"`（E3）；`responseMimeType = "application/json"`，讓回覆為純 JSON。
- 逾時 60 秒；`429`／`503` 遵守 `Retry-After`，有次數與總等待上限（比照轉錄的退避規則）；其他錯誤即失敗，交給既有的「整批 → 逐句」退回。
- 解析：取第一個 candidate 的非 `thought` 文字；`finishReason` 不是 `STOP` 視為截斷；之後完全沿用現有 cue ID 驗證。
- 安全過濾（`finishReason=SAFETY` 等）視為該批失敗，走逐句退回；逐句仍失敗的 cue 照既有規則標【未翻譯】。

### G3　供應商選擇與報告

- CLI 新增 `--translator gemini|deepseek`，**預設 `gemini`**；模型名稱以常數 `gemini-3.8-flash` 為預設，可用 `--gemini-model` 覆寫。
- 選 Gemini 但沒有金鑰時：`OfflineSubtitleError("Gemini API key is missing")`，**不自動改用 DeepSeek**（避免使用者以為用了 Gemini）。
- report 增加 `translator`（provider 與 model）、`usage`（輸入／輸出／思考 token 合計）；`prompt_version` 升為 `offline-subtitles-v2`。

### G4　費用估算

- `--estimate` 依 `--translator` 使用對應費率：Gemini 用 E5 的價格常數，附 `translation_pricing_revision`（查核日期）與「2027-01-01 起加倍」的日期判斷；
  輸出 token 估算納入思考 token 的保守上限。未知模型的費率回傳 `None`，不顯示為 0。

### G5　cafe_fansite

不需改程式（預設即 Gemini）。之後若要讓使用者在網頁上選擇供應商，另案處理。

## 3. 非目標

- 不改即時路徑、`translation_engines`、`config.py` 的即時翻譯設定。
- 不做自動備援到 DeepSeek（G3）。
- 不改轉錄（仍為 Groq Whisper）。
- 不呼叫付費 API 做測試。

## 4. 假設

| ID | 假設 | 如何驗證 |
|---|---|---|
| A1 | `responseMimeType: application/json` 與 `thinkingLevel: low` 可同時用於 `gemini-3.8-flash` | 實作後以一次真實小請求確認（使用者已同意付費測試）；單元測試以假回應覆蓋 |
| A2 | Gemini 回覆中 `thought` 部分會以 `part.thought=true` 標示，可安全排除 | 讀官方回應格式；測試含 thought part 的假回應 |
| A3 | 75 句基準的優勢可延續到離線字幕（較長的 cue 批次、歌詞與閒聊） | 真實驗證：同一支 SOOP 片段與 YouTube MV 的韓文字幕，分別用兩種 provider 產生，人工比較 |

## 5. 驗證計畫

1. 單元測試（全部 mock）：
   - 訊息轉換（system／user／assistant → systemInstruction／contents）、generationConfig 內容、金鑰只在標頭。
   - 回覆解析：純 JSON、含 thought part、`finishReason` 非 STOP、SAFETY、空 candidate。
   - 429／503 的 Retry-After 與上限；其他錯誤走逐句退回。
   - `--translator` 預設與切換、缺金鑰錯誤、report 的 provider／model／usage。
   - `--estimate` 的 Gemini 費率與 2027 年加倍判斷；DeepSeek 估算不變。
   - 金鑰不出現在 stdout、report、錯誤行（以假金鑰字串搜尋）。
2. `python -m pytest -q`（全套；暫存在系統 TEMP）。
3. 實作後唯讀審查（AGENTS.md → Codex agent settings；Codex 額度不足時改用唯讀審查 agent）。
4. 真實驗證（使用者已同意付費）：經 cafe_fansite 對 SOOP 31 秒片段與고세구 MV（官方韓文字幕 → 翻譯）各跑一次，並保留 DeepSeek 版本人工對照。

## 6. 審查者檢核清單

- E6：確認 repo 內確實沒有可沿用的 Gemini 程式，以及新增用戶端不會被即時路徑匯入。
- G2 的請求格式、`thinkingLevel`、`responseMimeType` 與官方 API 是否相符；退避與錯誤處理是否與既有轉錄規則一致。
- G3 不自動備援的取捨是否合理。
- G4 的費率與日期判斷是否正確，未知費率不顯示為 0。
- 金鑰不外洩的測試是否足夠。

## 7. 審查紀錄

### Codex review（round 1，2026-10-09）

審查者：Codex（CLI 0.162，`gpt-6.1-sol`，reasoning high，read-only）。結論：**REVISE**。

- E1–E5 接受；E6 字面不成立（repo 有 `scripts/gemini_live_translate_probe.py` 的 Live API 音訊 probe 與 `_log_token_usage()` 的 Gemini 欄位解析），
  但新增離線 REST client 的理由仍成立；目前沒有即時入口匯入 `offline_subtitles`。
- Blockers：
  1. 未區分「工作層級錯誤」與「cue 層級失敗」：現有 `translate()` 對所有例外改逐句重送，Gemini 的 429／503 耗盡退避、401／403、錯誤模型都會逐句再試；
     且翻譯供應商的缺金鑰檢查在付費轉錄之後。
  2. 思考預算與 usage 未成契約：Gemini 的輸出上限含思考 token；需定義預算公式、`MAX_TOKENS` 處理、usage 欄位對應，以及被拒回覆的 usage 也要累加。
  3. 金鑰防洩漏測試漏掉 stderr：`make_subtitles.py` 會把所有 `ValueError` 原文印到 stderr。
- Non-blocking：修正 E6 措辭；urllib `HTTPError` 用 `.code`／`.headers`，`Retry-After` 涵蓋數字、HTTP-date、缺失、無效；日期判斷註明時區並可注入；
  更新工具文件；A3 應讓兩個 provider 吃同一份 cues 與同一次轉錄。

### Claude Code response（round 2，2026-10-09）

全部同意，另併入使用者決定 **U2：Gemini 預付餘額用完時，要明確告訴使用者去 AI Studio 加值**（使用者帳戶為 Tier 1 Prepay、自動加值關閉）。
以下取代 §2 中對應的描述。

**R1　錯誤分層（Blocker 1、U2）**

| 類別 | 範例 | 處理 |
|---|---|---|
| 工作層級，立即中止 | 400（請求格式）、401／403（金鑰或權限）、404（模型不存在） | 以固定訊息的 `OfflineSubtitleError` 結束，不逐句重送 |
| 工作層級，餘額或配額 | 回應的 `error.status` 為 `RESOURCE_EXHAUSTED` 且訊息屬於配額或帳務（非每分鐘速率），或任何帳務相關錯誤 | 立即中止，訊息固定為「Gemini 餘額或配額不足，請到 Google AI Studio 加值或稍後再試」 |
| 可重試 | 429 速率限制、500／503、網路錯誤與逾時 | 遵守 `Retry-After`；**整個工作共用**一份重試預算（例如總等待 ≤ 300 秒、單一請求 ≤ 5 次）；預算耗盡即中止整個工作，不逐句重送 |
| 回覆內容問題（才退回逐句） | JSON 解析失敗、cue ID 驗證失敗、`finishReason` 為 `MAX_TOKENS`／`SAFETY`／`RECITATION`／`OTHER`、沒有 candidate 或文字 | 整批改逐句；逐句仍是內容問題則標【未翻譯】；逐句遇到工作層級錯誤仍中止 |

- 判斷配額或帳務錯誤只看 HTTP 狀態碼與回應中的 `error.status`，加上固定關鍵字比對；**不把回應內容放進任何輸出**。
- 開工前檢查：在任何付費呼叫（Groq 轉錄）之前，先確認所選翻譯供應商的金鑰存在；Gemini 缺金鑰即以固定訊息結束。

**R2　token 預算與用量（Blocker 2）**

- 回答預算沿用現有公式（依來源長度，512–8192）。Gemini 請求 `maxOutputTokens = min(16384, 回答預算 + 1024)`，1024 為思考額度
  （`thinkingLevel=low` 實測多為 0，但不保證）。
- 整批遇 `MAX_TOKENS` → 逐句；逐句的 `maxOutputTokens = 該句回答預算 + 1024`，仍 `MAX_TOKENS` → 該句標【未翻譯】。
- usage 對應：輸入 `promptTokenCount`；輸出 `candidatesTokenCount`；思考 `thoughtsTokenCount`（分開記錄，不重複計算）。
  費用＝輸入 × 輸入價＋（輸出＋思考）× 輸出價。
- 每一個有回應的請求都累加 usage，包括被 cue 驗證拒絕、截斷、安全過濾的回覆；回應缺 `usageMetadata` 時計入 `usage_missing_requests`，**不當成 0**。
- `--estimate`：思考 token 的範圍為 0 到「批次數 × 1024」（上限受請求參數約束，不是推測值）；日期以 **UTC** 判斷是否已到 2027-01-01，日期可注入以便測試。

**R3　金鑰防洩漏（Blocker 3）**

- Gemini 用戶端把所有錯誤包成 `ProviderError`，訊息只含 HTTP 狀態碼與 Google 的 `error.status`（例如 `RESOURCE_EXHAUSTED`），
  不含回應內容、URL、標頭；以 `raise … from None` 截斷例外鏈。不記錄請求內容。
- `make_subtitles.py` 的 stderr：只有 `OfflineSubtitleError` 才印出訊息；其他 `ValueError` 不再原文輸出（修正既有缺口）。
- 測試以假金鑰字串，涵蓋：含金鑰的 HTTP 錯誤內容與 URL、網路錯誤、訊息含金鑰的 `ValueError`；斷言 stdout、stderr、日誌（caplog）與 report 都不含該字串。

**其他（Non-blocking）**

- E6 改為：「目前沒有離線文字 `generateContent` 翻譯用戶端；既有的 Gemini 相關程式為 Live API 音訊 probe 與用量欄位解析，不適用。」
- `Retry-After` 以 urllib `HTTPError.code`／`.headers` 解析，測試涵蓋數字、HTTP-date、缺失、無效。
- 新增測試：即時入口（`main.py` 匯入鏈）不載入 Gemini 用戶端；`--estimate` 不建立 provider、不需金鑰；兩種 provider 的端到端 mock CLI。
- 更新 `docs/agent/TOOL_INVENTORY.md` 與 README 中「翻譯呼叫 DeepSeek」的描述。
- A3 真實比較：同一份韓文 cues（SOOP 共用同一次轉錄、MV 用同一份官方韓文字幕），兩個 provider 同 profile、同上下文、同後處理；
  記錄語意、名稱、韓文殘留、漏譯與失敗 cue。

### Codex review（round 2，2026-10-09）

結論：**REVISE**。Blocker 3 已解決；Blocker 1、2 未解決，無 new blocker。

- B1：官方 GenerateContent 錯誤契約為 **HTTP 402 + `RESOURCE_EXHAUSTED` = 預付餘額耗盡（需加值，不應重試）**；
  **HTTP 429 + `RESOURCE_EXHAUSTED`** 涵蓋 RPM／TPM／RPD／支出速率，`error.status` 與訊息關鍵字都不能穩定區分，R1 的關鍵字規則不可作為契約。
- B2：`maxOutputTokens` 限制的是回答＋思考的合計，不會把思考限制在 1024，也不保留回答額度；「批次數 × 1024 是參數上限」不成立。
- 實作驗收：`error.status` 映射為固定列舉或 `UNKNOWN`；外層訊息與日誌都只用受控值。

### Claude Code 修訂提案（round 3，待使用者決定）

依 CROSS_REVIEW_WORKFLOW 第 5 步，以下兩點由使用者決定是否採用後實作。

- **R1′**：分類只看 HTTP 狀態碼。402 → 立即中止，訊息固定為「Gemini 預付餘額已用完，請到 Google AI Studio 加值」，不重試、不逐句。
  429／500／502／503／504／網路錯誤 → 遵守 `Retry-After`，整個工作共用重試預算；耗盡時中止，訊息為「Gemini 速率或配額限制，重試後仍失敗」，
  不斷言是餘額問題。400／401／403／404 → 立即中止。移除關鍵字比對。`error.status` 只在固定列舉內才寫入訊息，否則記為 `UNKNOWN`。
- **R2′**：1024 改稱「思考預留估算值」，不是上限。`--estimate` 的硬上限改用每個請求的 `maxOutputTokens` 合計（回答＋思考），
  並註明逐句退回與重試不在估算內。報告中若有回應缺 `usageMetadata`，標示 `usage_complete: false`，費用欄位只代表已知部分。
- 新增測試：402 不重試不逐句；429 不誤判為餘額；跨批次共用重試預算；缺翻譯金鑰時 Groq 呼叫數為零；思考 > 1024 但合計未超限仍正常計量；
  整批與逐句 `MAX_TOKENS`；被拒回覆的 usage 恰好累加一次；缺 metadata 時不呈現完整的零費用。

**使用者決定（2026-10-09）**：採用 R1′、R2′，直接實作。實作者：Claude Code；實作後由 Codex 做 post-implementation review。

## 8. A3 實測（2026-10-09，Claude Code）

同一份韓文 cues、同 profile（`isegye_lilpa`）、同後處理；SOOP 31 秒片段只轉錄一次（Groq），兩個 provider 翻同一份 `.ko.vtt`；
MV 用 YouTube 官方韓文字幕（68 cues）。兩邊皆無失敗 cue。

| 來源 | Gemini 3.8 Flash | DeepSeek V4.1 Flash |
|---|---|---|
| SOOP（19 cues） | 0 品質旗標；1 請求，691／539／0（輸入／輸出／思考），US$0.0025 | 3 句韓文原樣殘留（오이루、오왓니）＋1 簡體 |
| MV（68 cues） | 0 旗標；4 請求，2969／1296／0，US$0.0071 | 0 旗標 |

- SOOP：Gemini 正確處理「翻垃圾桶排行第三」「환경미화원＝環境清潔員」與問候語；DeepSeek 誤譯為「你這個垃圾桶」「限定美化員」，並留下韓文。
  STT 雜訊句（낚시떠핑）兩邊都不確定。
- MV：Gemini 對貓咪語境較準（꾹꾹＝踩踩、발바닥 젤리＝肉球、사르르＝融化）；DeepSeek 譯成「按壓」「果凍」「輕輕柔柔」。
- 結論：支持預設使用 Gemini；樣本小，屬初步驗證，不是一般品質保證。

## 9. 實作後審查

### Codex post-implementation review（round 1）：FIX

1. 本機送出錯誤（例如金鑰含換行時 http.client 的 `ValueError`）被當成內容問題逐句重送，且訊息含金鑰。
2. 整批無效 JSON 後逐句遇到 402，`OfflineJobError.__context__` 鏈到含完整回覆的 `JSONDecodeError`。
3. `--estimate` 的上限用平均字數分配，句長不均時低估（21 句反例：10,752 vs 實際 11,808）。
- Non-blocking：數字 `Retry-After` 測試容差過寬；思考測試的合計超過請求上限；`prompt_version` 未升 v2、缺 `--gemini-model`。

### Claude Code 修正

1. 送出階段的其他例外一律轉成固定訊息的 `OfflineJobError`（工作層級，不逐句）；以真實 urllib／http.client 堆疊測試含換行的金鑰。
2. 回覆解析不保留解析例外；整批失敗後的逐句退回移到 except 區塊之外，任何中止都沒有例外鏈。測試檢查整條 `__cause__`／`__context__` 鏈。
3. 有實際 cues 時依每批 rows 計算 `maxOutputTokens` 合計（用原始 cue 長度）；只有 `--duration` 估算才平均分配。補 21 句反例測試。
- `prompt_version` 升為 `offline-subtitles-v2`；新增 `--gemini-model`（需 `--translator gemini`，名稱限 `[a-z0-9.-]`，未知模型費用為 null）。數字 `Retry-After` 改精確斷言；思考測試改為 1100＋100 ≤ 1536。
- 驗證：全套 pytest 1638 passed／2 skipped。

### Codex post-implementation review（round 2）：FIX → Claude Code 修正

- Blocker 1、2 與 Non-blocking 1、2 已解決。
- Blocker 3 部分解決：估算用原始文字，實際請求用 profile 正規化後的文字（例如 `소울라인`），仍可能低估。
  修正：新增 `normalized_sources()`，與 `translate()` 相同的 profile／activity 綁定與正規化；CLI 的 `--estimate` 用它計算每批上限。
  測試直接比較「估算上限」與「實際送出的 `maxOutputTokens` 合計」，含 isegye_lilpa 名稱修正與無 profile 三種情況。
- Non-blocking 3：`--gemini-model` 驗證移到 estimate 返回之前，補兩個 CLI 案例。
- 驗證：全套 pytest 1643 passed／2 skipped。依流程第 5 步不再循環審查，交由使用者決定提交。
