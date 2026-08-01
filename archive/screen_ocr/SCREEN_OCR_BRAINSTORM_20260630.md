# 直播畫面翻譯（OCR）— 多人發想文件

- 日期：2026-06-30
- 狀態：**Tier 4 發想（discussion only，NOT implementation-ready）**。促進升級前不可施工。
- 版控：**本地保留，永不 git add / commit / push**（review/發想類 md，比照 OPTIMIZATION_*.md / ARCHITECTURE_REVIEW*.md）。
- 參與者：User、Claude、Codex（多模型獨立發想再對照）。

---

## 0. 這份文件怎麼用（給所有參與者，先讀）

這是 **open 發想**，不是 plan sign-off，也不是 bug 診斷。目的是讓多個模型**各自獨立**對同一組事實發想，再把分歧攤開。

對 Codex 的請求（刻意壓 framing bias，請照順序）：
1. 先讀 §1（共享事實）+ §2（goal / non-goals）+ §3（開放設計軸）。
2. **在讀 §4 之前**，先到 §5 對每個軸寫出你自己的判斷。§3 的軸刻意不給「正確答案」，請自己推。
3. 寫完再讀 §4（Claude 立場）。然後在 §5 末尾**明確標出**：你和 §4 哪裡分歧、§4 有哪些主張你認為證據不足、以及 §1–§4 整份**都沒談到**的東西。
4. §4 是「一種立場」，不是這份文件的前提，也不是要你蓋章的結論。分歧愈具體愈有價值。

對 User / Claude 的自律：§4 寫成「主張 + 證據指標 + 信心」，不寫說服性散文；§3 各軸不偷渡 §4 的選擇；§5 的題目不點名 §4 的假設。

---

## 1. 共享事實（evidence，標來源，請自行驗證）

> 路徑皆相對於 `live_translate/`，行號為 2026-06-30 當下，可能漂移，以實檔為準。

1. **translator 公開入口是「純文字形狀」，不是 event 形狀。**
   - `modules/translator.py:598` `def translate(self, text: str, incomplete: bool=False)`；`:601` `def translate_event(self, text, incomplete) -> TranslationOutcome`。
   - `SentenceEvent`（`modules/pipeline_events.py:116`）那一批 `avg_logprob` / `no_speech_prob` / `audio_seconds` / `utterance_id` / `vad_cut_reason` 是**音訊專屬**，由上游 `sentence_buffer` / labeling 消費，translator 本體不碰。
   - 推論（非事實）：非音訊來源要餵 translator，只需提供 `text`（+ 一個 profile/glossary 脈絡），不需偽造音訊欄位。

2. **翻譯後端有「行程內單例」。**
   - `modules/translator.py:364` `_HISTORY_WRITE_LOCK = threading.Lock()`（module-global）。
   - `:442` `_new_translator_shared_state()` 建 cache / memory / policy / fallback；`:428` `max_cache_size=_CACHE_MAX_SIZE`（500）。Translator 實例持有這份 shared_state。
   - DB 是磁碟檔（`db_factory=_get_db`、`history_writer=_write_history` 皆 module-level）。
   - 推論（非事實）：第二個**行程**各自建 Translator → 各自一份 in-memory cache（互看不到 hit）+ 各自一條 SQLite 連線寫同一檔。

3. **AGENTS.md 現行官方立場 = context-only。**
   - `AGENTS.md:328-343`「Future outlook: OCR screen context」：OCR 當「非語音 context」、不當翻譯來源；偏好固定 ROI、加 de-dup 與短時間窗；prompt 要明標為 screen context 且叫模型別翻除非語音有指涉；runtime events 要分開記 OCR context 以便事後判斷幫忙或污染。

4. **已開但未決的設計討論串。**
   - `AGENTS.md:345-362`「Screen-translation design thread (2026-06-25, Tier 4)」：傾向 capture+OCR 為獨立 project/worktree、emit 既有 translator input、共享後端不 fork；**未決**：螢幕文字是 TARGET（自己出字幕）還是 CONTEXT；**待解張力**：主播念出畫面文字時音訊已翻，TARGET 模式須對音訊 dedup（forced_prefix 的視覺版）；環境已裝 Tesseract。

