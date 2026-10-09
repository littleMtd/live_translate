# 離線字幕檔：轉錄與翻譯（任務計畫）

- 起草：Claude Code，2026-10-09（依 `CROSS_REVIEW_WORKFLOW.md`，本文件為提案，待 Codex 交叉審查後實作）
- 任務類型：新增離線工具，重用 STT、翻譯、profile 與名稱校正；跨 `modules/stt.py`、`modules/translation_engines.py`、
  `modules/translator.py`、`modules/profile_context.py`、`utils/runtime_events.py`
- 不呼叫付費 API（測試全部 mock）；不提交

## 1. 背景與使用者決定

| ID | 內容 | 證據 |
|---|---|---|
| U1 | cafe_fansite 的影片要「一鍵下載＋時間軸中文字幕」，並在網站內用 HTML5 `<video>` 直接觀看（WebVTT 字幕軌） | user decision，2026-10-09 |
| U2 | 先做「下載完一次做好」，不做邊下載邊出字幕 | user decision |
| U3 | 字幕來源依序：官方繁中字幕 → 官方韓文字幕＋翻譯 → YouTube 自動韓文字幕＋翻譯 → 都沒有才語音辨識＋翻譯。前兩項的下載與挑選由 cafe_fansite 處理，live_translate 只負責「翻譯一份韓文字幕檔」與「把音訊轉成韓文字幕再翻譯」 | user decision |

實測（2026-10-09）：YouTube 고세구 MV 有上傳者字幕 `ko/zh-Hant/zh-Hans/en/ja` 與 157 種自動字幕；SOOP 片段兩者皆無。

## 2. 現況觀察

| Claim | 內容 | 證據 |
|---|---|---|
| C1 | Groq STT 以 `audio.transcriptions.create(model=cfg.stt.groq_model, language=cfg.stt.language, prompt=…, response_format="verbose_json", temperature=0.0)` 呼叫；`verbose_json` 會回傳帶時間的 segments | code：`modules/stt.py:892-899` |
| C2 | 即時路徑的 STT 輸入是 `np.ndarray`／`AudioChunk`，綁在擷取、VAD 與句子組裝流程上，不能直接餵整個檔案 | code：`modules/stt.py:407-411` |
| C3 | 翻譯引擎介面 `TranslationEngine.translate(text, system_prompt, incomplete, …)`；DeepSeek 引擎的正式 system prompt 由 `effective_system_prompt_for_engine` 改寫為 capsule prompt | code：`modules/translation_engines.py:913-1003, 1194-1230, 758-800` |
| C4 | 決定論後處理（名稱渲染、來源感知校正）在 `modules/translator.py` 的 `_apply_source_aware_corrections`、`_apply_source_gated_name_rendering` 等函式 | code：`modules/translator.py:903, 994` |
| C5 | `--profile` 以 `profile_state.configure_source(mode="manual")` 鎖定 profile，不改 `cfg`；`run_kind=cafe_clip` 已被證據工具預設排除 | code／plan：`docs/agent/CAFE_CLIP_LAUNCH_PLAN_20261008.md`（已實作，`ba0902c`） |
| C6 | 系統已有 ffmpeg 9.0.2（winget）；`scripts/download_audio.py` 以 yt-dlp 下載 YouTube 音訊 | runtime：2026-10-08 安裝；code：`scripts/download_audio.py` |

## 3. 提案

新增一個離線工具（暫名 `scripts/make_subtitles.py`；實作前依 `docs/agent/TOOL_INVENTORY.md` 確認沒有可擴充的既有工具），兩種模式：

### S1　`translate` 模式：翻譯一份韓文字幕檔

```
make_subtitles.py translate --input ko.vtt --profile isegye_lilpa --out-dir <dir>
```

- 讀 WebVTT 或 SRT（YouTube 自動字幕的 VTT 含逐字時間標記與重複行，要先合併成乾淨的句級 cue）。
- 輸出：`<name>.zh-TW.vtt`、`<name>.zh-TW.srt`，以及清理後的 `<name>.ko.vtt`（供網站切換雙語）。

