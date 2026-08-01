# live_translate 優化計畫

> 撰寫日期：2026-05-14
> 立場：**資料驅動，但要有具體問題**。Codex 建議的「先跑、先收 `runtime_events_*.jsonl`」是對的，但跑之前要先決定要回答哪些問題。

---

## 一、TL;DR — 我會這樣做

**這週**（共 ~1.5 小時程式碼動工 + 1 場直播）：

1. 做三個「不用資料」的小改動（見 §3），讓下一場直播的 `runtime_events` 含**Claude cache token**、**force-cut 旗標**、**主引擎探測頻率**改合理。
2. 開直播一場（≥ 30 分鐘語音、≥ 200 條翻譯），讓 JSONL 累積。
3. 用 `scripts/analyze_runtime_events.py` + 一支臨時的 cache hit-rate 查詢，回答 §2 的三個假設。
4. 依結果決定 §4 的中等規模優化要不要做。

**這個月以後**：根據 §5 的「先別做」清單推遲所有架構級改動，直到 §2 的資料給出明確訊號。

---

## 二、要靠資料驗證的三個假設（資料驅動）

每個都對應 `runtime_events_*.jsonl` 的一個或多個欄位。跑完一場直播就能回答。

### H1：Claude prompt cache 真的有命中嗎？

| 問題 | 怎麼測 | 對應決策 |
|------|--------|---------|
| 直播模式下 Claude 的 `cache_read_input_tokens / total_prompt_tokens` 比例 | 把 `_log_token_usage` 的數字推到 `runtime_events`（見 §3.1），跑完查 jsonl | 若命中率 > 70% → cache 設計成功，不動；若 < 30% → prompt 變動太頻繁（PromptEvolver 影響？streamer profile 切換？），需要鎖定 system_prompt 結構 |

**為什麼這是 P0**：Anthropic 的 prompt cache 是這個專案唯一一個能省 90% token 的槓桿。但目前**沒有任何地方在量它**。`translation_engines.py` 第 32-51 行有 log，但沒進 runtime_events 也沒上儀表板。等於你買了保險箱但沒檢查鎖。

### H2：`min_wait_seconds=3` / `force_cut_seconds=8` 設定合不合理？

| 問題 | 怎麼測 | 對應決策 |
|------|--------|---------|
| 句子被 `force-cut`（不完整）的比例多高？被 force-cut 的翻譯品質有沒有比較差？ | 在 `SentenceEvent` 加 `forced: bool`（`sentence_buffer.py` 已經算出來、只是沒傳出來），讓 `sentence_metadata()` 帶到 runtime_events | 若 force-cut > 40% 且品質旗標相關 → `force_cut_seconds` 太短；若 force-cut < 10% → `min_wait_seconds` 可以下調（換更快字幕） |

**為什麼這是 P0**：對即時字幕，**第一個字出現的時間**比準確度更影響觀感。`min_wait_seconds=3` 表示每句固定等 3 秒，肉眼可感。

### H3：engine_chain 順序合理嗎？

| 問題 | 怎麼測 | 對應決策 |
|------|--------|---------|
| 各引擎的 (latency_ms, quality_flags) 分布；不同引擎間品質 flag 命中率差多少 | `runtime_events` 已有 `engine` + `latency_ms` + `quality_flags`；直接統計 | 若 Gemini 的品質旗標數 ≤ Claude 且 latency 低 50% → 把 Gemini 排前面，省一半錢；若 Claude 顯著好 → 維持現狀 |

**為什麼這是 P0**：目前預設 `engine_chain = (claude, gemini, google_translate)` — **最貴的在最前面**。Claude Sonnet 4.6 input ≈ $3/Mtok、Gemini 2.5 Flash ≈ $0.3/Mtok（10× 差距）。如果品質差 < 5%，反過來排能直接省一個數量級。

---

## 三、不用資料就可以做（這次直播前完成）

每項都 < 30 分鐘，零風險，**收上來的事件資料才有用**。

### 3.1 把 Claude / Gemini 的 cache token 推到 runtime_events（10 分鐘）

**問題**：`translation_engines.py` 的 `_log_token_usage` 只 print 到 log，沒進 JSONL。

**做法**：
1. 讓 engine.translate 回傳 `(text, token_usage_dict)`，或在 engine 上 expose `last_token_usage` property
2. `translator.py` 第 196 行 `_record_success` 時把 `cache_read_input_tokens` / `cache_creation_input_tokens` / `prompt_token_count` / `output_token_count` 放進 `TranslationOutcome.engine` metadata
3. `TranslationOutcome.as_event_fields()` 已經接 metadata，自動展開到 jsonl

**測得到**：H1 的答案。