5. **既有的「先量再投」紀律（來自專案 QE 線，記憶 `project_live_translate_qe_labeling`）。**
   - 音訊線結論：translation engine 非戰場、detectable bad=7、瓶頸在 STT/speaker overlap；**量出指標前不啟動 resolver/multi-STT 等重投資**。本發想預設沿用同一把尺。

---

## 2. Goal / Non-goals（裸宣告）

**Goal**
- 釐清「直播畫面上的文字翻譯」這個附屬功能：值不值得做、做成什麼形狀、與既有音訊翻譯後端如何共存。
- 產出可被多模型獨立挑戰的設計軸與開放問題，而非定案。

**Non-goals（這次刻意不做）**
- 不在這份文件定案任何實作、不排期、不改任何 code。
- 不選定唯一架構或唯一 OCR 引擎。
- 不解決音訊側既有的 speaker overlap / wrong-speaker STT（那是另一條線）。
- 不假設一定要做：「結論是不值得做 / 只當 context」也是合法產出。

---

## 3. 開放設計軸（無預設答案；選項空間，不標偏好）

> 每軸只列「問題 + 已知選項空間 + 已知約束」。哪個對，留給 §5 各自獨立判。

**A. 共享後端的「共享」是哪一種結構？**
- 選項空間：(i) OCR 端 import live_translate 模組當 library；(ii) translator 跑成本地 service / IPC，OCR 走小協定；(iii) 抽出共用 core 套件兩邊都依賴。
- 已知約束：§1.2 的單例與磁碟 DB；「獨立 worktree 不污染既有測試套件」的初衷；translator.py 尚未完成既有重構（1196 行未拆）。

**B. capture+OCR 的 pipeline 形狀？**
- 選項空間：要不要鏡像音訊管線（capture→閘門→OCR→dedup→translator→display）；幀間差分擋在 OCR 前 vs 後；dedup 放在像素層、文字層、還是翻譯 cache 層。
- 已知約束：OCR 比音訊逐幀貴（GPU）；「沒變的畫面」是 OCR 版的「靜音」。

**C. OCR 引擎選型（韓文 + 直播畫面）？**
- 選項空間：Tesseract（已裝）/ PaddleOCR / vision-LLM / 多引擎。
- 已知約束：直播畫面是半透明 overlay、描邊、動態底；名稱正規化與 glossary 在後端（채나/이파리 規則）；§1.1 翻譯入口吃文字。
- 開放問題：引擎該只產文字、還是可一步出譯文？對「共享後端」有何後果？

**D. ROI（感興趣區域）策略？**
- 選項空間：固定使用者設定 ROI / 自動偵測 / 混合。
- 已知約束：不同區域類型（alert、遊戲 UI、tier list、clip 硬字幕、聊天 overlay）價值與穩定度不同；AGENTS.md 現偏好固定 ROI 起步。

**E. 顯示與標記？**
- 選項空間：獨立 overlay / 併入音訊字幕 / 側欄；以及來源標記（modality）放不放、怎麼放。
- 已知約束：§1.3 要求 runtime events 分開記 OCR，以便事後判斷幫忙或污染。
- 開放問題：什麼樣的標記能讓事後 labeling **量得出** OCR 是幫忙還是污染？

**F. 螢幕文字是 TARGET 還是 CONTEXT？以及這功能的價值上限由什麼決定、值不值得做？**
- 選項空間：全域 TARGET / 全域 CONTEXT / 視情況而定。
- 已知約束：§1.4 的未決張力（念出 → 音訊已翻 → 重複）；§1.5 的「先量再投」紀律。
- 開放問題：用什麼**可量指標**界定這功能的價值上限？在量到之前，重投資（自動 ROI、跨模態 dedup、全螢幕 vision-LLM）算不算下注？

---

## 4. Claude 立場（**一種**立場，待挑戰，非文件前提）

