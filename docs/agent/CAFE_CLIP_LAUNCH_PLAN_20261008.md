# 從 cafe_fansite 啟動 live_translate：指定設定檔與片段工作階段標記（任務計畫）

- 起草：Claude Code，2026-10-08（依 `CROSS_REVIEW_WORKFLOW.md`，本文件為提案，待 Codex 交叉審查後實作）
- 任務類型：CLI／設定行為變更，跨 `main.py`、設定匯出、profile 狀態、runtime 事件與 ChatGPT bundle 匯出
- 不呼叫付費 API；不提交

## 1. 背景與使用者決定

| ID | 內容 | 證據 |
|---|---|---|
| U1 | 使用者要在 cafe_fansite 的文章頁按「▶ 播放並開字幕」：開啟 YouTube／SOOP 片段，同時啟動 live_translate 即時字幕，並依文章所屬團體套用設定檔（이세계아이돌 → `isegye_lilpa`，UR:L → `url`） | user decision，2026-10-08 |
| U2 | 不下載影片也能用；停止時可以由 cafe_fansite 結束行程 | user decision |

## 2. 現況觀察

| Claim | 內容 | 證據 |
|---|---|---|
| C1 | `main.py` 沒有指定設定檔的參數；啟動時以 `cfg.active_streamer_profile` 與 `cfg.translation.profile_mode` 呼叫 `profile_state.configure_source` | code：`main.py:224-261` |
| C2 | auto 模式啟動時是 neutral，需由 identity ROI 取得 reviewed identity；`streamer_profile` 只是 configured source metadata，不能在 auto 模式自動取得 ownership。播放 Cafe 的 YouTube 片段時沒有 SOOP／CHZZK 播放器可供辨識 | doc：`README.md:46`；code：`config.py:263-265` |
| C3 | 啟動後 `utils.config_export.write()` 會把目前設定寫到 `logs/live_translate_config.json`；該檔同時是桌面控制台的設定，下次啟動時由 `_apply_dashboard_overrides` 讀回 | code：`main.py:266-270`、`config.py:600-689` |
| C4 | `ProfileControlWatcher` 只在 `logs/live_translate_config.json` 於啟動後**變動**時重新載入（建構時記錄 stamp） | code：`modules/profile_control.py:45-64, 119-140` |
| C5 | 桌面 app 停止 Python 後端的方式是強制結束（`child.kill()`），之後另外匯出 ChatGPT bundle | code：`src-tauri/src/handlers/python.rs:28-50` |
| C6 | 正常結束時 `_export_chatgpt_bundle_on_shutdown` 會自動匯出本次執行的 bundle | code：`main.py:98-130, 405` |
| C7 | README 規定自然 production 證據必須來自使用者自己的 SOOP／CHZZK 直播觀看流程；分析工具不得以外部播放替代 | doc：`README.md:42` |

## 3. 提案

### L1　`--profile <id>`：本次工作階段的手動設定檔

- 新增 `main.py --profile <id>`。只接受 `canonical_profile_id` 能解析的已知 ID；不合法時以非零碼結束並列出可用 ID。
- **作法（round 2 修訂，對應 Codex 選定的 L1-b 精神）：不修改全域 `cfg`。** 啟動時直接呼叫
  `profile_state.configure_source(<id>, mode="manual", translation_profile_applied=True, stt_glossary_applied=cfg.stt.use_profile_glossary)`，
  取代以 `cfg.active_streamer_profile` 呼叫的那一次。`cfg` 不變，所以 `config_export.write()` 照常匯出使用者原本的設定，
  不會把本次 `--profile` 寫進 `logs/live_translate_config.json`；Tauri `start_python` 等待該檔更新的行為也不受影響。
- 這個作法成立的前提是：執行期決定「目前 profile」的程式都經由 `profile_state`（例如 `effective_profile_id`），
  而不是直接讀 `cfg.active_streamer_profile`。列為 A1，實作前要盤點；發現直接讀 `cfg` 的執行期路徑時，回報並停止，不要以修改 `cfg` 繞過。
- 優先順序：
  1. 啟動時：`--profile` 優先於設定檔與 dashboard 匯出值。
  2. 執行中：使用者在控制台更新設定（Tauri `update_config` 寫檔 → watcher 熱重載，C4）時，照既有行為套用使用者的新設定。
     這是使用者明確操作，不另外鎖定。
  3. `--donation-ocr`：子行程的 `--profile` 改用本次生效的 profile（有 `--profile` 時傳該值），不再只看 `cfg.active_streamer_profile`。
- 參數關係：與 `--calibrate-identity-roi`、`--show-identity-roi` 互斥（若 `--profile` 放在 mode group 外，另外檢查並以非零碼結束）；
  **允許**與 `--stt-only`、`--listen` 併用（STT glossary 依 profile 仍有用途），並以測試確認 glossary 依指定 profile。

