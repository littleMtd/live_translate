# Code Review — 翻譯邏輯 + Pipeline (2026-06-11)

Review 範圍:
- 翻譯邏輯:`translator.py`, `translation_engines.py`, `translation_policy.py`, `translation_runtime.py`, `translation_memory.py`, `prompt_evolver.py`, `translation_corrections.py`
- Pipeline:`main.py`, `utils/pipeline.py`, `utils/queue_utils.py`, `audio_capture.py`, `stt.py`, `sentence_splitter.py`, `sentence_buffer.py`, `subtitle_display.py`

修復策略:按優先級逐項修復,每項獨立改動 + 測試,不一次性大改。

狀態圖例:`[ ]` 待修 / `[x]` 已修 / `[-]` 不修(記錄原因)

---

## 高優先(穩定性,先修)

### [x] H1. 序號空洞導致字幕永久停擺(已修,含回歸測試 TestMalformedItemDoesNotStallEmit)
- 位置:`translator.py` `collect_finished()`(~930 行)+ `translate_item` 前置代碼
- 問題:future 拋異常時 `completed[seq]` 永不填入,發射循環 `while next_emit_seq in completed` 永久卡住 → 之後所有字幕不再輸出,`completed` 無限增長。觸發點:`sentence_text(item)`、`sentence_metadata(item).copy()`、`_dependency_marker` 都在 `translate_item` 的 try 之外。
- 修法:
  1. `collect_finished` except 分支塞入合成 failed `_CompletedTranslation`,保證序號連續;
  2. `translate_item` 把前置解析移入 try 保護。

### [x] H2. Groq 翻譯重試路徑異常逃逸(已修,含回歸測試 TestGroqRetryExceptionContract)
- 位置:`translation_engines.py` `GroqTranslationEngine.translate` token-limit 重試(~1213 行)
- 問題:重試的內層 try 只捕 `HTTPError`;timeout/`URLError` 在 except handler 內拋出不會被同一 try 的 `except Exception` 接住,逃出 `translate()`,違反「失敗回傳 None」契約 → `call_with_fallback` 跳過鏈上剩餘引擎,整句直接 failed。
- 修法:重試塊改為捕獲所有 Exception,失敗回傳 None。

### [x] H3. Silero VAD 推理跑在 sounddevice 音頻回調內(已修:回調只入隊幀,新增 AudioVadWorker 線程;含測試 TestCallbackOffloadsToWorker)
- 位置:`audio_capture.py` `callback` → `vad.push()`
- 問題:每 100ms 幀做 torch 推理 + emit 時 `np.concatenate` 整個 buffer。回調超時 → WASAPI overflow 丟音頻,只留 status warning。
- 修法:回調只把幀放入內部 frame queue;新增 VAD 工作線程消費幀、跑狀態機。

---

## 中優先

### [x] M1. 共享鎖內做磁盤/DB I/O(已修:translation_memory 新增記憶體/I-O 拆分方法,history/DB 讀寫移出鎖外,history append 以獨立鎖序列化)
- 位置:`translator.py` `_record_success` → `translation_memory.record_success`(持 `_state_lock` 時 `_write_history` 文件寫 + `db_store` SQLite 寫)
- 影響:兩個 worker 共用鎖,慢 I/O 阻塞另一 worker 的 cache 查詢與 prompt 構建。
- 修法:鎖內只動內存結構,文件/DB 寫移出鎖外。

### [x] M2. Evolver prompt 無限膨脹(已修:_extra_slang 上限 30 條、最舊先逐出;緩存失效行為已加註釋確認為有意設計)
- 位置:cache key 含 `prompt_ver`(prompt md5);`prompt_evolver._extra_slang` 只增不減
- 修法(最小):`_extra_slang` 設上限(如 30 條,LRU 淘汰);緩存失效行為寫註釋確認為有意設計。

### [x] M3. VAD 短語音要等滿 hard_max 才處理(已修:silence gate 即時 discard,near-miss 保留 overlap;新增 TestVadEarlyDiscardAtSilenceGate)
- 位置:`audio_capture.py` `_VadState.push`
- 問題:`speech < min_speech` 時 `silence_hit` 不動作,等到 hard_max 才 discard → 短話最差延遲 = hard_max 秒;長靜默期間 buffer 持有數秒音頻、Silero 狀態不 reset。
- 修法:`silence_hit && speech < min_speech` 時提前丟棄(speech ≥ near_miss 則保留 overlap),並 reset。

