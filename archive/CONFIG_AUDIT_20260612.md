# Config 有效性稽核 (2026-06-12)

範圍:`config.py` 全部 132 個欄位,逐一比對 runtime 程式碼(modules/、utils/、main.py;排除 tests/scripts)。
方法:usage grep(含動態組名如 `f"{prefix}_context_window"` 的人工排除)+ getattr fallback 比對 + min/max 互蓋追蹤 + 語義對照。
動機:H1@CODE_REVIEW_20260612(`context_window` 被 `max(...,30)` 蓋掉)屬於「設定看起來能調、實際無效」一族,本稽核系統性掃同類問題。

狀態圖例:`[ ]` 待修 / `[x]` 已修 / `[-]` 不修(記錄原因)

---

## 中優先(設定無效或承諾的保護不存在)

### [ ] C1. `stt.max_repeat_ratio = 0.7` 是死設定,註解承諾的檢查不存在
- 註解:"reject if a repeated phrase fills > this fraction"
- 實況:全 codebase 無任何讀取。`stt_policy.is_hallucinated` 的重複檢查是 hardcoded「前半段文字在後半重現」邏輯,與 ratio 無關;translation_policy 另有 hardcoded 0.6/0.5。調這個值完全沒作用。
- 建議:wire 進 `is_hallucinated`,或刪欄位並修註解。

### [ ] C2. `stt.groq_daily_request_limit = 2000` 無任何實作
- 實況:全 codebase 無讀取。`groq_rate_limit_cooldown_sec` 有實作,但每日請求上限保護不存在——掛機一整天沒有成本煞車,設定卻讓人以為有。
- 建議:在 STT groq 呼叫處加每日計數(跨午夜重置),或刪欄位。

### [ ] C3. engine 端 max_tokens 全被 `min(cfg.translation.max_tokens=200, …)` 蓋死
- 位置:`translation_engines.py` Groq/OpenRouter `__init__`
- 實況:`groq_translation_max_tokens=512`、`openrouter_max_tokens=512` → 實際永遠 200,調大無效。
- 更重要:`groq_translation_retry_max_tokens=256` 的設計意圖是 413 token-limit 重試時**縮小**輸出預算,但 `min(200, 256) = 200` = 正常值 → 重試降額機制靜默失效。
- 建議:retry 值改為對 `self._max_tokens` 的比例或確保 < shared cap;config 註解標明 shared `max_tokens` 是全域上限。

### [ ] C4. `live_engine="anthropic"` 模式下 Claude 根本不在鏈上
- 位置:`config.py` `engine_chain=("openrouter","groq")` + `_build_engine_chain` anthropic 分支(直接用 engine_chain 當完整鏈)
- 實況:選 "anthropic" backend 不會建 ClaudeEngine——primary 變 openrouter。`model="claude-sonnet-4-6"`、`claude_timeout` 等整塊設定閒置。`_Config` 註解「"anthropic" uses engine_chain (with fallback)」沒有提醒 chain 裡要自己放 "claude"。
- 影響:目前 live/clip 都是 nvidia,所以未踩到;但這是切換 backend 時的地雷。
- 建議:anthropic 分支比照 nvidia 分支——強制 ClaudeEngine 為 primary 再接 chain;或 `__post_init__` 驗證 backend=anthropic 時 chain 含 "claude"。

## 低優先

### [ ] L1. 14 處 `getattr(cfg.x, "field", default)` 的 fallback 與 config 預設值不同
- 例:`use_profile` fallback `False` vs config `True`;`groq_translation_max_tokens` fallback `128` vs config `512`;`vad_overlap_sec` fallback `0.0` vs config `1.0`(完整清單見稽核腳本輸出)。
- 現在不會觸發(欄位都存在),但欄位改名/重構時行為會默默變更且無錯誤。
- 建議:欄位必存在的就直接 `cfg.x.field`(改名會直接 AttributeError,fail fast);要保留 getattr 的,fallback 統一等於 config 預設。

### [ ] L2. `subtitle.ctrl_bg` 死設定
- control bar 已移除(`subtitle_display.py:44` "Canvas only — no control bar"),欄位無人讀。刪除。

### [ ] L3. 文件漂移
- `_Translation` engine_chain docstring:說 `_make_engine()` 在 translator.py(實在 translation_engines.py);把 deepseek/deepl 列為 supported names,但 `_VALID_ENGINE_NAMES` 會直接 raise。
- `vad_max_speech_sec=6.5` 與其上方註解「7-10s chunks keep better sentence coherence」數值對不上。

### [-] L4. 設定動態性不一致(記錄,配合 Phase 2 再處理)
- per-call 讀取(隨時生效):splitter merge limits、pending timeout、translation prompts/profile、db cache 開關。
- 啟動時固定(改了要重啟):SentenceBuffer 的 segment_gap/silence_complete 參數(thread 啟動時快照)、各 engine 的 model/timeout(`__init__` 快照,翻譯側已有 `_refresh_engines_if_needed` 但 STT/audio 沒有)、subtitle 字型。
- 若 Tauri dashboard 要支援 runtime 調參,需在 frontend-design.md 標明哪些欄位需要重啟,否則 UI 會出現「調了沒反應」。

### [-] L5. `keys.deepseek` / `keys.deepl` 無消費者
- 已註明 not yet implemented,屬有意 placeholder,不動。

---

## 健康的部分

- 132 欄位中其餘均有實際消費者;動態組名(`{prefix}_context_window` 等 6 個)經人工確認由 `_limited_history` 使用。
- `__post_init__` 驗證(translation_mode / streamer_profile / engine_chain 名稱 / backend 模式)覆蓋了主要 enum 欄位,fail fast 行為正確。
- `dump_audio` 走 env var 開關的設計(免改碼開 labeling run)很乾淨。