### L2　以既有的 `run_kind` 標記片段工作階段（round 2 修訂，取代原本的 `--session-source`）

- 既有機制：`utils/runtime_events.py` 以環境變數 `LIVE_TRANSLATE_RUN_KIND` 決定 run 的 `run_kind`（允許 `live`、`test`、`replay`、`benchmark`），
  每個事件與 bundle manifest 都帶這個欄位（`utils/runtime_events.py:25, 43-49, 177, 191`；`utils/chatgpt_bundle.py:130, 374`）。
  `run_kind` 在模組載入時就決定，所以由**啟動方設定環境變數**，不用 CLI 參數。
- 修改：
  1. `_RUN_KINDS` 加入 `cafe_clip`。cafe_fansite 啟動時設定 `LIVE_TRANSLATE_RUN_KIND=cafe_clip`。
  2. `cafe_clip` 的 run 在 Python 正常／失敗關閉時**不自動匯出** ChatGPT bundle（C6、C7）。
  3. 證據入口預設排除 `cafe_clip`（只排除它，不改變對 `test`、`replay`、`benchmark` 的現有行為；缺 `run_kind` 的舊事件視為 `live`）。
     各工具加一個明確的選項才會納入：
     - `scripts/analyze_runtime_events.py`：已預設篩 `run_kind=live`，確認 `cafe_clip` 被排除即可（只加測試）。
     - `scripts/post_run_quality_loop.py`（預設取當日整檔）
     - `scripts/collection_sanity_report.py`（預設取最新事件檔）
     - `scripts/sample_labeling_cases.py`（預設取最新事件檔）
     - `scripts/replay_eval.py build`（從 glob 讀 translation 事件）
     - `scripts/export_chatgpt_bundle.py` 的 run 列表：顯示 `run_kind`，讓使用者手動匯出時看得到來源（`list_runs` 已取 `run_kind`，確認顯示即可）。
  4. 若審查／實作時發現其他預設會選取當日或最新事件檔的證據工具，比照處理並列在完成報告。

### L3　啟動時寫檔的完整性（round 2 新增，對應 Codex Blocker 3）

cafe_fansite 會在使用者按「停止」或計時到期時強制結束行程。原本以「桌面 app 也這樣做」作為依據，但這不能證明檔案完整性（Codex round 1）。
改為：
- 盤點強制結束時可能寫到一半的檔案，至少包含：`config_export.write()` 寫的 `logs/live_translate_config.json`（目前是非 atomic 的 `write_text`）、
  runtime 事件 JSONL、`profile_control` 的 status 檔（暫存檔再 replace，可能留下 `.tmp`）、SQLite 資料庫。
- `config_export.write()` 改為「寫暫存檔再 `replace`」的 atomic 寫入（這個檔案會被下一次啟動與控制台讀取，截斷的後果最大）。
- 確認 runtime 事件的讀取端遇到最後一行不完整時會略過而不是失敗；若不會，回報（不在本任務擴大修改讀取端，除非改動很小且有測試）。
- `.tmp` 殘留：確認下一次寫入會覆蓋，或在啟動時清除。

### L4　文件

README「啟動」段補一小節：由 cafe_fansite 啟動時使用的參數與環境變數、`cafe_clip` 不屬於 production 證據且預設被分析工具排除、
停止方式為強制結束。

## 4. 非目標

- 不新增優雅停止機制（C5：桌面 app 已採強制結束）。
- 不改 identity ROI、auto 模式的行為與 profile 資料。
- 不改翻譯管線、STT、字幕視窗。
- 不實作離線字幕檔（之後另案）。
- 不呼叫付費 API；測試不啟動真實的擷取、STT 或翻譯。

## 5. 假設

| ID | 假設 | 如何驗證 |
|---|---|---|
| A1 | （round 2 修訂）執行期決定目前 profile 的程式都經由 `profile_state`（例如 `effective_profile_id`），所以不改 `cfg`、只呼叫 `configure_source(<id>, mode="manual")` 即可讓翻譯 prompt、STT glossary、名稱規則都使用指定 profile | 盤點所有讀 `cfg.active_streamer_profile`／`cfg.translation.streamer_profile` 的執行期路徑；測試：`--profile url` 時 `effective_profile_id(...)` 與 translator 的 prompt 組裝都得到 `url` |
| A2 | ~~`config_export.write()` 是唯一寫入路徑~~ → Codex round 1 否證：Tauri `update_config` 也會寫同一檔。round 2 的 L1 不改 `cfg`，兩條寫入路徑都不會寫入本次覆寫 | 測試：有 `--profile` 時 `config_export.write()` 的輸出與無 `--profile` 時相同 |
| A3 | ~~強制結束不會留下需要清理的狀態~~ → 改為 L3 的盤點與 atomic 寫入，不再作為簽核依據 | 見 L3 |
| A4 | runtime 事件在 `main()` 早期就可發出，且 run_id 在此時已確定（Codex round 1：成立） | — |
| A5 | 環境變數 `LIVE_TRANSLATE_RUN_KIND=cafe_clip` 在行程啟動前設定，即可讓本次 run 的所有事件與 bundle manifest 帶 `cafe_clip` | 測試：設定環境變數後建立 writer，事件的 `run_kind` 為 `cafe_clip`；未設定時仍為 `live` |

