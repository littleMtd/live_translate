# 直播畫面翻譯 — 閘門 0/1/2 偵察腳本規格（給 Codex 實作）

- 日期：2026-06-30
- 角色分工：Claude=規格/發想；Codex=實作；User=決定並發 prompt。
- 狀態：**判死用的拋棄式偵察腳本**，不是功能本體。目的：用最便宜、最自動的測量，盡早判定「直播畫面翻譯」該不該繼續。
- 版控：本地保留，`*.md` 已被 `.git/info/exclude` 自動排除，不可 push。
- 上游脈絡：見 `SCREEN_OCR_BRAINSTORM_20260630.md` §7.4（收斂結論：先跑能自動判死的便宜測量，人工標註放最後且最小化）。

---

## 0. 核心原則（先讀，決定整份規格的形狀）

- **閘門只能「判死」，不能「確認」。** 任一閘門數字過低 → 結論可以是「不做 / 只當 context」，流程停止。沒有任何閘門能證明「值得做」；那要靠後面更貴的人工判斷。
- **按順序跑，前閘判死就不跑後閘。** 越前面越便宜、越自動。
- **這些是拋棄式腳本（throwaway / scout）**，放 `scripts/` 或 `scratch/`，不接 production pipeline、不建 service、不做 ROI editor、不做 overlay。可醜、可硬編路徑、可一次性。
- **不要為了這份規格去改 `modules/` 任何 production code。** 只讀既有函式（OCR 自己裝、STT/transcript 可重用既有 dump）。

---

## 1. User 要提供的唯一輸入（刻意最小化）

- **1 段錄影**（含畫面 + 聲音），取自 1–2 位代表性主播的代表性片段；10–20 分鐘即足夠做第一輪判讀。可以是螢幕錄影、VOD 片段、或既有 clip 檔。
  - 一個檔同時給出兩邊資料：抽 video frame → 餵 OCR（閘 1/2）；抽 audio → 餵既有 STT 得到帶時間戳的 transcript（閘 2）。
- **可選**：User 用一句話粗略指出幾個 ROI 矩形（例如「上方中央 = alert」「右側 = 聊天」）。**若懶得指，就跳過**，閘 1 先用全幀 + OCR 自帶 bbox 當區域。
- User 不需要事先標任何 label。人工只在閘 1 末尾花約 5 分鐘做一次極小 eyeball（見 §2.4）。

---

## 2. 閘 1 — 引擎能不能讀（OCR extractability）

**問題**：候選 OCR 引擎，對直播畫面的韓文，到底抽不抽得出可用文字？抽不出 → 功能當場死，後面全部不用跑。

### 2.1 輸入
- §1 的錄影抽出的 frame（建議每 1–2 秒抽 1 張，或在「畫面有變」時抽；先簡單均勻抽即可）。
- 引擎集合：至少 `Tesseract`（已裝）與 `PaddleOCR`；可選一個 vision-OCR 作對照。**vision-LLM 一步出譯文不在此測**（只測「抽文字」）。

### 2.2 流程
1. 每張 frame 跑每個引擎，取得 text boxes：`(text, bbox, confidence)`。
2. 不要求精準 ROI；PaddleOCR 自帶 bbox，Tesseract 用其 word/line conf。
3. 依 confidence 門檻（先設一個保守值，可調）分「高信心 box / 低信心 box」。

### 2.3 自動輸出（零主觀）
每引擎一列：
- frame 數、每 frame 平均偵測 box 數
- 高信心 box 比例、confidence 分佈（min/median/p90）
- 偵測到的文字平均長度、含韓文字元比例（用 Unicode Hangul 範圍粗判，過濾純符號/雜訊）

### 2.4 一次性極小 eyeball（不可省，但很小）
- 為什麼需要：confidence 會說謊，純自動數字不能證明「讀對」。
- 怎麼做：腳本隨機抽 **約 20 組** `(frame 裁切圖, 該引擎 OCR 文字)`，輸出成一頁可看的對照（並排圖+文字）。User 對每組按 `讀對 / 半對 / 雜訊`，約 5 分鐘。
- 這跟音訊 QE 那種整場標註**性質不同**：固定 ~20 組、一次、不分場。

