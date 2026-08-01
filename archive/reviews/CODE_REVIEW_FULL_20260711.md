# 全專案 Code Review — 2026-07-11

## 修復狀態(2026-07-11 驗證,全測試通過:809 unit + 26 integration)

| 項目 | 狀態 | 備註 |
|------|------|------|
| A1 gap boundary off-by-one | ✅ 已修(R1,第二輪) | gap 分支 lstrip-delta rebase + 多空白測試;punctuation 分支不改——能 carry 的 boundary 必為已失效者,無功能影響 |
| A2 字幕暫停殘留 | ✅ 已修 | `_toggle_translation` 清 `_pending_text`,含測試 |
| A3 dashboard override 驗證 | ✅ 已修 | 逐欄位型別+範圍驗證、font/top-level 驗證,含測試;行為改為「壞欄位丟棄、好欄位保留」 |
| A4 donation OCR profile 脫鉤 | ✅ 已修(R2,第二輪) | `_donation_ocr_command` 尊重 `use_profile`,含測試;無效 profile 警告仍未加(極低優先) |
| A5 donation 金額去重誤殺 | ✅ 已修 | norm key 保留數字 + 數字不同即視為新斗內,含測試;代價是 OCR 數字抖動可能重複顯示(方向正確) |
| A6 audio_capture demo | ✅ 已修 | |
| A7 假 config 欄位 | ⚠️ 部分修復 | `max_repeat_ratio` 已接進 runtime(新 phrase 重複偵測器);但仍不在 override 白名單,ConfigPanel 改它依然無效;`groq_daily_request_limit` 註解未動 |
| B/C 組 | 未處理 | 皆為可選項 |

**修復中順手做的額外修正(review 未列,已驗證正確):**
- scene_context GDI handle leak:`SelectObject` 還原後才 `DeleteObject`——原程式每次擷取洩漏一個 bitmap handle,長跑會耗盡 GDI 配額,是真 bug,修得對。
- audio_capture VAD worker thread 例外現在會 `stop_event.set()`(含測試)。
- `uptime_seconds` → `unix_timestamp_seconds` 跨 Rust/Vue/測試改名(命名誠實化)。

**新引入的迴歸風險(建議再改一行):**
- ~~`stt_policy.is_hallucinated` 新偵測器把字數下限從 6 降到 4~~
  → **已修(R3,第二輪)**:下限恢復 6,並新增 4 詞強調語
  (「okay okay okay okay」)的通過測試。

## 剩餘待修清單(第二輪,皆為一兩行改動)

> 2026-07-11 第二輪驗證:R1/R2/R3 已修,838 tests + 156 subtests 全過。
> R1 只改了 gap 分支——經再推導可接受:punctuation 分支能 carry 的 gap boundary
> 必然是已 drift 失效的(有效者必被選為切點),偏移對其無功能影響。
> R4(可選)未處理,維持 open。

- [x] **R1. sentence_buffer punctuation 分支殘留偏移**
  `modules/sentence_buffer.py` `_split_prefix_with_reason` 末段。
  兩分支統一改為:
  ```python
  raw = text[last + 1:]
  residual = raw.lstrip()
  shift = last + 1 + (len(raw) - len(residual))
  return prefix, residual, reason, shift
  ```
  (gap 分支的 prefix 仍取 `text[:last].strip()`,punctuation 分支取 `text[:last+1].strip()`。)
  影響低但一次修乾淨,drift metric 從此只反映真正的 STT 文字 drift。

- [x] **R2. donation OCR 面板在 use_profile=False 時會重新啟用 profile**
  `main.py`(--donation-ocr 啟動段)改傳:
  ```python
  "--profile",
  cfg.active_streamer_profile if cfg.translation.use_profile else "",
  ```
  面板端 `bool(args.profile)` 已支援空字串停用,只需改 main.py 這一處。
  (可選)`donation_ocr/app.py` 對 `canonical_profile_id` 回空的無效 profile 印警告。

