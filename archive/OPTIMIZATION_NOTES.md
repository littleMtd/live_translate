# live_translate 優化建議

> 日期：2026-05-14
> 性質：可執行 backlog（非 architecture review）
> 範圍：基於現行 `main` 分支的程式碼

---

## 一、快速勝利（半天內可完成，影響顯著）

### 1. NFC 正規化下放到記憶體快取 ⚡

**問題**
`modules/db.py::_normalize()` 對 source_text 做 NFC + whitespace 正規化才查 DB。但 `TranslationMemory.cache_lookup()` 直接用原始 text 當 key。結果：

- 同一句話的兩種 Unicode 表示（例如 `"한" (U+D55C)` vs `"ᄒ ᅡ ᆫ"` 組合形式）會 hit DB 同一列，卻在記憶體快取裡開兩個不同 entry
- 第二次出現時誤判 memory miss → 多一次 DB 查詢

**改進**
在 `cache_store` / `cache_lookup` 入口套 `unicodedata.normalize("NFC", text)`，把正規化邏輯抽到 `utils/text_heuristics.py` 共用。

**影響**：減少約 5-10% 的 DB query（依語料）
**成本**：30 min
**檔案**：[modules/translation_memory.py](modules/translation_memory.py), [modules/translation_runtime.py](modules/translation_runtime.py)

---

### 2. STT 短輸入跳過 streamer profile 注入 ⚡

**問題**
[modules/translator.py:_build_system_prompt](modules/translator.py) 每次都會在 base prompt 後接整段 streamer profile（最大可達 500+ tokens）。對 `"네"`、`"ㅋㅋ"` 這類 1-3 字輸入，profile 完全沒幫助但照樣付 token 費用。

**改進**
```python
def _build_system_prompt(self, text: str) -> str:
    is_qwen = _is_qwen_model()
    base = _QWEN_PROMPT if is_qwen else _BASE_PROMPT
    prompt = self._evolver.build_system_prompt(base)
    # Skip streamer profile for very short inputs — slang map already handles ㅋㅋ etc.
    if len(text) >= 8 and cfg.translation.use_profile:
        prompt += "\n\n" + get_translation_profile(cfg.active_streamer_profile, qwen=is_qwen)
    return prompt
```

**影響**：對短句翻譯 token 用量降 30-50%，且 Claude prompt cache 命中率變高（短句版 prompt 與長句版 prompt 形成兩個獨立 cache）
**成本**：1 hr（含 prompt_version hash 適配，因為 prompt 版本變多了）
**檔案**：[modules/translator.py](modules/translator.py)

---

### 3. 引擎成本排序 fallback ⚡

**問題**
目前 `engine_chain = ("claude", "gemini", "google_translate")` 是品質順序。失敗時切下一個，但下一個不一定便宜。長時間 Claude rate limit 期間，會一直走 Gemini，比直接走 Google Translate（最便宜）貴 ~20x。

**改進**
config 加上 `cost_aware_fallback: bool = False`。開啟時，遇到 rate_limit 直接跳到 chain 末端（最便宜），其他錯誤才走原本順序。

**影響**：rate-limit 高峰期成本可降 70%+
**成本**：1 hr
**檔案**：[modules/translation_runtime.py](modules/translation_runtime.py)、[config.py](config.py)

---

### 4. `runtime_events.jsonl` 日誌保留期限 ⚡

**問題**
無清理機制，長期使用會無限累積（每場直播約 500-2000 條事件）。

**改進**
`main.py` 啟動時掃 `logs/runtime_events_*.jsonl` 和 `logs/translations_*.txt`，刪除 mtime 超過 N 天的舊檔（預設 60 天，可配置）。

**影響**：避免磁碟膨脹；不影響功能
**成本**：30 min
**檔案**：[main.py](main.py)（或新增 `utils/log_retention.py`）

---

## 二、中期改進（1-3 天）

### 5. Claude Prompt Cache 命中率追蹤與優化 💰

**現況**
`live` 模式有設 `cache_control: ephemeral`，但每次 evolver 更新 slang/stream_context 都會改變 system prompt，導致 cache miss。`runtime_events.jsonl` 已記錄 `cache_read` / `cache_write` token，但沒有 aggregate view。