> 格式：主張 — 證據指標 — 信心。請 Codex 在 §5 先獨立作答後再讀此節，並標出分歧。

- **A**：傾向 service 邊界（§3.A-ii）。理由指標：§1.2 的單例＋磁碟 DB 使「library import 在另一行程」會 cache 分裂＋寫競爭；service 邊界讓音訊與 OCR 共用同一份翻譯 cache。協定面：§1.1 入口已是文字，協定可小至 `{text, profile_id, modality}`。信心：中高（單例是事實，選 service 是判斷）。
- **B**：傾向鏡像音訊管線，且把「幀間差分」擋在 OCR **之前**（OCR 是貴的那步），文字層時間窗 dedup 擺第二道。信心：中。
- **C**：任何引擎**只負責產文字，翻譯一律回後端**；vision-LLM 一步出譯文會繞過 glossary/profile/名稱正規化/cache，違背共享目的，故只能當「會 OCR 的引擎」。Tesseract 對直播韓文大概率不堪用，PaddleOCR 較可能。信心：產文字回後端=高；引擎優劣=中低（未實測）。
- **F-反 framing 注意**：以下是我較強的個人論點，**特別需要被獨立檢驗**——
  - **F1**：把 target/context 當「全域開關」是錯的框；應**按區域**判，判準是「主播會不會念出來」。會念的（tier list/文章）走 CONTEXT，本就不出第二條字幕→張力消失；不會念的（alert/遊戲 UI）才 TARGET。信心：中。
  - **F2**：這功能的價值上限**不是工程上限而是內容結構上限**——「OCR 能獨佔的文字」和「值得翻的文字」反相關（有價值的多半被念出來、音訊已覆蓋；OCR 獨佔的多半低資訊量）。信心：中低（這是論點不是測量）。
  - **F3**：唯一結構性高價值缺口是「主播有反應、但沒念出來的畫面事件」（看他笑卻不知在笑什麼）。信心：中低。
  - **F4**：照 §1.5 紀律，上限現在是空白；該先跑一場 labeled session 量「我因『沒念出來的畫面文字』而漏掉理解的時刻數」，再決定升級與否。信心：中（方法論主張）。

---

## 5. 給 Codex 的位置（請**先獨立作答**，再讀 §4）

不要預設 §4 正確。請用 §1 的證據自行推導，可標來源的合理推測允許列為 hypothesis（不可當定論）。

- **Q1（獨立推導，勿引用 §4）**：只憑 §1 的事實，你認為這個功能的「價值上限」由什麼決定？用什麼**可量指標**能在動工前界定它？你的初判是值得做、值得但限定形狀、還是不值得？給依據。
- **Q2**：一個最直覺的實作（capture→OCR→翻譯→overlay）會撞上的**最深設計缺陷**是什麼？為什麼一行修復解不掉？（引用 §1 或一般原理，標來源）
- **Q3（專逼跳框）**：列出 §1–§4 **完全沒談到**、但你認為會左右成敗的考量（例如但不限於：延遲預算、隱私/螢幕擷取權限、版權/ToS、多顯示器、效能與音訊管線搶資源、失敗模式與降級）。
- **Q4（讀 §4 後才填）**：逐條標你和 §4 的分歧——哪些主張你認為證據不足、哪些信心被高估/低估、A 與 F1–F4 你各自的獨立結論為何。判準請用「§4 哪個有用貢獻會被你的替代方案摧毀」，不要為反對而反對。

---

### 附：協作守則（比照既有 cross-review 紀律）
- 本檔為發想，非 sign-off；任何升級為實作須走標準流程並更新 Tier 2 執行計畫，不得直接由本檔施工。
- Codex 寫入後，其原文段落不被回頭改寫；Claude 評 Codex 回覆須指出「具體哪些有用貢獻會被某版摧毀」，不得蓋章。
- 下一步 prompt 由 User 發出。

---

## 6. Codex 獨立回答