### S2　`transcribe` 模式：音訊轉韓文字幕再翻譯

```
make_subtitles.py transcribe --input audio.m4a --profile isegye_lilpa --out-dir <dir>
```

- ffmpeg 轉成 16 kHz 單聲道，切成不超過 Groq 檔案上限的片段（例如 10 分鐘），每段呼叫 C1 的 Groq STT，
  segment 時間加上片段起點，合併成 cue。STT prompt 使用指定 profile 的 glossary（沿用 `build_groq_prompt` 的 glossary 部分）。
- 片段邊界的句子處理：相鄰片段重疊數秒並去除重複 cue，或在靜音處切割；兩種做法請審查者依程式現況擇一。
- 之後與 S1 相同的翻譯流程。

### S3　翻譯流程（兩種模式共用）

- 以 `profile_state.configure_source(<profile>, mode="manual")` 鎖定 profile（同 C5）。
- 批次翻譯：一次送 15–25 個 cue（JSON 陣列），附前一批最後幾句作為上下文；DeepSeek 引擎與正式 capsule prompt（profile facts）相同，
  另加一段「離線字幕」說明：逐 cue 對應、不合併不拆分、保留順序。回傳長度不符或解析失敗時，該批改逐句翻譯。
- 每個譯文 cue 經 C4 的決定論後處理（名稱渲染、來源校正），與即時字幕使用同一套規則。
- 不使用即時路徑的字幕視窗、provisional、句子組裝。

### S4　進度、費用與工作階段標記

- stdout 逐行輸出 JSON 進度，例如 `{"stage":"transcribe","done":3,"total":12}`、`{"stage":"translate","done":40,"total":120}`、
  最後一行 `{"stage":"done","files":{…}}`；失敗時 `{"stage":"error","message":"…"}` 並以非零碼結束。cafe_fansite 解析這些行顯示進度。
- `--estimate`：只讀長度（ffprobe）與 cue 數，不呼叫 API，輸出預估 STT 秒數、翻譯 token 與費用。費率來源請審查者確認 repo 內是否已有定價設定；
  沒有時以設定檔參數提供，並在輸出標示「預估」。
- 本工具的執行以 `LIVE_TRANSLATE_RUN_KIND=cafe_clip` 標記（工具在匯入 `runtime_events` 前設定），沿用既有的證據排除（C5）。

## 4. 非目標

- 不改即時路徑（擷取、STT、翻譯、字幕視窗）的行為。
- 不下載影片、不挑選字幕來源（cafe_fansite 負責）。
- 不做邊下載邊出字幕（U2）。
- 不呼叫付費 API 做測試；真實執行一次短片（31 秒 SOOP 片段）需使用者同意。

## 5. 假設

| ID | 假設 | 如何驗證 |
|---|---|---|
| A1 | Groq `verbose_json` 的 segments 時間對整段音訊檔可用，足以產生句級字幕 | 讀 `modules/stt.py` 對 response 的處理；以 mock response 測試 segment → cue 轉換 |
| A2 | 可以在不啟動擷取與 pipeline 的情況下，單獨建立 Groq client 與 DeepSeek 引擎（金鑰與設定從既有 config 讀取） | 讀 STT／translation engine 的建構方式；測試以 mock client 建立 |
| A3 | 決定論後處理函式可對單一 cue 呼叫，不依賴即時路徑的 request context | 讀 `_apply_source_aware_corrections` 等函式的依賴；若需要 request context，改用與即時相同的建立方式 |
| A4 | Groq 帳號的速率限制（每小時音訊秒數等）會限制長 VOD 的轉錄 | 查 repo 內既有的 Groq 錯誤處理與 `scripts/analyze_groq_error_bursts.py`；工具遇 429 以退避重試，並在進度中回報 |
| A5 | YouTube 自動字幕 VTT 的重複行可以穩定合併 | 以實際下載的 VTT 片段做 fixture（Claude Code 實作前提供） |