**改進**
1. 擴充 `scripts/analyze_runtime_events.py` 輸出 cache hit ratio 報表
2. 在 `_build_system_prompt` 加上「穩定段」與「可變段」分離：把 evolver 注入的 slang/context 放最後，前面的 base prompt + streamer profile 才打 cache 標記。Anthropic 允許多個 cache breakpoint，可以善用
3. evolver 累積到 N 個新 slang 才觸發更新（已有 `evolve_every`，但每次都重寫整個 slang dict — 改成增量比較）

**影響**：cache hit ratio 從 ~40% 提到 70%+，Claude token 成本減半
**成本**：2 days（含驗證）
**檔案**：[modules/translation_engines.py](modules/translation_engines.py)、[modules/translator.py](modules/translator.py)、[scripts/analyze_runtime_events.py](scripts/analyze_runtime_events.py)

---

### 6. 輸入字數上限保護 🛡️

**問題**
若 STT 因故產出超長 hallucination（例如 5000 字重複文字），整段會送進翻譯 API。Claude 200 token max_tokens 限制只防輸出爆量，**輸入長度沒上限**。一次失誤可能花 $0.50+。

**改進**
- `translation_policy.prepare_input()` 加上 `max_input_chars: int = 500` 檢查
- 超過時，先呼叫 `is_stt_garbage()` 加嚴判斷；非 garbage 則截斷尾部並 log warning
- 新增 `runtime_events` event_type=`"input_truncated"`

**影響**：杜絕「一筆翻譯燒掉一日預算」的失誤路徑
**成本**：2 hr
**檔案**：[modules/translation_policy.py](modules/translation_policy.py)

---

### 7. 主執行緒生命週期 ordering 🛡️

**問題**
[main.py](main.py) 結束時：
```python
stop_event.set()
for t in threads:
    t.join(timeout=cfg.thread_join_timeout)
```

執行緒順序 = `[audio, stt, splitter, translator, _stt_printer]`，但這是**啟動順序**，不是關閉順序。停止時應該反向：先停 audio（不再進新資料）、再停 stt、splitter，最後 translator（讓既有 queue 排空）。否則最後幾條 audio chunk 翻譯結果會被 stop 截斷。

**改進**
```python
stop_event.set()
for t in reversed(threads):  # consumer-to-producer 反序更保險
    t.join(timeout=cfg.thread_join_timeout)
```

實際上更精細：分階段——先 set 一個「停止接新音訊」flag 讓 audio_capture 退出，再等 stt/splitter 清完 queue，最後關 translator。

**影響**：關閉時不再丟棄最後 1-2 條字幕
**成本**：3 hr（含 graceful shutdown 規範化）
**檔案**：[main.py](main.py)、各 module 的 `start()` 函數

---

### 8. Engine 統一錯誤分類介面 🔧

**問題**
5 個 engine 各自 try/except，邏輯重複：
```python
except Exception as e:
    kind = classify_error(e)
    if kind == "auth": log.error(...)
    elif kind == "rate_limit": log.warning(...)
    ...
```

每加一個 engine 就要複製這段。也不一致：有的引擎 mask API key，有的沒有。

**改進**
把 try/except 上升到 `call_with_fallback()`，engine 的 `translate()` 直接 raise，由 fallback 統一處理錯誤分類、log 格式、key masking。

**影響**：可讀性、新引擎接入成本降一半
**成本**：1 day
**檔案**：[modules/translation_engines.py](modules/translation_engines.py)、[modules/translation_runtime.py](modules/translation_runtime.py)

---

### 9. STT 串流：邊翻邊更新字幕 ⏱️

**問題**
目前 sentence splitter 等 `min_wait_seconds=3s` 才送翻譯。一句完整韓文要 3 秒延遲才看到字幕。

**改進**
新增「draft mode」：1.5s 時送出 incomplete=True 翻譯（已支援），完整成型時送 incomplete=False 取代。subtitle_display 已有 `_pending_text` 機制，加上 draft → final 的更新行為即可。

需注意：
- draft 翻譯成本翻倍（每句翻 1-2 次）
- 命中 memory cache 時不會增加實際成本
- 適合直播但不適合剪輯模式

加 config flag `cfg.splitter.draft_mode_enabled: bool = False`。

**影響**：感知延遲從 3s 降到 1.5s（對直播體驗有感）
**成本**：2-3 days（含 UX 微調）
**檔案**：[modules/sentence_buffer.py](modules/sentence_buffer.py)、[modules/subtitle_display.py](modules/subtitle_display.py)

---

## 三、長期 / 較大改動（1 週以上）

### 10. config 熱重載（Phase 2 dashboard 需要）🏗️