### [x] M4. chunk 帶大段前導靜默送 STT(已修:隨 M3 的 silence-gate 提前 reset 一併解決)
- 位置:同上(靜默不清 buffer)
- 修法:emit 前裁掉前導靜默(保留 ~0.2s padding),或隨 M3 的提前 reset 一併解決。

### [-] M5. Splitter 合併邏輯不對稱(不修:test_two_incomplete_cuts_emit_bounded_merge 證明為有意設計——合併雙方都已等過一輪 force-cut 視窗,立即 emit 是為了限定延遲上界;每個片段最多多等一輪)
- 位置:`sentence_splitter.py` start() 主循環(~184 行)
- 問題:pending_incomplete + 新 incomplete cut 可合併時直接 emit(incomplete);合併失敗時卻繼續 buffer。策略相反。
- 修法:合併結果若仍 incomplete 且未超限,回存 pending_incomplete 繼續等;統一策略。

### [x] M6. 段間隙邊界 offset 漂移靜默失效(已修最小方案:新增 sentence.gap_boundary_used / sentence.gap_boundary_drifted metrics,先收數據再決定是否重構)
- 位置:`sentence_buffer._record_segment_gap_boundaries`
- 問題:offset 假設 buffer 文本 = segments 以單空格相接,實際存的是 STT 整段 text,漂移後 `isspace()` 守衛使邊界靜默失效。
- 修法(最小):加 metrics 計數邊界命中/失效,先取得數據再決定是否重構為 offset 映射。

### [x] M7. Groq STT 雙 key 冷卻期直接丟 chunk(已修最小方案:新增 stt.dropped.<reason> metrics;chunk 暫存留作後續評估)
- 位置:`stt.py` `_transcribe_groq` 冷卻分支
- 修法(最小):增加 `stt.dropped_rate_limit` metrics;可選:把 chunk 留作下次 overlap。

---

## 低優先

### [x] L1. filter_reason 跨 worker 競態(已修:rejection_reason + prepare_input + sanitize_rejection 讀取合併到同一鎖區段)
- `translator.translate_event` 無鎖讀 policy 狀態。修法:把 rejection_reason + prepare_input 合併到同一鎖區段內回傳。

### [x] L2. ClaudeEngine 硬編碼 timeout(已修:新增 cfg.translation.claude_timeout,預設 5.0)
- 移入 config,與 Gemini(12s)/Nvidia/Groq 對齊。

### [x] L3. mudang_shoes 硬編碼規則(已修:遷入 JSON;corrections schema 新增 match:"all" 支援多詞同現條件,並修正原先「更懂鞋子」會被替換成「神力更強子」的順序問題)
- 遷入 `data/translation_corrections.json`。

### [x] L4. 源規範化每次重建 dict + sorted(已修:模塊加載時預排序 shared 與 shared+profile 合併表)
- 模塊加載時按 profile 預排序緩存。

### [x] L5. history limiter 與 system prompt 構建重複(已修:抽出 _limited_history(config_prefix) 與 _compose_system_prompt 共用)
- 抽共用函數。

### [x] L6. body 變數遮蔽(已修:Nvidia 與 OpenRouter 錯誤分支改名 error_body)
- 改名 error body 變數。

### [x] L7. 探測線程引擎鏈永久緩存(已修:依 (mode, engine) key 變更時重建)
- 配置變更不反映。修法:每次探測前檢查 mode 是否變更,變更則重建。

### [x] L8. completed backlog 無上限保護(已修:超過 64 條時告警 + metrics translation.completed_backlog_high)
- H1 修復後風險大減;補上限保護(completed 超限時告警)。

### [x] L9. STT segment 統計重複計算(已修:成功路徑重用 segment_rejection_reason 回傳的 stats,移除重複轉換)
- `stt.py` `_transcribe_groq` 內 515 與 570 行。

### [-] L10. 音量門檻作用在歸一化後音頻(確認為有意:歸一化能救回的小聲語音不該在門檻處被丟,已加註釋說明)
- `stt_normalize_enabled` 開啟時門檻實際變鬆;確認意圖,或門檻改用原始音頻。