### 6.0 閱讀順序與來源驗證
- 順序紀錄：我先讀 §0、§1、§2、§3 與 §5 問題，並先形成 Q1-Q3 判斷；之後才讀 §4 並回答 Q4。
- §1.1 已驗證：`modules/translator.py:598-601` 的公開入口是 `translate(text, incomplete)` / `translate_event(text, incomplete)`；`modules/pipeline_events.py:115-135` 的 `SentenceEvent` 帶的是音訊/STT metadata。來源類型：code。
- §1.2 已驗證：`modules/translator.py:364` 是 module-level history lock；`modules/translator.py:420-430` 建立 translation memory/cache/DB/history writer；`modules/translator.py:442-448` 建 shared state；`modules/translator.py:537-545` 讓 `Translator` 實例持有 shared state。來源類型：code。
- §1.3/§1.4 已驗證：`AGENTS.md:328-343` 是 context-only outlook；`AGENTS.md:345-362` 是 Tier 4 screen-translation design thread，明示 TARGET vs CONTEXT 未決、須 dedup、Tesseract 已裝。來源類型：project memory，仍需 runtime/code 驗證後才能升級為實作依據。
- 補充漂移：`modules/translator.py` 目前是 1256 行，不是 §3 記的 1196 行；這不改變「尚未小型化成乾淨 library 邊界」這個約束，但行號已漂移。來源類型：code audit。

### Q1. 價值上限與可量指標
我的獨立結論：值得做的是「有限形狀的探索」，不是全螢幕 TARGET 翻譯。價值上限由「OCR 能提供多少音訊管線沒有提供、且觀眾真的需要的資訊」決定；在量到這件事之前，自動 ROI、跨模態 dedup、vision-LLM 全螢幕理解都屬於重投資下注。來源類型：一般產品原理；`AGENTS.md:332-340` 已把 OCR 預設成 context-only 並要求獨立記錄幫忙/污染；`AGENTS.md:355-357` 明示主播念出畫面文字時會和音訊重複。

我會把上限拆成四個可量乘數：
- `visual_need_rate`：每小時有多少「觀眾因未翻畫面文字而少理解」的時刻，而且不是音訊翻譯已覆蓋。來源類型：一般原理；§1.4 的 TARGET/CONTEXT 張力。
- `extractable_text_rate`：候選 ROI 裡的韓文能否被 OCR 穩定抽成可翻文字。因 translator 入口只吃文字（`modules/translator.py:598-601`），OCR 失準會直接限制後端價值。來源類型：code + 一般 OCR 原理。
- `timely_delivery_rate`：OCR+翻譯能否在文字仍可見、仍相關時出現。直播畫面文字是有 dwell time 的狀態，不是已切好的句子事件。來源類型：一般即時系統原理；hypothesis，需用 sample stream 量。
- `pollution_cost_rate`：重複音訊字幕、誤讀背景字、把 context 當 target、或 stale overlay 造成的干擾頻率。來源類型：`AGENTS.md:339-340` 要求分開記錄以判斷幫忙或污染；一般 UX 原理。

動工前可先用低成本標註界定上限：
- 抽 1-2 場 representative stream，不寫整合 code，只人工或用現有工具截圖/切片；標出 ROI 類型、可見韓文、是否被主播在前後 N 秒念出、是否影響理解、理想顯示模式（TARGET/CONTEXT/SUPPRESS）。
- 對同一批 frame 跑候選 OCR 的離線可用率：字元/詞錯率、可翻譯率、需要手修比例、穩定 dwell time。這是 engine scouting，不等於架構實作。
- 產出 `net_helpful_visual_events_per_hour = helpful_unique_target_or_context_events - duplicate_or_confusing_events`。若這個值低，結論可以是不值得做；若高但集中於 alert/donation/game UI，就只值得做固定 ROI 的 bounded feature。

初判：值得但限定形狀。先把 donation/alert、固定遊戲 UI、clip hard-sub、文章/tier list 這些區域分開量；不應先預設「直播畫面翻譯」是一個全域功能。來源類型：一般產品分層原理；§3.D/F 的 ROI 與 TARGET/CONTEXT 開放軸。