**問題**
`config.py` 是 frozen dataclass，dashboard 改 config 必須重啟 Python。違反 Phase 2 spec 中的 "Live config editing without restart"。

**改進**
- 新增 `utils/config_runtime.py`：mutable proxy 包裝 `cfg`，從 `live_translate_config.json` 讀寫
- 主要模組改讀 proxy（`cfg.audio.volume_threshold` → `runtime_cfg.audio.volume_threshold`）
- 開一個背景執行緒 poll JSON mtime，變動時呼叫 callbacks 通知模組（subtitle 字型大小變化即時生效、engine_chain 變化下次翻譯生效）

**影響**：解鎖 Phase 2 dashboard 即時編輯體驗
**成本**：1 week（含模組 wiring 與測試）
**檔案**：[config.py](config.py)、[utils/](utils/)、所有讀 cfg 的 module

---

### 11. 後端模式命名重構 🏗️

**問題**
`cfg.live_engine = "anthropic"` 實際語意是「走 engine_chain」，但 chain 預設第一個是 Claude 也叫 anthropic，命名混淆。

**改進**
重命名 `_VALID_BACKEND_MODES = {"chain", "ollama", "nvidia"}`，並在讀 config 時做 backward compat：
```python
def _migrate_backend_mode(value: str) -> str:
    if value == "anthropic":
        log.warning("'anthropic' backend mode is deprecated, use 'chain' instead")
        return "chain"
    return value
```

**影響**：新進開發者不會誤解
**成本**：4 hr（含測試 + README/system.md 更新）
**檔案**：[config.py](config.py)、[modules/translation_engines.py](modules/translation_engines.py)、文件

---

### 12. 快取預熱（Streamer Pre-warm）🎯

**問題**
每場直播開始時記憶體快取全空，DB 雖然有歷史但同步成本高。前 5 分鐘大量重複問候語都會打 API。

**改進**
- 新增 `scripts/prewarm_cache.py`：依 `cfg.active_streamer_profile` 預先翻譯該主播常見 30-50 句問候、招呼、口頭禪
- 結果寫入 DB；首次跑當前 prompt_version 時 1-2 美分成本
- main.py 啟動時若偵測到 `cfg.active_streamer_profile` 變更，自動觸發

**影響**：每場前 5 分鐘 API call 減 70%
**成本**：1 week（含 prewarm 語料整理 + 自動觸發邏輯）
**檔案**：新增 `scripts/prewarm_cache.py`、[main.py](main.py)、[data/streamer_profiles.json](data/streamer_profiles.json) 加 `prewarm_phrases`

---

### 13. 測試並行化 + 慢測標記 ⚙️

**問題**
全量測試 30s（293 tests）。其中 `test_integration.py` 的執行緒測試佔 ~20s（含 `time.sleep`）。CI 跑得不夠快。

**改進**
- 安裝 `pytest-xdist`，跑 `pytest -n auto`
- 慢測標記 `@pytest.mark.slow`，本機 quick check 跑 `pytest -m "not slow"` 約 5s
- 把 `time.sleep` based 的整合測試改為 event-driven（用 `threading.Event` 通知）

**影響**：開發迭代速度快 6 倍
**成本**：2-3 days
**檔案**：[tests/](tests/) 大部分檔案、[pyproject.toml](pyproject.toml)

---

### 14. Translator 並行批次處理 ⏱️

**問題**
sentence_queue 同一時刻只翻一句。如果直播語速快、句子短，sentence_queue 會塞 1-2 個 item，translator 一個一個處理。

**改進**
小工程：translator 一次從 queue pull 多個 item，組成 batch 送 LLM（Claude 支援 system + multi-turn 結構，可一次塞 3-5 個獨立翻譯任務）。LLM 回多筆結果，分別 put 進 subtitle_queue。

需注意：
- Google Translate v2 已支援 batch（`q` 可為 array）
- Claude / Gemini 要 prompt engineering（用 numbered list 確保輸出順序對應）
- subtitle_display 必須能消化「同時收到 3 條字幕」的場景

**影響**：高速直播延遲降一半、token 成本因 system prompt 共享而降 30%
**成本**：1-2 weeks（含品質驗證）
**檔案**：[modules/translator.py](modules/translator.py)、[modules/translation_engines.py](modules/translation_engines.py)

---

## 四、可觀測性 / 治理

### 15. Token 預算守門員 💰

**問題**
沒有總用量上限。Bug 或失控時可能單場直播花 $50+。