### 2.5 判死邏輯（門檻是形狀，數字待看數據再定）
- 若**最好的引擎**在高信心 box 上的 eyeball「讀對率」都偏低（雜訊為主）→ **判死**：OCR 抽不出可用韓文，功能不成立，停止。
- 若某引擎讀對率明顯可用 → 記下「閘 1 winner 引擎 + 其 confidence 門檻」，進閘 2。
- 不要在規格裡釘死「幾 % 算過」；先把數字量出來，門檻交給 User 看表決定。

---

## 3. 閘 2 — OCR 有沒有獨佔資訊（exclusivity proxy）

**問題**：OCR 抽到的文字，有多少是音訊 transcript **已經涵蓋**的（主播念出來了）、多少是音訊完全沒碰的（OCR 獨佔）？獨佔率近 0 → 音訊已覆蓋 → OCR 當 TARGET 沒價值 → 判死或只當 context。

### 3.1 前置依賴（重要）
- **必須先過閘 1，且只用「閘 1 winner 引擎 + 高信心 box」。** 否則 OCR 雜訊會被誤算成「不在 transcript 裡 = 獨佔」，把獨佔率灌高，結論失真。這條請在程式裡強制（低信心 box 不進閘 2）。

### 3.2 輸入
- 同一段錄影：
  - OCR 側：閘 1 winner 引擎逐 frame 抽文字 → **跨幀 dedup**（同一塊文字持續出現算一個 event，記 first_seen / last_seen 時間戳、dwell 時長）。
  - 音訊側：該段 audio 餵既有 STT（可重用 `LIVE_TRANSLATE_DUMP_AUDIO` 路徑或既有 transcript dump），得到帶時間戳的 transcript 片段。

### 3.3 比對流程
1. 對每個 OCR text event，在音訊 transcript 中找 **±N 秒**（先設個值，可調）內是否有 fuzzy match。
2. 比對前兩邊都做輕量正規化（去空白/標點、大小寫、可選簡單同義）；相似度用 token overlap 或 Levenshtein ratio，門檻可調。**不需要動 production 的名稱正規化**，這裡用簡版即可。
3. 分類每個 OCR event：
   - `SPOKEN`：±N 秒內音訊有 match（音訊已覆蓋）
   - `EXCLUSIVE`：無 match（OCR 獨佔）

### 3.4 輸出
- **彙總**：`exclusivity_rate = EXCLUSIVE / total_ocr_events`，並按可得維度拆（若有 ROI 標籤就按區域；否則按文字長度桶 / 是否含數字金額 等粗分類）。
- **明細 JSONL**：每個 OCR event 一行 `{first_seen, last_seen, dwell_s, region?, ocr_text, confidence, classification, matched_transcript_snippet?}`。
- 這份 `EXCLUSIVE` 子集 = 之後人工判「值不值得懂」(閘 3) 的**預篩候選清單**，所以閘 2 跑完，人要看的東西已經被機器縮到一小撮。

### 3.5 判死邏輯（形狀）
- 獨佔率近 0 → **判死 / 降級為 context-only**：音訊已覆蓋，OCR 當 TARGET 無增量。
- 獨佔率可觀 → 不代表值得做（獨佔不等於有價值，例：洗版斗內）；**只代表有資格進閘 3**（對 EXCLUSIVE 子集做最小人工判讀，那是另開的事）。
- 同樣不釘死門檻，先出數字。

---

## 4. Codex 交付物
- `scripts/`（或 `scratch/`）下兩支可獨立跑的腳本：`gate1_ocr_scout.py`、`gate2_exclusivity.py`。
- 閘 1：§2.3 的引擎對照表 + §2.4 的 20 組 eyeball 對照頁。
- 閘 2：§3.4 的彙總數字 + 明細 JSONL。
- 一頁 readout：每閘的數字、是否判死、若過則記下 winner 引擎/門檻設定。**不要替 User 下「值得做」結論**——閘門只判死。
- 不改 `modules/` production code；新增檔皆 `*.md` 以外的拋棄式腳本/輸出，放在不污染既有測試套件的位置。

## 5. 非目標（這次明確不做）
- 不建 service、不定 production event schema、不做 ROI editor、不做 overlay、不接 Tauri。
- 不做跨模態 dedup 的 production 版（閘 2 的比對是離線一次性量測，不是 runtime 機制）。
- 不選定最終引擎（閘 1 只挑出「能往下走的 winner」，正式選型是後話）。

---
*下一步 prompt 由 User 發給 Codex；本檔為規格，非執行指令。*