### Q2. 直覺 pipeline 的最深設計缺陷
`capture -> OCR -> translate -> overlay` 的最深缺陷不是少一個 filter，而是把「螢幕狀態流」錯當成「語音句子事件流」。畫面文字有 ROI、bbox、出現/消失時間、穩定度、OCR confidence、與音訊的時間關係；直覺 pipeline 若只把 OCR text 丟進 `translate_event(text)`，這些 provenance 會在進 translator 前消失。來源類型：code，`modules/translator.py:598-601` 只接 text/incomplete；`modules/pipeline_events.py:115-135` 的 metadata 是音訊/STT 語義，不能直接偽造成 OCR 證據。

這一行修不掉，因為至少需要四個邊界決策：
- 事件模型：OCR 事件需要 `modality=ocr`、ROI id、bbox、capture timestamp、stable duration、OCR confidence、dedup key、和 audio-correlation 欄位。現有 translation worker 會把 `profile_id` 從全域 config 寫入 metadata（`modules/translator.py:1008-1024`），不是 request-scoped OCR event schema。來源類型：code。
- 閘門位置：沒變的畫面應在 OCR 前被擋，文字層 dedup 只能當第二道。否則會把 persistent overlay 翻成 repeated subtitles。來源類型：一般系統原理；§3.B 已指出「沒變的畫面」是 OCR 版靜音。
- 跨模態 dedup：主播念出畫面文字時，音訊翻譯與 OCR TARGET 會重複；主播只指涉「這個」時，OCR 又可能是 context。這不是單一 if 可以判定，因為需要時間窗、文字相似度、ROI 類型、以及顯示策略。來源類型：`AGENTS.md:355-357` + 一般原理。
- 可觀測性：若 runtime/log 不能分開記 OCR 幫忙或污染，就無法沿用 §1.5 的「先量再投」紀律。來源類型：`AGENTS.md:339-340`。

因此直覺 pipeline 可以是 prototype 的草圖，但不能是 design boundary。最小正確形狀更像 `capture -> ROI/change gate -> OCR -> visual text state -> cross-modal policy -> translator/backend -> modality-aware display/logging`。

### Q3. §1-§3 未充分覆蓋、會左右成敗的考量
以下是我在未讀 §4 前列出的候選風險；讀完 §4 後，§4 有補到部分 pipeline/engine/target-context，但仍沒有完整處理這些項目。

- 延遲預算與 dwell time：若 P95 OCR+翻譯延遲大於文字可見時間，TARGET 輸出即使正確也無用。來源類型：一般即時系統原理。
- 隱私與螢幕擷取權限：桌面 capture 可能掃到 DM、瀏覽器、會員內容、OBS 預覽或多螢幕；若 vision/OCR 走雲端，風險放大。來源類型：一般安全/隱私原理；hypothesis，需依實作與供應商驗證。
- 版權/平台 ToS：clip hard-sub、遊戲畫面、聊天訊息、donation overlay 的截圖保存與雲端處理可能有不同限制。來源類型：一般法務/平台風險；hypothesis，不作法律定論。
- 多顯示器、DPI scaling、HDR、全螢幕遊戲、視窗遮擋、OBS/game capture source 差異：這些會影響 ROI 座標與 capture 穩定度。來源類型：一般桌面 capture 原理。
- 資源競爭：OCR 可能吃 CPU/GPU/VRAM，與 STT、翻譯 API retry、Tauri overlay、streaming/OBS 搶資源；需要 backpressure 與 degrade mode。來源類型：一般即時系統原理。
- Prompt injection / untrusted screen text：若 OCR 當 LLM context，畫面文字本身是不可信輸入，需明確 delimiter、指令隔離與「不可把 screen text 當 system instruction」。來源類型：一般 LLM 安全原理。
- 語言與內容分類：韓文、日文、英文、繁中、符號、遊戲專有名詞、已翻譯字幕混在同一 ROI 時，需先判定是否該翻；否則會翻已懂內容或污染 glossary。來源類型：一般 OCR/translation 原理。
- 使用者控制面：ROI editor、per-scene profile、hotkey pause、per-region TARGET/CONTEXT/SUPPRESS、confidence display、臨時關閉某區域，會決定這功能是否可用。來源類型：一般 UX 原理。
- 失敗模式與降級：低 confidence、短暫閃字、動態背景、stale translation、重複字幕、overlay 遮住原文，都需要可預期的 suppress/degrade 行為。來源類型：一般 UX/系統原理。
- 評估資料形狀：若要量幫忙或污染，annotation 不能只存 OCR text；需要 frame/ROI/bbox/timestamp、音訊是否覆蓋、display mode、觀眾理解影響。來源類型：一般 evaluation design 原理；呼應 `AGENTS.md:339-340`。
- 引擎授權與部署：Tesseract language data、PaddleOCR dependency、GPU runtime、vision-LLM API 成本/隱私，會改變「可發布」的形狀。來源類型：一般部署原理；hypothesis，需逐引擎驗證。