- [x] **R3. is_hallucinated 迴歸:恢復字數下限 6**
  `modules/stt_policy.py`:`if len(words) >= 4:` → `if len(words) >= 6:`。
  理由:4–5 詞的雙詞片語重複是韓語常見強調/重述(「아 진짜 아 진짜」),
  在 STT 層整句丟棄等於字幕直接消失;真正的 Whisper 幻覺迴圈幾乎都更長。
  現有測試(8 詞)不受影響。

- [ ] **R4.(可選)max_repeat_ratio 的 UI 一致性**
  已接進 runtime,但不在 `_DASHBOARD_OVERRIDE_FIELDS` 白名單,ConfigPanel 改它無效。
  二選一:加入白名單(需同時在 `_dashboard_value_is_valid` 加範圍檢查,建議 0.3–1.0),
  或把 ConfigPanel.vue 的該欄位改為唯讀顯示。
  同場加映:`config.py` 的 `groq_daily_request_limit` 註解補「report-only,
  runtime 不強制」。

---

> 範圍:核心 runtime 全部逐行審閱(main.py、config.py、modules/ 9 個模組、utils/ 9 個模組、
> donation_ocr/app.py,約 8,500 行);scripts/、tests/、src-tauri、src-frontend 僅做交叉引用查證。
> 本文件為本地 review 紀錄,**不 commit / 不 push**(同 OPTIMIZATION_*.md 慣例)。

## 總評

整體品質高:pipeline 併發設計(M1 鎖分離、seq 順序輸出、synthetic failed completion 防
emit loop 卡死、shutdown 兩段 drain)、config `__post_init__` 驗證、runtime events 可觀測性、
針對歷史 bug 的 L1–L15 / M1–M7 註解都很扎實。**未發現會讓主管線在正常情況下掛掉的重大 bug。**
以下依嚴重度排序。

---

## A. 正確性問題(建議修)

### A1. sentence_buffer:residual carry-back 後 gap boundary off-by-one 【高】

- 位置:`modules/sentence_buffer.py:79-81`(gap 分支 `residual = text[last:].strip()`)、
  punctuation 分支 `text[last+1:].strip()` 同理;重算 boundary 在 `:302-306`。
- 問題:`residual` 的 `.strip()` 會剝掉開頭空白,但殘留 gap boundary 只做
  `boundary - boundary_index`,沒扣掉被 strip 的字元數。**每次 forced_prefix 切割後,
  carry-back buffer 裡所有 gap boundary 往右偏 1**。
- 後果:下一輪 `_split_prefix_with_reason` 檢查 `text[boundary].isspace()` 失敗,
  boundary 被當成 drift 靜默丟棄——`sentence.gap_boundary_drifted` metric 記到的其實是
  自家 off-by-one,不是 STT 文字 drift,metric 反而掩蓋 bug。
- 修法:計算 residual 時先記錄 `lstrip` 掉的長度,重算 boundary 一併平移。
- **修復附註(0711)**:gap 分支已改為 `text[last+1:].lstrip()` + boundary_index=last+1,正確。
  punctuation 分支(`forced_prefix`)的 `residual = text[last+1:].strip()` 仍未平移
  被 strip 的前導空白。實際影響低:走到該分支時,大於 boundary 的 gap boundary
  必然已是 drift 失效的(否則會被選為切點),偏移只可能讓失效 boundary 偶然落到
  別的空白上、在錯誤但仍為詞界的位置切割。統一修法:
  `raw = text[last+1:]; residual = raw.lstrip(); shift = last+1 + (len(raw)-len(residual))`
  兩個分支共用。

### A2. 字幕視窗:暫停/恢復後顯示過期字幕 【中】

- 位置:`modules/subtitle_display.py:80-93`(`_toggle_translation`)、`:137-141`(`_poll`)。
- 問題:空白鍵暫停時 drain 了所有 queue,但**沒清 `self._pending_text`**。
  暫停瞬間若有字幕正在 min-display 等待中,恢復後會把這條暫停前的舊字幕顯示出來。