## 6. 驗證計畫

1. **測試切點**：把 `main()` 中「解析參數 → 決定 profile → 設定 profile_state → 匯出設定 → 決定是否自動匯出 bundle」
   抽成可直接呼叫的函式，測試只呼叫到這一段，不進入擷取、STT、翻譯的 pipeline（Codex round 1 指出直接呼叫 `main()` 會繼續進入 pipeline）。
2. 新增測試：
   - 參數解析：`--profile url` 被接受；未知 ID 以非零碼結束；與 ROI 模式互斥；與 `--stt-only`、`--listen` 併用時 STT glossary 使用指定 profile。
   - 覆寫生效：`profile_state.current()` 為 manual、`url`；`effective_profile_id` 與 translator 的 profile 解析得到 `url`（A1）。
   - 不持久化：以暫存路徑取代 `logs/live_translate_config.json`，有無 `--profile` 時 `config_export.write()` 的輸出相同（A2）。
   - `config_export.write()` 為 atomic 寫入（寫入中途失敗時原檔不變）（L3）。
   - `--donation-ocr` 子行程命令帶本次生效的 profile。
   - `run_kind`：`LIVE_TRANSLATE_RUN_KIND=cafe_clip` 被接受並出現在事件與 bundle manifest（A5）；`cafe_clip` 不自動匯出 bundle，`live` 照舊。
   - 證據入口：以同一天混合 `live` 與 `cafe_clip` 事件的 fixture，`analyze_runtime_events`、`post_run_quality_loop`、
     `collection_sanity_report`、`sample_labeling_cases`、`replay_eval build` 預設都只取 `live`；加上明確選項才納入 `cafe_clip`。
   - runtime 事件 JSONL 最後一行不完整時的讀取行為（L3）。
3. `python -m pytest -q`（全套）。
4. `git status --short`、`git diff --stat`。
5. 依 `AGENTS.md` 執行實作後的唯讀審查。

## 7. 審查者檢核清單

- C1–C7 是否與程式碼／文件相符。
- L1-a／L1-b 的選擇是否有程式依據；是否還有其他會持久化設定的路徑（A2）。
- `--profile` 與既有 mode group、`--donation-ocr`（`main.py:88-90` 會把 profile 傳給 OCR 子行程）的互動。
- `cafe_clip` 標記是否足以讓事後分析辨識；有沒有遺漏會把 `cafe_clip` run 當成 production 證據的自動流程。
- 測試是否能在不啟動擷取、STT、翻譯的情況下驗證以上行為。

## 8. 審查紀錄

### Codex review（round 1，2026-10-08）

審查者：Codex（`codex exec -s read-only`）。結論：**REVISE**。

- Claim：C1、C4、C6、C7 成立；C2、C3 部分成立（C2 的「YouTube 片段無播放器可辨識」屬使用情境推論；手動 CLI 啟動不會讀回 dashboard 覆寫，
  只有 Tauri 透過環境變數啟用時才讀）；C5 成立但不能證明強制結束時的檔案完整性。
- 假設：A1 部分成立；A2 不成立（Tauri `update_config` 也寫同一檔）；A3 未證實；A4 成立。
- L1：選 L1-b；L1-a 會讓 Tauri `start_python` 等不到設定檔更新而逾時。
- Blockers：
  1. L2 只靠單一事件不足；`run_kind` 預設仍為 `live`，多個證據入口（`post_run_quality_loop`、`collection_sanity_report`、
     `sample_labeling_cases`、`replay_eval build`）會把片段混入。
  2. L1-b 的具體匯出方式未定義；控制台更新、熱重載與 OCR 子行程的優先順序未說明。
  3. A3 以桌面 app 的做法代替驗證。
- Non-blocking：決定 `--profile` 與 `--stt-only`／`--listen` 的關係；C2 標為使用情境假設；明列測試切點與同日混合來源 fixture。

### Claude Code response（round 2，2026-10-08）