### [x] L11. _KEY_FOR_ENGINE 死代碼(已修:config._VALID_ENGINE_NAMES 本就拒絕這兩個名字,移除條目並加註)
- 與 `_make_engine` 對齊,或補引擎實現前先移除。

### [x] L12. 隊列策略未記錄(已修:system.md 新增 backpressure strategy 一節)
- audio/text/subtitle 用 put_latest、sentence 用 put_drop_oldest,屬有意設計 → 在 `system.md` 記錄理由。

### [x] L13. overlap 變數名誤導(已修:拆為 overlap_sample_count / overlap_seconds)
- 改名避免誤導。

### [-] L14. 暫停 drain 競態(接受:最多漏一條舊 token,影響可忽略,已在代碼加註)
- 影響極小,記錄即可;可在 resume 後丟棄第一批舊 token。

### [x] L15. subtitle pending 覆蓋無觀測(已修:新增 subtitle.pending_overwritten 指標)
- 補 `subtitle.overwritten` 指標,對齊 translator 端 `stale_skipped`。

---

## 已確認的良好設計(不動)

- 軟/硬 fallback + 後台 probe 恢復;`_merge_fallback_state` 併發合併分支正確。
- audio/STT 初始化失敗 `stop_event.set()` 快速放倒 pipeline。
- overlap + timestamp dedupe + transcript dedupe 三層防重複。
- runtime events 埋點 + utterance_id 全鏈路歸因。
- `prepare_input` 對 `last_input` 污染順序的處理與註釋。

---

## 後續輪次(2026-06-12)

### [x] P0. Labeling 樣本 5 條 host_only 翻譯錯誤
- S034:시둥이→해둥이 source_norm(stellive_hina,推斷為 STT 誤聽,待聽音檔確認)
- S028:hades profile 加時尚熱詞(배바지/새깅/지리다)
- S012/S026/S037:語篇連接/上下文主語/疑似 STT 錯誤,無法以規則修,待音檔

### [x] P1. Prompt 修正(prompt_ver 變更,快取失效成本經量測 ≈ 0)
- Qwen 人名規則與 base/[Preserve As-Is]/profile 衝突 → 統一為「profile 詞彙表 > 保留韓文 > 慣用音譯」
- 뱅송(直播)/뱅종(下播)映射歧義 → 拆成明確兩條
- 重複數字規則與【致命特徵】交叉引用(占比 ≤50% 才去重翻譯)
- 例 27 重複編號 → 例 27b;docstring 模型名過時 → 通用化
- evolver [本場新增俚語] 加優先級聲明(固定詞彙表為準)

### [x] P2. Live 模式停用 SQLite 快取層
- 量測:17 天 9,964 筆,僅 45 次重用(0.45%),全為短感嘆詞;52% 行掛在舊 prompt_ver 無法命中
- 新增 cfg.database.live_db_cache(預設 False);clip 模式不變
- invalidate(刪除)不受開關影響,壞數據永遠可清
- 測試:TestDbCacheGating 三例;test_db 整合測試改在 clip 模式下跑

### [-] P3. 舊 prompt_ver 數據不刪
- 保留 logs/live_translate.db 全部歷史行作為 ko→zh 對照語料(標註/評測可用)

### [x] P4. 移除 GeminiEngine 與 PromptEvolver(2026-06-12)
- GeminiEngine:engine_chain 實際只用 nvidia/openrouter/groq,移除類、工廠分支、key 表、_VALID_ENGINE_NAMES
- PromptEvolver:evolve_enabled 預設 False 且生產從未啟用;未驗證詞條直接進 prompt 的設計與人工 corrections 流程衝突 → 整體移除(prompt_evolver.py、translator 掛點、config evolve_*/keys.gemini/gemini_model、測試、文檔)
- 副作用:prompt_ver 對固定 base+profile 完全穩定;google.genai 依賴歸零;_compose_system_prompt 變純函數(不再持鎖)
- 後續若需「場中自動學習」,以離線建議產生器形態重建(輸出候選詞條供人工審核,進 corrections JSON),引擎用 nvidia/openrouter