- 修法:`_toggle_translation` 兩個分支都加 `self._pending_text = None`。

### A3. Dashboard override 白名單欄位缺型別/值驗證,壞值延遲爆炸 【中】

- 位置:`config.py:382-425`(`_apply_dashboard_overrides`)。
- 問題:防線靠 `replace()` 觸發 `__post_init__`,但白名單所屬的 `_Audio`、`_STT`、
  `_Subtitle` **沒有** `__post_init__`,dataclass 也不檢查型別。例:
  - JSON `subtitle.alpha` 是字串 → 一路通過,到 `subtitle_display.py:39`
    `root.attributes("-alpha", ...)` 才以 TclError 炸掉字幕視窗;
  - `stt.primary_engine` 拼錯 → 誤走 SenseVoice 載入路徑(可恢復但啟動劣化)。
- docstring 宣稱「malformed JSON never breaks startup」目前只對半成立。
- 修法:套用 override 時對每個欄位做 `isinstance(value, type(getattr(base_section, name)))`
  檢查,不符即整體 fallback 回 base。

### A4. donation OCR 面板 profile 與主管線脫鉤 【中】

- 位置:`main.py:194`(啟動子行程不帶參數)、`donation_ocr/app.py:601`
  (`--profile` 預設寫死 `isegye_lilpa`)。
- 問題:主程式切到其他 streamer profile 時,斗內面板仍套 lilpa 的 few-shot/糾錯規則。
  以本專案 profile 汙染隔離的標準,這是實際風險。
- 另:`--profile` 傳入無效值時 `canonical_profile_id` 靜默回 `""`,不報錯。
- 修法:main.py 啟動子行程時把 `cfg.translation.streamer_profile` 以
  `--profile` 傳入;app.py 對無效 profile 顯式警告。
- **修復附註(0711)**:main.py 已傳 `--profile cfg.active_streamer_profile`,
  app.py 的 `--profile` 預設改為 None(沿用 config)、空字串會停用 profile,
  機制正確。剩一個邊角:`active_streamer_profile` 不看 `use_profile`,
  所以主程式 `use_profile=False` 時仍傳出 profile id,面板端 `bool(args.profile)`
  會把 profile 重新打開。建議 main.py 改傳
  `cfg.active_streamer_profile if cfg.translation.use_profile else ""`。
  另外 app.py 對無效 profile 仍是靜默回 general(未加警告)。

### A5. donation OCR 去重吃掉「同文不同金額」的斗內 【中,與 0705 實測 bug 高度相關】

- 位置:`donation_ocr/app.py:212-215`(`norm_text` 剝掉所有數字)、
  `:227-241`(`RecencyCache._same` 的 containment 規則)。
- 問題一:數字被剝掉後,**180 秒內同一觀眾同樣訊息、不同金額的第二筆斗內
  (「…별풍선 100개」→「…별풍선 500개」)去重 key 完全相同,直接不顯示**。
- 問題二:containment 規則(短字串 80% 被包含即視為同一條)可能把
  「舊訊息 + 新增內容」的新斗內誤殺。
- 若實測症狀是「有些斗內沒翻出來」,這兩處是首要嫌疑。
- 修法方向(待討論):norm key 保留金額數字(只正規化 OCR 抖動格式,如
  去 `!`、全形轉半形),或對「數字不同」的相似文字放行。

### A6. `audio_capture.py` 的 `__main__` demo 已壞 【低,demo-only】

- 位置:`modules/audio_capture.py:549-554`。
- 問題:queue 裡現在裝 `AudioChunk`,`_rms(c)` 會因 `AudioChunk ** 2` 不支援而 TypeError。
- 修法:改 `_rms(c.audio)`、`len(c.audio)`。

### A7. 兩個 config 欄位是「假的」 【低】

- `cfg.stt.max_repeat_ratio`(`config.py:102`):runtime 完全沒用——
  `stt_policy.is_hallucinated`(`:80-86`)用寫死的「前半句重複」檢查。
  但此欄位匯出到 Tauri 且出現在 ConfigPanel.vue,**使用者調整是 no-op**
  (也不在 override 白名單,改了也回不到 Python)。要嘛接上,要嘛從 UI 拿掉。