### 3.2 把 sentence force-cut 旗標傳到 runtime_events（10 分鐘）

**問題**：`SentenceBuffer.pop_ready()` 回傳的 `SentenceCut.forced` 在 `transcription_to_sentence()` **沒被保留**。

**做法**：
1. `pipeline_events.SentenceEvent` 加一個 `forced: bool = False` 欄位
2. `transcription_to_sentence()` 接受 `forced` 參數並寫入
3. `sentence_splitter.py` 第 62 行傳 `cut.forced` 進去
4. `sentence_metadata()` 把 `forced` 加進輸出 dict

**測得到**：H2 的答案。

### 3.3 把主引擎恢復探測改成時間制（10 分鐘）

**問題**：`translator.py:_FALLBACK_PROBE_EVERY = 50`。對直播來說：50 句翻譯 ≈ 5–10 分鐘。主引擎在這段時間如果恢復，沒人會發現。

**做法**：改成「距上次探測 ≥ 60 秒就探測」，不再用次數。改動約 5 行（在 `translation_runtime.call_with_fallback` 用 `time.monotonic()` 取代 `state.probe_counter`）。

**為什麼現在做**：影響 §2 H3 的資料判讀 — 探測太久會讓事件 log 看起來只跑 fallback engine。

---

## 四、依資料結果再決定的中等優化

跑完一場直播後，**看 §2 三個答案再決定**這些做不做。

### 4.1 引擎順序調整（若 H3 顯示 Gemini 夠用）

把 `engine_chain` 預設改成 `(gemini, claude, google_translate)`。改 1 行 config，但要先有資料背書。

### 4.2 sentence_splitter 參數調整（若 H2 顯示 force-cut < 10%）

`min_wait_seconds: 3 → 2`。1 行 config。能直接省 1 秒字幕延遲。

### 4.3 PromptEvolver 處置（不用等資料，但需要決策）

目前狀態：
- `evolve_enabled=False` 是預設
- 沒人開過（grep 沒人改它）
- 之前埋過 auth-error spam bug（已修，但模組仍存在）
- 是唯一還用 Gemini 做「prompt 元分析」的地方
- docstring 之前還寫錯說用 Claude

**選一個**：
- (A) **保留但凍結**：在 `config.py` 加 `# TODO: validate or remove before next major release` 註解。
- (B) **刪除**：移除 `prompt_evolver.py`、`PromptEvolver` 從 `Translator.__init__` 拆掉、移除 `cfg.translation.evolve_enabled` / `evolve_every`。約 30 分鐘工作。

我傾向 (B)，理由：你不會在直播時對著看不見的 daemon 期望它變聰明，這種「自我演化」要做就要做到能被測量；目前是死路一條的複雜度。

### 4.4 Cache 命中率最佳化（若 H1 顯示 < 30%）

若 Claude cache 命中率低，最可能原因：

1. **streamer_profile 切換** — 改 profile 等於改 prompt 等於 cache 全失效
2. **PromptEvolver 變動** — 同上
3. **`max_tokens=200` 浮動** — 不會影響 cache，排除

對應修法：把 streamer_profile 鎖定為 build-time const，runtime 不能切。

---

## 五、長期、先別做

這些都不在當前優化清單裡，但會被 review 多次提到 — 寫清楚為什麼**目前**不做：

| 項目 | 為何先別做 |
|------|-----------|
| Tauri Dashboard 雙向 config 熱載入 | Phase 2 還沒做完到「能看 cache stats」階段，先不要急著加可變狀態 |
| STT 換流式串接（Whisper streaming / Deepgram） | 大改，且只有當 §2 H2 顯示 splitter 是延遲瓶頸時才划算 |
| 把 sqlite 升級到 PostgreSQL | 單機 1 個 writer，連 sqlite 都還沒到效能瓶頸 |
| 把 Python pipeline 改 async / 拆 process | 目前 thread model 沒有效能問題；複雜度成本 > 任何效益 |
| 字幕翻譯品質的人工標註 / 對齊評估 | `runtime_events.quality_flags` 是免費的訊號；先吃乾這些再考慮人工 |

---

## 六、跑完一場直播後要看的 5 個數字

用 `scripts/analyze_runtime_events.py` + 一支臨時查詢（5-10 行 Python）：