## 6. 驗證計畫

1. 單元測試（全部 mock，不連網、不呼叫付費 API）：
   - VTT／SRT 解析與輸出；YouTube 自動字幕重複行合併（A5 fixture）。
   - 片段切割與 segment 時間偏移；片段邊界去重。
   - 批次翻譯：正常、長度不符退回逐句、解析失敗退回逐句；譯文經名稱校正（例如 `고세구` → `Gosegu`、`세구땅` 保留）。
   - profile 鎖定生效且不改 `cfg`；`run_kind` 為 `cafe_clip`。
   - 進度 JSON 行格式；錯誤時非零碼與 error 行。
   - `--estimate` 不呼叫 API。
2. `python -m pytest -q`（全套；暫存與 cache 在系統 TEMP）。
3. 實作後的唯讀審查（`AGENTS.md` → Codex agent settings）。
4. 真實執行（經使用者同意）：SOOP 31 秒片段 `transcribe` 一次、고세구 MV 的韓文字幕 `translate` 一次，人工檢查時間軸與譯文。

## 7. 審查者檢核清單

- C1–C6 是否與程式碼相符。
- A2、A3：離線建立 STT／翻譯元件與呼叫後處理，是否會牽動即時路徑的全域狀態（metrics、runtime events、cache、circuit breaker）。
- S2 的片段切割與邊界處理建議哪一種，理由。
- S3 的批次翻譯是否應沿用既有翻譯 cache／translation memory，或刻意隔離。
- S4 的費用估算是否有 repo 內的定價依據。
- 新工具是否應放在 `scripts/` 或其他位置；`TOOL_INVENTORY.md` 是否要登記。

## 附錄：測試資料（A5）

Claude Code 已於 2026-10-09 以 yt-dlp 下載고세구 MV（`https://youtu.be/zGOzGcEbd-s`）的字幕並截取前段，放在 `tests/fixtures/subtitles/`：
- `youtube_official_ko.vtt`：上傳者提供的韓文字幕（乾淨的句級 cue）。
- `youtube_auto_ko.vtt`：YouTube 自動字幕（`ko-orig`），含逐字時間標記 `<00:00:08.760><c>…</c>`、重複的上一行、極短的過渡 cue；
  且辨識錯誤明顯（歌詞被聽成「하나님이 속가」）。說明自動字幕品質不穩，cafe_fansite 應優先使用官方字幕。
- `youtube_official_zh-Hant.vtt`：上傳者提供的繁中字幕（cafe_fansite 會直接使用，不經本工具）。

## 8. 審查紀錄

### Codex review（round 1，2026-10-09）

審查者：Codex（CLI 0.162，`gpt-6.1-sol`，reasoning high，read-only）。結論：**REVISE**。

- Claim：C1、C3、C4 成立；C2（元件可獨立建立，只是沒有檔案入口）、C5（並非所有分析器都排除 cafe_clip，例如
  `scripts/analyze_groq_error_bursts.py:33`）、C6（安裝紀錄不能取代執行期的 ffmpeg 可用性檢查）部分成立。
- 假設：A1、A3、A4、A5 部分成立；A2 成立但非無副作用（`profile_state.configure_source` 會改程序內 singleton；
  `effective_profile_id()` 只讀「已綁定」的 snapshot，未綁定時退回 cfg，`modules/activity_context.py:407`）。
- Blockers：
  - B1 profile 與隔離契約不完整（獨立程序、snapshot binding、activity、禁止 live DB／history／shared state）。
  - B2 批次翻譯無法沿用 `translate()`：會重建 prompt 丟棄離線指示；預設 160 output tokens、4 秒逾時
    （`modules/translation_engines.py:601`、`config.py:212`）；回應須以 cue ID 驗證。
  - B3「與即時相同規則」未完整設計：即時路徑另有 source normalization、entity obligations、request protection、
    placeholder 還原、OpenCC、最終驗收（`modules/translator.py:2251, 1134`）。
  - B4 既有 logger 寫 stdout（`utils/logger.py:11`），會破壞 JSON 進度行。
  - B5 時間軸與失敗交付的驗收條件不足；A5 fixture 缺人工確認的預期結果。