- `cfg.stt.groq_daily_request_limit`:只有 `scripts/analyze_runtime_events.py` 報表使用,
  runtime 無額度熔斷。建議 config 註解標明 report-only。

---

## B. 次要問題 / 觀察(可不修,先知道)

- **B1. quality-retry 的 token 歸帳遺失**(`modules/translator.py:940`):
  直接呼叫 `alternate.translate()` 覆寫 thread-local token usage/diagnostics;
  retry 被拒時主引擎 usage 因 engine 不匹配被丟棄,retry 呼叫本身的成本也沒記錄。
  發生率 ~0.2%,成本報表會少算。
- **B2. `classify_error` 子字串比對可能誤判**(`utils/api_retry.py:19-31`):
  `"500" in msg` 等規則對含數字的例外訊息有誤判空間。現況風險低,列為已知限制。
- **B3. STT skipped 事件 `key_role` 永遠 "none"**(`modules/stt.py:414-416`):
  音量門檻 skip 路徑上 key_role 尚未指派。分析 skipped 事件別依賴此欄位。
- **B4. scene_context 找不到目標視窗時每 20 秒刷一條 warning**
  (`modules/scene_context.py:174` raise → `:284` catch):下播後 log 刷版,可做同因去重。
- **B5. Google Translate key 在 URL query string**:該 API 標準用法,錯誤訊息已遮罩
  (`modules/translation_engines.py:652`);`.env` 未被 git 追蹤,已確認。可接受。

---

## C. 清理 / 簡化建議(不影響行為)

- **C1. `_groq_system_prompt` 與 `_openrouter_system_prompt` 逐字相同**
  (`modules/translation_engines.py:375-396`)——旁邊註解正在講「兩份手寫近似複本
  無故 drift 的病」,自己就是一例。合併成單一參數化 helper。
- **C2. Groq 413 重試複製了整段 request 組裝**
  (`translation_engines.py:1285-1307` vs `:1229-1254`),抽 `_build_request(messages,
  max_tokens)` 可砍約 40 行。
- **C3. fallback probe 遺留物**:`call_with_fallback` 的 `probe_every` 參數
  (`modules/translation_runtime.py:87`)函式體內未使用;`translator.py` 的
  `_FALLBACK_PROBE_EVERY` 與 `_probe_counter` property(`:594-600`)同屬
  user-path probe 廢止後的遺跡。
- **C4. TranslationMemory 舊版合併方法疑似只剩測試在用**:
  `lookup_existing(_event)`、`record_direct`、`record_success`、`invalidate`
  (`modules/translation_memory.py:46-82, 166-235`)——production 已全走 M1 拆分版。
  確認測試後可刪,避免兩套語義並存。
- **C5. `_ACTIONABLE_QUALITY_RETRY_FLAGS` 空集合**(`modules/translator.py:68`):
  `actionable` 永為 False,屬刻意保留,建議常數旁標註 "currently empty by design"。

---

## D. 做得好的地方(保持)

- translator worker pool + seq 順序輸出:malformed item → synthetic failed outcome 防
  gap 卡死、`_MAX_COMPLETED_BACKLOG` 監控 emit stall、shutdown 帶 deadline drain。
- audio_capture 把 Silero 推理移出 sounddevice callback(100ms 預算);daemon thread
  例外經 `stop_event` 上浮,避免靜默死亡。
- profiles/corrections/slang 全 JSON 化 + 載入期 schema 驗證,消滅手寫複本 drift。
- 測試面極廣(60+ test files),含 dashboard override contract、config export 測試。

---

## 建議處理順序

1. **A1、A2**:小改動、高確定性,可直接修。
2. **A4、A5**:與 donation OCR 待討論的實測 bug 綁在一起談。
3. **A3、A7**:dashboard/config 契約收斂,一次處理。
4. C 組清理可搭任一次順手做。
