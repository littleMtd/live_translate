# Code Review — 翻譯邏輯 + Pipeline (2026-06-12)

Review 範圍:
- 翻譯邏輯:`translator.py`, `translation_engines.py`, `translation_policy.py`, `translation_runtime.py`, `translation_memory.py`, `translation_corrections.py`
- Pipeline:`main.py`, `utils/pipeline.py`, `utils/queue_utils.py`, `sentence_splitter.py`, `sentence_buffer.py`, `pipeline_events.py`, `db.py`, `subtitle_display.py`(僅佇列互動)

基準 commit:`ba3fa43`(已含本日稍早修復的四項:soft-fallback engine 歸屬、last_input race、splitter stop flush、translator stop drain)。本文只列**新發現**。

狀態圖例:`[ ]` 待修 / `[x]` 已修 / `[-]` 不修(記錄原因)

---

## 高優先

### [x] H1. `context_window` 設定被忽略,實際每次 API call 都送 30 對歷史
- 位置:`translator.py:417` `_new_translation_memory()`
- 問題:`recent_window = max(getattr(cfg.translation, 'context_window', 0) or 0, 30)` 是「至少 30」,不是上限。config 預設 `context_window=10`(註解寫 "recent translations passed as context to LLM"),實測 `m.recent.maxlen == 30`。`memory.context()` 回傳整個 deque,Claude / Nvidia / Ollama 的 `translate()` 都不限長直接展開成 messages → 主引擎每句多揹 60 則歷史訊息(30 對),token 成本與 latency 是設定值的 3 倍。只有 groq / openrouter 有自己的 `_limited_history`(預設 2 對)截斷。
- 影響:live 模式 hot path 每句的 prompt 大小、費用、延遲;`context_window` 調小完全無效。
- 建議:`recent_window = cfg.translation.context_window or 30`(或 deque 維持 30、`context()` 依 config 截尾);並考慮給 Claude/Nvidia/Ollama 套用與 groq 同款的 history limiter。

## 中優先

### [x] M1. `pending_incomplete`(已修:`pending_incomplete_timeout_seconds` 預設 8s,主迴圈每輪檢查) 沒有時限:靜默後字幕永不顯示、跨長靜默誤合併
- 位置:`sentence_splitter.py` 主迴圈 merge 邏輯(~186-213 行)+ `_can_merge_cuts`
- 問題(兩面一體):
  1. force-cut 產生的 incomplete cut 會被 buffer 成 `pending_incomplete`,等下一個 cut 才 emit。若之後講者靜默,沒有新 token → 沒有新 cut → 這句**無限期不顯示**(直到 stop flush)。
  2. `_can_merge_cuts` 只限 source 數與字數,**不限時間間隔**:靜默 5 分鐘後的新句子會跟 5 分鐘前的殘句 merge 成一句送翻譯,語意無關卻被當同句。
- 建議:給 `pending_incomplete` 記錄建立時間;超過閾值(如 `force_cut_seconds`)就單獨 emit;`_can_merge_cuts` 加 max-gap 條件。

### [x] M2. in-memory cache key(已修:cache key 納入 engine+model) 不含 engine:fallback 結果在 primary 恢復後仍被供應且標錯來源
- 位置:`translation_memory.py` cache key `(text, incomplete, prompt_ver)`;`translator.py:606-633` memory-hit 路徑
- 問題:soft/hard fallback 期間由次級引擎(例如 google_translate)產出的譯文寫進記憶體 cache;primary 恢復後同句再來,直接 memory_hit 回傳該結果,且 outcome 的 `engine`/`model` 填的是**當下 active engine**(primary)——與今日修復的 fix1 同族,但在 cache 命中路徑仍存在。品質面:較差的 fallback 譯文會在整個 session 內持續蓋過 primary。
- 建議:cache value 帶上產出 engine,命中時 (a) 正確標註 outcome,(b) 可選擇 primary 在線時略過 fallback 產物(或記 TTL)。

### [x] M3.(已修:`effective_system_prompt_for_engine` + per-engine prompt_ver,record_success 前以 used_engine 重算)fallback 引擎實際使用 compact prompt,卻以主 prompt 的 `prompt_version` 入快取
- 位置:`translation_engines.py` `_groq_system_prompt` / `_openrouter_system_prompt`(直接**取代**傳入的 system_prompt);`translator.py` `_record_success` → `db_store(prompt_ver)`
- 問題:`prompt_ver = md5(主 prompt)`,但 groq/openrouter 真正用的是 compact prompt。fix1 之後 DB row 正確掛 engine=groq,但 prompt_version 欄位語義錯誤:換主 prompt 會讓 groq 的 cache 全失效(實際 prompt 沒變),反之 compact prompt 或 `groq_translation_compact_prompt` 開關改變時 cache 不失效(實際 prompt 變了)。
- 建議:engine 介面回報「實際使用的 prompt」或自行計算 effective prompt hash,store/lookup 用它。

## 低優先