- Non-blocking：執行期檢查 ffmpeg／ffprobe 並支援明確路徑；遵守 `Retry-After`、有限重試、取消；fixture 補談話與真實重複句；
  進度加 job ID；Groq `whisper-large-v3` 官方價 USD 0.111／音訊小時，每次至少計 10 秒；DeepSeek 定價已在 `config.py:217`。

### Claude Code response（round 2，2026-10-09）

逐項驗證後全部同意，修訂如下（取代 §3 中對應的描述）：

**R1（B1）獨立程序與隔離**
- 工具一律以獨立子程序執行（cafe_fansite 以子程序呼叫），**第一行程式在任何 import 之前**設定
  `LIVE_TRANSLATE_RUN_KIND=cafe_clip`。
- 建立 snapshot：`profile_state.configure_source(<profile>, mode="manual")` 取得 snapshot 後，以 `bind_profile_snapshot()`
  包住「prompt 建構」與「每個 cue 的前後處理」；activity 綁定為空（不繼承控制台的即時情境）。
- 不建立完整 `STTEngine()`；只建立離線用的 Groq client（沿用 `modules/stt.py:373` 的建構方式與金鑰設定）。
- 不讀寫 live SQLite cache、translation memory、translation history、對話 memory；不使用 live 的 `_TranslatorSharedState`／breaker。
  需要續跑時，使用 job 專屬 checkpoint（輸出目錄內），鍵含模型、profile、prompt 版本、cue 來源。

**R2（B2）離線翻譯請求**
- 新增離線 message builder，經既有 `translate_messages` 邊界呼叫 DeepSeek，不經 `effective_system_prompt_for_engine` 改寫。
  system prompt＝該 profile 的 facts（`get_translation_profile_facts`，與正式 capsule 同源）＋離線字幕指示。
- 每次請求自訂 `max_tokens`（依 cue 數與來源長度估算，有上限）與逾時（例如 60 秒），**不修改 live 預設值**。
- 請求與回應以 cue ID 對應：輸入 `[{"id": "c12", "ko": "…"}]`，輸出 `[{"id": "c12", "zh": "…"}]`。
  缺 ID、重複 ID、多出 ID、型別錯誤、截斷時，整批退回逐句翻譯；逐句仍失敗的 cue 見 R5。

**R3（B3）沿用的後處理範圍（明列，不宣稱與即時等價）**
每個 cue 依序：
1. 翻譯前：`_normalize_source_before_matching`（profile 的 STT 校正，例如 `늘파`→`릴파`）。
2. 翻譯後：`_apply_source_aware_corrections`、`_apply_source_gated_name_rendering`、台灣正體腳本正規化（DeepSeek 路徑既有的 OpenCC 處理）。
3. 每個 cue 前重設校正紀錄（thread-local／ContextVar），避免殘留。
不沿用：request protection 與 placeholder、canonical obligations 的最終驗收（publication rejection）。離線字幕不拒絕輸出；
改為在完成報告（`done` 行與 `<name>.report.json`）列出品質旗標，例如譯文含未核准的韓文、簡體字。

**R4（B4）stdout 只放 JSON**
- CLI 啟動時把所有 logging handler 改導 stderr（包含 `utils/logger.py` 建立的 handler）；ffmpeg／ffprobe 的輸出一律擷取。
- 進度行格式：`{"job": "<id>", "stage": "...", ...}`，每行一個 JSON；以真實 CLI 子程序測試 stdout 每一行都可解析。

**R5（B5）時間軸與失敗處理**
- segment 時間：丟棄 NaN、負值、`end <= start`；裁到媒體長度內；毫秒精度輸出。
- 切片：每片 L 秒（預設 600）、前後重疊 O 秒（預設 5）；片段 k 擁有 `[k·L, (k+1)·L)`，**只保留中點落在擁有區間內的 segment**。
  以「所有權」去重而不是比對文字，所以真正重複的發言會保留。每片實際輸出 bytes 須低於 Groq 上限並留餘量（16 kHz mono PCM16 十分鐘約 19.2 MB）。