```python
# 在 logs/ 目錄跑
from scripts.analyze_runtime_events import analyze_runtime_events
r = analyze_runtime_events()

# 1. Cache 整體命中率
hits = sum(c["count"] for c in r["by_cache_status"] if c["value"] in ("memory_hit", "db_hit"))
total = r["translation_events"]
print(f"cache hit rate: {hits/total:.1%}")

# 2. 各引擎 share（順序合不合理？）
for row in r["by_engine"]:
    print(f"{row['value']}: {row['count']} ({row['count']/total:.1%})")

# 3. p95 latency（即時字幕的死線）
print(f"p95 latency: {r['latency_ms']['p95']} ms")

# 4. 品質旗標排行
for row in r["quality_flags"][:5]:
    print(f"{row['flag']}: {row['count']}")

# 5. force-cut 比例（做完 §3.2 後可量）
forced = sum(1 for s in r["latest"] if s.get("forced"))   # 範例
print(f"forced cut ratio (sample): {forced}/{len(r['latest'])}")
```

**判讀準則**：

- Cache hit rate **> 50%** = 健康；**20–50%** = 還行；**< 20%** = §4.4 要動
- 主引擎 share **> 80%** = fallback 不常觸發（健康）；**< 50%** = 主引擎不穩
- p95 latency **< 2000 ms** = 字幕跟得上；**> 4000 ms** = §4.2 要動
- 品質旗標總命中 **< 5%** = 引擎合適；**> 15%** = §4.1 要動

---

## 七、優先順序總表

| 編號 | 動作 | 類型 | 時間 | 風險 | 觸發條件 |
|------|------|------|------|------|----------|
| 3.1 | 推 Claude cache token 進 runtime_events | 觀測 | 10 m | 低 | **現在做** |
| 3.2 | 推 sentence forced 旗標進 runtime_events | 觀測 | 10 m | 低 | **現在做** |
| 3.3 | 主引擎探測改時間制 | 行為 | 10 m | 低 | **現在做** |
| — | 跑一場 ≥ 30 分鐘直播 | 資料收集 | 30 m | — | **§3 全部完成後** |
| 4.1 | 引擎順序調整 | 設定 | 1 m | 中 | H3 答案支持 |
| 4.2 | 縮短 `min_wait_seconds` | 設定 | 1 m | 中 | H2 答案支持 |
| 4.3 | PromptEvolver 決策 | 程式 | 30 m | 低 | **獨立進行** |
| 4.4 | 鎖定 streamer_profile | 程式 | 15 m | 低 | H1 答案 < 30% |
| 5.* | 架構級改動 | — | — | — | **明確需求出現前不做** |

---

## 八、為什麼不做更多

`live_translate` 已經寫得相當乾淨：
- 三層 cache（LRU + SQLite + API fallback）有理有據
- `TranslationOutcome` + `runtime_events` 觀測接口完整
- pipeline health 防呆做完（audio fast-fail、STT unavailable propagation、PromptEvolver auto-disable）

**剩下的不是「架構問題」是「沒人看儀表板問題」**。優化清單就 3 個小改 + 1 場直播 + 4 個依資料決定的微調。除非 §6 的數字明顯不對勁，別把這個專案改成「複雜度比生產力高」的階段。

最後一個提醒：本檔自己是 **review 類文件**，不該進 git。要保留就放本機，要嘛轉進 issue tracker，要嘛刪。

---

## 九、Claude 的補充與修訂建議

（在 codex v1 草案後加入，2026-05-14）

### 9.1 強烈同意、且我原本想不到的

- **§3.1 推 Claude cache token 進 `runtime_events`**
  這是整份計畫最重要的一條。我原本的提案是「建 daily digest 系統」，但發現連 cache hit / miss 都還沒進 JSONL，搞 dashboard 等於沒有溫度計就先裝空調控制系統。**這個必須做在最前面**。
- **§3.2 `forced` 旗標傳到 `runtime_events`**
  同樣是觀測缺口我沒看到。`SentenceCut.forced` 在 `sentence_buffer.py:67` 已經算好，只是 `transcription_to_sentence()` 沒接住。30 分鐘的工作換 H2 的答案，極划算。
- **§4.3 PromptEvolver 處置**
  我傾向 (B) 刪除。理由與 codex 相同，補充一點：先前 commit 修了它的 docstring + auth-error spam bug，這些都是「為了一個沒人開的功能」付出的維護成本。每次重構都要顧及它，划不來。

### 9.2 我提過但 codex 沒提的（重新評估）

| 項目 | 我的原始提議 | 重新判斷 |
|------|------------|---------|
| **輸入字數上限保護** | 「收資料前必做」 | **降級為「順手做」**。STT 5000 字 hallucination 雖然會污染資料分布，但發生率低；若真的發生，codex 的 quality_flags 已有 `long_target_ratio` 旗標會抓到。仍建議補（30 分鐘），但**不擋路**——可以放到 §3 三項做完後、跑直播前順手加。 |
| **Graceful shutdown 反序 join** | 「3 小時，每場救回 1-3 條尾巴翻譯」 | **撤回**。Codex 沒提這個是對的判斷：影響太小、工程成本不對等。我那 3 小時應該花在 §3 三件事上。 |
| **Daily digest 自動化系統（3-5 days）** | 「最高槓桿」 | **降級為以後再說**。Codex 的 §6（5-10 行 ad-hoc Python query）更務實：先驗證資料真的有人在看、有人在做決策，再投資自動化。否則就會變成「跑了三個月沒人 query 的 cron」。 |