**改進**
新增 `cfg.translation.daily_token_budget: int | None = None`。translator 累計每日 token 用量（從 `runtime_events.jsonl` 抓），超過則停止 API 翻譯只用快取，並從 subtitle 顯示「⚠️ 今日 API 預算已用盡」。

**影響**：成本意外的最後一道防線
**成本**：1 day
**檔案**：[modules/translator.py](modules/translator.py)、[utils/runtime_events.py](utils/runtime_events.py)

---

### 16. 翻譯品質 A/B 評估器 📊

**問題**
換模型、改 prompt 後，沒有自動化機制驗證品質是否退步。

**改進**
- `data/eval_cases.json` 已有評估案例（眼前已存在）
- 擴充 `scripts/evaluate_translations.py`（已存在）：跑 N 個案例，比較 baseline vs current 的 BLEU / ROUGE / Gemini-as-judge 分數
- CI 啟用：每次 PR 自動跑，分數退步超過 5% 阻擋 merge

**影響**：可大膽改 prompt / 換引擎
**成本**：3-5 days
**檔案**：[scripts/evaluate_translations.py](scripts/evaluate_translations.py)、[.github/workflows/](.github/workflows/)

---

## 五、優先順序（建議實施次序）

| 排序 | 項目 | 類別 | 預估 ROI |
|------|------|------|---------|
| 🥇 | #1 NFC 快取正規化 | 性能 | 高（小工程大效益） |
| 🥇 | #4 日誌保留期限 | 治理 | 中（防呆） |
| 🥇 | #6 輸入字數上限 | 成本 | 高（杜絕失誤路徑） |
| 🥈 | #2 短句跳過 profile | 成本 | 中 |
| 🥈 | #3 成本感知 fallback | 成本 | 中-高 |
| 🥈 | #15 Token 預算守門員 | 成本 | 高（安全網） |
| 🥈 | #7 graceful shutdown | 可靠 | 中 |
| 🥉 | #5 Claude prompt cache 強化 | 成本 | 高（長期） |
| 🥉 | #8 Engine 統一錯誤介面 | 維護 | 中 |
| 🥉 | #13 測試並行化 | 開發效率 | 中 |
| ⏳ | #11 後端模式重命名 | 維護 | 低-中 |
| ⏳ | #10 config 熱重載 | 架構 | 中（解鎖 Phase 2） |
| ⏳ | #16 A/B 評估器 | 治理 | 中-高 |
| ⏳ | #9 STT 串流字幕 | UX | 高（但工程量大） |
| ⏳ | #12 快取預熱 | 成本 | 中 |
| ⏳ | #14 批次翻譯 | 性能 | 高（但風險高） |

---

## 六、刻意排除的方向

下列項目**不建議現在做**，列在這裡是為了避免將來重複討論：

- **改用 gRPC 取代 stdout pipe**：[frontend-design.md](frontend-design.md) 提過。除非 Tauri 需要傳大量結構化資料，否則 JSON 已夠用，gRPC 帶來的部署複雜度不值得。
- **STT 本機 GPU + Groq 雙跑取 quorum**：成本翻倍但品質提升有限（whisper-large-v3 vs SenseVoice-Small 在韓文上差距小）。除非要做語音研究。
- **完整重寫 Translator 為 async/await**：當前 thread-based 設計簡單可讀，async 不會帶來顯著好處（瓶頸是 API latency 不是 CPU）。
- **Web Dashboard 取代 Tauri**：Tauri 已在開發，切換成本遠大於收益。
- **多用戶 SaaS**：目前是單機桌面工具，雲端化是 Phase 4+ 的事，現在不要為此預先抽象。

---

## 七、要動之前該先量化的指標

開始任何 cost / latency 優化前，先用既有資料建立 baseline：

```bash
# 跑一場 1 小時的直播 → analyze 結果
python scripts/analyze_runtime_events.py logs/runtime_events_$(date +%Y%m%d).jsonl
```

需要記錄的指標：
- **翻譯延遲** p50 / p95 / p99（已有 `metrics.observe_latency("translation", ...)`）
- **Cache hit 比例**（memory_hit / db_hit / miss）
- **每小時 token 花費**（從 `runtime_events.jsonl` 的 `cache_read` / `cache_write` 推算）
- **Engine 切換頻率**（`translation.fallback.*` counter）
- **STT consecutive_none 警告數**

有了基準才知道每項優化的實際 ROI。