- 429：遵守 `Retry-After`，同一片段重試，有次數與總等待上限；超過則以 error 結束。
- 逐句翻譯仍失敗的 cue：中文字幕軌放 `【未翻譯】＋韓文原文`，計入 report；工作仍可完成。
- 原子交付：所有檔案先寫到暫存名稱，全部成功才改名並輸出 `done`；任何 error 都不留下最終檔名。
- YouTube 自動字幕清理規則見附錄 B，fixture 的預期結果由 Claude Code 人工確認。

**其他（Non-blocking）**
- 啟動時檢查 ffmpeg／ffprobe（`--ffmpeg` 可指定路徑）；找不到時以 error 結束並說明。
- `--estimate`：Groq `whisper-large-v3` USD 0.111／音訊小時（官方定價，查核日期 2026-10-09；含重疊秒數、每次至少 10 秒），
  DeepSeek 用 `config.py:217` 的費率；翻譯 token 未知時輸出範圍與假設，未知費率不顯示為 0。
- 程式結構：`scripts/make_subtitles.py` 為薄 CLI；cue 結構、解析、切片、翻譯協調放 `modules/offline_subtitles.py`。
  登記到 `docs/agent/TOOL_INVENTORY.md`，寫明付費界線與狀態隔離。
- C5 的描述修正為「主要的 collection、sampling、replay、quality-loop 工具預設排除 `cafe_clip`」。

## 附錄 B：YouTube 自動字幕清理規則與 fixture 預期結果（Claude Code 人工確認）

規則（依序）：
1. 移除行內時間標記 `<hh:mm:ss.mmm>` 與 `<c>`／`</c>` 標籤，去除首尾空白。
2. 丟棄長度 < 50 ms 的過渡 cue。
3. 自動字幕是「滾動」格式：cue 的第一行**經規則 1、4、5 正規化後**若等於上一個保留 cue 的（正規化後）文字，
   視為延續，只取其餘行作為新內容。（例：`[음악] 불 꺼진` 正規化後為 `불 꺼진`，等於上一句。）
4. 移除 `[음악]` 這類方括號音效標記；移除後為空的 cue 丟棄。
5. 合併多個空白為一個。

`tests/fixtures/subtitles/youtube_auto_ko.vtt` 的預期結果：

| # | 開始 | 結束 | 文字 |
|---|---|---|---|
| 1 | 00:00:04.000 | 00:00:08.505 | 우우우 |
| 2 | 00:00:08.515 | 00:00:11.509 | 불 꺼진 |
| 3 | 00:00:11.519 | 00:00:14.910 | 않는 말도 없는 |
| 4 | 00:00:14.920 | 00:00:18.990 | 화 하나님이 속가 보이지 않아 괜찮은 |
| 5 | 00:00:19.000 | 00:00:21.605 | 적게도 티가 |
| 6 | 00:00:22.439 | 00:00:24.950 | 한 발짝 |
| 7 | 00:00:24.960 | 00:00:26.710 | 조심술에 |
| 8 | 00:00:26.720 | 00:00:28.750 | 다가갈게 |
| 9 | 00:00:28.760 | 00:00:30.310 | 오늘이 |

（`00:00:21.615 --> 00:00:22.429` 只有 `[음악]`，依規則 4 丟棄。辨識錯誤如「하나님이 속가」照原樣保留：清理不修正內容。）

`youtube_official_ko.vtt` 的預期：每個 cue 原樣保留（多行合併為一行，以空白連接）；檔尾若有不完整或無內容的 cue 則丟棄。

### Codex re-review（round 2，2026-10-09）

審查者：Codex（CLI 0.162，`gpt-6.1-sol`，reasoning high，read-only）。結論：**YES**，B1–B5 在計畫層級均已解決，無 new blocker。
附錄 B 以記憶體模擬驗證：19 個輸入 cue → 丟棄 9 個 10 ms 過渡 cue 與 1 個清理後為空的音效 cue → 9 個 cue，時間與文字逐項相符。