### [x] L1.(部分修:Groq 已加 diagnostics、translate_item 加 `reset_last_engine_diagnostics()` 杜絕跨 item 殘留;Claude/Google/Ollama 仍未寫)Claude / Google / Ollama / Groq 不寫 engine diagnostics;soft fallback 成功時 primary 失敗診斷遺失
- 位置:`translation_engines.py`(只有 Nvidia、OpenRouter 呼叫 `_set_last_engine_diagnostics`)
- 問題:其餘引擎成功/失敗後,thread-local 殘留上一次(可能是 Nvidia)的診斷。`_api_event_fields` 以 engine 名稱比對守住了錯掛,但代價是這些引擎的 API 事件欄位永遠空白;且 fix1 之後 soft fallback 成功的事件掛 fallback engine,primary 的失敗診斷(timeout 類型等)無處落地。
- 建議:各引擎統一呼叫 `_set_last_engine_diagnostics`;或在 outcome 另加 `primary_failure_*` 欄位。

### [ ] L2.(部分修:per-item reset 已防跨 item 殘留;同 item 內跨 engine 錯掛仍在)token usage 可能跨引擎錯掛
- 位置:`translator.py` `translate_item`(`get_last_token_usage()` 無 engine 比對)
- 問題:primary 回了 bad output(已 `_log_token_usage`)→ fallback 是不回報 usage 的引擎(google)→ 事件的 `token_*` 是 primary 的數字掛在 fallback outcome 上。
- 建議:`_TOKEN_USAGE` 一併記 engine 名,attach 前比對。

### [x] L3. `translation.cache.miss` 把 skipped 一起計入
- 位置:`translator.py` `_lookup_existing_translation_event`(incomplete / no engine / db 關閉分支也 `increment("translation.cache.miss")`)
- 問題:live 模式 db cache 預設關閉 → 幾乎每句 +1 miss,指標失真(跟 memory.lookup_existing_event 的語義不一致)。
- 建議:skipped 分支改計 `translation.cache.skipped`。

### [x] L4. memory_hit 重複 append `recent`,context 出現重複對
- 位置:`translation_memory.py` `lookup_memory_event` → `_remember_recent`
- 問題:同句多次命中會在 recent deque 裡塞多份相同 (ko, zh),擠掉其他 context 且浪費 prompt token。
- 建議:append 前檢查與最後 N 項重複。

### [ ] L5. `rejection_reason` 在 hot path 算兩次
- 位置:`translator.py:572-576`(先 `rejection_reason(raw_text)` 再 `prepare_input(text)`,後者內部又算一次),且都在 shared lock 內
- 建議:`prepare_input` 回傳 (text, reason) 或快取單次結果。

### [x] L6.(已修:drain = join_timeout − 0.5s margin)stop drain timeout(5s)與 `thread_join_timeout`(5s)相等
- 位置:`translator.py` `_STOP_DRAIN_TIMEOUT_SEC` vs `config.py:297`
- 問題:shutdown 時 main 反向 join,translator 最長 drain 5s,join timeout 也是 5s → 邊界情況會誤報 "Thread Translator did not stop within 5s"。
- 建議:drain timeout 設為 `thread_join_timeout` 的一半,或從 config 派生。

### [x] L7.(已修:`_refresh_engines_if_needed` + chain key,變更時重置 FallbackState)每個 translation worker 各自 build 一份 engine chain,config 改變不會重建
- 位置:`translator.py` `Translator.__init__`(`_build_engine_chain()`)
- 問題:兩個 worker + probe thread 各持一份引擎實例(Anthropic client ×N、重複 init log);probe 有 L7 修復會在 mode/engine 變更時重建,worker 不會——若 Phase 2 dashboard 支援 runtime 換 engine,worker 會用舊 chain,且 `FallbackState.active_idx` 對不同 chain 失義。
- 建議:chain 移入 shared_state 並加 chain_key 檢查(同 probe 的做法)。

### [-] L8. incomplete + incomplete merge 立即 emit(不續 buffer)
- 位置:`sentence_splitter.py` merge 路徑、`_merge_cuts`(`incomplete=second.incomplete`)
- 觀察:pending(incomplete)+ 新 cut(incomplete)可合併時會立刻 emit 一個 incomplete 合併句,而不是繼續等補完。視為 latency 取捨的有意設計,僅記錄;若要改,需配合 M1 的時限機制。

---

## 正面觀察

- 序號式 in-order emit + 合成 failed completion(H1@0611)讓亂序與 worker 崩潰都不會卡住輸出。
- M1@0611 的鎖紀律(in-memory 變更持鎖、file/DB I/O 在鎖外)在 memory/policy 各分割方法上執行得一致。
- `recent` context 有 quality gating(warn/bad 不進 context),避免劣質譯文自我放大。
- queue 策略分工清楚:sentence_queue 用 drop-oldest(保 backlog)、subtitle_queue 用 drain-keep-latest(保新)。

## 驗證方式

- H1:`python -c` 實測 `cfg.translation.context_window==10` 而 `TranslationMemory.recent.maxlen==30`。
- M1/M2/M3/L1-L7:靜態追讀 + 與 0611 review、ba3fa43 修復對照;未逐項寫 repro。