| Codex 項目 | 驗證 | 修訂 |
|---|---|---|
| Blocker 1 | 同意。另查到既有的 `run_kind` 機制（環境變數 `LIVE_TRANSLATE_RUN_KIND`、允許值集合 `_RUN_KINDS`、事件與 manifest 都帶此欄位，`analyze_runtime_events` 已預設篩 `live`） | L2 改為新增 `run_kind=cafe_clip`，取代 `--session-source`；四個未篩選的證據入口預設排除 `cafe_clip` |
| Blocker 2 | 同意 `config_export` 沒有「原始值」介面 | L1 改為不修改 `cfg`，直接以指定 ID 呼叫 `configure_source(mode="manual")`，匯出內容自然不含本次覆寫；寫明優先順序與 OCR 子行程的處理；前提列為 A1，須盤點直接讀 `cfg` 的執行期路徑 |
| Blocker 3 | 同意 | 新增 L3：盤點寫入點、`config_export.write()` 改 atomic、確認 JSONL 讀取端與 `.tmp` 殘留；A3 不再作為簽核依據 |
| Non-blocking | 同意 | 允許與 `--stt-only`／`--listen` 併用並加測試；§6 明列測試切點與混合來源 fixture。C2 視為使用情境前提（使用者要播放的是 YouTube／SOOP 片段頁） |

### Codex re-review（round 2，2026-10-08）

審查者：Codex（`codex exec -s read-only`）。結論：**REVISE**。Blocker 3 已解決（計畫層級）；Blocker 1、2 各剩一項具體缺口，無 new blocker：

1. （Blocker 1）`scripts/analyze_runtime_events.py` 的函式允許值與 CLI choices 沒有 `cafe_clip`，無法以明確選項選取；`all` 會一併納入其他種類。
2. （Blocker 2／A1）`effective_profile_id(fallback)` 只有在 profile 已綁定於目前執行脈絡時才取 snapshot，否則回傳傳入的 `cfg` 值
   （`modules/activity_context.py:407`）。一般翻譯路徑有綁定，但 fallback probe thread 只綁定 activity snapshot，
   建立 system prompt 時可能用到原本的 `cfg` profile（`modules/translator.py:3261, 3292`）。
   其他執行期讀取 `cfg` 的路徑：OCR 子行程參數（L1 已處理）與原本的啟動 `configure_source` 呼叫（L1 已取代）。
   `src-tauri`、`src-frontend` 對 `run_kind` 無封閉列舉，不會拒絕 `cafe_clip`。

### 使用者決定（2026-10-08）

依 `CROSS_REVIEW_WORKFLOW.md` 第 5 步，round 2 後仍有 Blocker 時交由使用者決定。使用者選擇：**把 round 2 的兩項修正併入計畫，直接實作，不再進行第 3 輪計畫審查**；實作後照例進行獨立審查。

併入的修正：

- **L2 補充**：`scripts/analyze_runtime_events.py` 的允許值與 CLI choices 加入 `cafe_clip`，預設仍只取 `live`；
  測試：預設排除 `cafe_clip`、明確指定 `cafe_clip` 時只取它。
- **L1 補充**：fallback probe thread 建立 system prompt 時，也綁定當下 `profile_state.current()` 的 snapshot
  （與一般翻譯路徑相同的綁定方式）。測試：`--profile` 與 `cfg` 的 profile 不同時，probe 組出的 prompt 使用指定 profile。
  實作時若再發現其他未綁定 snapshot、會退回 `cfg` 的執行期路徑，比照處理並列在完成報告。

## 9. 實作與實作後審查（2026-10-08～09）

- 實作：Codex（`gpt-6-sol`，workspace-write）。Claude Code 追加：runtime 事件 writer 在檔尾缺換行時先補換行。
- 實作後審查 round 1：Codex `gpt-6.1-sol` high（CLI 0.162）。**REVISE**，6 項：Donation OCR 停用 profile 時仍傳 `--profile`；
  共用篩選器連 test／replay／benchmark 也排除（超出 L2）、Phase 0 builder 缺明確納入選項；強制結束殘留的 `.tmp` 未清理；
  補換行在首次寫入失敗後會被跳過、截斷的多位元字元讓 strict UTF-8 讀取端失敗；測試放寬（OCR 斷言、mock 掉 `configure_source`、
  混合事件測試未證明預設排除）；README 未寫強制結束與非 production 證據。
- 修正：Codex `gpt-6.1-sol`。六項全部修正並補測試；其內部獨立審查（`gpt-6.1-sol` high）**APPROVE**。
- 最終驗證（Claude Code，非沙箱環境）：全套 1550 passed、1 skipped、451 subtests passed。
- 非阻擋限制：未滿 24 小時的 `.tmp` 保留；不同行程交錯寫入可能多出空行（讀取端會略過）；
  尚未以真實 cafe_fansite「播放並開字幕」強制停止驗證（會呼叫付費 API，由使用者操作）。