### Q4. 讀 §4 後的分歧、證據不足點、A/F 獨立結論
我認為 §4 的有用貢獻是：保留共享 translator/profile/cache 的方向、把 OCR 前差分當第一道成本閘、禁止引擎直接繞過後端出譯文、把 TARGET/CONTEXT 改成按區域判、要求先做 labeled session。我的替代方案不應摧毀這些貢獻；主要差異是把「區域/時間/信心/跨模態關係」提升成一等事件，而不是只在 `{text, profile_id, modality}` 外面包一層 service。

**A：共享後端結構**
- 與 §4 重疊：production 若真的要讓 audio 與 OCR 同時工作，我也偏向 service/shared backend，因為 cache/profile/fallback state 在現有 code 裡是行程內 shared state（`modules/translator.py:420-430`, `:442-448`, `:537-545`），另一個行程 import library 會自然分裂 in-memory cache。這是 §4 的有用貢獻，不應被替代方案摧毀。
- 分歧：§4 說協定可小至 `{text, profile_id, modality}`，我認為證據不足。現有公開入口沒有 request-scoped `profile_id` 或 `modality`（`modules/translator.py:598-601`），profile prompt 由全域 `cfg.active_streamer_profile` 組成（`modules/translator.py:903-919`），translation worker metadata 也從全域 cfg 寫 `profile_id`（`modules/translator.py:1008-1024`）。若做 service，協定至少還要處理 ROI、timestamp、confidence、dedup key、source modality、request-scoped profile 或明確禁止 per-request profile。
- 獨立結論：A-ii service 是較合理的 production 方向，但現在只能作為候選結構，不是 sign-off。若只是先量價值上限，離線標註與 engine scouting 不需要先建 service。

**B：pipeline**
- 與 §4 重疊：我同意幀間差分應在 OCR 前，文字層時間窗 dedup 擺第二道。這保留 §4 對成本閘門的貢獻。
- 分歧：我不會稱它「鏡像音訊管線」就結束。音訊是 transient utterance，螢幕是 persistent visual state；OCR 需要 state lifecycle、ROI identity、stable duration、消失事件與 stale cleanup。來源類型：一般系統原理；§3.B 也已暗示沒變畫面等於 OCR 靜音。
- 證據不足：§4 沒處理 display lifecycle 與 logging schema，所以 B 的信心「中」合理但還不能落成架構。

**C：OCR 引擎**
- 與 §4 重疊：我同意「引擎只產文字，翻譯回後端」信心可高。理由是 profile/glossary/cache 都在既有後端，profile prompt 由 translator 組成（`modules/translator.py:903-919`），繞過後端會破壞共享目的。
- 分歧：`Tesseract 對直播韓文大概率不堪用，PaddleOCR 較可能` 是合理 hypothesis，但不是目前證據。沒有 sample frame benchmark 前，這不能變成選型依據。
- 獨立結論：先做 engine scouting matrix：Tesseract/PaddleOCR/vision OCR 都只評估「文字抽取」，直接出譯文的 vision-LLM 只能作為對照，不應成為主線。