### 9.3 想跟 codex 商榷的點

- **§3.3 主引擎探測改時間制**
  我同意「次數制不可預測」這個立論，但 codex 的描述「主引擎在這段時間如果恢復沒人會發現」**技術上不準確**——`call_with_fallback` 只有在 `active_idx > 0`（已經切走）時才探測，主引擎沒倒就根本不會 probe。所以這個改動的真實效益是「**fallback 期間恢復偵測**從 50-call-quantized 變成 60-second-quantized」，差異對使用者體感較小。但因為改動只 5 行、不破壞語意，**我同意做**，只是不要過度宣傳它的效益。

- **§4.4「鎖定 streamer_profile」**
  Codex 把 streamer_profile 切換列為 cache miss 主因之一。但實務上 `cfg.translation.streamer_profile` 是 frozen dataclass 欄位，runtime 不能改（除非改 config.py + 重啟），所以「鎖定」其實**已經是當前狀態**。
  如果 H1 命中率真的 < 30%，更可能的元兇是：
  1. PromptEvolver 在跑（即使 `evolve_enabled=False` 也要確認 `_extra_slang` / `_stream_context` 是 empty）
  2. `_QWEN_PROMPT` vs `_BASE_PROMPT` 切換（`_is_qwen_model()` 依 cfg.translation.model 動態判斷）
  3. Claude 那邊的 cache TTL（5 分鐘）配上直播間歇造成 cache 過期
  4. `cache_control: ephemeral` 只設在 `system` 段，messages 段的 history（最多 30 對）佔了大部分 token 但沒進 cache
  最後一點（messages history 沒進 cache）如果是真的，**才是 H1 < 30% 的最大可能元兇**，修法是把 history 也加 cache breakpoint。建議 §4.4 觸發時，先驗證是哪一個原因再決定修法。

### 9.4 補一個 codex 沒提到的「資料汙染風險」

收資料前要確認一件事：`runtime_events.jsonl` 在 `_disabled=True`（PromptEvolver 因缺 GEMINI key 自我關閉）的情況下，**`prompt_version` 哈希是不是穩定的**？因為 `_evolver.build_system_prompt(base)` 在 disabled 時應該 return base 原樣，所以 prompt_version 應該穩定——但這要實測確認，否則 H1 的 cache 命中率分析會被 noise 干擾。

驗證方式：跑 10 次 `Translator()._get_prompt_version_hash()`，看 hash 是否完全一致。10 秒的驗證，比花一場直播再回頭 debug 便宜。

### 9.5 最終整合後的執行順序

照 codex 的順序 + 我的補充：

```
1. 驗證 prompt_version 穩定性     (10 sec, §9.4)
2. §3.1 cache token → events      (10 min)
3. §3.2 forced flag → events      (10 min)
4. §3.3 probe 改時間制            (10 min)
5. §4.3 PromptEvolver 刪除        (30 min)  ← 跟 §3 同一輪做完，因為刪除後 prompt_version 一勞永逸穩定
6. 順手：輸入字數上限              (30 min, 我的補充)
7. 跑一場直播 ≥ 30 min            (場景驗證)
8. 跑 codex §6 的 5 個查詢         (5-10 min)
9. 根據 H1 / H2 / H3 結果 → §4.1, 4.2, 4.4
```

總前置工程約 **1.5–2 小時**（vs 我原本估的 5 小時 + 3-5 days）。Codex 是對的，我之前太繁雜。

### 9.6 給雙方的最終 sanity check

- 若 user 要砍預算（時間不夠），最低可行集是哪幾項？
  我的答案：§3.1 + §3.2 + §3.3（共 30 分鐘）就能跑直播了。§4.3 / 我的 #6 都可延後。
- 若 H1 / H2 / H3 全部結果「正常」（cache > 50%、force-cut < 20%、Claude 品質明顯較好），代表目前架構真的沒問題，**下一步反而是停止讀這份文件**，去做別的事情。
- 收完一場資料後，回來覆寫第九節以下的內容，不要在原文加註解越積越厚。

---

## 簽核欄

- Codex v1：2026-05-14 — §1–§8
- Claude 補充 v1：2026-05-14 — §9
- 拍板決策日期：（user 決定後填寫）