實作須注意（審查指出）：
- `translate_messages` 沒有 `max_tokens=`／`timeout=` 參數；每次請求的 token 上限與逾時要透過 `RouteRequest`
  （`modules/translation_request.py:29`）：token 上限寫入 `body`、設定 `timeout_seconds`，以 `route_request=` 傳入；
  此分支回傳 `EngineResult`，用 `finish_reason` 判斷截斷。
- 空 activity：`capture_activity_snapshot("")` 搭配 `bind_activity_snapshot`。
- `_apply_source_aware_corrections` 內部已呼叫名稱渲染，不必再呼叫 `_apply_source_gated_name_rendering`。
- logger：在任何可能產生日誌的初始化之前，以 `setStream(sys.stderr)` 重導 `utils/logger.py` 建立的 handler。
- 原子交付需涵蓋「改名途中失敗」的情況。

**狀態**：計畫通過交叉審查，進入實作。

## 9. 實作與實作後審查（2026-10-09）

- 實作：Codex（CLI 0.162，`gpt-6.1-sol`，workspace-write）。完成後的內部審查因 Codex 用量額度用完而未執行。
- 實作後審查：因 Codex 額度不足，改由 Claude Code 另開的唯讀審查 agent（全新上下文）執行。
  - round 1：**REVISE**。Blocker：轉錄沿用即時路徑的 Groq 逾時（`cfg.stt.groq_timeout`，10 秒），長切片會失敗。
    Non-blocking：最後一片所有權止於媒體長度、會丟掉超出結尾的最後一句；`simplified_script` 旗標為死碼；
    DeepSeek 回覆被 ```json 包住時整批失敗；全部 cue 失敗仍回報 done；report 的 run_kind 為寫死常數。
  - 修正（Claude Code）：離線 STT 逾時 300 秒（`OFFLINE_STT_TIMEOUT_SECONDS`）；最後一片 `owner_end = inf`；剝除整段程式碼圍欄；
    `simplified_script` 改比較轉換前後；全部 cue 失敗時 raise；run_kind 改讀 runtime writer；argparse 說明改字面字串。各有回歸測試。
  - round 2：**APPROVE**，無新問題。
- 最終驗證：全套 1588 passed、2 skipped、451 subtests passed；`tests/test_offline_subtitles.py` 38 passed、1 skipped。
- 真實執行（使用者已同意付費）：經 cafe_fansite 對 SOOP 31 秒片段執行 `transcribe` 兩次，19 句、時間軸正確，
  report `profile=url`、`run_kind=cafe_clip`、失敗 0、品質旗標 0。

### 後續修正（2026-10-09）

- 起因：使用者的 cafe_fansite 在 winget 安裝 ffmpeg 之前啟動，「下載＋字幕」只顯示 `offline job failed`（原因只在 stderr）；
  估價還會先產生與影片等長的靜音 WAV（3 小時約 180 MB）。
- 修正：新增 `OfflineSubtitleError`（本地驗證、訊息為固定字串，才放進 JSON 錯誤行；provider 例外仍為通用訊息）；
  `transcribe --estimate --duration SECONDS`（不需輸入檔與 ffmpeg；需為正有限值且不超過 72 小時）。
- 審查：Claude Code 另開的唯讀審查 agent，round 3 **APPROVE**；採納其 non-blocking 建議中的 `--duration` 上限。
  未採納（列為之後處理）：argparse 錯誤改為可顯示訊息、`CalledProcessError`／`OSError` 的 stderr 原因、except 區塊內的 import、
  一個測試依賴目前目錄。
- 驗證：全套 1595 passed、2 skipped、451 subtests passed；經 cafe_fansite（PATH 不含 ffmpeg）實測：3 小時 VOD 顯示確認頁與估價，
  20 秒 SOOP 片段完整轉錄翻譯成功。