**F1：TARGET/CONTEXT 按區域**
- 與 §4 重疊：我同意全域 TARGET/CONTEXT 是錯框，按區域或事件類型判斷是 §4 的有用貢獻。
- 分歧：`主播會不會念出來` 不是充分判準。還要看：文字是否真的影響理解、是否只是 UI noise、是否可 OCR、dwell time 是否足夠、是否會造成 overlay clutter、音訊是否只是 paraphrase、觀眾是否需要逐字內容。
- 獨立結論：mode 應至少是 per-region/per-event 的 `TARGET / CONTEXT / SUPPRESS / DIAGNOSTIC`，而不是只用 TARGET/CONTEXT 二分。

**F2：價值上限是內容結構上限**
- 與 §4 重疊：我同意價值上限主要取決於內容結構，而不只是工程能力；這是 §4 的有用提醒。
- 分歧：`OCR 能獨佔的文字` 與 `值得翻的文字` 反相關，證據不足。Donation/alert、game objective、chat overlay、clip hard-sub、主播看了但沒完整念出的表格或文章，都可能是 OCR 獨佔且有價值。
- 獨立結論：F2 應降成 hypothesis，用 `helpful_unique_visual_events_per_hour` 和 `duplicate_or_polluting_events_per_hour` 量，而不是先採信反相關。

**F3：唯一高價值缺口**
- 分歧最大：`唯一結構性高價值缺口是主播有反應但沒念出來的畫面事件` 太窄，且證據不足。它是一個高價值類別，但不是唯一類別。
- 會被我保留的 §4 貢獻：把「主播反應但觀眾缺少畫面文字」列為 labeling tag，因為這類事件能直接量出 OCR 的補洞價值。
- 獨立結論：F3 應改成多類別上限問題：reaction-without-reading、unread alert/donation、persistent game UI/objective、clip hard-sub、chat/tier/article partial reading。各類別分別量，不先合併成單一論點。

**F4：先 labeled session**
- 與 §4 重疊：我同意先量，且這是目前最穩的下一步；符合 §1.5 的「先量再投」紀律。
- 分歧：`一場 labeled session` 可能不足，除非它是 stratified sample。只量「因沒念出畫面文字而漏理解的時刻數」也不夠；還要量 OCR extractability、dwell/latency、重複音訊率、污染率、以及資源/隱私限制。
- 獨立結論：F4 應升級成「stratified labeled audit + engine scouting」，輸出各 ROI 類型的上限表，再決定是否把某一類 promotion 成 proposal。

**§4 信心評估**
- 我會調高：C 的「翻譯回後端」信心；B 的「OCR 前差分」信心。
- 我會維持：A 的 service 方向信心，但降低「協定很小」的信心。
- 我會調低：C 的具體引擎排序、F2 的反相關論點、F3 的唯一缺口論點。

**§1-§4 仍未完整談到**
- §4 沒完整處理 Q3 的延遲/dwell、privacy/permission、ToS、multi-monitor/DPI/capture compatibility、resource contention、prompt injection、data retention、user controls、failure/degrade、annotation schema。這些不一定推翻 §4，但會決定後續 proposal 能不能實作成可用功能。

---

## 7. Claude 對 §6 的評估（不改寫 §6；判準＝「哪個有用貢獻會被某版摧毀」）

先說處置：§6 不是蓋章型回覆，有一條 code-grounded 的真修正、兩條真深化。我獨立重驗了關鍵 code 主張後逐條裁。

**7.1 已獨立重驗的 code 主張（Codex 對，我認帳）**
- 「協定可小至 `{text, profile_id, modality}`」**證據不足，§4-A 此句撤回**。重驗：`translator.py:915` `_compose_system_prompt()` 從全域 `cfg.active_streamer_profile` + `cfg.translation.use_profile` 組 prompt；`translator.py:280-281`、`:309` 來源正規化同樣讀全域 profile；`translator.py:1019` worker metadata 的 `profile_id` 也來自全域 cfg。→ 今天的入口**不吃 request-scoped profile**，傳 `profile_id` 是 no-op。讓 OCR 帶自己的 profile/modality 是真 plumbing（牽動 prompt_ver 穩定性、來源正規化、metadata），不是包一層 service 就有。來源類型：code 重驗。
- 行數漂移：`translator.py` 為 **1256** 行（§3.A 寫的 1196 已過時）。不改變「未小型化成乾淨 library 邊界」的約束。來源類型：code。
- **對共享結構結論的淨影響**：A-ii service 方向**仍成立**（單例＋磁碟 DB 使跨行程 library import 會 cache 分裂，這點 §4 與 §6 一致），但記憶與 §4 隱含的「emit 既有 input 形狀很便宜」**只在 OCR 沿用音訊同一個全域 profile 時成立**；一旦要 per-source profile/modality，便宜假設破。

**7.2 我會保留、不容替代方案摧毀的 §6 貢獻**
- **Q2「螢幕狀態流 ≠ 語音句子事件流」**：視覺文字有 lifecycle（出現/dwell/消失/stable duration/confidence），音訊是 transient utterance。§4-B「鏡像音訊管線」對此 under-serve，採納 Codex 的深化——最小正確形狀含 visual text state + ROI identity + stale cleanup，不只是把 text 丟進 `translate_event`。
- **mode 從二分擴成 `TARGET / CONTEXT / SUPPRESS / DIAGNOSTIC`（F1 深化）**：採納。「主播會不會念」是必要訊號但非充分；UI noise 要能 SUPPRESS，這是 §4-F1 漏的第三態。
- **Q3 風險清單**：採納為後續 proposal 的 gate，尤其三條 §1-§4 完全沒談、且可能直接否決可用性的——(a) 延遲預算 vs dwell time（P95 延遲 > 文字可見時間則 TARGET 再準也沒用）；(b) prompt injection（螢幕文字當 LLM context 是 untrusted 輸入）；(c) 螢幕擷取的 privacy/ToS。

**7.3 我守住、不全盤折讓的點（亦不對 reviewer 蓋章）**
- **F3「唯一缺口」**：認「唯一」過窄、撤回；但保留一個 taxonomy 區分——reaction-without-reading 與其餘 OCR 獨佔類別**性質不同**：前者是「音訊在場卻無法解釋一個可見事件」（音訊主動失能），後者多是「音訊根本沒碰」（音訊沉默）。這個區分要進 labeling tag，因為兩者證明 OCR 價值的方式不同。所以 F3 不是被 Codex 的「多類別」取代，而是「多類別 + 保留此軸」。
- **F2 反相關**：同意降為 hypothesis、按類別量（與我 §4 已標「論點非測量、需獨立檢驗」一致——這正是反 framing 機制生效：我標弱的點，獨立 reviewer 也獨立判定弱）。

**7.4 收斂結論（雙方一致，且符合 §1.5 紀律）**
- 下一步**不建任何東西**（不建 service、不定 event schema、不選引擎）。
- 下一步是**離線、便宜**的兩件並行：(1) **stratified labeled audit**——抽 representative streams，按 ROI 類型（donation/alert、game UI/objective、tier list/article、clip hard-sub、chat overlay、reaction-without-reading）分層標 helpful-unique vs duplicate/polluting、是否被念、dwell、理想 mode；(2) **engine scouting matrix**——Tesseract/PaddleOCR/vision-OCR 只評「文字抽取率」，vision-LLM 一步出譯文僅作對照、不入主線。
- 產出**各 ROI 類型的上限表**後，才決定是否把某一類 promotion 成正式 proposal 並更新 Tier 2。在那之前自動 ROI、跨模態 dedup、service plumbing 皆屬下注。
- **一個留給 User 的決策**：上面的 labeled audit 要不要真的開跑（誰標、標幾場、用哪幾位主播的場），這是 promotion 的觸發點，依協作守則由 User 決定並發 prompt。
