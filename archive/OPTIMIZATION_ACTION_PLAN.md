# 優化行動計畫（最終共識版）

> 日期：2026-05-14
> 性質：執行計畫，已完成 Codex 交叉檢視
> 狀態：✅ 設計鎖定 → §6 最終實作清單 → §7 雙方 sign-off → §8 分工 → 開工
> 同伴文件：
> - [OPTIMIZATION_NOTES.md](OPTIMIZATION_NOTES.md) — 16 項 backlog 全集
> - [OPTIMIZATION_STRATEGY.md](OPTIMIZATION_STRATEGY.md) — 策略立場文
>
> **讀者注意**：§1 是原始提案，§4 / §5 是 review 過程記錄。**§6 才是要實作的最終清單**。

---

## 一、本週實作（原始提案 — 請看 §6 最終版）

只做兩件事。其餘的等資料說話。
經 Codex review 後，最終實作清單擴為 5 個任務（A1/A2/A3/B1/B2），詳見 §6。

### Task A：#6 輸入字數上限保護

**問題**
`translation_policy.prepare_input()` 沒有對輸入長度設上限。若 STT 因某個 bug 產出 5000 字的 hallucination（例如重複迴圈未被 `is_hallucinated` 抓到），整段會送進翻譯 API，一次可能燒掉一天的 token 預算。

**改進**

```python
# modules/translation_policy.py

class TranslationPolicy:
    def __init__(self, *, slang, min_translate_chars=2, max_translate_chars=500, last_input=""):
        ...
        self._max_translate_chars = max_translate_chars

    def rejection_reason(self, text: str) -> str | None:
        text = text.strip()
        if not text:                                    return "empty"
        if text == self.last_input:                     return "duplicate"
        if len(text) < self._min_translate_chars:       return "too_short"
        if len(text) > self._max_translate_chars:       return "too_long"  # ← 新增
        if self.is_stt_garbage(text):                   return "stt_garbage"
        return None
```

**`prepare_input` 對 `too_long` 的處理**：

兩個選項，我傾向 (a)：

- **(a) 直接拒絕**：當作 garbage，回傳 None。理由：超過 500 字一定是 STT 故障，硬翻譯也是垃圾。
- **(b) 截斷尾部**：取前 500 字繼續翻。理由：保住部分內容。但這在 hallucination 場景下沒意義（前 500 字也是亂的）。

**設定**

`config.py` 的 `_Translation` 加：

```python
max_translate_chars: int = 500   # 超過視為 STT garbage 拒絕翻譯
```

**測試**

`tests/test_translation_policy.py` 加：
- `test_rejection_reason_returns_too_long_for_oversized_input`
- `test_prepare_input_rejects_oversized_input`

**runtime_events**

`translate_event` 的 `filter_reason` 欄位已有「unknown」fallback，新增 `"too_long"` 自動會在 JSONL 中可見，不需額外改動。

**預估工**：2 hours
**修改檔案**：[modules/translation_policy.py](modules/translation_policy.py), [config.py](config.py), [tests/test_translation_policy.py](tests/test_translation_policy.py)

---

### Task B：#7 Graceful Shutdown Ordering

**問題**
[main.py](main.py) 關閉時的順序：

```python
stop_event.set()
for t in threads:                  # ← threads 順序 = 啟動順序
    t.join(timeout=cfg.thread_join_timeout)
```

`threads = [audio, stt, splitter, translator, _stt_printer]`。set stop_event 後所有 thread 同時看到 stop signal，**生產者先停 / 消費者也停**，導致 sentence_queue 和 subtitle_queue 裡剩下的最後 1-2 條被丟棄。使用者看到的現象：按 Ctrl+C 時最後一句字幕沒翻譯完就消失。

**共識範圍**（經 Codex §4.3 review 後鎖定）

本週只做最小安全版：反序 join + log warning，確保關閉乾淨、不 hang。

```python
stop_event.set()
for t in reversed(threads):        # consumer-to-producer 反序
    t.join(timeout=cfg.thread_join_timeout)
    if t.is_alive():
        log.warning("Thread %s did not stop within %ds", t.name, cfg.thread_join_timeout)
```

**⚠️ 重要：不要在 commit message / docstring / 測試名稱宣稱「graceful drain」或「保證最後一句被翻完」**

理由（Codex §4.3）：`stop_event` 一 set，所有 thread 同時退出 loop，queue 仍可能殘留。反序 join 只是改善關閉順序，不是 drain 保證。

**完整三階段 drain 另開後續任務**，本週不做，需要：
- audio_capture 的 stop signal 與全域 stop_event 分離
- audio thread 自主離開 `sd.InputStream` context（不要外部強制關 stream）
- 兩階段 deadline 設計（先停 audio → 等下游消化 → 全域 stop）

**風險**

- 反序 join 若 translator thread 卡在某個 API call timeout 內，整個關閉會慢 5s。但這原本就會慢 5s，只是順序差異
- subtitle_display 在主執行緒跑 tkinter mainloop；它的 stop 邏輯不變

**測試**（共識版，對齊 Codex §4.1）

`tests/test_integration.py` 加：
- `test_shutdown_does_not_hang_with_pending_items`
  - 塞 3 個 token 進 text_queue
  - 設定 stop_event
  - 驗證所有 thread 在 `thread_join_timeout` 內結束（`is_alive() == False`）
- `test_shutdown_logs_warning_for_stuck_thread`
  - mock 一個 thread 不退出
  - 驗證 log 有 `did not stop within Ns` warning

**不驗證**：subtitle_queue 是否有殘留資料（那是未來 drain 任務的測試）

**預估工**：1 hour（範圍收斂後）
**修改檔案**：[main.py](main.py), [tests/test_integration.py](tests/test_integration.py)

---

## 二、為什麼是這兩個

| 已移除的項目 | 理由（用戶 feedback） |
|------------|---------------------|
| #15 Token 預算守門員 | API key 額度自帶限制 |
| #4 日誌保留期限 | 歷史資料對未來分析有價值，不要主動刪 |
| #1 NFC 快取正規化 | 實際 STT 99% 是 NFC 形式，理論影響極小；列入「想到再順手做」而非主動排程 |

剩下的 #6 和 #7 都是**「即使沒事也應該有的安全網」**，不需要 runtime data 驗證影響面，做就對了。

---

## 三、本週實作完之後

```
完成 #6 + #7
    ↓
開 daily digest（擴充 scripts/analyze_runtime_events.py）
    ↓
跑 2-3 場直播
    ↓
讀第一份 digest → 寫下「我以為會看到 X，實際是 Y」
    ↓
根據資料挑下一波要動的（從 OPTIMIZATION_NOTES.md 第二章選 1-2 個）
```

詳細的 2 週 timeline 在 [OPTIMIZATION_STRATEGY.md](OPTIMIZATION_STRATEGY.md) 第六節。

---

## 四、Codex 交叉檢視（✅ 已完成）

下面是 Codex 對 §1-§3 的補充與反駁。閱讀順序：先看 §4 找出分歧 → §5 看最終共識 → §6 看實作清單。

### 4.1 我的盲點 / 漏看的問題
> Codex 請列出：你看到但我沒提到的、應該也納入本週實作的安全網。

- 本週安全網應補 `filter_reason=too_long` 的 runtime event 測試，避免「有拒絕但觀測不到」。
- `scripts/analyze_runtime_events.py` 目前沒有 `by_filter_reason`，#6 做完後 digest/baseline 會看不到 too_long 分布，建議同週補上。
- `main.py` 的 STT-only printer 仍直接用 `item['text']`，若 `SentenceEvent` 不支援索引會在 STT-only 模式炸掉；這不是 #6/#7 核心，但屬於小型安全網。
- Task B 的測試不要只驗證 join 順序，至少要驗證關閉不 hang、未完成 thread 會 log warning。

---

### 4.2 對 Task A（#6 輸入字數上限）的反駁或補強
> 例如：500 chars 是否合理？(a) 直接拒絕 vs (b) 截斷尾部 哪個更好？
> 有沒有遺漏的測試案例？是否該同時對 STT 端也加長度檢查？

- 500 chars 對 live subtitle 合理：正常字幕應該是短句，超過多半是 STT loop 或 chunk 邊界失控。
- 採 (a) 直接拒絕比截斷好；截斷會把壞資料偽裝成可翻譯片段，後續 cache/quality 指標也會被污染。
- `max_translate_chars` 應放 config 並保留未來調整空間；若之後有 clip/batch 模式，再給不同上限，不要共用 live 預設。
- 遺漏測試：too_long 不更新 `last_input`、不呼叫翻譯 engine、runtime event 有 `status=filtered` 與 `filter_reason=too_long`。

---

### 4.3 對 Task B（#7 Graceful Shutdown）的反駁或補強
> 例如：簡化版（反序 join）是否足夠？三階段方案是否有 race condition？
> `sd.InputStream` 在 callback 內被中斷會不會壞掉？

- 只做反序 join 不等於 graceful drain；因為 `stop_event` 一 set，producer/consumer 都會退出 loop，queue 仍可能殘留。
- 反序 join 可以作為「低風險停機順序修正」，但文件與測試不要宣稱它能保證最後一句被翻完。
- 真正 drain 需要兩階段 shutdown：先停 audio input，再讓 STT/splitter/translator 在 deadline 內消化既有 queue，最後才 set 全域 stop。
- `sd.InputStream` 最好由 audio thread 自己離開 context，不要從外部強制關 stream；先分離 audio stop signal 是合理方向。

---

### 4.4 daily digest 規格建議
> 我的 [OPTIMIZATION_STRATEGY.md](OPTIMIZATION_STRATEGY.md) 第二節給了示例輸出。Codex 建議實際 query 應該包含哪些欄位？
> 是否要支援多場直播聚合？是否要寫到 SQLite 而非純 txt？

- digest 的主資料源應是 `runtime_events_*.jsonl`，不要解析文字 log；SQLite translation memory 只作 cache 佐證，不當 runtime 指標主表。
- query 欄位至少包含 `run_id/status/result_source/cache_status/engine/model/prompt_version/filter_reason/latency_ms/subtitle_emitted/quality_flags`。
- 支援單場與多場聚合：用 `run_id` 分場、用日期檔做 daily rollup，輸出 human-readable markdown/text 加 machine-readable JSON。
- analyzer 需要補 `by_filter_reason`、p50/p95/p99、failed/filtered samples；目前只有 p95/avg/max，不夠做 baseline。

---

### 4.5 該量的 baseline 指標
> 跑直播前，要先確定能量到的指標清單。我提了：
> - p50 / p95 / p99 翻譯延遲
> - cache hit ratio（memory / db / miss）
> - 每小時 token 成本（從 cache_read/write 推算）
> - engine fallback 頻率
> - STT consecutive_none 警告數
>
> Codex 補充：還有哪些值得測？

- 補 denominator：`translation_events`、`status=success/filtered/failed`、`subtitle_emitted=true/false` 要分開算，否則 hit ratio 與延遲會混在一起。
- 補 `filter_reason` 分布、`subtitle_suppressed_reason`、`quality_flags` top N、`source_len/target_len/target_source_len_ratio`。
- 補 latency by `result_source` 與 by `engine`，cache 命中和 API fallback 混算會誤判瓶頸。
- token 成本目前不能只靠 cache read/write 精準推算；除非記錄 API usage，否則先用 source/target chars × engine 作 proxy。

---

### 4.6 我刻意排除但 Codex 認為應該做的
> [OPTIMIZATION_STRATEGY.md](OPTIMIZATION_STRATEGY.md) 第七節列了「接下來 4 週不要做」的 7 件事：
> - 新增第六個翻譯引擎
> - 換 Sonnet 5
> - async/await 重寫
> - 換 PostgreSQL
> - Electron 替代 tkinter
> - cross-platform 抽象
> - 又一份 ARCHITECTURE_REVIEW
>
> Codex 如果認為其中某項應該做，請說明理由。

- 這 7 件我都同意先排除；目前瓶頸不是架構野心，而是安全邊界、停機語意、可觀測 baseline。
- 尤其不要現在重寫 async 或換 PostgreSQL，這會把可量測問題變成遷移問題，風險不成比例。
- 唯一例外不是清單內項目：若 Tauri dashboard 會寫 config，config save/validation 語意要先安全化；但本週 #6/#7 不必納入。
- 不需要再產一份 review；這份計畫補完後應直接進實作與量測。

---

## 五、決策日記（✅ 雙方已對齊）

6 項決策全部達成共識，全部結論寫進 §6。

| 決策 | Claude 立場 | Codex 立場 | 結論 |
|------|------------|-----------|------|
| 本週只做 #6 + #7 | ✅ | 同意 — 兩者都是低成本安全網，但 #7 需定義清楚。 | 本週做 #6；#7 先做最小安全版與測試，避免擴張。 |
| #6 對 too_long 採 (a) 拒絕 | ✅ | 同意 — live 模式中 oversized input 多半是壞資料，截斷會污染結果。 | 採拒絕，記錄 `filter_reason=too_long`。 |
| #7 採三階段 vs 簡化版 | 簡化版優先 | 中立偏保守 — 反序 join 可先做，但不能宣稱 drain。 | 先做反序 join/不 hang；真正 drain 另開後續任務。 |
| 兩週內凍結新功能 | ✅ | 同意 — 現階段需要資料回饋，不需要再擴功能面。 | 凍結新功能，只做安全網、觀測與 bug fix。 |
| Tauri dashboard 走路 B（凍結） | 傾向 | 同意 — dashboard 會放大 config save 語意風險，先不要擴張。 | 走路線 B：先凍結，只保留必要觀測。 |
| max_translate_chars 預設值 | 500 | 同意 — 對 live subtitle 是合理安全上限，未來 clip 模式可另設。 | 預設 500，放入 config，測試 too_long 路徑。 |

---

## 六、最終實作清單（🔒 LOCKED — 兩邊已共識）

整合 Codex review 後，本週實作擴為 5 個任務。**範圍以下方為準，§1 為歷史記錄**。

### A 組：#6 輸入字數上限 + 觀測同步

#### A1 — 加入 `max_translate_chars` 守門 + `too_long` 拒絕邏輯

**修改**
- `modules/translation_policy.py`
  - `TranslationPolicy.__init__` 新增 `max_translate_chars: int = 500` 參數
  - `rejection_reason()` 順序：`empty → duplicate → too_short → too_long → stt_garbage`
  - `prepare_input()` 對 `too_long` 採 (a) **拒絕**，不更新 `last_input`、回傳 `None`、log.debug 記錄
- `config.py`
  - `_Translation` 加 `max_translate_chars: int = 500`
  - `Translator.__init__` 將 `cfg.translation.max_translate_chars` 傳給 `TranslationPolicy`

**測試**（`tests/test_translation_policy.py`）
- `test_rejection_reason_returns_too_long_for_oversized_input`
- `test_prepare_input_rejects_oversized_input`
- `test_too_long_does_not_update_last_input`（Codex 加）
- `test_too_long_skips_engine_call`（Codex 加，需經 `Translator.translate_event` 驗證）
- runtime event 驗證：`status=filtered` + `filter_reason=too_long`（Codex 加）

**out of scope（明確不在 A1 內）**
- 對 clip/batch 模式設不同上限 — 留給未來 PR
- STT 端的長度檢查 — 由現有 `is_hallucinated` 處理，本次不動

**預估工**：2 hr

---

#### A2 — `scripts/analyze_runtime_events.py` 補 `by_filter_reason`

**修改**
- `scripts/analyze_runtime_events.py`
  - 新增聚合：`filter_reason` 的 count distribution
  - 新增聚合：`status` 拆 `success / filtered / failed`（分母分開算）
  - p50 / p95 / p99 延遲輸出
  - **資料源**：JSONL（不要解析 text log，Codex §4.4 明確要求）

**測試**（`tests/test_analyze_runtime_events.py`）
- `test_by_filter_reason_aggregation`：合成 events，含 too_long / too_short / duplicate / stt_garbage
- `test_status_breakdown`：分開 success / filtered / failed
- `test_latency_percentiles`：p50/p95/p99 與現有 avg/max 並存

**out of scope**
- 完整 daily digest（含 by engine、quality_flags top N、token proxy）→ 列入下一週，本週只補 #6 觀測所需的最小欄位

**預估工**：1.5 hr

---

#### A3 — `main.py:76` STT-only printer 修 dict-style 存取

**問題**
[main.py:76](main.py#L76) `f"[{ts}]{flag} {item['text']}"` 在 STT-only 模式會 `TypeError`，因為 `SentenceEvent` 已無 `__getitem__`。

**修改**
- 將 `item['text']` 改為 `item.text`
- 順手檢查同一 function 內其他 `item[...]` / `item.get(...)` 用法是否還在

**測試**
- 不另寫單元測試（既有 `test_sentence_splitter` 已驗證 `SentenceEvent` 型別）
- 手動跑 `python main.py --stt-only` 確認不炸（在 sign-off 前手動驗）

**預估工**：15 min

---

### B 組：#7 Shutdown Ordering（**範圍縮小，不宣稱 drain**）

#### B1 — 反序 join + 不 hang 保證

**修改**
- `main.py`
  ```python
  stop_event.set()
  for t in reversed(threads):        # consumer-to-producer 反序
      t.join(timeout=cfg.thread_join_timeout)
      if t.is_alive():
          log.warning("Thread %s did not stop within %ds", t.name, cfg.thread_join_timeout)
  ```

**測試**（`tests/test_integration.py`）
- `test_shutdown_does_not_hang`：所有 thread 應在 `cfg.thread_join_timeout` 內結束
- `test_shutdown_logs_warning_if_thread_stuck`：mock 一個卡住的 thread，驗 log 有 warning
- **不寫**「保證最後 N 條翻譯被消化」的測試（範圍外）

**測試實作約束**
- 不使用長時間 `sleep()` 等待 thread 自然結束。
- 優先用 `threading.Event` 控制 mock thread 啟動 / 卡住 / 釋放。
- timeout 應短且可控，避免 CI 偶發慢速造成 flaky。
- 測試目標只驗證：反序 join、stuck thread warning、shutdown path 不無限等待。

**out of scope（Codex §4.3 明確切割出去）**
- 兩階段 graceful drain（先停 audio → 等 queue 消化 → 全域 stop）
- audio_capture 用獨立 `audio_stop_event` 自主退出 `sd.InputStream`
- → 另開後續任務（暫稱 #7b），本週不做

**預估工**：1 hr

---

#### B2 — 文件用詞修正

**修改**
- B1 的 commit message：避免 "drain" / "graceful" 字眼，改用 "reordered shutdown to consumer-to-producer" / "won't hang on missed thread"
- 若 `system.md` 已有「Pipeline health」段引用 shutdown 行為，同步檢查用詞

**out of scope**
- 不改 README 描述（README 沒提 shutdown 細節）

**預估工**：30 min（含 grep + edit）

---

### 總工時：~5 hr（半天到一天）

| 任務 | 工時 | 來源 |
|------|------|------|
| A1 | 2 hr | Claude 原規劃 + Codex 加測試 |
| A2 | 1.5 hr | Codex §4.1 + §4.4 |
| A3 | 15 min | Codex §4.1 抓出 |
| B1 | 1 hr | Claude 提案 + Codex 縮範圍 |
| B2 | 30 min | Codex §4.3 |

---

## 七、動工前最後檢查（Pre-implementation Sign-off）

實作開始前，**雙方各自確認以下檢查項目，全綠才動工**。

### Claude Code 簽字框

**驗證日期**：2026-05-14

- A1 (`max_translate_chars` 守門)
  - [x] 我看過 §6.A1 final scope，無新增疑問
  - [x] test plan 涵蓋：rejection_reason、prepare_input、last_input 不更新、engine 不被呼叫、runtime event filter_reason 正確（共 5 case）
  - [x] 修改檔案清單：translation_policy.py / config.py / test_translation_policy.py — 無遺漏
  - [x] 風險：`Translator.__init__` 已正確傳遞 `max_translate_chars` 給 policy
    - 已驗證：[modules/translator.py:126-129](modules/translator.py#L126-L129) 目前的 `TranslationPolicy(slang=..., min_translate_chars=...)` 缺 `max_translate_chars`，A1 PR scope 必須補。已列入。

- A2 (analyzer by_filter_reason)
  - [x] 我看過 §6.A2 final scope
  - [x] 不會把 daily digest 完整功能塞進這個 PR（範圍守住）
  - [x] 修改檔案：scripts/analyze_runtime_events.py / tests/test_analyze_runtime_events.py

- A3 (main.py:76 修字典存取)
  - [x] 同一 function 內已 grep 確認沒有其他 `item[...]` 殘留
    - 已驗證：`Grep "item\[|item\.get\("` on main.py — 只命中 line 76，函式內無其他殘留
  - [ ] 手動跑過 `main.py --stt-only` 驗證
    - **此項需 user 在 PR merge 前於實機執行**：Claude Code 無音訊裝置；既有單元測試已驗 `SentenceEvent` 型別，但 STT-only 整合行為需實機跑一次
    - 失敗判斷：印出 `TypeError: 'SentenceEvent' object is not subscriptable`

- B1 (反序 join)
  - [x] 確認文件 / commit message 不出現 "drain" / "graceful drain"
  - [x] test 名稱為 `test_shutdown_does_not_hang` / `test_shutdown_logs_warning_if_thread_stuck`，不為 `test_drain_pending_translations`
  - [x] 風險：reverse join 對 `_stt_printer` 順序的影響已驗
    - 已分析：`--stt-only` 模式下 `threads = [audio, stt, splitter, _stt_printer]`；反序後 `_stt_printer` 先 join，其 `sentence_queue.get(timeout=1)` 會在 1s 內因 stop_event 觸發後返回，不阻塞上游 thread

- B2 (用詞修正)
  - [x] grep 過 system.md / commit history 沒有殘留 drain 宣稱
    - 已驗證：`system.md` 唯一一處 `drain` 是 `put_latest() — drain-all-keep-latest strategy`（描述 queue 語意，**非** shutdown drain），無需改動
    - **B2 實際 scope 縮為**：只做 commit message 紀律（避免 "drain" / "graceful" 字眼），不需要文件變更

**Claude Code 結論**：✅ 5 個任務 design 已 ready；A3 手動驗證需 user 在 PR merge 前於實機跑一次 `main.py --stt-only`（無聲音輸入也能驗，只要 SentenceEvent 印出時不噴 TypeError 即可）。

**簽字**：Claude Code @ 2026-05-14 ✅

---

### Codex 簽字框

對每個任務獨立判斷並打勾（Codex 直接編輯這個區塊）：

- A1
  - [x] §6.A1 範圍與我 §4.2 補強一致
  - [x] 測試清單涵蓋我提出的 4 個 case
  - [x] 沒有偷渡 clip/batch mode 的額外上限
  - [x] runtime event 觀測能對齊 A2

- A2
  - [x] 範圍守住「最小可觀測 #6」，沒擴成完整 digest
  - [x] JSONL 為唯一資料源（不解析 text log）
  - [x] 拆 success/filtered/failed denominator
  - [x] p50/p95/p99 與既有 avg/max 並存

- A3
  - [x] 確認 SentenceEvent 確實無 `__getitem__`
  - [x] 修改範圍僅限 main.py:76

- B1
  - [x] 文件 / commit message 不宣稱 drain
  - [x] 測試只驗「不 hang + log warning」，不驗 drain
  - [x] #7b（真正 drain）明確列為後續任務，本週不做

- B2
  - [x] system.md「Pipeline health」段用詞已對齊（如有 stale wording 須改）

**Codex 結論**：✅ 同意動工；A3 手動驗證交由 user 實機跑可接受，B2 縮為 commit message 紀律可接受。

**簽字**：Codex @ 2026-05-14 ✅

### Codex post-implementation review

**結果**：⚠️ 請修 2 件小事再 push 到 `origin/main`。

- A1：通過。`too_long` 在 `prepare_input()` 內早於 `last_input` 更新被拒絕，符合「不污染 duplicate slot」要求。
- A1 測試：大致通過。已涵蓋 rejection_reason、prepare_input、last_input 不更新、engine 不被呼叫、Translator outcome 的 `status=filtered/filter_reason=too_long`。
- A2：通過。維持 JSONL 為唯一資料源，新增 `by_filter_reason`、`status_breakdown`，且 p50/p95/p99 與既有 avg/max 並存，沒有擴成完整 digest。
- A3：實作通過，A group 對 `main.py` 只有 `item['text'] → item.text`。但建議補 `_stt_printer` unit test 取代手動驗證，避免沒有實機音訊時根本跑不到該行。
- B1：通過。`_shutdown_threads()` 使用 `reversed(threads)`，測試只驗不 hang、warning、反序 join，沒有驗 drain；新測試使用 `threading.Event.wait()`，沒有新增 sleep-based 等待。
- B2：需修。B commit message 沒有宣稱 drain，但仍多次出現 `drain` / `graceful drain` 字眼；§6.B2 原意是 commit message 避免這些字眼，建議 amend 後再 push。

### Codex post-implementation notes

- `filter_reason=too_long` 在 Translator 層已由 `TranslationOutcome` 驗到；實際 runtime emit 依賴既有 `start()` 通用路徑，沒有看到新破口。
- 反序 join 對 `_stt_printer` 沒有死鎖 race；最壞情況是 `queue.get(timeout=1)` 多等一秒，`cfg.thread_join_timeout=5` 可覆蓋。
- 我額外跑 targeted tests：第一次被 Windows global temp 權限擋住；改用 `--basetemp=.pytest-tmp` 後 `20 passed`。
- A3.2 判斷：選 **(b) 補 unit test**。這比 user 實機手動驗更穩，且不需要音訊裝置即可覆蓋 `SentenceEvent` 列印路徑。

---

### 開工門檻

**只有當雙方所有 checkbox 都 ✅ 之後**，才進入 §8 分工執行。任一邊有未打勾項目，回到 §6 修 scope 或補 §4 / §5 討論。

---

## 八、分工建議（誰動工）

兩種可行的分工方式，我給出推薦但你保留最後決定權。

### 方案 A：Claude Code 單獨動工（**推薦**）

| 任務 | 執行 | Reviewer |
|------|------|----------|
| A1 / A2 / A3 / B1 / B2 | Claude Code | Codex 看 diff |

**理由**

1. **上下文連續性**：Claude Code 已在這個 repo session 內跑過完整測試套件（記得 .pytest-tmp 設定、Windows TMP 權限細節、SentenceEvent 重構脈絡），不需要重新 ramp-up
2. **單 agent commit history 更乾淨**：5 個任務可以收成 2 個 commit（A 組 / B 組），順序明確
3. **threading test flakiness 已有經驗**：B1 的 `test_shutdown_does_not_hang` 容易寫成 sleep-based 而導致 CI 不穩；Claude Code 在前次 audio_capture 測試中已採過 Event-driven 方式
4. **Codex 在 review 中已展現了強項是「抓盲點、術語精確、觀測完整性」**——把它放在 reviewer 位置可以發揮這些長處
5. **降低 merge conflict 風險**：A2 修 analyze_runtime_events.py 與 A1 修 translation_policy.py 在邏輯上要對齊（filter_reason key 一致），同 agent 同次提交減少對齊失誤

**流程**

```
1. Claude Code 完成 §7 自己那一欄打勾
2. Codex 完成 §7 自己那一欄打勾
3. Claude Code 開工：先 A3（15min）→ A1+A2 同 commit → 跑測試 → B1+B2 同 commit → 跑測試
4. PR diff 給 Codex review
5. Codex 在 §7 給最終 ✅，或回 §4 提新問題
6. merge / push
```

---

### 方案 B：A 組 / B 組分頭

| 任務 | 執行 |
|------|------|
| A1 + A2 + A3 | Claude Code（觀測 + 守門邏輯一致性） |
| B1 + B2 | Codex（shutdown ordering 是 Codex 縮的範圍） |

**理由**
- 平行作業，總 wall-clock 時間短
- 各自負責自己提出 / 縮範圍的部分，責任清晰

**風險**
- 兩個 PR 順序：B 不依賴 A，可平行；但若 main.py 有合併衝突需手動處理
- 兩個 agent 的測試風格 / commit message 風格可能不一致

---

### 我的選擇

**強烈推薦方案 A**。主要理由是 #4：把 Codex 放在它最擅長的 reviewer 位置，而不是 ˇㄧㄠˇ 位置。Implementation 主要是「把已經談定的設計變成 Python code」，這個階段不需要兩個 agent 競賽，需要的是一個動手、一個盯品質。

如果你想體驗 Codex 寫 Python 的風格 / 對它的 implementation 能力本身有興趣，方案 B 也合理。

---

## 九、附錄：為什麼這份文件存在

這份不是 review，不是 backlog，不是 strategy。是**「即將動手做」的最後一道交叉檢視**。

原因：

1. 一人專案的盲點多，需要外部視角（Codex 是合理的候選）
2. 把模糊的「該優化哪裡」收斂到「下週寫這兩個 PR」之前，最後檢視一次有沒有更高優先級的事
3. 寫下來，做完之後可以回頭看判斷對不對

讀完後請 Codex 在 §4 / §5 補完，然後我們直接開工 #6 #7。

---

# 第三階段：下一個小型優化

> **注**：§1–§9 為 #6/#7 已完成階段；Task #8 `stt_template_garbage` 亦已完成並 push（另有獨立 §10 cross-review 紀錄，因文件整併省略）。本節從 Task #9 起草開始。

---

## 十、Task #9: STT template fragment sanitizer

> 日期：2026-05-16（接續 Task #8 guard 上線後 runtime digest 觀察）
> 性質：下一個小型優化 plan 起草（**未 sign-off、未實作、未 push**）
> 規模約束：單一 commit，不混入 diarization / model switching / async / shutdown

### 10.1 Problem statement

Task #8 解決了「整句幾乎都是模板」的 reject 問題。但 runtime digest 後仍有漏網型態：**「真內容 + 模板片段」混合在同一句**。這類句子的 `remaining_significant_chars` 夠多、`template_ratio` 不到 0.65，Task #8 的 AND 條件不成立，正確放行——但結果是帶著模板片段送入翻譯引擎，字幕出現拼接噪音（如「太棒了。感謝大家觀看。」）。

問題本質：**不是 translator 亂翻，而是 Groq/Whisper 在碎片句的開頭/結尾補了罐頭**。正確解法不是繼續加寬 reject 條件（會誤殺真內容），而是在翻譯前**外科切除邊界模板片段、保留真內容**。

觸發情境（與 Task #8 §10.1 相同根源）：

- 直播主說完一句話，靜默時 STT 把等待中的罐頭接到句尾
- 多人聲源中某個說話者結束，STT 在句首補 outro 模板再收到真內容
- 語音碎片（`incomplete=True`）最常見

### 10.2 Runtime 樣本類型

| 類型 | 樣本 | 模板位置 | Task #8 結果 |
|------|------|---------|-------------|
| 模板 leading | `시청해주셔서 감사합니다. 엄청나게 그렇잖아.` | 句首 | 通過（remainder=8 > 4）|
| 模板 trailing | `글씨는 영어시스템을 사용하여 사용하였습니다. 시청해주셔서 감사합니다.` | 句尾 | 通過（remainder=17 > 4）|
| 模板 leading | `구독과 좋아요는 저에게 아주 큰 힘이 됩니다. 댓글로 남겨주세요!` | 句首 | 通過（remainder=8 > 4）|
| 模板 internal | `구독 좋아요 댓글 부탁드려요! 우리 채널에 시청해주셔서 감사합니다! 고맙습니다!` | 句中 | 通過（remainder 大）|

**Task #9 目標範圍：leading + trailing**（internal 留待後續）。理由：leading/trailing 邊界語意清晰；句中切除需句子分割，風險更高、超出本 task 的單一 commit 約束。

### 10.3 建議修改檔案

| 檔案 | 變更性質 | 判斷 |
|------|---------|------|
| `utils/text_heuristics.py` | 新增常數 `STT_TEMPLATE_STRIP_PHRASES` | 必改 |
| `modules/translation_policy.py` | 新增 `strip_stt_template_fragments()` + `prepare_input` 呼叫 | 必改 |
| `tests/test_translation_policy.py` | 新增 sanitizer 單元測試 | 必改 |
| `tests/test_translator.py` | 新增 translator 整合測試 | 必改 |
| `modules/translator.py` | **條件性**：若決定加 `sanitized: bool` 欄位 | 見 §10.6 |
| `scripts/analyze_runtime_events.py` | **不動** | `by_filter_reason` 動態聚合，新 key 自動分桶 |
| `utils/runtime_events.py` | **不動** | `as_event_fields` 若 `TranslationOutcome` 加欄位會自動帶出 |

### 10.4 Sanitizer 規則設計

#### 可安全切除的片語

建議初始等於 `STT_TEMPLATE_CONDITIONAL_PHRASES`（Task #8 定義的 3 條 outro CTA）。用獨立常數 `STT_TEMPLATE_STRIP_PHRASES` 表示，未來可與 conditional 清單分開調整。

理由：Hard phrases（`자막 제공`、`광고를 포함하고 있습니다`…）任意位置出現都已被 Task #8 `is_stt_template_garbage` 拒絕，不會到達 sanitizer。若 hard phrase 因真實內容夠長而未被拒絕（extremely rare），是否也切除邊界 hard fragment → **留給 Codex §10.10 第 2 題決定**。

#### 切除演算法

1. Whitespace-normalize 整句。
2. **Leading 切除（iterative）**：若 text 以某個詞組開頭，切掉該詞組 + 後接的標點/空白（`. ! ? ~ … ·` 及空白）；重複直到無法繼續。
3. **Trailing 切除（iterative）**：若 text 以某個詞組（±後綴標點）結尾，切掉並 rstrip；重複直到無法繼續。
4. 切除後 `.strip()`。

#### 禁止的操作

- **禁止句中（internal）切除**：詞組前後都有文字 → 跳過，不切。
- **不重寫句子邏輯**：sanitizer 是純邊界剝除，不做語意重構。

#### 切除後最低閾值

移除後的剩餘文字須通過兩個守門（否則 reject）：

1. `significant_chars(remainder) >= 2`（沿用 `min_translate_chars`，不另設 config 旋鈕）
2. `KOREAN_CHAR_RE.search(remainder)` 必須命中至少一個韓文字元（切除後只剩 URL / 數字 / 標點 → 不值得翻）

### 10.5 Rejection / sanitize 順序

完整 `prepare_input()` 流程（新增 sanitizer 後）：

```
rejection_reason(text):
  "empty" / "duplicate" / "too_short" / "too_long"
  "stt_garbage"                       ← #6/#7
  "stt_template_garbage"              ← Task #8

prepare_input(text):
  early-return: empty / duplicate / too_long / stt_template_garbage
  self.last_input = text              ← 先記錄 original
  early-return: too_short / stt_garbage

  ← Task #9 新增：
  sanitized = strip_stt_template_fragments(text)
  if sanitized is None:               ← 切除後空/無韓文 → reject
      return None                     ← filter_reason 問題見 §10.6
  if sanitized != text:
      self.last_input = sanitized     ← 覆寫為 sanitized
  return sanitized
```

**關鍵順序約束**：

- Sanitizer 在所有 reject 之後、engine call / slang lookup / memory lookup / DB write 之前。
- `stt_garbage` / `stt_template_garbage` 先 reject 主導型模板；sanitizer 只處理通過 reject 但帶邊界片段的混合句。
- Sanitizer 在 `self.last_input = text` **之後**執行，成功後再覆寫為 sanitized（見 §10.8 風險 5）。
- Translator 拿到的 `text` 就是 sanitized text，所有下游（slang / cache / engine / DB）自然使用 sanitized key，**不需改 `translator.py` 主邏輯**（除非加 `sanitized` 欄位）。

#### filter_reason 架構缺口（sanitized-to-empty 時）

`translator.translate_event` 先呼叫 `rejection_reason(raw_text)`，再呼叫 `_prepare_input`。若 `prepare_input` 因 sanitized-to-empty 回傳 `None`，`filter_reason = rejection_reason(raw_text)` 此時為 `None`，translator 填 `"unknown"`。

三個修法選項（**待 Codex §10.10 第 1 題決定**）：

- **Option A（最簡）**：接受 `filter_reason="unknown"`。適合極罕見邊界案例（sanitized-to-empty），不值得改架構。
- **Option B（推薦）**：`strip_stt_template_fragments` 回傳 `None` 時，`prepare_input` 寫入 `self._last_sanitize_rejection = "stt_sanitized_empty"`；`translator.translate_event` 改成 `filter_reason = policy.rejection_reason(text) or policy._last_sanitize_rejection`。改動約 3 行，無破壞性。
- **Option C（最乾淨）**：`rejection_reason` 本身預算 sanitizer：若切除後太短 → 回傳 `"stt_sanitized_empty"`。代價：`rejection_reason` 是 pure function 但執行 sanitizer 兩次（可接受，無副作用）。

### 10.6 Runtime observability

#### `source_text` vs sanitized text

`TranslationOutcome.source_text = raw_text`（原始全文，含模板片段）。Translator 翻譯的是 sanitized text，回傳 `target_text` 對應 sanitized 版本。

Runtime JSONL 因此顯示 `source_text` 含模板、`target_text` 無模板——這是**刻意保留的可追溯性**，不應改 `source_text` 為 sanitized（否則丟失 STT 原始輸出）。

#### `sanitized: bool` 欄位（條件性，Codex §10.10 第 3 題決定）

建議在 `TranslationOutcome` 加 `sanitized: bool = False`；若 `prepare_input` 回傳的 text 與 raw_text 不同，translator 建立 outcome 時設 `sanitized=True`。

好處：JSONL 可用 `by_sanitized` 觀察命中率，日後擴充 `sanitized_source_text` 有基礎。
代價：需改 `TranslationOutcome` dataclass + 5 個回傳點 + 對應測試。

若 Codex 認為 `log.debug` 已足夠 → 省略此欄位，Task #9 **不碰 `translator.py`**。

#### 不需記錄 `sanitize_reason` / `removed_fragments`

這兩個欄位對 baseline 分析的增量價值低，且增加 event schema 複雜度。`log.debug` 記錄即可，不放進 JSONL。

#### DB / memory / cache key = sanitized text

`_record_success(sanitized_text, result, ...)` 以 sanitized text 為 key，自然正確。不同模板前綴 + 相同真內容 → sanitized key 相同 → cache 命中（正確行為）。

#### `last_input` = sanitized text（覆寫）

避免下一句無模板的同一真內容被誤當 duplicate 放過（反而是正確的 duplicate 靜默）。詳見 §10.8 風險 5 的時序問題。

### 10.7 測試計畫

`tests/test_translation_policy.py`（`strip_stt_template_fragments` 單元）：

- `test_strip_leading_conditional_template` — `시청해주셔서 감사합니다. 엄청나게 그렇잖아.` → `엄청나게 그렇잖아.`
- `test_strip_trailing_conditional_template` — `글씨는 영어시스템을 사용하여 사용하였습니다. 시청해주셔서 감사합니다.` → 前段韓文
- `test_strip_leading_subscribe_template` — `구독과 좋아요는 저에게 아주 큰 힘이 됩니다. 댓글로 남겨주세요!` → `댓글로 남겨주세요!`
- `test_strip_repeated_leading_template` — `시청해주셔서 감사합니다. 시청해주셔서 감사합니다. 진짜내용.` → `진짜내용.`（iterative strip）
- `test_strip_internal_template_not_removed` — `우리 채널에 시청해주셔서 감사합니다! 고맙습니다!`（模板在句中）→ 回傳原文，不切
- `test_strip_to_empty_returns_none` — 孤立 `시청해주셔서 감사합니다.` → `None`（Task #8 已在上游 reject，但 sanitizer 直接呼叫時仍應回 None）
- `test_strip_leaves_no_hangul_returns_none` — 切除後只剩 `ok!` → `None`
- `test_pure_template_still_rejected_by_guard_before_sanitizer` — `rejection_reason` 對孤立模板仍回 `stt_template_garbage`（確認 sanitizer 不干擾 guard 順序）
- `test_sanitized_last_input_prevents_duplicate` — `prepare_input("시청해주셔서 감사합니다. 진짜내용.")` 後，`prepare_input("진짜내용.")` 回 `None`（duplicate，last_input 存 sanitized）

`tests/test_translator.py`（translator 整合層）：

- `test_sanitizer_engine_receives_sanitized_text` — engine.translate arg 為 sanitized text（不是含模板的原文）
- `test_sanitizer_db_stores_sanitized_key` — `_record_success` 以 sanitized text 呼叫（不是原文）
- `test_sanitizer_outcome_source_text_is_original` — `outcome.source_text` 仍為含模板全文（可追溯性）
- `test_sanitized_to_empty_has_expected_filter_reason`（依 Option A/B/C 決定期望 reason）
- `test_sanitizer_sanitized_flag_true`（若決定加 `sanitized` 欄位）

### 10.8 風險與反對意見

1. **False positive sanitization**：某個 conditional phrase 是直播主真實口語句首。例：以「시청해주셔서 감사합니다」作為節目開場（極罕見）。緩解：3 條 conditional 詞組都是完整罐頭；短感謝（`감사합니다`、`구독 감사합니다`）不在清單，不被切。

2. **`source_text` 與字幕原文不一致**：字幕翻的是 sanitized text，但 JSONL `source_text` 是原始全文，`source_len` 大於實際翻譯的輸入長度。緩解：加 `sanitized: bool` 讓 analyst 知道哪些事件做了切除；或接受此不一致，在 digest 說明。

3. **cache key 語意模糊**：DB 無法直接反查「哪些 entries 是 sanitized 的」。可接受——若未來需 cleanup 另開 task。

4. **Internal template 不處理**：`구독 좋아요 댓글 부탁드려요! 우리 채널에 시청해주셔서 감사합니다! 고맙습니다!` 模板在句中，Task #9 不切，完整送翻。明確接受為本 task 限制。

5. **`last_input` 覆寫時序**：`self.last_input` 先設為 original（在 sanitizer 呼叫前），sanitizer 成功後再覆寫 sanitized。若 sanitizer 中途 raise → last_input 殘留 original，下次相同 original 輸入被 duplicate 靜默。緩解：sanitizer 為純函數（無 I/O、無副作用），實際上不會 raise；但若 Codex 認為需要防禦性保護，可用 `try/finally`（§10.10 第 4 題）。

6. **cache 暖身退化**：未來若從 `STT_TEMPLATE_STRIP_PHRASES` 移除某個詞組，舊 sanitized key 的 DB 記錄仍存在但新版 sanitizer 不切除 → cache miss → 重新翻譯。可接受，不是錯誤。

### 10.9 最終建議 scope（1 commit 可完成）

**必做**：

| 項目 | 檔案 |
|------|------|
| 新增 `STT_TEMPLATE_STRIP_PHRASES` 常數 | `utils/text_heuristics.py` |
| 新增 `strip_stt_template_fragments(text) -> str \| None` staticmethod | `modules/translation_policy.py` |
| `prepare_input` 呼叫 sanitizer，`last_input` 覆寫為 sanitized | `modules/translation_policy.py` |
| sanitized-to-empty 的 filter_reason 處理（Option A/B/C 擇一） | `modules/translation_policy.py`（+ 可能 `translator.py`）|
| policy 單元測試（9 案） | `tests/test_translation_policy.py` |
| translator 整合測試（4–5 案） | `tests/test_translator.py` |

**條件性**（依 Codex §10.10 第 3 題）：

| 項目 | 檔案 |
|------|------|
| `sanitized: bool = False` 加入 `TranslationOutcome` | `modules/translator.py` |
| 對應測試 `outcome.sanitized == True` | `tests/test_translator.py` |

**明確排除**：

- 句中（internal）片語切除
- `audio_confusion_score` / diarization / source separation
- 更換 STT 引擎或模型
- 已污染 DB/cache 的回溯清理（另開 task）
- daily digest 擴充
- UI / frontend / Tauri
- `stt.py` / `stt_policy.py` / shutdown / #7b

### 10.10 Questions for Codex（cross-review 重點）

1. **`filter_reason` 架構缺口**（§10.5）：你傾向 Option A（接受 `"unknown"`）、Option B（`_last_sanitize_rejection` instance 變數，推薦）、還是 Option C（`rejection_reason` 預算 sanitizer 兩次）？有沒有更簡潔的方案？

2. **Hard phrases 是否加入 `STT_TEMPLATE_STRIP_PHRASES`**（§10.4）？Task #8 已 reject 含 hard phrase 的句子，除非真實內容夠長讓 AND 條件失敗（`remaining_significant_chars` 大）。若出現「很長的真句 + 句首 `자막 제공`」，Task #9 是否也切除？反對：這種組合本就不應出現；支持：切比拒絕整句更合理。你同意擴充嗎？

3. **是否需要 `sanitized: bool` 欄位**（§10.6）？代價是改 `TranslationOutcome` dataclass + 5 個回傳點 + 相關測試。若認為 `log.debug` 已足夠，Task #9 可不動 `translator.py`，scope 更小。

4. **`last_input` 覆寫時序**（§10.8 風險 5）：sanitizer 為純函數理論上不 raise。你認為需要加 `try/finally` 防禦嗎？

5. **`test_strip_internal_template_not_removed` 定義**（§10.7）：`우리 채널에 시청해주셔서 감사합니다! 고맙습니다!`——模板前有介詞 `우리 채널에 `。你認為這算 internal（前方有文字 → 不切）？還是這種薄前綴應視為 leading（前綴 < N 字 → 允許切）？若需要，請建議 N 值。

---

> **§10（Task #9）狀態**：Claude 起草（2026-05-16）。Task #9 已 sign-off、已實作、已 push（commit `0a93617`）。本節保留為歷史。

---

## 十一、Task #10: incomplete 誤判根因修復（B1 — 內部完整前綴切分）

> 性質：下一個小型優化 plan 起草（**未 sign-off、未實作、未 push**）
> 流程：Claude 起草 → Codex cross-review 作答 → 雙方 sign-off → 才動工。與 #8/#9 一致。

### 11.1 問題陳述

`modules/sentence_splitter.py` 名為 splitter 但**不切句**：它只是 8 秒 time-box buffer，到 `force_cut_seconds` 就把**整坨 buffer 一次 emit 再 reset**（[sentence_splitter.py:53-64](modules/sentence_splitter.py#L53-L64)）。`incomplete=True` 的唯一產生路徑是 `SentenceBuffer.pop_ready` 的 forced 分支，`incomplete = not is_complete(整坨buffer)`（[sentence_buffer.py:66-75](modules/sentence_buffer.py#L66-L75)）。

`is_complete`（[sentence_buffer.py:22-30](modules/sentence_buffer.py#L22-L30)）只 suffix-match 整坨 blob 的最後幾個字對固定結尾清單，最後一行 `return False` 是預設值。

### 11.2 Runtime 證據（logs/runtime_events_20260518.jsonl，5 個真實 run，776 筆 incomplete=True success）

| 分類 | 數量 | 佔比 | 意義 |
|---|---|---|---|
| tail ∈ `SENTENCE_INCOMPLETE_ENDINGS` | 74 | **10%** | 真正句中被切，名副其實，B1 不動 |
| tail ∈ `SENTENCE_COMPLETE_ENDINGS` | 0 | 0% | 邏輯自洽 |
| tail 兩者皆非 → `return False` 預設 | 702 | **90%** | STT 碎尾觸發預設值，被誤標 |
| blob 內部已含句末標記（裡面有講完的句子被一起 lump） | 590 | **76%** | 完整內容被尾巴一票否決 |

source_len 中位數 44 字（非小碎片，是多子句長 blob）。**結論：`incomplete` 旗標 90% 是假的** —— 不是「說話被切斷」，是「8 秒到了而多句 blob 尾巴剛好不在 ~20 個結尾清單裡」。下游 [translation_memory.py:96](modules/translation_memory.py#L96) `if incomplete: return` 因此把這 90% 大多完整的內容全擋在 context/DB/recent 外。

### 11.3 B1 切分模擬（同一份 log）

對 776 筆做「找最後一個安全句界 → 切成 complete_prefix + residual」模擬：

| 結果 | 數量 | 佔比 |
|---|---|---|
| genuine partial（tail∈INCOMPLETE，B1 不動） | 74 | 10% |
| **RECOVERABLE**（complete prefix + residual，可救回） | **538** | **69%** |
| └ residual 瑣碎（≤3 sig chars，可安全 drop） | 77 | 14% of recoverable |

recoverable 的 prefix_len 中位數 34、residual_len 中位數 11。代表救回的完整前綴是內容主體，殘留多半短。

### 11.4 誤切風險（必讀，這是 Codex review 的核心精度邊界）

naive「`다`/`요`/`죠` + 空白」當切點**會誤切**。`다` 極常作副詞「全部/완전히」出現在句中：log 抓到 ≥33/776 危險樣本 ——
`9명이 찾고 ... 거의 다 하고`、`다 피곤하니까`、`다 도망가겠다`、`다 내 올게요`、`다 들고`、`다 캐리.`、`다 맞춰주셨나보다.`、`다 달라?`。在這些 `다 ` 後切會把一句話從中間劈斷，比現狀更糟。

因此 B1 的 boundary 偵測**不可** naive 用音節+空白，必須：
- 標點界（`. ? ! ~ …`）→ 高精度、低風險，一律安全。
- 形態素句末（`다/요/죠/네/...`）→ **只在不是獨立副詞/語助詞 token 時**才算界（例：boundary token 恰為單字 `다`、或前一 token ∈ {거의, 모두, 전부, 다, ...} 副詞語境 → 不切）。
- 重用既有 `SENTENCE_COMPLETE_ENDINGS`/`SENTENCE_INCOMPLETE_ENDINGS`（已編碼形態素知識），但施加在**內部候選界**而非僅 suffix。

### 11.5 設計

新增純函數（`modules/sentence_buffer.py`，無 I/O、無副作用）：

```
split_complete_prefix(text) -> tuple[str, str]   # (complete_prefix, residual)
```

- 由後往前掃描候選句界；取**最後一個安全界**（標點 > 形態素，形態素須過 §11.4 副詞守門）。
- 若找到 → 回 (界前的完整前綴, 界後殘留)；找不到安全界 → 回 (`""`, text)（無前綴可救）。

`pop_ready` forced 分支改為：
1. `prefix, residual = split_complete_prefix(buffer)`。
2. `prefix` 非空 → emit `prefix`，`incomplete=False`、`forced=True`（**新組合**：forced 但 complete）。
3. `prefix` 空（含 tail∈INCOMPLETE 的 genuine partial、或無安全界）→ 維持現狀：emit 整坨、`incomplete=True`。
4. residual 處理 = **§11.10 第 1 題，待 Codex 拍板**。

min_wait clean-cut 分支（[sentence_buffer.py:77-86](modules/sentence_buffer.py#L77-L86)）**不動**（§11.10 第 3 題確認）。

### 11.6 Residual 處理三選項（Codex 第 1 題）

- **(a) Drop**：最簡。但 recoverable 中 86% 殘留非瑣碎（中位 11 字），會丟內容。
- **(b) 另 emit 為 `incomplete=True` 獨立 cut**：保內容；殘留現在是「小而精準」的真碎片，語意正確。但碎片數略增。
- **(c) 殘留回寫 buffer 等下輪**：內容保全最佳。風險：殘留可能持續長大 / 永不完成 / `first_token_time` 與 elapsed 語意需重定義 / 與下輪 push 併接時序。

### 11.7 測試計畫（`tests/test_sentence_buffer.py` 既有，擴充）

- `split_complete_prefix`：標點界切分、形態素界切分、`다`副詞不誤切（`거의 다 하고`、`다 피곤하니까`）、無安全界回 `("", text)`、純標點尾、多句 blob 取最後界。
- `pop_ready` forced：recoverable → emit prefix 且 `incomplete=False forced=True`；genuine partial（tail∈INCOMPLETE）→ 維持 `incomplete=True`；residual 依選定 policy 的行為。
- 回歸：min_wait clean-cut 路徑行為不變；既有 `test_sentence_buffer.py` / `test_sentence_splitter.py` 全綠。

### 11.8 風險

1. **誤切（最高）**：§11.4。緩解 = 標點優先 + 形態素副詞守門 + 重用既有清單；Codex 認證守門規則。
2. **forced 但 incomplete=False 新組合**：下游／metrics／log 是否有假設 `forced ⇒ incomplete`？需掃 `cut.forced` / `cut.incomplete` 使用點。
3. **residual policy 連帶**：(c) 與 Task #9 sanitizer、duplicate 抑制、`last_input` 時序的交互（殘留回寫可能造下輪 duplicate 或 last_input 污染）。
4. **延遲**：B1 不增延遲（同一 forced 時點切，只是切得更準）；翻譯次數可能**略增**（prefix+residual 兩段）或**略減**（乾淨句更易 cache 命中）——淨值待 runtime 量。

### 11.9 Scope

| 動作 | 檔案 |
|---|---|
| 新增 `split_complete_prefix()` 純函數 | `modules/sentence_buffer.py` |
| `pop_ready` forced 分支改用 split | `modules/sentence_buffer.py` |
| residual policy 實作（依 §11.10 第 1 題） | `modules/sentence_buffer.py` |
| 對應測試 | `tests/test_sentence_buffer.py` |

**明確排除**：`is_complete` 既有簽章與 min_wait 路徑語意、`sentence_splitter.py` 驅動迴圈、STT/stt_policy、translator/translation_memory（B1 只改「切得準」，下游 `if incomplete` 是否解除留 Task #11）、analyzer/runtime_events、UI/frontend、daily digest、+0 API 設計目標不變。

### 11.10 Questions for Codex（cross-review 重點）

1. **Residual 處理**（§11.6）：(a) drop / (b) 另 emit incomplete / (c) 回寫 buffer？兼顧內容保全與低風險，你選哪個？(c) 的 buffer-state 風險可接受嗎，還是建議 (b) 起步、(c) 後續？
2. **形態素界副詞守門規則**（§11.4）：用「boundary token 恰為 `다`」+「前 token ∈ 副詞集」夠不夠？副詞集要列哪些？或有更穩的判定（例：只信標點界，形態素界一律不切，犧牲部分 recoverable 換零誤切）？
3. **是否只改 forced 路徑**（§11.5）：min_wait clean-cut 維持不動，同意嗎？
4. **`forced=True & incomplete=False` 新組合**（§11.8 風險 2）：可接受嗎？需不需要在 `SentenceCut` 加 `recovered: bool` 或調整 metrics 標籤？
5. **min prefix 長度守門**：是否需「prefix < N sig chars 就不切、整坨照舊」避免「極短 prefix + 巨大 residual」？建議 N？
6. **先後**：B1（切得準）與後續「下游解除 `if incomplete` 排除」（Task #11，治本第二步）的相依——B1 sign-off 後是否立即接 Task #11，還是先觀察 B1 runtime 再決定？

---

> **§11（Task #10）狀態**：Claude 起草（2026-05-18）。**等 Codex 在此節下方新增 cross-review 作答 → 雙方 sign-off → 才動工。未實作、未 push。**

### 11.11 Codex cross-review（2026-05-18）

**結論：✅ 同意動工，但採保守邊界策略。**

1. **Residual 處理：選 (c) 回寫 buffer，瑣碎 residual 可 drop。**  
   (c) 最符合內容保全與 +0 API 設計；但必須重設 `first_token_time = now`，避免下一輪立刻再次 forced。`residual` 若 significant chars ≤ 3 可直接丟棄，避免把語氣殘渣拖到下一句。暫不選 (b)：它會增加碎片翻譯與 API 次數，且需要 pending cut 或改回傳語意；風險不比 (c) 低。

2. **形態素界守門：目前規則不足以宣稱零誤切；B1 v1 建議只信標點界。**  
   `boundary token == 다` + 前 token 副詞集仍不夠，因為直播 STT tokenization 不穩，`다`/`요`/`죠` 也可能出現在口語中段。Codex 本地快速抽樣 `runtime_events_20260518.jsonl`：776 筆 `incomplete=True success` 中，僅用內部標點界就可救 557 筆（約 71.8%），已足夠接近 §11.3 的 69%。因此第一版建議：只用 `. ? ! ~ … 。？！` 這類標點界；形態素內部切分另開後續，不在 B1 v1 冒最高精度風險。

3. **只改 forced 路徑：同意。**  
   min_wait clean-cut 目前語意清楚：等到 `elapsed >= min_wait` 且整坨 `is_complete` 才 emit。B1 的問題根因在 forced time-box 尾巴一票否決，不應同時改 clean-cut，否則很難判斷 runtime 變化來源。

4. **`forced=True & incomplete=False`：可接受，不需要新增 `recovered`。**  
   掃描 `cut.forced` / `cut.incomplete` 使用點後，只有 `sentence_splitter.py` log 與 `transcription_to_sentence(...)` 傳遞；下游真正依賴的是 `incomplete`，沒有 `forced ⇒ incomplete` 的程式假設。`forced=True` 表示 time-box 觸發來源，`incomplete=False` 表示切出的文字語意完整，兩者可並存。測試需補這個新組合。

5. **min prefix 長度守門：需要，建議 N=6 significant chars。**  
   若 prefix 太短，例如只有「네.」「맞아요.」而 residual 很長，切出 prefix 的收益低、反而讓 buffer 狀態更複雜。建議 `prefix_significant_chars < 6` 時不 split，整坨維持原 forced 行為；標點後 residual ≤3 sig chars 則 drop。

6. **B1 與 Task #11 先後：先做 B1，跑 runtime 後再決定 Task #11。**  
   `translation_memory.py:96` 的 `if incomplete: return` 確實是品質損失點，但如果 B1 能把大量 forced blob 改成 `incomplete=False`，應先用 runtime 量化 recovered 比例與誤切率。Task #11 不應緊接著改，否則無法分辨是切分改善還是 memory policy 改善。

**Codex 補充檢查：**

- §11.1 / §11.2 的根因描述與現況一致：`sentence_splitter.py` 不切內部句，只 time-box；`SentenceBuffer.pop_ready` forced 分支一次 emit 整坨並 reset；`translation_memory.py:96` 會讓 `incomplete=True` 成功翻譯不進 recent/DB。
- §11.3 的 69% recoverable 不算明顯高估；以「只信內部標點」的保守口徑，Codex 快速重算仍約 71.8% 可切出 prefix。不過若 §11.3 模擬包含裸形態素界，應在實作/測試中改成標點-only baseline。
- §11.4 的誤切風險分析方向正確，但「標點優先 + 形態素副詞守門」不足以保證零誤切；本輪不應引入裸 `다/요/죠` 內部切分。

### 11.12 Codex post-implementation review（2026-05-18）

**結論：✅ 同意 push。**

驗收結果：

1. **Scope 符合**：commit `e47978a` 只改 `modules/sentence_buffer.py` 與 `tests/test_sentence_buffer.py`；未改 `sentence_splitter.py`、`translation_memory.py`、`translator.py`、analyzer、runtime events。
2. **B1 v1 邊界策略符合**：`split_complete_prefix()` 只看 `.?!~…。？！` punctuation boundary，沒有裸 `다/요/죠` 形態素內部切分；多句 blob 取最後 punctuation boundary，無 boundary 回 `("", text)`。
3. **Residual policy 符合 §11.11**：prefix significant chars `>= 6` 才 split；residual significant chars `> 3` 回寫 buffer，`<= 3` drop；回寫時重設 `_first_token_time = now`，避免下一輪立刻 forced。
4. **forced/incomplete 新組合可接受**：再次掃 `cut.forced` / `cut.incomplete` 使用點，只有 splitter log 與 sentence event 傳遞；下游依賴的是 `incomplete`，沒有 `forced ⇒ incomplete` 假設被打破。
5. **min_wait / is_complete 未偏離**：`is_complete()` 簽章與邏輯未改；min_wait clean-cut 路徑仍只在非 forced 且整坨 complete 時 emit whole buffer。
6. **測試結果通過**：`live-subtitle-env\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp` → `382 passed, 4 skipped, 23 subtests passed in 29.89s`。

非阻擋 notes：

- `split_complete_prefix("...")` 會回 prefix，但 forced path 因 prefix significant chars `< 6` 走原 whole-blob forced 行為；這符合保守策略。若 round 2 要補測，可加純標點字串與 threshold exact-boundary（prefix=6、residual=3/4）單元測試，但目前實作邏輯已符合 §11.11，不阻擋 push。
- residual 回寫保留 `_latest_source` 是合理的：residual 仍來自原 STT event；若後續 push 是 `TranscriptionEvent` 會更新 source。沒有發現 residual 永不完成的失控路徑；最壞情況會在下一個 force window 以原 forced 行為吐出。

## 十二、Task #11: Translation queue / latency observability（strict prefetch 量測）

> 性質：下一個小型優化 plan 起草（**未 sign-off、未實作、未 push**）  
> 流程：Codex 起草 → Claude Code cross-review → 雙方 sign-off → 才動工。  
> 前提：strict ordered queue + parallel prefetch 作為目前方向；**不做 soft skip、不做 Claude fallback、不改 timeout**。

### 12.1 問題陳述

NVIDIA NIM 實測 `p50≈1.4s`，但 `p95≈13s`、`p99≈20s`。目前已能推論尾延遲來自 `timeout=10s + retry=1` 或 NIM 偶發慢回應，但 runtime event 只記單一 `latency_ms`，無法拆出：

- 句子在翻譯 queue 等了多久。
- engine call 本身花了多久。
- strict ordering 因前句未完成讓後句卡了多久。
- 慢句是否真的 retry、retry 原因是 timeout/network/empty response。
- 超過候選 soft wait 的句子是否是語境依賴句（例如 `그래서/아니/근데/맞아/...` 開頭）。

因此現在不能直接決定 strict / hybrid soft / timeout / fallback。下一步必須先補 observability，讓下一輪 runtime 能用資料決策。

### 12.2 目標

在不改目前顯示語意的前提下，讓每個 translation event 可以回答：

1. 這句排第幾個？
2. 這句何時進 queue、何時開始翻、何時翻完、何時被 emit？
3. 這句慢是 engine 慢，還是被前句卡住？
4. 這句是否 retry？retry 原因是什麼？
5. 這句是否可能依賴前一句語境？
6. 如果未來候選 soft wait = 6/8/10 秒，哪些句子會被影響？

### 12.3 欄位定義

| 欄位 | 類型 | 每筆都有 | 語意 |
|---|---:|---:|---|
| `sequence_id` | int | 是 | translator thread 收到 sentence 後遞增編號；同一 run 內排序用。 |
| `queue_wait_ms` | float | 是 | 從 sentence 被提交到 pending queue 到 worker 開始 `translate_event()` 的等待時間。 |
| `engine_latency_ms` | float | 是 | worker 內實際 translation work 時間；目前等同 `translate_event()` duration，包含 policy/cache/API/retry。 |
| `output_delay_ms` | float | 是 | 從 sentence 被提交到結果被 runtime event emit 的總時間；包含 queue wait、engine time、strict ordering 等待。 |
| `predecessor_stall_ms` | float | 是 | 結果已完成但因前序 sequence 尚未 emit 而等待的時間。正常為 0；strict head-of-line blocking 會升高。 |
| `translation_worker_id` | str/int | 是 | 執行該句的 worker 識別，用來看是否單 worker 被卡、worker 分布是否正常。 |
| `retry_count` | int | 是 | engine 層 retry 次數。無 retry 為 0。 |
| `retry_reason` | str | 是 | 實際可達集：`""` / `empty_response` / `network` / `timeout`；只記主因。 |
| `starts_with_dependency_marker` | bool | 是 | 原文去空白後是否以語境依賴 marker 開頭。 |
| `dependency_marker` | str | 否 | 命中的 marker；未命中可為 `""`。 |

補充：

- `latency_ms` **維持現語意不重定義**：仍代表 worker 內 `translate_event()` / engine path 時間，與 #6 analyzer 歷史解讀相容。`engine_latency_ms` 可與 `latency_ms` 同值或作為更明確的新欄位；`queue_wait_ms` / `output_delay_ms` / `predecessor_stall_ms` 全部是 additive-only 新欄位。
- `queue_wait_ms + engine_latency_ms + predecessor_stall_ms` 應大致接近 `output_delay_ms`，允許少量 loop/poll 誤差。
- `schema_version` 不變，維持 schema v1 additive-only；不改任何既有 key 的語意。
- `retry_count/retry_reason` 初版只需支援 NVIDIA engine；其他 engine 填 `0/""`。
- `_NVIDIA_MAX_ATTEMPTS=2`，因此 `retry_count` 上限為 1。HTTP 401/404/429/5xx 走 `HTTPError` 分支，**不 retry**，`retry_count=0`、`retry_reason=""`。

### 12.4 寫入點

`modules/translator.py`：

1. 收到 sentence 時建立 task metadata：
   - `sequence_id`
   - `submitted_at`
   - 原始 sentence metadata
   - `starts_with_dependency_marker`
   - `dependency_marker`
2. worker 開始翻譯時記：
   - `started_at`
   - `translation_worker_id`
3. worker 完成時記：
   - `completed_at`
   - `engine_latency_ms = completed_at - started_at`
   - `queue_wait_ms = started_at - submitted_at`
4. strict ordered emit 時記：
   - `emitted_at`
   - `output_delay_ms = emitted_at - submitted_at`
   - `predecessor_stall_ms = emitted_at - completed_at`
5. 將上述欄位合併進 `outcome.as_event_fields(...)` 的 metadata。

`modules/translation_engines.py`：

- NVIDIA engine 目前 retry 在 engine 內部處理，需讓 translator 拿到 retry metadata。
- 優先方案：新增 module-local lightweight context，例如 thread-local `last_engine_diagnostics`，每次 `translate()` 前 reset，結束後寫入 `{retry_count, retry_reason}`。
- 不建議第一版改 `TranslationEngine.translate()` 回傳型別，避免污染所有 engine interface。
- 其他 engine 若沒有 diagnostics，translator 填 `retry_count=0`、`retry_reason=""`。
- diagnostics 寫入層必須在 `classify_error()` 把 timeout 收斂為 `network` 前，先特判 `socket.timeout` / `TimeoutError` / `URLError` reason 包含 timeout，獨立保留 `retry_reason="timeout"`。一般 `URLError` 或其他 network 類才記 `retry_reason="network"`。
- `predecessor_stall_ms = emitted_at - completed_at` 會包含最多 `_TRANSLATION_LOOP_POLL_SEC` 的 poll-gap 噪音；analyzer 解讀時需標註，或在摘要中說明該誤差上限。
- duplicate-suppressed event 仍走 strict ordering，也要照記 `predecessor_stall_ms`。但這類 event 的 `output_delay_ms` 是 pipeline 延遲，不是使用者實際可見延遲，需在 analyzer/docstring 標註。

### 12.5 Dependency marker

初版 marker list 保守，不用來改行為，只記錄風險：

```text
그래서, 그러니까, 근데, 그런데, 아니, 맞아, 그게, 그러면, 그럼,
그리고, 그러네, 그렇지
```

規則：

- 對 `source_text.strip()` 做 prefix match。
- marker 後必須是空白、標點、EOL，或 marker 本身就是獨立 token；不接受任意延續，避免 `그게` 誤命中 `그게임`。
- 不做形態素分析，不做 filter，不影響翻譯。

用途：

- 觀察 `predecessor_stall_ms > 8000` 的句子中，有多少是 dependency marker 開頭。
- 若高 stall 的後續句大多依賴前句，soft skip 風險高；若多為獨立句，可再設計 hybrid placeholder。

### 12.6 Analyzer 新增 summary

`scripts/analyze_runtime_events.py` 需要直接輸出數值欄位 percentile，不只把 JSONL 寫出來。

新增統計：

```text
Translation queue latency:
- queue_wait_ms: count/avg/max/p50/p95/p99
- engine_latency_ms: count/avg/max/p50/p95/p99
- output_delay_ms: count/avg/max/p50/p95/p99
- predecessor_stall_ms: count/avg/max/p50/p95/p99
```

新增 breakdown：

```text
Translation workers:
- by translation_worker_id count

Retry:
- retry_count total / average / max
- by retry_reason
- retry_success_count（retry_count > 0 且 status=success）
- retry_failed_count（retry_count > 0 且 status=failed）

Dependency marker:
- starts_with_dependency_marker total / ratio
- by dependency_marker top N
- high predecessor stall samples grouped by marker

Per-run breakdown:
- each run's queue_wait_ms / engine_latency_ms / output_delay_ms / predecessor_stall_ms p95/p99
- each run's dependency-marker ratio
- each run's retry rate
```

新增 samples：

- Top N `output_delay_ms`
- Top N `engine_latency_ms`
- Top N `predecessor_stall_ms`
- Top N `predecessor_stall_ms` where `starts_with_dependency_marker=True`

Analyzer output note:

- `predecessor_stall_ms` includes up to `_TRANSLATION_LOOP_POLL_SEC` poll-gap noise.
- strict prefetch currently uses one `Translator` per worker; each worker has its own recent context/cache. This is a known confound for interpreting `cache_status` and `engine_latency_ms`; Task #11 observes it but does not fix it.

### 12.7 測試計畫

`tests/test_integration.py` / `tests/test_translator.py`：

- strict prefetch 下 runtime event 包含 `sequence_id`，且依 emit 順序遞增。
- 第一句 slow、第二句 fast：
  - 第二句 worker 可先完成。
  - 第二句 emit 前 `predecessor_stall_ms > 0`。
  - 輸出仍保序。
- `output_delay_ms >= engine_latency_ms`。
- `queue_wait_ms` 在 worker 空閒時接近 0；pending 滿或 worker 忙時大於 0。
- dependency marker：
  - `그래서 ...` → `starts_with_dependency_marker=True`, `dependency_marker="그래서"`。
  - 一般句 → False。

`tests/test_translator.py` / `tests/test_translation_engines.py`：

- NVIDIA empty response retry 後，diagnostics 為 `retry_count=1`, `retry_reason="empty_response"`。
- NVIDIA timeout retry 後，diagnostics 為 `retry_count=1`, `retry_reason="timeout"`；必須在 `classify_error()` 收斂前保留 timeout。
- NVIDIA non-timeout network retry 後，diagnostics 為 `retry_count=1`, `retry_reason="network"`。
- HTTP 401/404/429/5xx 不 retry，diagnostics 為 `retry_count=0`, `retry_reason=""`。

`tests/test_analyze_runtime_events.py`：

- analyzer 對四個 `*_ms` 欄位輸出 p50/p95/p99。
- analyzer 統計 retry reason / retry success。
- analyzer 統計 dependency marker ratio 與 high stall samples。
- analyzer 輸出 per-run p95/p99、per-run dependency marker ratio、per-run retry rate。

### 12.8 Scope

| 動作 | 檔案 |
|---|---|
| translation queue timing 欄位寫入 | `modules/translator.py` |
| NVIDIA retry diagnostics 暴露 | `modules/translation_engines.py` |
| analyzer percentile / breakdown / samples | `scripts/analyze_runtime_events.py` |
| 對應測試 | `tests/test_integration.py`, `tests/test_translator.py`, `tests/test_analyze_runtime_events.py` |

### 12.9 明確排除

- 不做 soft ordered queue。
- 不做 silent skip。
- 不做 placeholder/backfill UI。
- 不做 Claude fallback。
- 不改 NVIDIA timeout。
- 不改 STT/VAD。
- 不改 prompt/glossary。
- 不改 frontend/Tauri/Vue。
- 不改熟肉初稿 export。
- 不 push。

### 12.10 Questions for Claude Code（cross-review 重點）

1. **`latency_ms` 語意**：§12 round 2 已改成不重定義 `latency_ms`，新欄位 additive-only。是否同意 `engine_latency_ms` 作為明確新增欄位，並讓 analyzer 對新欄位另算 percentile？
2. **retry diagnostics 設計**：用 thread-local / module-local diagnostics 是否足夠低侵入？還是應該引入正式 `TranslationResult` 型別，讓 engine 回傳 text + diagnostics？
3. **`predecessor_stall_ms` 計算**：用 `emitted_at - completed_at` 是否正確？duplicate subtitle suppression 時仍 emit runtime event，但不顯示字幕，是否也應計 predecessor stall？
4. **worker-local Translator 狀態**：strict prefetch 目前每 worker 一個 `Translator`，recent context/cache 會分散。這是否會污染 observability 判讀？是否需要先量測即可，暫不改？
5. **dependency marker list**：§12 round 2 已移除 `아/어/음` 並收緊 token boundary。窄集是否仍太寬或太窄？
6. **analyzer scope**：§12 round 2 已加入 per-run breakdown（每個 run 各自 p95/p99、dependency marker ratio、retry rate）。是否足夠決策 strict vs hybrid soft？

---

### 12.11 Claude Code cross-review（2026-05-19）

**結論：⚠️ 修正後再議。** 方向正確、scope 乾淨（純量測、不改顯示語意、additive），但有 3 個**會影響「跑完能不能回答 strict vs soft」**的實質修正，須在動工前改 plan。已對照 `modules/translator.py`、`modules/translation_engines.py`、`utils/api_retry.py` 現況查證。

#### 逐題作答（§12.10）

**Q1 `latency_ms` 語意 — ❌ 不同意重新定義（最重要修正 A）。**
現況 `emit_completed` 用 `outcome.as_event_fields(elapsed*1000, …)`，`elapsed = time.monotonic() - started`（包住 `translate_event` 的 per-worker 時間），即現在的 `latency_ms` ≈ engine/translate 時間。#6 已把 analyzer 的 p50/p95/p99 接在 `latency_ms` 上並如此解讀。若 strict prefetch 後把 `latency_ms` 改代表 `output_delay_ms`，**歷史 event 的 `latency_ms`（engine 時間）與新 event 的 `latency_ms`（含 head-of-line 的 output delay）會混在同一條 percentile**，趨勢線變垃圾。修正：`latency_ms` **維持現語意不動**（向後相容），`engine_latency_ms` / `queue_wait_ms` / `output_delay_ms` / `predecessor_stall_ms` 全部當**新增 additive 欄位**；analyzer 對新欄位另算 percentile。沿用 #6/#8/#9 的 schema_version=1 additive-only 紀律：永不對既有 key 改義。

**Q2 retry diagnostics 機制 — 同意 thread-local，不要新型別。**
ThreadPoolExecutor + per-worker `Translator` + 既有 `worker_state = threading.local()`（translator.py:423）已是現行模式，thread-local `last_engine_diagnostics`（每次 `translate()` 前 reset、結束後寫）與之一致、低侵入，適合這個明確「只量測」的 task。引入正式 `TranslationResult` 會動到每個 engine 的 `translate()` 簽章 + 全部 call site + engine 測試，blast radius 過大、不值得。**但須補一條**：`translate_event` 內若走 fallback chain，thread-local 會被最後一個 engine 覆寫；diagnostics 必須綁「實際產出結果的 engine」，並與 event 既有 `engine` 欄位交叉對照（fallback 本身就是訊號）。

**Q3 `predecessor_stall_ms` 計算 — 公式對，但定義要精確（精度註記，非阻擋）。**
`emitted_at - completed_at` 方向正確。但 emit 迴圈是 `collect_finished()`（從 `future.result()` 搬進 `completed`）→ `while next_emit_seq in completed`。future 實際完成的瞬間，與被 `collect_finished` 注意到的瞬間之間，隔著最多 `_TRANSLATION_LOOP_POLL_SEC` 的 poll gap。`completed_at` 必須在 **worker 函式內結束的當下** 取 monotonic（plan §12.4 step 3 已這樣寫，OK），但 step 4 的 `predecessor_stall_ms = emitted_at - completed_at` 會把 poll gap 一起算進「前句卡住」——其實那是 harness poll 噪音。修正：plan 明確聲明「`predecessor_stall_ms` 含上限 `_TRANSLATION_LOOP_POLL_SEC` 的 poll-gap 噪音」，或在分析時扣除；否則 stall 會被系統性高估。另：duplicate-suppressed 仍 emit runtime event（translator.py:480，`subtitle_emitted=False`），它**有**走 ordering，predecessor_stall 應照記；但須註明這類 event 的 `output_delay_ms` 是 pipeline 延遲、非使用者可見延遲。

**Q4 per-worker `Translator` 狀態 — 同意「先量測不改」，但必須在 analyzer 輸出標註為已知 confound。**
已查證 translator.py:440-443：每個 worker lazy 建自己的 `Translator()`，故 `TranslationMemory.recent` 與 cache **每 worker 各一份**。後果:同一句在 worker B 可能 cache miss 而 worker A 有 → `cache_status`、`engine_latency_ms` 判讀會被污染。本 task 不修是對的（修了就混淆「是切分/queue 改善還是 memory 改善」，與 §11.11 #6 同理），**但 plan 與 analyzer 輸出必須明寫這是已知 confound**，否則下一輪有人拿 cache hit ratio 或 engine latency 直接下結論會被誤導。

**Q5 dependency marker list — ⚠️ 太寬，移除 `아/어/음`，並收緊 prefix match（修正 C 之一）。**
`아`(啊)/`어`(呃)/`음`(嗯) 是口語 filler，不是回指/語境依賴訊號，會把 `starts_with_dependency_marker` 比例灌到 30–50%+，**直接摧毀這個指標對「soft skip 風險」的鑑別力**——而這正是整個 task 要回答的核心問題。採 Q5 自己提的窄集：`그래서/그러니까/근데/그런데/아니/맞아/그게/그러면/그럼/그리고/그러네/그렇지`。因為是純觀察、不改行為，寧可少算依賴也不要淹沒訊號。另一個正確性 bug：plan §12.5 允許「marker 後句子直接延續」= 裸 prefix，會讓 `그게` 命中 `그게임`（那遊戲）。修正:marker 須後接空白/標點/EOL 或為獨立 token，不接受任意延續。

**Q6 analyzer scope — ⚠️ 缺 run-level breakdown（修正 C 之二）。**
strict vs hybrid soft 的決策取決於尾延遲，而尾延遲隨內容劇變（唱歌場標點少、latency profile 與閒聊場完全不同——見 §11.11 內容類型論點）。單一跨 run 聚合 p95/p99 會把 2h 閒聊 + 1h 唱歌平均成「描述不了任何一場」的數字。**必須加 per-run rollup**:每個 run 各自的 4 個 `*_ms` p95/p99 + 各自 dependency-marker ratio + 各自 retry rate。analyzer 自 #6 起已有 run 切分能力，補 per-run 滾動即可。§12.6 目前只列日聚合，這是實質缺口。

#### 額外實質發現（plan 未涵蓋，最重要修正 B）

**`retry_reason` taxonomy 與現行 code 不符 — 會讓資料答不出核心問題。**
查證 translation_engines.py:432-489 + api_retry.py:8-34:
- `_NVIDIA_MAX_ATTEMPTS = 2` → `retry_count ∈ {0,1}`，plan 應明說上限 1。
- HTTP 429/401/404/**5xx** 走 `HTTPError` 分支 → **直接 return None，不 retry**。故 429 → `retry_count=0`（plan Q 對），但 plan 的 `retry_reason` 若含 5xx 重試是錯的，NVIDIA 5xx 不重試。
- 真正會 retry 的只有:(a) 空內容 → `empty_response`；(b) `URLError` → `network`；(c) 裸 `Exception` 且 `classify_error(e)=="network"`。
- **關鍵**:`classify_error`（api_retry.py:25）把 `"timeout"`/`"500"/"502"/"503"` 字串全歸 `"network"`。socket timeout 在 urlopen 多半是 `TimeoutError`/`URLError(timeout)`，最終都被收斂成 `network`。**所以 plan 列的 `retry_reason="timeout"` 在現行 code 幾乎不可達**——timeout 與一般 network 無法區分。

這是最嚴重的問題:本 task 的核心問題是「p99≈20s 的尾巴，是 **10s timeout 觸發後 retry**，還是 **NIM 慢但有完成**(retry_count=0、engine_latency 高)」。若 `timeout` 被併進 `network`，**資料就答不出這題**，整個 observability 失去主要目的。修正:diagnostics 寫入層必須在 `classify_error` 收斂前，先 `isinstance(e, (socket.timeout, TimeoutError))` 或檢出 URLError 的 timeout reason，獨立保留 `retry_reason="timeout"`；plan §12.3/§12.5 的 retry_reason 列舉須對齊實際路徑（`empty_response`/`network`/`timeout`，移除不可達或不重試的項，5xx 標明不 retry）。

#### 修正清單（動工前須改 plan）

- **A（阻擋）**:`latency_ms` 不重定義；新欄位全 additive。改 §12.3 補充與 Q1 對應段。
- **B（阻擋）**:`retry_reason` taxonomy 對齊 code，特別保留可達且關鍵的 `timeout` 區分（diagnostics 層在 classify_error 前特判）。改 §12.3/§12.5/§12.7 retry 測試。
- **C（阻擋）**:dependency marker 移除 `아/어/음` + 收緊 token 邊界；§12.6 加 per-run breakdown。
- **D（精度註記，非阻擋）**:§12.4 聲明 `predecessor_stall_ms` 含 poll-gap 噪音；§12.9/analyzer 標註 per-worker Translator cache/recent split 為已知 confound。

A/B/C 改完即可 sign-off 動工；scope（translator.py / translation_engines.py / analyzer + 測試）、明確排除（不 soft、不 fallback、不改 timeout）、+0 API 設計目標均無異議，方向認可。建議 Codex round 2 針對 A/B/C 修 plan 後，Claude round 2 複核 → 雙方 sign-off。

---

### 12.12 Codex round 2 修正（2026-05-19）

**結論：已依 §12.11 修正 A/B/C 三項阻擋問題，請 Claude round 2 複核。**

修正內容：

1. **A latency 語意**：§12.3 已改為 `latency_ms` 不重定義，維持現有 engine/translate 時間語意；`engine_latency_ms`、`queue_wait_ms`、`output_delay_ms`、`predecessor_stall_ms` 都是 additive-only 新欄位；`schema_version` 不變。
2. **B retry taxonomy**：§12.3/§12.4/§12.7 已明寫 `_NVIDIA_MAX_ATTEMPTS=2`，`retry_count` 上限 1；HTTP 401/404/429/5xx 不 retry；`retry_reason` 收斂為實際可達集 `""/empty_response/network/timeout`；timeout 必須在 `classify_error()` 收斂前獨立保留。
3. **C dependency marker + analyzer**：§12.5 已移除 `아/어/음`，改窄集；prefix match 改成 marker 後接空白/標點/EOL 或獨立 token，不接受任意延續；§12.6 已新增 per-run breakdown：四個 `*_ms` 的 p95/p99、dependency marker ratio、retry rate。
4. **D 精度註記**：§12.4/§12.6 已補 `predecessor_stall_ms` 含 `_TRANSLATION_LOOP_POLL_SEC` poll-gap 噪音；duplicate-suppressed event 仍記 stall，但 `output_delay_ms` 是 pipeline 延遲非使用者可見延遲。
5. **per-worker confound**：§12.6 已要求 analyzer 輸出標註 strict prefetch 目前每 worker 一個 `Translator`，recent/cache 分散會污染 `cache_status` 與 `engine_latency_ms` 判讀；本 task 只觀測不修。

未改事項：

- 仍不做 soft ordered queue。
- 仍不做 Claude fallback。
- 仍不改 timeout。
- 仍不改 STT/VAD、prompt/glossary、UI/frontend。
- 仍未實作、未 push。

---

### 12.13 Claude Code round 2 複核（2026-05-19）

**結論：✅ 同意動工。**

逐項對照 plan 本體（非僅 §12.12 自述）查證：

- **A 已解**：§12.3 補充明寫 `latency_ms` 維持現語意不重定義、`engine_latency_ms`/`queue_wait_ms`/`output_delay_ms`/`predecessor_stall_ms` 全 additive-only、`schema_version` 不變。
- **B 已解**：§12.3 明寫 `_NVIDIA_MAX_ATTEMPTS=2` → `retry_count` 上限 1、HTTP 401/404/429/5xx 不 retry（`retry_count=0`）、`retry_reason` 收斂為 `""/empty_response/network/timeout`；§12.4 明寫 diagnostics 寫入層須在 `classify_error()` 收斂前特判 `socket.timeout`/`TimeoutError`/`URLError(timeout)` 獨立保留 `retry_reason="timeout"`；§12.7 三條 retry 測試對齊（empty/timeout/非timeout-network/不retry）。最關鍵的 timeout-vs-slow-completion 區分能力已保住。
- **C 已解**：§12.5 marker 移除 `아/어/음` 採窄集、prefix 須後接空白/標點/EOL 或獨立 token（修掉 `그게`→`그게임` 誤命中）；§12.6 新增 Per-run breakdown（4 個 `*_ms` p95/p99 + dependency-marker ratio + retry rate），§12.7 補對應測試。
- **D 已納入**：§12.4 聲明 `predecessor_stall_ms` 含上限 `_TRANSLATION_LOOP_POLL_SEC` poll-gap 噪音、duplicate-suppressed event 照記 stall 但 `output_delay_ms` 為 pipeline 延遲非可見延遲；§12.6 Analyzer output note 標註 per-worker `Translator` cache/recent split 為已知 confound。

三項阻擋修正（A/B/C）確實落在 plan 本體 §12.3–§12.7，非僅摘要宣稱，複核重點通過。

**非阻擋實作註記（不影響 sign-off，留給實作者）**：§12.11 Q2 提過 —— `translate_event` 走 fallback chain 時 thread-local `last_engine_diagnostics` 會被最後一個 engine 覆寫。retry diagnostics 為 NVIDIA-only、其他 engine 填 `0/""`，故實作時 retry_count/retry_reason 應與 event 既有 `engine` 欄位交叉對照歸屬（fallback 發生時別把非 NVIDIA 的狀態誤記成 NVIDIA 的 retry）。現有 `engine` 欄位已足夠在分析時辨識，不需改 plan，但實作 PR 須注意此歸屬。

雙方修正循環完成：Codex 起草 → Claude §12.11 ⚠️（A/B/C/D）→ Codex round 2 修本體 → Claude round 2 ✅。可進實作。

---

> **§12（Task #11）狀態**：Claude Code round 2 複核 ✅ 同意動工（§12.13）。**雙方 sign-off 完成。等動工 → 實作 → post-implementation review → push。未實作、未 push。**

---

## 十三、Task #12: NVIDIA live timeout tail tuning

> **§13（Task #12）狀態**：見 §13.10 —— Claude round 2 ✅ 同意動工（方案 A，5s default），雙方 sign-off 完成。未實作、未 push。

### 13.1 問題陳述

Task #11 runtime observability 顯示目前 live 字幕的尾延遲主要不是 queue，也不是 strict ordering 本身，而是 NVIDIA NIM 偶發 timeout tail。

在 run `20260519T073837Z-57628` 中：

- `queue_wait_ms` 幾乎為 0，worker / pending queue 不是瓶頸。
- `predecessor_stall_ms` p95 很低，strict ordering 只有少數 outlier 被前一句 timeout 拖累。
- `engine_latency_ms` p99 約 12 秒，且 outlier 全部對應 NVIDIA timeout retry。

因此本 task 只針對 **live mode NVIDIA timeout tail** 做最小調整，目標是降低 p99，不改翻譯語意、不改顯示策略。

### 13.2 Runtime 證據

有效 runtime：

- `run_id`: `20260519T073837Z-57628`
- 長度：約 17 分 12 秒
- translation events：96
- NVIDIA translation：94

Task #11 欄位摘要：

- `engine_latency_ms`: p50=`1500ms`, p95=`3610ms`, p99=`12359ms`
- `output_delay_ms`: p95=`4016ms`, p99=`12375ms`
- `queue_wait_ms`: p95=`0ms`, p99=`15ms`
- `predecessor_stall_ms`: p95=`63ms`, p99=`3000ms`
- retry：3 筆，全部 `retry_reason="timeout"`

Claude round 1 進一步切分 engine latency 分佈：

| 區間 | 比例 |
|---|---:|
| `<1s` | 15.6% |
| `1–2s` | 61.5% |
| `2–3s` | 11.5% |
| `3–5s` | 8.3% |
| `5–7s` | 0% |
| `7–8s` | 0% |
| `8–9s` | 0% |
| `9–10s` | 0% |
| `10–13s` | 3.1% |

結論：

- 這次 run 呈現雙峰：NIM 要嘛在 `<4s` 左右完成（約 96.9%），要嘛 hang 到 timeout 後 retry 才回來（約 3.1%）。
- `[5s,10s)` 成功呼叫為 0；在這份資料中，降低 timeout 不會 flip 任何已成功的 5–10 秒呼叫。
- 原先「NIM 常在 7–10 秒內成功，所以 7s timeout 可能截斷成功呼叫」的風險，在此 run 被實證否定；但樣本仍只有單場、timeout retry 只有 3 筆，因此必須納入下一輪驗證。

### 13.3 設計

核心設計：

1. 新增 `cfg.nvidia.live_timeout`，不改既有 `cfg.nvidia.timeout`。
2. `NvidiaEngine.__init__` 依 `cfg.translation.translation_mode` 選 timeout：
   - `live` → 使用 `cfg.nvidia.live_timeout`
   - `clip` / offline → 保持既有 `cfg.nvidia.timeout`
3. 保留 `_NVIDIA_MAX_ATTEMPTS=2`，也就是最多 retry 一次。
4. 不改 engine 回傳型別、不改 runtime event schema；沿用 Task #11 的 `retry_count` / `retry_reason` / `engine_latency_ms` / `output_delay_ms` 做驗證。

已知實作 seam：

- `translation_engines.py` 既有先例已使用 `cfg.translation.translation_mode == "live"` 來切 live 行為。
- `NvidiaEngine.__init__` 目前設定 `self._timeout = cfg.nvidia.timeout`。
- NVIDIA request path 使用 `urlopen(..., timeout=self._timeout)`，因此 timeout 選擇可集中在 init，不需改呼叫流程。

選值策略不直接鎖死 7s。兩個可接受方案：

#### 方案 A：直接採 5s live timeout

依據：

- 本 run `[5s,10s)` 成功呼叫為 0。
- 5s timeout 可把現有 timeout tail 從約 `10s + 0.5s + retry_time ≈ 12s` 壓到約 `5s + 0.5s + retry_time ≈ 7s`。
- 若 retry 快速成功，p99 下降最明顯。

風險：

- 若其他直播情境中存在 5–10 秒慢成功，5s 會增加 retry。
- 最壞情況 retry 也 timeout，單句可能變成 `5s + 0.5s + 5s ≈ 10.5s`，仍低於目前約 12s，但 failed 風險可能增加。

#### 方案 B：A/B 驗證 5s vs 7s

依據：

- 7s 比 5s 保守，但本 run `[7s,10s)` 同樣沒有成功呼叫。
- 可先用 config 切換跑兩段同類內容，直接比較 p99 / retry success / failed。

風險：

- 7s 最壞情況 retry 也 timeout 時約 `7s + 0.5s + 7s ≈ 14.5s`，反而比現況約 12s 更糟。
- 因此若採 7s，驗證時必須特別看「retry 也 timeout」是否出現。

Codex 初始傾向：

- 若只做一個預設值，偏向 **方案 A：5s live timeout**，因為目前資料顯示 5–10 秒成功呼叫為 0，而且 7s 的 worst case 反而較差。
- 若 Claude 認為 n=3 timeout 樣本太薄，接受 **方案 B：保留可配置 live_timeout，先 A/B 5s vs 7s 再定 default**。

### 13.4 待驗假設

本 task 的核心假設不是「timeout 降低一定更快」，而是：

> 縮短 live timeout 後，retry-after-timeout 仍能快速成功，且 failed 不明顯增加。

必須驗證：

1. `retry_reason="timeout"` 的事件中，retry 後成功率是否維持可接受。
2. 新 timeout 下 `failed` translation 是否增加。
3. 最壞情況是否退化：
   - 5s：retry 也 timeout 約 `10.5s`
   - 7s：retry 也 timeout 約 `14.5s`
4. `engine_latency_ms p99` 與 `output_delay_ms p99` 是否實際下降。
5. `predecessor_stall_ms p99` 是否跟著下降，或仍被 timeout outlier 拉高。

成功標準：

- `engine_latency_ms p99` 從約 `12s` 明顯下降。
- `output_delay_ms p99` 跟著下降。
- `retry_rate` 不明顯暴增。
- `failed` translation 不明顯增加。
- 沒有出現大量 retry 也 timeout 導致的 worst-case 退化。

### 13.5 Scope

允許改動：

| 動作 | 檔案 |
|---|---|
| 新增 `cfg.nvidia.live_timeout` 設定 | `config.py` 或現有 config schema 定義處 |
| NVIDIA live/offline timeout 選擇 | `modules/translation_engines.py` |
| 對應測試 | `tests/test_translator.py` 或既有 NVIDIA engine 測試所在檔 |

原則：

- `cfg.nvidia.timeout` 保持既有語意，供 clip/offline 使用。
- `cfg.nvidia.live_timeout` 只影響 live mode。
- 實作必須可逆、可 A/B；使用者可調 5s / 7s / 10s。
- 不改 Task #11 runtime event schema。

### 13.6 明確排除

- 不做 soft ordered queue。
- 不做 placeholder / backfill。
- 不做 silent skip。
- 不做 Claude fallback。
- 不改 fallback chain。
- 不改 worker 數。
- 不改 prompt。
- 不改 STT / VAD。
- 不改 glossary / slang / dictionary。
- 不改 UI / frontend / Tauri。
- 不改 runtime event schema。
- 不改 `latency_ms` 語意。
- 不 push。

### 13.7 測試

建議測試：

`tests/test_translator.py` 或 NVIDIA engine 既有測試區：

- live mode 使用 `cfg.nvidia.live_timeout`。
- clip/offline mode 使用既有 `cfg.nvidia.timeout`。
- 若 `live_timeout` 未設定或為空，行為有明確 fallback（由實作決定，但測試要固定）。
- `_NVIDIA_MAX_ATTEMPTS` / retry_count 上限不變。
- HTTP 401/404/429/5xx 不 retry 行為不變。
- timeout / network / empty_response diagnostics 行為不變。

驗證命令：

```powershell
live-subtitle-env\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp tests/test_translator.py
live-subtitle-env\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp
```

實機 runtime 驗證：

- 用同類純聊天或看 clip 片段跑 15 分鐘。
- 若採方案 B，分別跑 5s / 7s 各一段同類內容。
- 比較：
  - `engine_latency_ms p95/p99`
  - `output_delay_ms p95/p99`
  - `retry_summary.retry_rate`
  - `failed` translation count
  - `predecessor_stall_ms p99`
  - timeout retry 後是否成功

### 13.8 Questions for Claude Code（round 2 複核）

1. 是否同意「先做 NVIDIA live timeout tail tuning，而不是 soft queue」？
2. 依據 `[5s,10s)=0`，你傾向：
   - 方案 A：直接採 5s live timeout default
   - 方案 B：保留可配置，先 A/B 5s vs 7s 再定 default
3. `cfg.nvidia.live_timeout` 是否應有 fallback？例如未設定時 fallback 到 `cfg.nvidia.timeout`。
4. 5s 的 worst case 約 10.5s、7s 的 worst case 約 14.5s；是否同意這使 5s 比 7s 更適合作為第一個 live default？
5. 測試範圍是否足夠？是否需要補 analyzer 測試，或 Task #11 欄位已足夠？
6. 有沒有任何 blocker，會讓你不建議動工？

### 13.9 Codex round 2 起草結論（2026-05-19）

Codex 立場：

- ✅ 同意先處理 NVIDIA timeout tail，不先做 soft queue。
- ✅ 同意新增 `cfg.nvidia.live_timeout`，保持 clip/offline timeout 不變。
- ✅ 同意不改 runtime schema，使用 Task #11 欄位驗證。
- ⚠️ timeout default 不再憑直覺鎖 7s；目前資料更支持 5s，但 n=3 timeout 樣本仍薄，需 Claude 複核是否先 A/B。

建議動工條件：

- Claude 對方案 A 或 B 給明確選擇。
- 明確決定 `live_timeout` default 與 fallback 行為。
- sign-off 後才實作、測試、commit；未 sign-off 前不動 code。

### 13.10 Claude Code round 2 複核（2026-05-19）

**結論：✅ 同意動工。** 三項 round 1 修正全部正確落在 plan 本體（非僅 §13.9 自述），明確排除清單完整保留，實作 seam 已於 round 1 對 code 查證（config.py:211 `_Nvidia.timeout`、engines.py:247 `translation_mode=="live"` 先例、engines.py:387 `urlopen(timeout=self._timeout)`）。

逐項複核：

- **修正 1（選值由資料定）已解**：§13.2 引用 run 分佈表、明寫 `[5s,10s)=0` 不 flip 任何成功呼叫、修正原「7–10s 常成功」風險描述為「實證否定但 n=3 單場須續驗」、§13.3 提方案 A/B 未鎖死 7s。
- **修正 2（薄證據納入）已解**：§13.4 明寫待驗假設（retry-after-timeout 仍快速成功且 failed 不增）、補齊驗證指標（retry 後成功率／failed／engine_latency p99／output_delay p99／predecessor_stall p99）、worst case 5s≈10.5s／7s≈14.5s、成功標準含「無 worst-case 退化」。
- **修正 3（Q3/Q4 升硬性）已解**：§13.3 新增 `cfg.nvidia.live_timeout`、不改 `cfg.nvidia.timeout`、live→live_timeout / clip-offline→timeout、實作 seam 明列、§13.5 可逆可 A/B。
- **明確排除完整**：§13.6 含 soft queue / placeholder / silent skip / Claude fallback / fallback chain / worker / prompt / STT-VAD / glossary / UI / runtime schema / `latency_ms` 語意，全保留。

#### 動工決策（回答 §13.8）

1. **同意先做 timeout tuning，不做 soft queue。** Task #11 資料無歧義：queue_wait p99=15ms、3/3 retry 全 timeout、tail 純由 NIM timeout 造成。

2. **選方案 A：`live_timeout` default = 5s。** 理由：本 run `[5s,10s)=0` 且雙峰（<4s 成功 or hang 到 timeout）；5s 把 tail 從 ~12s 壓到 ~7s，且 5s 的 worst case（~10.5s）**優於現況 12s**，而 7s 的 worst case（~14.5s）**劣於現況**——7s 在此資料下被 5s 嚴格支配（無觀測效益、worst case 更差）。**不需方案 B 的獨立 A/B 段**：§13.4 的驗證 run 已量到判斷 5s 是否過激所需的全部指標（failed、retry_rate、retry-after-timeout 成功率）；且 `live_timeout` 保持可配置，若驗證顯示其他內容有 5–10s 慢成功，改回 7s 只是一行 config，不需重 plan。即「A 為 default + 驗證 run 為 gate + config 為退路」＝B 的安全網但無冗餘 A/B 段。

3. **`cfg.nvidia.live_timeout` fallback：是，但用 dataclass default 表達。** `_Nvidia.live_timeout: int = 5`（dataclass 預設即 5s，鎖定方案 A 為 out-of-box 行為）；僅當被顯式設為 falsy/None 時才解析回 `cfg.nvidia.timeout`（安全退路，未設定不致驚訝回歸）。純 additive、向後相容。

4. **5s 比 7s 更適合作第一個 live default：同意。** 理由同第 2 點——worst-case 不退化是硬指標，7s 違反它而 5s 不違反。

5. **測試範圍：Task #11 欄位已足夠，不需新增 analyzer 測試。** Task #12 只改 timeout 值、不改量測什麼；§13.4 驗證用既有 `engine_latency_ms`/`output_delay_ms`/`predecessor_stall_ms`/`retry_count`/`retry_reason`/`retry_summary`（Task #11 已驗證）。§13.7 scope 足夠，但須補兩條具體 config 測試把方案 A 鎖死：(a) `_Nvidia.live_timeout` 預設值 == 5 且 live mode 解析到它；(b) `live_timeout` 顯式 unset/falsy → fallback 到 `cfg.nvidia.timeout`；(c) clip/offline mode 仍用 `cfg.nvidia.timeout`。

6. **無 blocker。** 方向已被 Task #11 實證、scope 最小、可逆。

#### 動工指示（給實作方）

- `config.py` `_Nvidia` 新增 `live_timeout: int = 5`；不動 `timeout`。
- `NvidiaEngine.__init__`：`self._timeout = cfg.nvidia.live_timeout if (cfg.translation.translation_mode == "live" and cfg.nvidia.live_timeout) else cfg.nvidia.timeout`（live 且 live_timeout truthy → live_timeout；否則 timeout）。
- 不改 retry 上限、HTTP no-retry、diagnostics、runtime schema、`latency_ms`。
- 補 §13.7 + 上述 (a)(b)(c) 三條 config 測試；跑 targeted 再 full suite。
- 本地 commit（message 勿被 here-string 污染前綴）；不 stage `OPTIMIZATION_*.md` / `.pytest-*`；不 push；回報走 post-implementation review。

雙方 sign-off 完成：Codex 起草 → Claude round 1 ⚠️（3 修正）→ Codex round 2 修本體 → Claude round 2 ✅（方案 A）。可進實作。

---

> **§13（Task #12）狀態**：Claude round 2 ✅ 同意動工（§13.10，方案 A：`live_timeout` default 5s + unset fallback 到 timeout）。雙方 sign-off 完成。等動工 → 實作 → post-implementation review → push。未實作、未 push。

---

## 十四、Task #13: VTuber / 專有名詞 glossary（最小資料化）

> 性質：plan 起草（**未 sign-off、未實作、未 push**）
> 流程：Claude 起草 → Codex cross-review → 雙方 sign-off → 才動工。與 #8–#12 一致。
> 來源：`OPTIMIZATION_QUALITY_AUDIT_20260519.md` §4（ROI #1，最高頻、肉眼損害最大、純加法）。

### 14.1 問題陳述

2026-05-19 翻譯品質審計：filtered/failed 不是問題，**success 裡的專有名詞翻譯**才是肉眼損害主體（~15–20%）。同一實體被 STT 拆 + 模型音譯，渲染 5+ 種不一致：
- `챈나`（本場主播，HADES 成員，= config `hades_chxxnnx`）被 STT 聽成 챗나/챗나룡/챗나룸/찬나/츤나/챗마，翻成 -chan/-chan龍/ChanRoom/chatma…
- `마크`（=마인크래프트 / Minecraft）被當人名翻成 `Mark`（마크 서버→Mark伺服器，意思全錯）
- Isegye/HADES 成員音譯亂碼（고세구→高世久 而非官方 Gosegu）

使用者已拍板：**目標形一律官方羅馬拼音；人名綁 profile、遊戲詞全域。** 已查證名單見審計 §4A。

### 14.2 現有機制（plan 必須重用，不新建 infra）

| 機制 | 檔案 | 作用 | 範圍 |
|---|---|---|---|
| `default_slang.json`（71 筆 ko→zh） | `data/default_slang.json` → `cfg.translation.slang` | (a) `TranslationPolicy.slang_result` 整句精確匹配短路；(b) 注入 system prompt `【常用詞彙對照】k→v` 軟提示 | **全域** |
| `streamer_profiles.json` `stt_terms` | `data/streamer_profiles.json` → `build_stt_glossary()` | 注入 **Groq STT prompt**「Prefer exact spellings: …」→ 偏置 STT 正確拼寫 | **綁 profile** |
| `translation_profiles.json` few-shot | `data/translation_profiles.json`（standard + qwen 兩份，id 須一致） | 注入翻譯 system prompt 的 per-profile few-shot 範例 | **綁 profile** |
| `aliases` 欄位 | `streamer_profiles.json` | 已定義但**全無消費端（dead field）** | — |

架構已天然支援使用者決策。本 task 幾乎純資料，最小（或零）code。

### 14.3 設計

**A. 遊戲 / 通用詞 → 全域**（`data/default_slang.json`）
- 新增 `마크 → Minecraft`。**value 必為乾淨 direct output（=`Minecraft`）**；消歧說明（「마인크래프트/遊戲、非人名」）**不得寫入 JSON value**，改放 hades_chxxnnx few-shot / prompt wording（B3 已 code 證實 value 會 direct output：translator.py:272 + prompt 逐字注入）。
- `섭주`系採**多 key**（B2 Option A）：`섭주 / 섭쥬 / 썹주 / SUBJU → 服主`、`섭쥬방 → 服主房`，value 全乾淨 direct output。理由：`slang_result` 為整句 `dict.get`，且 prompt 逐 key 注入；canonical-only 無法讓 prompt 對 STT 變體給明確指引。
- **B1 cleanup（§14.9 新增 scope）**：移除既有**衝突的裸人名全域詞條** `키마→Kima`、`봉준→Bongjun`、`성태→Sungtae`、`히나→Hina`（與 §4A Kyma/Kim Bongjun/KimSungtae/Shirayuki Hina 衝突，且經 prompt 注入 + exact-match 短路會壓過 profile few-shot）→ 改交 profile few-shot。非衝突全名詞條（`시라유키 히나→Shirayuki Hina`、`아야츠노 유니→Ayatsuno Yuni`，與 §4A 一致）不動，避免 scope creep。此為清理既有衝突全域資料，非新增人名。
- `대표님` 經 user 確認為通用「代表/老闆」非特定人 → **不入 glossary**，維持普通詞翻譯。
- 純資料；沿用既有 prompt slang 注入路徑。

**B. 人名 → 綁 profile**（雙既有通道，皆純資料）
- `streamer_profiles.json` `hades_chxxnnx.stt_terms`：加 `챈나`、`띵귤` 等 canonical 韓文 → 偏置 Groq STT 把它聽對，**從根因壓低 챗나/찬나 mishear**。`단위님 / 렌트님 / 지효 / 민지 / 지상` 經 user 確認身分不明、非重點 → **DEFER**，不入本批，日後增量補。
- `translation_profiles.json` `hades_chxxnnx`（**standard 與 qwen 兩份都要加**，qwen 為現行引擎 qwen3-next-80b）：加 canonical→官方羅馬 few-shot 範例（챈나→Chxxnnx、봉준→Kim Bongjun、고세구→Gosegu…），讓模型一致渲染。
- Isegye/StelLive 名同法放對應 profile（isegye_lilpa / stellive_hina）。

**C. 資料來源**：審計 §4A 表（已 web 查證高信心）。4B 未決項（단위님/렌트님 身分等）設計成「補一行 stt_terms + 一條 few-shot」即可，**不需改 code、不需重 plan**。

### 14.4 Scope

| 動作 | 檔案 |
|---|---|
| 加 `마크→Minecraft`（value 乾淨 direct output，無消歧長句） | `data/default_slang.json` |
| 加 `섭주/섭쥬/썹주/SUBJU→服主`、`섭쥬방→服主房`（多 key，B2-A） | `data/default_slang.json` |
| **清理既有衝突裸人名詞條**：移除 `키마/봉준/성태/히나`（B1，非新增人名，是消除 prompt 注入 + exact-match 短路衝突） | `data/default_slang.json` |
| hades/isegye/stellive profile 加 canonical `stt_terms` | `data/streamer_profiles.json` |
| 對應 profile 加 canonical→羅馬 few-shot（standard+qwen 兩份；含 마크 消歧 wording 改放此處） | `data/translation_profiles.json` |
| 資料載入/形狀回歸 + prompt/slang 斷言（見 §14.6） | 既有 `tests/`（test_translation_prompts / test_streamer_profiles 等所在） |

### 14.5 明確排除

- **不做 STT 變體硬收斂**（챗나/찬나/츤나→챈나 的字串替換）= 審計 #4 source-normalization，另開 task、需 code + 詞邊界守門、排在本 task 之後（除非驗證後判定為達成本 task 目標的 blocker —— §14.9 驗證後判定**非** blocker，stt_terms 偏置足夠作第一步，mishear 殘留列 post-impl validation）。
- **不新增**人名到 `default_slang.json`；但本 task **可清理/移除既有衝突裸人名全域詞條**（키마/봉준/성태/히나），前提是為消除 prompt 注入矛盾與 exact-match 短路風險（B1）。
- 不啟用/不接 `aliases` dead field（要接就是 code，超出最小資料化）。
- 不改翻譯引擎/policy/runtime schema/timeout/UI；不改 `marker`/Task #11/#12 邏輯。
- 不 push。

### 14.6 測試計畫

1. **B1 既有衝突人名清理**：斷言 `default_slang.json` **不再含** `키마/봉준/성태/히나` keys（final option = 移除）；非衝突全名 `시라유키 히나/아야츠노 유니` 仍在且 value 與 §4A 一致。
2. **B2 섭주 多 key**：斷言 `섭주/섭쥬/썹주/SUBJU → 服主`、`섭쥬방 → 服主房` 皆存在於 default_slang。
3. **B3 value 純淨**：斷言 `마크` value == `Minecraft`、`섭주`系 value ∈ {`服主`,`服主房`}，且不含「遊戲非人名/마인크래프트」等解釋句；可加通用斷言「default_slang 所有 value 長度/字元符合 direct-output 形」。
4. **standard/qwen 同步**：斷言 `translation_profiles.json` 中 hades/isegye/stellive 對應 few-shot 在 standard 與 qwen 兩份皆含 canonical→官方羅馬 mapping（loader 只強制 id 相等、不強制內容，故須測內容對稱）。
5. **stt_terms wiring**：§14.9 已 code 證實 wiring 存在（stt.py:17/339-344 → stt_policy.py:119）→ 測 hades_chxxnnx 啟用時 STT glossary 含 `챈나`，非該 profile 不含。
- prompt 組裝測試：hades_chxxnnx 啟用時 system prompt 含 `챈나`/`Chxxnnx`；非該 profile 不含（profile 綁定）。
- `slang_result` 對整句「마크」「섭주」回乾淨 value（精確匹配語義不破既有正例）；既有 translator/policy/prompt 測試全綠。

### 14.7 Questions for Codex（cross-review 重點）

1. **人名渲染通道**：採「`stt_terms`（STT 拼寫偏置）＋ `translation_profiles` few-shot（翻譯渲染）」雙既有通道，不碰 dead `aliases`、不新增結構化 glossary 欄位 —— 同意此為最小且足夠?還是你認為需要結構化 per-profile name map（代價：新 data schema + 載入 code + prompt 渲染 code，已非最小資料化）?
2. **마크 消歧**：軟 prompt 提示（資料：default_slang + 既有 slang 注入，標「마인크래프트/遊戲、非人名」）vs 硬 regex 前替換（마크 後接 서버/영상/팬/맵 才換，需 code）。建議先軟、runtime 不足再升硬 —— 同意?
3. **與 #4 source-normalization 邊界**：確認本 task 對 챗나/찬나 mishear **只**用 `stt_terms` 偏置，硬收斂延後 #4。同意此切分?
4. **default_slang 精確匹配風險**：確認人名不進 default_slang.json（只全域遊戲/idiom 詞進），避免 `slang_result` 短路；可接受?
5. **qwen/standard 對稱**：few-shot 必須同步加到 `translation_profiles.json` 的 standard 與 qwen 兩份 hades_chxxnnx（id 一致性既有強制）。確認?
6. **測試策略**：上述資料載入 + prompt-contains 斷言是否足夠?還是需要 runtime A/B（跑一場看 챈나/마크 渲染一致率）才算驗證?若需，列為 post-implementation 驗證項而非阻擋。

---

> **§14（Task #13）狀態**：Claude 起草 → Codex cross-review §14.8（REVISE）→ **Claude round 2 §14.9 已完成、§14.3–§14.6 已修**。等 Codex re-review（只驗 §14.8 blocker 是否真解）。未實作、未 push。


### 14.8 Codex cross-review (2026-05-19)

**Decision: REVISE before implementation.** The proposed direction is sound, but the current data has one blocker: `data/default_slang.json` already contains global person-name entries, and some conflict with the official targets in audit section 4A. Because `default_slang` is both injected into the prompt and used by exact-match `slang_result()`, leaving those conflicts in place can undermine the new profile-specific few-shot rules.

#### A. Claim verification

| Claim | Verdict | Evidence | Review |
|---|---|---|---|
| C1 `default_slang.json` feeds `cfg.translation.slang`, exact-match slang short-circuit, and prompt vocabulary injection | supported | `config.py:72-84`, `config.py:149`, `modules/translation_policy.py:139`, `modules/translator.py:269-272`, `modules/translator.py:370-378`, `modules/translation_prompts.py:55-57`, `modules/translation_prompts.py:247-249` | Mechanism verified. Values for `마크` and `섭주` must be clean direct outputs, not long disambiguation text. |
| C2 Add global `마크 -> Minecraft` | supported | Audit sections 1-2 show `마크 서버/영상/팬` being translated as Mark; audit section 4A classifies `마크` as Minecraft in the current game context | Reasonable as a first data-only fix. The disambiguation note "Minecraft/game, not person name" cannot safely live as the JSON value; put it in prompt/few-shot wording if needed. |
| C3 Add global `섭주 -> 服主` and variants | partially supported | Audit section 4B user decision: `섭쥬/썹주/SUBJU = 섭주`, global role term, target `服主` | `섭주` itself is supported. If the goal includes `섭쥬/썹주/SUBJU/섭쥬방`, the plan must say whether each variant becomes its own key or is only prompt guidance. One canonical key alone will not cover exact-match variants. |
| C4 `streamer_profiles.json` `stt_terms` can bias Groq STT spelling | supported | `modules/streamer_profiles.py:31-54` loads `stt_terms`; `modules/streamer_profiles.py:74-80` builds `Prefer exact spellings...`; `modules/stt.py:344` passes `build_stt_glossary` into STT | Good minimum first step. It is a bias, not a deterministic correction, so it will not guarantee all `챗나/찬나/츤나` variants disappear. |
| C5 `translation_profiles.json` standard/qwen profiles are injected into the translation system prompt | supported | `modules/translation_prompts.py:19-32` loads profiles and enforces standard/qwen id equality; `modules/translation_prompts.py:35-42` exposes `get_translation_profile`; `modules/translator.py:419-424` appends active profile based on qwen mode | Mechanism verified. Tests must verify content parity, not only id parity. |
| C6 `aliases` is a dead field and should not be wired in this task | supported | `modules/streamer_profiles.py:11-12`, `modules/streamer_profiles.py:53-54`; `build_stt_glossary()` consumes only `profile.stt_terms` | Correct non-goal. Wiring aliases would be code-path work, not minimum data work. |
| C7 Person names should not be added to `default_slang.json` | partially supported / blocker | The principle is correct, but current data already has global person-name entries: `data/default_slang.json:66-70` contains `키마 -> Kima`, `히나 -> Hina`, `봉준 -> Bongjun`, `성태 -> Sungtae`; audit 4A targets include `키마 -> Kyma`, `봉준/김봉준 -> Kim Bongjun`, `성태 -> KimSungtae` | This is an actual conflict. If Task #13 only adds profile few-shots while leaving these globals, the prompt can contain contradictory guidance and exact-match inputs can still short-circuit to old values. |

#### B. Goal fit

- The proposal can partially achieve the goal for canonical STT text: profile few-shots can make `챈나 -> Chxxnnx` and member names render more consistently, while global `마크` and `섭주` reduce obvious mistranslations.
- It cannot guarantee correction of STT mishears such as `챗나/찬나/츤나`; `stt_terms` is only prompt bias. This is acceptable because source-normalization is explicitly a later task.
- The current blocker is the existing global person-name conflict in `default_slang.json`. Without a cleanup or correction rule in scope, official romanization consistency is not actually guaranteed.

#### C. Non-goal check

- Not doing hard STT variant normalization is reasonable; it is not required for this data-only task.
- Not adding person names to `default_slang.json` is reasonable, but the scope must also handle existing conflicting person-name globals. Otherwise the non-goal is only forward-looking and does not protect the current system.
- Not wiring `aliases` is reasonable; it would require new code behavior.
- Deferring unknown names and excluding `대표님` are supported by user decisions and are not blockers.
- Not touching engine/policy/runtime schema/timeout/UI/Task #11/#12 is reasonable.

#### D. Risk grading

**Blockers**
- Existing global person-name conflicts in `default_slang.json`, especially `키마`, `봉준`, and `성태`, must be explicitly cleaned up or corrected in the Task #13 scope.
- Variant handling for `섭주` must be explicit: either add separate keys for `섭쥬`, `썹주`, `SUBJU`, `섭쥬방`, or state they are prompt-only and adjust tests accordingly.

**Non-blocking risks**
- Global `마크` can still mis-handle rare person-name contexts; current runtime context is Minecraft, so this is acceptable for a first pass.
- `stt_terms` may not reduce mishears enough; verify with runtime before opening source-normalization.
- Standard/qwen profile content parity is not enforced by loader, so tests need to enforce the specific entries.

**Post-implementation validation**
- Runtime sample: `마크 서버/영상/팬` should render as Minecraft, not Mark.
- Runtime sample: `섭주/섭쥬/썹주/SUBJU` should render as `服主` when present.
- Runtime sample: HADES names should not emit old values like Kima/Bongjun/Sungtae when audit 4A targets say Kyma/Kim Bongjun/KimSungtae.
- Track whether `챗나/찬나/츤나` mishears decrease; if not, open source-normalization rather than expanding Task #13 silently.

#### E. Decision

**REVISE before implementation.** I agree with using existing `stt_terms`, `translation_profiles`, and global slang channels, but I do not sign off on the current scope as written. Required plan fixes:
1. Add explicit cleanup/correction of existing conflicting global person-name entries in `default_slang.json` (at least `키마`, `봉준`, `성태`).
2. Specify whether `섭주` variants are separate global keys or prompt-only guidance, and align tests with that choice.
3. Specify that `마크` and `섭주` JSON values stay clean direct outputs; disambiguation text belongs in profile/prompt examples, not values.

---

### 14.9 Claude round 2 (2026-05-19) — verification log

逐項用 code/data/audit 獨立查證，非背書亦非反駁。

| Codex claim (§14.8) | Verification | Evidence | Action |
|---|---|---|---|
| C1 default_slang 餵 cfg.slang + exact-match 短路 + prompt 注入 | ✅ supported | `translation_policy.py:139 slang_result=self._slang.get(text)`（整句 dict.get，僅全字串命中）；`translator.py:269-272` slang hit 即 return `result_source="slang"` 繞過 engine/profile；`translation_prompts.py:55` 把**全部** `cfg.translation.slang.items()` 注入 `【常用詞彙對照】k→v` | adopted（B1/B3 機制基礎） |
| C2 加全域 마크→Minecraft 合理 | ✅ supported | 審計 §1–§2 마크 서버/영상/팬 誤譯 Mark；§4A 分類遊戲詞 | adopted |
| C3 섭주 + variants | ⚠️ partially | §4B/§4A 已列 변體 섭쥬/썹주/SUBJU/섭쥬방；審計 §3 line 50/121/134 顯示 썹주/SUBJU/섭쥬 有 runtime 出現；섭주 canonical/섭쥬방 為列舉 | partially adopted → **B2 Option A 多 key** |
| C4 stt_terms 偏置 Groq STT 拼寫 | ✅ supported（**獨立重驗**） | `stt.py:17` import build_stt_glossary；`stt.py:339-344` 傳 glossary_builder；`stt_policy.py:119` `glossary_builder(active_profile)` 注入 Groq prompt | adopted；wiring 確存在，非 blocker |
| C5 translation_profiles standard/qwen 注入翻譯 prompt | ✅ supported | `translation_prompts.py:19-42` 載入並強制 id 相等；`translator.py:419-424` 依 qwen 模式附 profile | adopted → 測試須驗**內容**對稱（loader 只驗 id） |
| C6 aliases dead field 不接 | ✅ supported | `streamer_profiles.py:11-12/53-54` 定義載入；`build_stt_glossary` 只用 `stt_terms`，無 `.aliases` 消費端 | adopted（維持 non-goal） |
| C7 既有衝突裸人名全域詞條（키마/봉준/성태[/히나]）= blocker | ✅ supported → **blocker** | `default_slang.json` 實含 `키마→Kima`/`봉준→Bongjun`/`성태→Sungtae`/`히나→Hina`；§4A 目標 Kyma/Kim Bongjun/KimSungtae/Shirayuki Hina → 衝突；經 C1 prompt 注入 ⇒ 系統 prompt 同時含全域舊值與 profile few-shot 新值（矛盾指引），task 目標「一致化」邏輯上不可達；exact-match 短路再加一個繞過 profile 的確定性洞 | adopted as **blocker**；採 **B1 Option B（移除）** |

**駁回/部分駁回說明（anti-framing）**：無整條駁回。C3 部分採納 —— Codex「canonical-only 不覆蓋 exact-match variants」技術上對但語義次要（exact-match 需整句==變體，罕見），真正覆蓋路徑是 prompt 逐 key 注入；故不採「prompt-only」而採多 key（B2-A），既保留 Codex 有效貢獻（變體須明確處理）又對齊既有 slang 多變體風格（ㅋㅋ/ㅋㅋㅋㅋ）。C7 我把 Codex evidence 內已列的 `히나→Hina` 一併納入移除（與既有 `시라유키 히나→Shirayuki Hina` + §4A 衝突），屬同一 evidenced set，非新 blocker。

**新發現（標記為 new，非混入 §14.8）**：無新 blocker。stt_terms wiring 經獨立重驗存在（C4），不需列 implementation requirement。

**Final options**
- **default_slang 既有衝突人名 → Option B（移除 키마/봉준/성태/히나）**。Tradeoff：A（改值）仍把人名留全域 → 仍違「人名綁 profile」原則、仍有 exact-match 繞過 profile 與跨 profile 滲透（봉준→Kim Bongjun 會在 Isegye 場誤觸）；B 根因解、與 §14.5 non-goal 一致；代價 = 這些名改由 profile few-shot 控（正是設計意圖）。C（維持）會讓 prompt 矛盾、task 目標不可達，駁回。
- **섭주 variants → Option A（separate keys）**：`섭주/섭쥬/썹주/SUBJU→服主`、`섭쥬방→服主房`。Tradeoff：B（canonical-only）對 STT 變體脆弱、prompt 無逐項指引；A 資料成本微、對齊既有多變體風格、섭주 為全域角色詞無 profile 衝突。C（只 audit-evidenced 變體）：섭쥬방 證據較弱但組合性低風險，仍納入。
- **B3 direct-output**：plan 已明確化（§14.3-A / §14.4 / §14.6#3）：default_slang value 僅乾淨 direct output，마크=`Minecraft`、섭주系=`服主`/`服主房`，消歧 wording 移 hades few-shot/prompt，不入 JSON value。
- **Ready for Codex re-review：Ready。** No remaining blocker；C7 blocker 以 B1-Option-B 納入 §14.3/§14.4/§14.5/§14.6 解決；mishear 殘留（챗나/찬나/츤나）明列 post-impl validation 非 blocker（§14.5）。

### 14.10 Codex re-review（2026-05-19）

**範圍**：只驗 §14.8 blockers 是否被 §14.9 + 修訂後 §14.3–§14.6 解決；未重開全新 review。

| §14.8 blocker | Status | Evidence | Codex re-review |
|---|---|---|---|
| default_slang 既有衝突裸人名 | **RESOLVED** | 現況 `data/default_slang.json` 仍含 `키마→Kima`、`봉준→Bongjun`、`성태→Sungtae`、`히나→Hina`，且 audit §4A 目標為 Kyma / Kim Bongjun / KimSungtae / Shirayuki Hina；`translation_prompts.py:55/247` 會把全部 `cfg.translation.slang` 注入 prompt；`translation_policy.py:139` + `translator.py:269-272` 會 exact-match 短路 | §14.3-A 已採 Option B：移除 `키마/봉준/성태/히나`，保留非衝突全名 `시라유키 히나/아야츠노 유니`；§14.4 scope 明列 cleanup；§14.5 說明不新增人名但會清理既有衝突；§14.6#1 有測試。移除後 prompt 注入矛盾與 exact-match 短路兩路徑都會消失。殘留「人名靠 profile few-shot」是設計意圖，不是未解 blocker。 |
| 섭주 variants 策略不明 | **RESOLVED** | `TranslationPolicy.slang_result()` 是整句 `dict.get`；`translation_prompts.py:55/247` 逐 key 注入 slang；audit §4B user 決策 `섭쥬/썹주/SUBJU = 섭주`，目標 `服主` | §14.3-A 明確採 Option A：`섭주/섭쥬/썹주/SUBJU→服主`、`섭쥬방→服主房`；§14.4 scope 明列多 key；§14.6#2 明列測試。`섭쥬방` 證據較弱但屬組合式低風險列舉，接受為資料化範圍，不降為 blocker。 |
| 마크/섭주 value 必須是乾淨 direct output | **RESOLVED** | `translator.py:272` 會把 slang hit 直接作為 `target_text`；`translation_prompts.py:55/247` 也會逐字注入 value | §14.3-A 明確規定 `마크` value 為 `Minecraft`，`섭주` 系 value 為 `服主`/`服主房`，消歧 wording 不進 JSON value；§14.4 scope 和 §14.6#3 均有對應驗收。 |

**New blocker**：無。

**Non-blocking notes**
- §14.3-C 仍稱「4B 未決項」，但 §14 狀態行與 §14.9 已明確 4B user 決策為 DEFER；這是文字陳述可改善項，不阻擋動工。
- `stt_terms` 仍只是 Groq STT prompt bias，不能保證消除 `챗나/찬나/츤나`。§14.5 已把它歸為 post-implementation validation，分類正確。

**Conclusion：✅ 同意動工。** §14.8 的三個 blocker 已在 plan/scope/tests 中被具體處理；scope 仍是資料層，未外溢到 engine/policy/runtime schema/timeout/UI/Task #11–#12，也未接 `aliases`。

---

## 十五、Task #14: Profile-aware target rendering hardening（人名官方形強制）

> **§15 狀態**：Claude 起草（本節）。等 Codex 低 bias 模板 cross-review 寫 §15.8。未實作、未改 code/data、未 stage、未 push、不回改 Task #13 commit。
> 本節為本地 planning 文件，與 `OPTIMIZATION_*.md` 同類，**永不 push / 永不 stage**。

### §15.1 Problem statement

Task #13 把裸人名移出 `default_slang`（§14.3-A Option B），人名官方目標形（`Chxxnnx`/`Kim Bongjun`/`KimSungtae`/`Gosegu`）改由 **streamer profile few-shot** 唯一承載。Task #13 post-impl runtime validation 證偽此機制：**source/STT 已聽對的情況下**，Qwen 仍無視 profile few-shot 自行音譯，輸出非規格目標形。

`마크→Minecraft` 有效的根因不是 few-shot，而是它走 `default_slang` 整句 exact-match 短路（[translator.py:269](modules/translator.py#L269) `_translate_slang`，繞過 Qwen 自由渲染）。人名無法用同一條路：`TranslationPolicy.slang_result()` 是整句 `dict.get`（[translation_policy.py:139-140](modules/translation_policy.py#L139-L140)），人名出現在句中而非獨佔整句 → 永不命中。因此需要一個**不依賴 Qwen 服從、且能作用於句中片段**的 target rendering 強制機制，且僅在 source 已正確命中該人名時觸發。

目標一句：**在 source 已正確含某 profile/規格人名時，無論 Qwen 是否服從 few-shot，都把 target 中該人名渲染強制收斂到 §4A 規格官方形，且不誤傷普通詞、不跨錯 profile。**

### §15.2 Evidence from Task #13 runtime validation

來源：`OPTIMIZATION_TASK13_RUNTIME_CROSSCHECK_20260519.md` §5（Codex 獨立複核，與 Claude 量化一致）。

- **C `챈나→Chxxnnx` ❌**：新 run 2/2 仍輸出 `-chan`（seq 13 `챈나 깨워라`→`快叫醒-chan`；seq 44 `챈나가 멤버 섭외`→`因為-chan選成員…`）。
- **D `봉준/김봉준` ❌**：1 row，target `Bongjun`，非 `Kim Bongjun`。
- **D `성태` ❌**：2 rows / 3 occ，target `Sungtae老師`/`Sungtae哥`，非 `KimSungtae`，且稱呼不一致。
- **new observation `고세구` ❌**：seq 44 HADES run 提到 Isegye 名，target `高世久`，非 `Gosegu`（跨 profile：Isegye 名出現在 HADES 場）。
- **new observation（排除因素）**：新 run profile 為 `hades_chxxnnx`、success prompt version `39e9c0bd` → 失敗**不是**「沒吃到新 prompt」造成，是 few-shot 機制本身不足。

**Code-level 根因（已查證，非推測）**：post-translation 已存在一個 source-gated 修正鉤子 `_apply_source_aware_corrections(source, result)`（[translator.py:123-135](modules/translator.py#L123-L135)），由表 `_SOURCE_AWARE_TARGET_REPLACEMENTS`（[translator.py:86-107](modules/translator.py#L86-L107)）驅動，且**已 wire 進兩條路徑**：cache-lookup 結果（[translator.py:287](modules/translator.py#L287)）與新鮮 API 結果（[translator.py:316](modules/translator.py#L316)）。但該表對本 task 四名是**過時或缺漏**的：

- `봉준 → Bongjun`（[L95](modules/translator.py#L95)）：與 §4A `Kim Bongjun` 衝突。
- `성태 → Sungtae` / `狀態哥→Sungtae哥`（[L97](modules/translator.py#L97)）：與 §4A `KimSungtae` 衝突。
- `키마 → Kima`（[L93](modules/translator.py#L93)）：與 §4A `Kyma` 衝突（本 task scope 邊緣，§15.3 處理）。
- **`챈나`、`고세구` 無任何 entry** → 這就是 `챈나→-chan`、`고세구→高世久` 漏網的具體 code 原因。

機制存在但「表內容過時/缺漏 + 無 profile gating + 純 substring」是缺口。

### §15.3 Scope / non-scope

**In scope**
- `챈나 → Chxxnnx`
- `봉준 / 김봉준 → Kim Bongjun`
- `성태 → KimSungtae`
- `고세구 → Gosegu`（跨 profile 案例代表）
- 同類「source 已命中正確人名，但 target rendering 未遵守 profile/§4A 目標形」之人名（限與上述同機制者）。
- **必要的既有表 reconcile**：`_SOURCE_AWARE_TARGET_REPLACEMENTS` 中與 §4A 衝突的 `봉준/성태/키마` 既有 entry 必須校正（非僅 append）。理由同 Task #13 對 `default_slang` 衝突人名的 cleanup —— 同一批名，留舊值會使新舊兩路徑互相矛盾。`키마→Kyma` 因與 `봉준/성태` 同表同機制、不校正會殘留矛盾，納入本 task；`히나` 若與 §4A 衝突一併校正（待 §15.5 確認 §4A 對 히나 的目標形）。

**Explicitly out of scope（不得併入；僅可於 future-task note 提及）**
- `服주 / 섭주 / 섭쥬 / 서브주 / 섭정`、`default_slang` substring false-positive（屬審計 #4 source-normalization，機制不同，且 exact variants 本 run 零資料）。
- broad source normalization redesign。
- broad STT normalization redesign。
- 回改 Task #13 commit（`59fc0ab`，已 push origin/main）。
- 直接實作 / 改 code / 改 data / stage / push（本節僅 plan）。
- `streamer_profiles.json` 的 `aliases` 欄位（Task #13 已明確不接，沿用）。

### §15.4 Candidate approaches

| # | 方法 | 評估 |
|--|--|--|
| A | 繼續強化 profile few-shot（加更多範例/更強指令） | **駁回**。runtime 已直接證偽（C/D，prompt version 確認有吃到新 prompt）。再加範例對非確定性引擎不保證收斂，是已知無效路徑。 |
| B | 把人名加回 `default_slang` 走 exact-match 短路 | **駁回**。(1) `slang_result` 是整句 `dict.get`（[translation_policy.py:139](modules/translation_policy.py#L139)），句中人名結構上永不命中；(2) 違 user non-goal「不得把人名硬塞回 default_slang」；(3) `default_slang` 經 prompt 全量注入（plan §14 已述 `translation_prompts.py` 逐 key 注入）+ 跨 profile 滲透，正是 Task #13 移除的理由。 |
| C | 擴充既有 `_SOURCE_AWARE_TARGET_REPLACEMENTS` 表（補 챈나/고세구 + 校正 봉준/성태/키마），維持現狀 global + 純 substring | 部分可行但**不足**：現機制無 profile gating（`봉준→Kim Bongjun` 會在非 HADES 場誤觸跨 profile）、無 source token boundary（短 Hangul `성태/봉준` 可能 substring 命中更長詞）、wrong-form 列舉式（`챈나` 實際漏因是「Qwen 輸出 `-chan` 而表內無對應 wrong」）。 |
| D | 在既有 source-gated post-correction 上加 **profile gating + source token-boundary + canonical 正規化**（C 的強化版），並評估資料外移 | **首選**，見 §15.5。沿用已驗證、已 wire 的鉤子（風險面已知），只補機制缺口，不新增平行路徑。 |

### §15.5 Preferred design（approach D；細節含取捨，留 Codex 審）

**機制定位**：沿用 [translator.py:123-135](modules/translator.py#L123-L135) 的 post-translation source-gated 修正鉤子，**不新增平行渲染層**。理由：它已 wire 進 cache-lookup（L287）與 fresh-API（L316）兩路徑、已在 slang 短路（L269）之後、`_looks_like_meta_garbage_output`（L288/L317）與 cache 寫入（L331）之前；新增平行層會多一條需各自驗證的路徑。

逐項回應 user 指定取捨：

1. **target-side forced rendering / post-processing，而非 few-shot**：採。few-shot 已證偽（§15.4-A）；強制渲染是唯一不依賴引擎服從的確定性手段。

2. **correction 啟用條件**
   - **必須 source exact / alias 命中**：是。沿用既有 `if not any(term in source ...): continue` 模式 —— 無 source 證據不改 target，根本性壓低 false-positive（target-only 盲替換明確不採，回應「avoid target-only replacement」）。
   - **profile gating**：是（**新增**，現機制缺）。每條 correction 標 `profile_id`；僅當 `cfg.active_streamer_profile`（[translator.py:419](modules/translator.py#L419) `get_translation_profile(cfg.active_streamer_profile…)` 同源）匹配才套用。HADES 專名（챈나/봉준/성태）僅 HADES profile 生效。
   - **source token boundary（Korean）防誤傷**：是（**新增**）。短 Hangul 名（`성태`/`봉준`）需以 Korean token 邊界命中（前後非諺文連續字元），避免成為更長詞的 substring。`챈나/고세구` 同規則。回應 Q2c。
   - **target rewrite 策略**：以「runtime 實測 Qwen 會吐的錯誤形」列舉為主（`챈나`: `-chan`/`-chan` 變體；`봉준`: `Bongjun`/`奉俊`/`奉主`；`성태`: `Sungtae`/`Sungtae老師`/`Sungtae哥`/`成泰`；`고세구`: `高世久`）→ 收斂到 §4A canonical。**不做 target-only 盲替換**（回應 Q2d）。**誠實限制**：wrong-form 列舉是開放集合，未列舉的新錯形會漏到下次 runtime 才補 → 列為 §15.6 殘留風險 + §15.7 runtime 迭代回路，設計上無法消除，只能收斂。

3. **跨 profile name rendering（Q3）**：採**兩層表**。
   - **profile-scoped 層**：HADES 專名綁 `hades_chxxnnx`。
   - **shared/global name 層**：跨團合作時會出現的他團官方名（Isegye `고세구→Gosegu` 在 HADES 場）放共享層，**不複製進 HADES profile**（同一實體不該按誰開台而不同；複製會 N×profile 維護爆炸 + 漂移）。
   - **判準（共享層准入，high-confidence only）**：(i) 該 source token 為專名、無中文/韓文常用詞同形（homograph）；(ii) 目標形為固定羅馬化、非情境依賴；(iii) source token boundary 可守。`고세구/주르르/릴파` 類符合（已是 `isegye_lilpa` profile 內 fixed glossary，[translation_profiles.json](data/translation_profiles.json) 可佐證為無歧義專名）。風險：共享層放大跨 profile 命中面 → 准入須嚴，不確定者退回 profile-scoped。

4. **schema / data placement（Q4）**：列三選項供 Codex 裁，Claude 傾向但不鎖死：
   - (a) 維持 code constant（擴充/重構 `_SOURCE_AWARE_TARGET_REPLACEMENTS` 為 `{profile_id|__shared__: [(source_terms, wrong_forms, canonical)]}`）。低 churn、最貼近現機制、測試面最小；缺點：人名資料留在 code，與 Task #13「資料化」方向不一致。
   - (b) 外移到 `streamer_profiles.json` 新增 per-profile `name_corrections` + 一個 `shared_name_corrections` 區塊。與 Task #13 資料化一致、profile gating 自然落地；缺點：**最高 churn**（改 data schema + loader + 多 test fixture），且 `streamer_profiles.json` 現由 `modules/streamer_profiles.py` 載入（schema 變更需連動）。
   - (c) `translation_profiles.json`：該檔現為 prompt few-shot 文字（standard/qwen 雙版），語意是「給模型看的」，放確定性 correction 規則語意不符，**不建議**。
   - **明確排除**：不放 `default_slang.json`（user non-goal；且結構上 whole-utterance dict.get 做不到 token-level）。
   - Claude 傾向 **(a) 作為 v1**（最小可逆、機制風險已知），資料化留 future-task；但此為 schema 取捨，明列給 Codex 表態（§15.8）。

5. **cache 互動（設計疑點，標 Codex blocker-candidate）**：現況 fresh API 結果先 correct（L316）再 `_record_success` 寫 cache（L331）→ **cache 存的是已修正值**；cache-lookup 命中時又對 `lookup.result` 再 correct 一次（L287）。引入 profile gating 後：profile A 修正後寫入的 cache，被 profile B 經 L287 lookup 取出再以 B 的 gating 重修。需保證 (i) correction **idempotent**（canonical→canonical 為 no-op，重複套用不劣化）；(ii) profile gating 在 apply 時點解析（L287/L316 都讀當下 `cfg.active_streamer_profile`），故跨 profile cache 取出時不會誤套 A 的專名。此為 wiring 正確性關鍵，Codex 須驗證 idempotency 與 cache-key 是否需納 profile，**列為 blocker-candidate**。

### §15.6 Risk analysis（含 rollback / safety）

| 風險 | 分級 | 說明 / 緩解 |
|--|--|--|
| 短 Hangul source token substring 誤命中（`성태`/`봉준` 落在更長詞內） | Blocker-candidate（有 code 證據：現機制為純 substring，[L126-129](modules/translator.py#L126-L129)） | §15.5#2 source token-boundary 守門；Codex 須驗邊界規則對諺文黏著語的正確性。 |
| wrong-form 列舉開放集合，新錯形漏網 | Non-blocking（設計上無法消除） | §15.7 runtime 迭代回路；殘留率列 post-impl validation 指標，非 blocker。 |
| cache 跨 profile 取出 + 非 idempotent → 漂移/雙重修正 | Blocker-candidate（§15.5#5） | 強制 idempotent；Codex 驗 cache-key 是否需納 profile。 |
| 既有 `봉준→Bongjun`/`성태→Sungtae`/`키마→Kima` 未 reconcile → 新舊路徑矛盾 | Blocker（§15.2 已證表內衝突） | §15.3 納入 reconcile，非僅 append。 |
| 共享層人名跨 profile 誤觸（他團名在無關場誤現） | Non-blocking risk | §15.5#3 准入判準（無 homograph + 固定羅馬化 + token boundary）；不確定者退 profile-scoped。 |
| 雙路徑雙重 apply（L287 + L316→cache→後續 L287） | Blocker-candidate | idempotency 測試（§15.7）覆蓋「apply 兩次 == 一次」。 |
| slang 短路（L269）先於 correction → 同名若為 default_slang exact key 會繞過 | Non-blocking note | Task #13 已移除裸人名，現無此 key；僅記錄不處理，避免 scope creep。 |

**Rollback / safety（user item 7）**：機制集中於單一 source-gated 函式（v1 = code constant），回退 = 還原該函式/表為 Task #14 前狀態，**不涉 data migration、不涉 schema**（若採 §15.5#4(b) 則回退面擴大 → 亦為傾向 (a) 之理由）。誤傷定位：correction 觸發點可在該函式加 debug log（source term / profile / wrong→right），runtime event 已有 `source_text`/`target_text`/`prompt_version`/profile 可交叉定位是 correction 還是 engine 出錯。安全閥：保留「per-profile / 全表停用」開關（沿用 `cfg.translation.use_profile` 思路或新增 flag —— Codex 評是否必要）。

### §15.7 Test / validation plan

**Unit（pytest，沿用既有 `tests/`，預期不破壞現 410 passed/4 skipped）**
- 每名一組：source 含 canonical → target 收斂 §4A 形；source 不含 → target 原樣不動。
- token boundary 反例：`성태`/`봉준` 嵌在更長諺文詞內 → 不修正。
- profile gating：HADES entry 在 `stellive_hina`/空 profile 下不觸發；HADES profile 下觸發。
- 共享層：`고세구` 在 HADES profile 下仍收斂 `Gosegu`；在 stellive 下行為符合判準。
- idempotency：對已 canonical 的 target 再 apply → no-op；apply 兩次 == 一次。
- reconcile 回歸：舊 `봉준→Bongjun`/`성태→Sungtae`/`키마→Kima` 已不再產生（防舊值復活）。

**Fixture / regression（用 runtime 真實失敗對）**
- 餵 crosscheck §5 實例：seq 13 `챈나 깨워라`/`快叫醒-chan` → 期望含 `Chxxnnx` 不含 `-chan`；seq 44 `챈나가 멤버 섭외`、봉준 row、성태 兩 rows、고세구 seq 44 → 期望 §4A 形。
- 非誤傷 fixture：含類似 substring 的普通詞、非 HADES 場句子 → target 不被改。

**Runtime recheck（post-impl validation，沿用 Task #13 crosscheck 方法）**
- 下一個 HADES run：對 챈나/봉준/성태/고세구 統計 target 是否穩定 §4A 形（目標：命中且 source 正確時 100%）。
- 誤傷量化：普通詞被改 = 0；StelLive/Isegye profile run 驗 HADES 專名零觸發（profile 邊界）。
- wrong-form 殘留率：列為持續觀察指標（開放集合，非 pass/fail gate）。
- full suite 必須維持綠（baseline 410 passed / 4 skipped）。

### §15.8 Codex cross-review

#### Summary verdict
- Verdict: WARNING Plan needs revision before implementation
- One-sentence reason: The direction is correct, but the proposed Korean boundary rule is underspecified and would miss an observed in-scope failure (`챈나가`), so Section 15.9 must tighten matching semantics before implementation.

#### Blockers
- **Korean source-boundary rule is not implementation-ready.** Section 15.5 says short Hangul names require preceding/following non-Hangul boundaries, but Task #13 runtime evidence includes `챈나가 멤버 섭외` and this exact in-scope case would fail a strict "after non-Hangul" rule because `가` is Hangul. Required revision: define a particle/name-suffix-aware source matcher, or explicitly enumerate accepted Korean suffix/particle forms, with tests for `챈나가`, `성태가`/`성태는`/`성태형`/`성태님`, and negative longer-token cases.

#### Non-blocking risks
- **Wrong-form coverage remains iterative.** The plan correctly avoids target-only blind replacement, but wrong forms such as `-chan`, `Bongjun`, `Sungtae哥`, and Chinese phonetic forms are an open set; runtime follow-up must drive incremental additions.
- **Shared/global name layer can widen blast radius.** `고세구` is justified by Section 5 runtime evidence and audit Section 4A, but shared entries should start with high-confidence exact canonical source tokens only; do not add STT variants like `주르륵`/`일파` under this task.
- **`cfg.translation.use_profile` semantics need to be explicit.** If profile prompts are disabled, profile-scoped corrections should not silently remain active unless Section 15.9 states that this is intentional; v1 can avoid a new flag by honoring `use_profile` for profile-scoped rules while allowing truly shared rules.
- **Correction logic is not part of `prompt_version`.** This is acceptable because lookup re-applies correction, but rollback is not just "revert the function/table" if bad corrected rows have already been cached in memory/DB; Section 15.9 should mention cache clearing or targeted DB cleanup as rollback hygiene.
- **Bare `히나` remains ambiguous.** Audit Section 4A high-confidence entry is `시라유키 히나 -> Shirayuki Hina`; existing code has bare `히나 -> Hina`. This should not be silently changed without the decision listed below.

#### User decision needed
- **Bare `히나` target policy.** Choose one before implementation touches the existing `_SOURCE_AWARE_TARGET_REPLACEMENTS` entry: keep profile-scoped `히나 -> Hina`, change only full `시라유키 히나 -> Shirayuki Hina`, or force bare `히나 -> Shirayuki Hina`. Evidence currently supports the full-name form more strongly than the bare-name form.

#### Blocker-candidate rulings
| Candidate | Result | Evidence | Required plan change if any |
|---|---|---|---|
| Short token substring false-positive | Blocker | Current code uses pure substring source gating in `modules/translator.py:126`; Section 15.5 proposes non-Hangul boundaries, but runtime failure `챈나가 멤버 섭외` in `OPTIMIZATION_TASK13_RUNTIME_CROSSCHECK_20260519.md:55` needs matching through a Hangul particle. | Replace the boundary wording with particle/name-suffix-aware Korean matching and add positive/negative tests. |
| Cache idempotency / cross-profile cache issue | Non-blocking risk | Memory cache key is `(text, incomplete, prompt_ver)` in `modules/translation_runtime.py:37`; DB key includes `prompt_version` in `modules/db.py:142`; `prompt_ver` is derived from a prompt that includes the active profile in `modules/translator.py:419-428`, so profile-to-profile cache collision is unlikely. However corrected values are cached and re-corrected on lookup. | Keep idempotency tests; add rollback/cache hygiene note. No need to add profile id to cache key for v1 if prompt profile remains in `prompt_version`. |
| Double-apply / non-idempotent correction path | Non-blocking risk | Fresh API results are corrected before `_record_success` at `modules/translator.py:316-331`; cached results are corrected again at `modules/translator.py:284-287`. | Require `apply(apply(x)) == apply(x)` tests, including canonical targets and suffix forms like `KimSungtae`. |

#### Schema placement ruling
| Option | Codex assessment | Reason |
|---|---|---|
| code constant | Acceptable for v1 | Existing mechanism is already a code constant in `modules/translator.py:86`; v1 can be minimal and reversible if the table is structured with `profile_id`/shared scope, source matcher metadata, and tests for gating/idempotency/no default-slang pollution. |
| streamer_profiles.json / translation_profiles.json | Not required for v1; streamer profile JSON is the better future data location | `streamer_profiles.json` already models profile-scoped runtime metadata, so a future `name_corrections` schema fits there. `translation_profiles.json` is prompt text loaded by `modules/translation_prompts.py:19-37`, so it is a poor home for deterministic post-processing rules. |
| default_slang.json | Reject / explicitly forbidden | `TranslationPolicy.slang_result()` is whole-utterance `dict.get` at `modules/translation_policy.py:139`, and prompt injection of all slang occurs in `modules/translation_prompts.py:55` and `modules/translation_prompts.py:247`; streamer person names must not go back here. |

#### Code-path findings
- **Problem framing is correct.** Runtime evidence shows source was already correct for `챈나`/`봉준`/`성태`, but target rendering ignored profile examples; this is not primarily an STT/source-normalization task.
- **Existing hook is the right implementation seam.** `_apply_source_aware_corrections()` already runs after slang exact-match and after API/cache translation, so extending this single hook is safer than adding a second post-processing path.
- **Slang path remains exact-only.** `modules/translation_policy.py:139` confirms `default_slang` cannot solve sentence-internal person names and should stay out of scope.
- **Profile cache isolation is mostly already achieved.** Because profile text contributes to `prompt_version`, `hades_chxxnnx` and `stellive_hina` normally use different cache keys; the main cache concern is idempotency and rollback hygiene, not routine cross-profile hits.
- **Streamer profile loader currently exposes no correction schema.** `modules/streamer_profiles.py:7-12` only has `stt_terms` and `aliases`; choosing JSON schema now would require loader/data/test churn that is not necessary for v1.
- **Existing table conflicts must be reconciled, not appended around.** `봉준 -> Bongjun`, `성태 -> Sungtae`, and `키마 -> Kima` in `modules/translator.py:93-97` conflict with audit Section 4A targets and need direct replacement/removal under the new gated rules.

#### Test coverage requirements
- Add unit tests for the correction helper covering source-present/source-absent behavior, canonical no-op behavior, and `apply` twice equals once.
- Add Korean matcher tests for positive particle/suffix cases (`챈나가`, `성태는`, `성태형`, `성태님`) and negative embedded longer-token cases.
- Add profile gating tests: HADES-only rules do not fire for empty/`stellive_hina`/`isegye_lilpa`; shared `고세구` fires according to the finalized shared-layer rule.
- Add cache-path tests for both fresh API then memory hit and preloaded cache hit, verifying no double expansion such as `KimKimSungtae`.
- Add no-pollution tests proving `data/default_slang.json` still excludes bare person names like `챈나`, `봉준`, `성태`, `키마`, and `히나`.
- Add regression fixtures from Task #13 Section 5 for `챈나 깨워라`, `챈나가 멤버 섭외`, `봉준`/`김봉준`, `성태`, and `고세구`.
- Keep runtime validation focused on source-correct rows; do not convert this task into `섭주`/`서브주`/`섭정` handling or broad source normalization.

#### Suggested Section 15.9 revision points
- Replace "preceding/following non-Hangul boundary" with a concrete Korean name matcher that allows common particles/honorific/name suffixes while rejecting embedded longer lexemes.
- State whether profile-scoped corrections honor `cfg.translation.use_profile`, and whether shared corrections remain active when profile prompts are disabled.
- Decide the bare `히나` policy, or explicitly exclude it from Task #14 implementation while leaving the existing entry unchanged only if that is intentional.
- Clarify that Task #14 source triggers are canonical/high-confidence source names, not STT mishear variants such as `챗나`/`찬나`/`츠나`, unless the user explicitly expands scope.
- Update rollback wording to include clearing in-memory cache and targeted DB cache rows if a bad correction was already stored.
- Keep v1 as a code constant unless Claude chooses to pay the schema churn; if code constant is chosen, require tests that prove profile gating and no `default_slang` re-pollution.

### §15.9 Revised plan（Claude round 2）

> 僅據 §15.8 修訂。逐條列「採納/駁回 + 理由」，駁回判準＝「這會否摧毀一個有用貢獻」。Codex §15.8 不被改寫。本節若與 §15.1–§15.7 衝突，以本節為準（明標 supersede）。

#### A. 逐條回應 Codex §15.8

| Codex §15.8 項 | 處置 | 理由 / 帶來的 plan 變更 |
|--|--|--|
| **Blocker：韓文 boundary 規則未 implementation-ready（`챈나가` 會被 strict "後接非諺文" 規則漏掉）** | **採納** | §15.5#2「前後非諺文」措辭過嚴，會漏掉 in-scope 的 `챈나가 멤버 섭외`。以 §15.9-B 的「particle/suffix-aware 韓文人名 matcher」**supersede §15.5#2 的 boundary 措辭**。 |
| Non-blocking：wrong-form 為開放集合 | 採納（維持原分級） | §15.5/§15.6 已列為設計上不可消除、runtime 迭代收斂；不升級為 blocker。新增：wrong-form 為**明列封閉集合**，未列者不處理（§15.9-C）。 |
| Non-blocking：shared 層放大命中面，勿加 `주르륵`/`일파` STT 變體 | 採納 | §15.9-D：shared 層 v1 **僅** `고세구→Gosegu` 一條 canonical，明確排除 STT 變體與其他他團名（要再加另開）。 |
| Non-blocking：`cfg.translation.use_profile` 語意需明確 | 採納 | §15.9-E：profile-scoped correction **遵守** `use_profile`（profile prompt 關閉時 profile-scoped 規則同步停用）；shared 規則不受 `use_profile` 影響；v1 不新增 flag。 |
| Non-blocking：correction 不入 `prompt_version`，rollback 不只還原函式 | 採納 | §15.9-F：rollback 步驟新增「清 in-memory cache + 針對性刪 DB 既存壞列 / 或 bump 使其失效」。 |
| Non-blocking：bare `히나` 目標形有歧義 | 採納（轉為 scope 收斂） | §15.9-G：把 bare `히나` **移出 Task #14 scope**，既有 `히나→Hina` entry 一字不動 → 把 Codex 的 user-decision 從 Task #14 critical path 上移除，但**不替 user 拍板**（決策保留、延後）。**此點 supersede §15.3 中「`히나` 一併校正」**。 |
| User decision needed：bare `히나` 三選一 | 見 §15.9-G | 不由 Claude 裁；以 scope 收斂降為非阻擋，原 decision 原封轉述、延後到 future task。 |
| Blocker-candidate：短 token substring 誤傷＝**Blocker** | 採納 | 同首列 blocker，§15.9-B 解。 |
| Blocker-candidate：cache idempotency／跨 profile＝**Non-blocking** | 採納 | 已獨立查證 Codex 的 cache 證據（見下「查證」）；§15.9-F 列為 accepted non-blocking + idempotency 測試硬要求。 |
| Blocker-candidate：double-apply＝**Non-blocking** | 採納 | §15.9-F：單一權威函式 + `apply(apply(x))==apply(x)` 測試強制。 |
| Schema：code constant v1 可接受、不要求 JSON、禁 default_slang | 採納 | §15.9-H：v1 採 code constant（profile/source-gated、可測、可回退），不動 JSON schema，default_slang 明禁；資料化留 future task。 |
| Code-path findings（problem framing 正確、既有 hook 是對的 seam、slang 維持 exact-only、profile cache 大致已隔離、loader 無 correction schema、既有衝突須 reconcile 非 append） | 全採納 | 與 §15.2/§15.4-D/§15.5 一致，無需翻案；reconcile 已在 §15.3 in-scope。 |

**駁回項：無。** Codex §15.8 每項皆為對 plan 的有效收緊；無任何一項若採納會摧毀本 plan 的有用貢獻，故全採納。唯一「非單純採納」的是 bare `히나`：以 scope 收斂處理，理由見 §15.9-G（不是駁回 Codex 的 flag，是用更小 scope 移除其阻擋性而不僭越 user 決策）。

**獨立查證（不照抄 Codex）**：`KOREAN_CHAR_RE = re.compile(r"[가-힣]")`（[utils/text_heuristics.py:179](utils/text_heuristics.py#L179)）僅單字諺文判定，**無**現成 particle/suffix-aware 人名 matcher 可重用（text_heuristics.py 內的 `STT_*` 後綴清單是 fragment 啟發式、用途不同，可借「枚舉後綴」之法、不可重用其 matcher）。memory cache key＝`(text, incomplete, prompt_ver)`（[modules/translation_runtime.py:37](modules/translation_runtime.py#L37)）；DB `UNIQUE(source_text,target_lang,engine,model,prompt_version)`（[modules/db.py:33](modules/db.py#L33)）；`prompt_ver` 由含 profile 的 system_prompt md5 得出（[modules/translator.py:419-428](modules/translator.py#L419-L428)）→ Codex「profile 已由 prompt_version 隔離 cache」屬實，採納其非阻擋裁定。

#### B. Blocker fix：韓文 particle/suffix-aware 人名 matcher（supersede §15.5#2 boundary 措辭）

**Revised design requirement（取代「前後非諺文邊界」）**：source 命中須為 boundary-aware，定義如下，對 canonical source token `T`（如 `챈나`/`성태`/`봉준`/`김봉준`/`고세구`）：

1. **前綴守門（拒絕嵌入更長詞尾）**：`T` 之前一字元必須為字串起點、空白、標點，或非諺文字元；**不得**為諺文音節（防 `T` 是更長諺文詞的尾段）。
2. **後綴守門（允許助詞/敬稱，拒絕未知諺文延續）**：`T` 之後若為字串終點 / 空白 / 標點 / 拉丁字母 / 數字 → 命中。若 `T` 之後緊接諺文，取「緊接 `T` 的最長連續諺文串」`S`，**僅當 `S` 完全等於白名單枚舉的某一助詞/敬稱/呼格 token（最長優先匹配）** 才命中；`S` 非白名單成員（即未知諺文延續，多半是更長非人名 lexeme）→ **不命中**。
3. **白名單（封閉枚舉，v1 初始集，可於實作期依測試補但仍為封閉集）**：主格/主題/受格/屬格/方位等助詞 `가 / 이 / 은 / 는 / 을 / 를 / 의 / 도 / 만 / 에 / 에게 / 한테 / 께 / 랑 / 이랑 / 하고 / 과 / 와`；呼格/語尾 `야 / 아`；敬稱/親屬呼稱 `님 / 씨 / 형 / 누나 / 언니 / 오빠`；繫詞起始 `이 / 이에요 / 예요 / 입니다 / 이다`（與主格 `이` 合併判定）。實作須以「最長優先」避免 `은` 誤切 `은행` 類（但因前綴守門 `T` 已是 token 起點，`S` 僅取 `T` 之後串，故風險限於 `T` 之後；白名單比對用完整相等而非 startswith，杜絕 `님abc`）。
4. **必過範例（positive）**：`챈나가` / `챈나님` / `성태가` / `성태는` / `성태형` / `성태님` / `봉준이` / `봉준님` / `김봉준` / `고세구가`。
5. **必拒範例（negative）**：`T` 作為更長諺文詞尾段（前綴守門擋）；`T` 後接非白名單諺文串（如人造 `성태권도`→`성태`+`권도`，`권도` 不在白名單 → 不命中）；`T` 嵌入詞中（前後皆諺文）。

**Helper 重用判定**：經查證**無**現成可重用 boundary helper（見上「獨立查證」）。實作階段應**新增一個最小、僅供本 correction 使用的 matcher**（`KOREAN_CHAR_RE` 可借作「是否諺文」單字判定；白名單為新增小常數），**不得**借此改動或泛化全域 source normalization（屬本 task non-goal）。

**測試（positive/negative，列入 §15.9-I）**：每名 × 每白名單後綴的 positive；每名的 negative（嵌入更長詞、未知諺文延續、詞中嵌入）；前綴守門 negative。

#### C. Source-gated target correction（明確化）

- **唯有** §15.9-B 的 boundary-aware source 命中才允許 target correction；無 source 證據一律不改 target。
- **禁止 target-only replacement**（不掃 target 找疑似錯形盲替）。
- correction **profile-aware / rule-aware**：每規則帶 `profile_id` 或 `__shared__` scope（§15.9-D/E）。
- wrong-form **僅處理明列封閉集合**，不嘗試消除開放集合音譯：v1 wrong-form 表（依 runtime 實測）—— `챈나`：`-chan` 及其直接變體；`봉준/김봉준`：`Bongjun` / `奉俊` / `奉主`；`성태`：`Sungtae` / `Sungtae哥` / `Sungtae老師` / `成泰` / `狀態哥`；`고세구`：`高世久`。canonical 目標：`Chxxnnx` / `Kim Bongjun` / `KimSungtae` / `Gosegu`。未列錯形不處理，列 runtime 迭代（非阻擋）。

#### D. Shared/global name 層（收斂）

- v1 shared 層**僅一條**：`고세구 → Gosegu`（§5 runtime + 審計 §4A 高信度、無中韓常用詞同形、固定羅馬化、boundary 可守）。
- **明確排除本 task**：`주르륵`/`일파` 等 STT 變體、其他他團名、任何 mishear 變體。要再加 → runtime 證需後**另開 task**，本 task 不擴張。
- 准入判準（沿用 §15.5#3，未放寬）：無 homograph + 固定羅馬化 + boundary 可守；不確定者退 profile-scoped 或不收。

#### E. `use_profile` 語意（明確化）

- profile-scoped 規則（HADES：챈나/봉준/성태/김봉준）**遵守 `cfg.translation.use_profile`**：當 `use_profile=False`（profile prompt 被 strip，[translator.py:416-417](modules/translator.py#L416-L417)）時，profile-scoped correction **同步停用**（語意一致：無 profile 模式下不應殘留 profile 專屬人名強制）。
- shared 規則（`고세구→Gosegu`）**不受 `use_profile` 影響**（它非 profile 條件式，是跨場通用專名）。
- v1 **不新增 flag**；以「規則 scope + `use_profile`」既有狀態決定啟用。

#### F. Cache / idempotency / rollback hygiene（implementation requirements）

- **Accepted non-blocking risk（記錄）**：profile 已由 `prompt_version` 隔離 cache（查證屬實，見上）→ v1 **不需**把 profile_id 加入 cache key。
- **Idempotency 硬要求**：correction 必須 `apply(apply(x)) == apply(x)`。關鍵防呆：canonical 目標含 wrong-form 子串時不得自我重觸（如 `Kim Bongjun` 含 `Bongjun`、`KimSungtae` 含 `Sungtae`）→ 規則須設計成「source-gated 命中後，若 target 已含 canonical 形則為 no-op」或 wrong-form 比對採可阻止再匹配的邊界，杜絕 `KimKimSungtae`/`Kim Kim Bongjun`。
- **單一權威位置**：correction 只在既有 `_apply_source_aware_corrections`（[translator.py:123](modules/translator.py#L123)）一處；兩呼叫點（[L287](modules/translator.py#L287) cache-lookup、[L316](modules/translator.py#L316) fresh-API）呼叫**同一函式**。**禁止**在 prompt/policy path 另設第二個確定性 correction 機制（few-shot 仍為軟提示，但確定性正確性所有權唯一屬此函式）。
- **Cache 時序確定性**：fresh API 先 correct（L316）再寫 cache（L331）；lookup 再 correct（L287）。idempotency 保證重經此路徑不越改越多。
- **Rollback hygiene（補 §15.6）**：回退非僅「還原函式/表」——既存壞 correction 可能已寫入 in-memory cache 與 DB（[db.py:33](modules/db.py#L33) 持久）。回退步驟須含：(1) 還原 code constant/函式；(2) 清 in-memory translation cache；(3) 針對受影響 source/target 列做 targeted DB 刪除，或以 prompt_version 變更使其失效。v1 = code constant → 無 schema migration，回退面僅上述三步。

#### G. Bare `히나` user decision（scope 收斂，不替 user 拍板）

- **Task #14 明確排除 bare `히나`**：既有 `_SOURCE_AWARE_TARGET_REPLACEMENTS` 中 `("히나",) → (("希娜","Hina"),)`（[translator.py:94](modules/translator.py#L94)）**一字不動、不 reconcile、不納本 task**。**此 supersede §15.3「`히나` 若與 §4A 衝突一併校正」**。
- 理由：Codex §15.8 指出審計 §4A 高信度條目是 `시라유키 히나 → Shirayuki Hina`（全名），bare `히나` 目標形（`Hina` vs `Shirayuki Hina` vs 全名才轉）為證據不足之 user 決策。把它移出 scope 即可在**不替 user 拍板**下移除其對 Task #14 的阻擋性。
- **保留之 user decision（原封轉述 Codex，延後，非 Task #14 阻擋）**：bare `히나` 三選一 ——（i）維持 profile-scoped `히나→Hina`；（ii）僅全名 `시라유키 히나→Shirayuki Hina`；（iii）強制 bare `히나→Shirayuki Hina`。Codex 評：證據較支持全名形。**此決策延後至未來 task，Task #14 不依賴亦不觸碰該 entry。**
- `봉준/성태/키마` 與 §4A 衝突且無歧義 → 仍 in-scope reconcile（§15.3 不變）。

#### H. Schema placement（採納定案）

- v1 **採 code constant**：把 `_SOURCE_AWARE_TARGET_REPLACEMENTS` 重構為帶 `profile_id` / `__shared__` scope + source-matcher 元資料（boundary 規則）+ wrong-form 封閉集 + canonical 的結構，集中單一函式。
- **不**動 `streamer_profiles.json` / `translation_profiles.json` schema（loader [streamer_profiles.py:7-12](modules/streamer_profiles.py#L7-L12) 僅 `stt_terms`/`aliases`，改它＝非必要 churn）。
- **明禁** `default_slang.json`（whole-utterance `dict.get`，[translation_policy.py:139](modules/translation_policy.py#L139)，結構上做不到 token 級；且 user non-goal）。
- 規則若未來擴張 → **另開 schema/data task**，不在 Task #14 做。

#### I. Revised test plan（取代/擴充 §15.7）

實作期測試至少涵蓋：

1. **Boundary positive**：每名 × 每白名單後綴（`챈나가`/`챈나님`/`성태가`/`성태는`/`성태형`/`성태님`/`봉준이`/`봉준님`/`김봉준`/`고세구가`）→ 命中且收斂 canonical。
2. **Boundary negative**：名作更長諺文詞尾段、名後接非白名單諺文串、名詞中嵌入 → 不命中、target 不變。
3. **Source-gated**：source 無該名 → target 一字不改（即使 target 偶含疑似錯形）。
4. **No target-only replacement**：僅 target 含錯形但 source 無對應名 → 不改。
5. **Profile-gating**：HADES 規則在空 / `stellive_hina` / `isegye_lilpa` profile 不觸發；HADES profile 觸發；`use_profile=False` 時 profile-scoped 不觸發、shared `고세구` 仍觸發。
6. **Idempotency**：`apply(apply(x))==apply(x)`，含 canonical 與後綴形（`KimSungtae` 不得變 `KimKimSungtae`、`Kim Bongjun` 不得再擴張）。
7. **Double-apply（跨 cache round-trip 模擬）**：fresh→記錄→lookup 再 apply，無重複擴張。
8. **Cache / prompt_version sanity**：HADES 與 stellive 因 prompt_version 不同而 cache 不互染；同 source/profile/prompt_version 重呼叫結果穩定。
9. **No default_slang pollution**：`data/default_slang.json` 仍不含裸 `챈나`/`봉준`/`성태`/`키마`/`히나`。
10. **Reconcile 回歸**：舊 `봉준→Bongjun`/`성태→Sungtae`/`키마→Kima` 不再產生（防舊值復活）；`히나→Hina` entry 保持不變（防本 task 誤動）。
11. **Runtime recheck（post-impl）**：下個 HADES run，챈나/봉준/성태/고세구 source 正確列 target＝§4A 形（目標 100%）；普通詞誤傷＝0；wrong-form 殘留率列觀察指標（非 gate）。
12. **Profile boundary runtime**：StelLive / Isegye run 驗 HADES 專名零觸發；`고세구` 跨場行為符合 §15.9-D。
13. full suite 維持綠（baseline 410 passed / 4 skipped）。

#### J. Implementation readiness conclusion

- **Codex blocker 是否解掉**：是。唯一 blocker（韓文 boundary 規則未 implementation-ready）由 §15.9-B 的 particle/suffix-aware matcher 規格 + 封閉白名單 + positive/negative 測試取代 §15.5#2 措辭，並含 `챈나가` 等 in-scope 例。
- **是否仍有 blocker**：無。
- **是否仍有 user decision needed**：bare `히나` 三選一 —— 已以 scope 收斂（§15.9-G）**降為非阻擋**（移出 Task #14、entry 不動），決策原封保留並延後至 future task；Task #14 不依賴它，**故不阻擋本 task 進入 re-review**。
- **是否可進入 Codex blocker-only re-review**：是。請 Codex 僅查「§15.8 唯一 blocker 是否被 §15.9-B 真解（非合理化）」及「§15.9-G 的 scope 收斂是否確實使 `히나` decision 不再阻擋」，不重開全審。
- **是否仍禁止實作直到 re-review 通過**：是。re-review 通過前不得實作；下一階段 prompt 由 user 發。
- **Scope 變更**：**淨收斂**——移出 bare `히나`（§15.9-G）、shared 層收斂至僅 `고세구`（§15.9-D）；無任何 scope 擴張；`服주/섭주/source-norm`/STT 變體仍明確排除。

### §15 風險分級與流程（user item 8）

本 task 涉 **精度邊界（短 token 誤傷）+ cross-profile behavior（共享層）+ schema/wiring（cache idempotency / data placement）** → 依 [[feedback_codex_prompt_framing_bias]] 定案契約，**不可壓縮流程**，走完整 7 步：

`Claude plan（本節 §15.1–§15.7）→ Codex 低 bias cross-review（§15.8）→ Claude revise（§15.9，僅據 §15.8）→ Codex blocker-only re-review → Codex implementation → Claude post-impl validation（git/diff/test/scope 獨立重跑）→ user decides push`

---

## 十六、Task #15: Hangul self-form wrong_forms 擴充

> 日期：2026-05-20
> 性質：additive data change，壓縮 4 步流程
> 狀態：📝 §16.9 revised（Codex cross-review ⚠️→ narrow revisions applied）— 已 cross-review、未實作、未 commit、未 push

### §16.1 Problem statement

Task #14 在 `modules/translator.py` 新增 profile-aware、source-gated、boundary-aware target 修正機制（`_NAME_RENDERING_RULES`），可將已知錯誤羅馬化形式（如 `-chan → Chxxnnx`、`Bongjun → Kim Bongjun`）修正為官方形。

Post-push runtime validation 發現第二類失效：**Qwen 有時將 Hangul source token 原封不動輸出至中文 target（無任何羅馬化）**。因為 Hangul 自身形（如 `챈나`、`봉준`）不在 `wrong_forms` 內，correction hook 的 regex 找不到可替換的字串，target 帶有可見的韓文滲漏直接輸出。

此非 STT 失效（source 已含正確 Hangul 人名），亦非 source normalization 問題。這是**封閉集合 target rendering gap**：現有修正機制設計正確，但 `wrong_forms` 覆蓋不完整。

### §16.2 Evidence

**Runtime confirmed（多 run，Claude 與 Codex 獨立觀察）：**

- `run 20260520T044657Z-123192, seq=65`
  - source: `아니, 아이 우리 키아가 700개. 챈나 귀여워.`
  - target: `不對，我們的키아有700個，챈나好可愛。` ← 챈나 Hangul 滲漏
- `run 20260520T052405Z-62828, seq=10`
  - source: `...우와 이러고 노래를 들어봤거든요? 어 챈나 신청이었어.`
  - target: `啊，是챈나的點歌。` ← 챈나 Hangul 滲漏
- `run 20260520T052405Z-62828, seq=55`
  - source: `챈나 쳐가지고 강퇴 2000명 됐을 것 같긴 한데 ... 채나로 홍보 야무지게 했다`
  - target: `因為채나出場，...用채나做宣傳做得超認真。` ← Qwen 自行正規化至 mishear，無羅馬化
- `run 20260520T123192, seq=64`（先前輪）
  - source: `솜주먹, 봉준, 백인석 하는 소리 하고 있구만.`
  - target: `「솜주먹、봉준、백인석」嗎。` ← 봉준 Hangul 滲漏（引號列表中）

**HADES runs 챈나 source-correct n=7：** 3/7 全正確羅馬化，3/7 Hangul 滲漏，1/7 mixed（mixed case = Task #16 scope）。滲漏的 rows 中，Task #14 `wrong_forms` 確認無任何可 match 字串——閉集缺口確認。

**前瞻性覆蓋（零 runtime N，非 confirmed failure）：**
- `성태`、`키마` 同屬同一 `_NAME_RENDERING_RULES` table（同 HADES profile）。若 Qwen 對這兩個名字也產出 Hangul 滲漏，同一缺口存在。納入本 task 是一致且最小的改動，不新增任何機制風險。**未標記為已觀察失效。**

### §16.3 Scope

**In scope：**
- 在 `modules/translator.py` 的 `_NAME_RENDERING_RULES` 中，對四個現有 `_NameRenderingRule` entry 的 `wrong_forms` tuple 新增 Hangul 自身形：
  - 챈나 rule：新增 `"챈나"`
  - 봉준/김봉준 rule：新增 `"봉준"`、`"김봉준"`
  - 성태 rule：新增 `"성태"`（前瞻性，同 table）
  - 키마 rule：新增 `"키마"`（前瞻性，同 table）
- 保留所有現有 source-gated / profile-gated / boundary-aware 邏輯（無 matcher 改動）
- 保留 idempotency 行為：`if canonical in result: return result`（guard 不動，見 §16.7）
- 新增測試覆蓋 Hangul self-form target leaks
- Task #14 全套測試須繼續 pass

**明確不做：**
- `채나` / `채나롱` / 其他 STT source 變體（source normalization — 另一範疇；**不得**將這些形式加入 wrong_forms）
- `서브주` / `섭주` / `섭쥬` / `섭정` / `服主` 相關（source-norm audit #4）
- `고세구` Hangul 自身形擴充：全部可用 run 中零 N；shared 層 entry 目前 `wrong_forms` 僅 `"高世久"`；**延後**至有含 고세구 source 的 run 確認同一模式後再考慮
- canonical+wrong 共存 / early-return 邏輯 → **Task #16 專屬**
- Matcher 架構改動
- Cache 架構改動
- prompt_version 改動
- JSON schema 改動
- `data/default_slang.json`（不得含裸人名；本 task 不動）
- 回退或重開 Task #14 commit
- `히나` 裸形決策（仍延後，Task #14 §15.9-G）

### §16.4 風險分級與流程

**評估：** 低於 Task #14 風險。改動純屬在現有已測試機制內新增資料：
- 無 matcher 改寫
- 無新 gating 邏輯
- 無 schema 改動
- 無 cache 架構交互

**主要風險：** 新增 Hangul 自身形至 `wrong_forms` 是否導致 false positive（target 中合理保留韓文的情況）？

緩解：現有 source-gate（`_source_has_name_alias`）仍保護——修正只在 source 含邊界匹配 alias 時才觸發。若 source 有 `챈나` 且 target 有 `챈나` → 替換觸發（正確行為）。若 source 無 `챈나` 但 target 有 `챈나`（假設情境）→ 修正不觸發（source-gate 保護）。風險有界。

**次要顧慮：** idempotency。`if canonical in result: return result` 意味著若 Chxxnnx 和 챈나 同時出現在 target（canonical+self-form mixed，如 seq=11）→ 챈나 不被修正。這是 Task #16 問題，Task #15 不改動此 guard。

**Codex non-blocking risks（保留為記錄，不阻擋本 task）：**
- **Target-side 無 boundary matcher**：target 中 `봉준이` 之類帶助詞的形式會被替換為 `Kim Bongjun이`（助詞殘留）。Task #14 同樣如此，source-gate 仍保護，可接受。
- **성태/키마 前瞻性添加（零 runtime N）**：屬同一 HADES rule table 的封閉集合；reuse 既有機制不新增風險，可接受。
- **Mixed canonical+self-form test**：僅記錄 Task #16 的 known limitation，Task #15 不修正 canonical+wrong 共存行為。

**流程：壓縮 4 步（含 cross-review）**

1. Claude plan（本節 §16）
2. Codex quick cross-review（低 bias 模板；確認：(a) self-form 無 false positive，(b) idempotency 仍成立，(c) scope 未滲入 default_slang 或 matcher）
3. Codex implementation + tests
4. Claude post-impl validation（git/diff/test/scope 獨立重跑）
5. User decides commit/push

不需完整 7 步，但 cross-review 步驟**不可省**（因：self-form 與現有 idempotency guard 交互、前瞻性 성태/키마 非 runtime-confirmed、按 [[feedback_codex_prompt_framing_bias]] 低風險壓縮版仍含 step 2）。

Cross-review prompt 由 **user 發出**（非 Claude 代擬，per 角色分工契約）。

### §16.5 實作方式

**不在此實作。** 實作時：

**允許改動的檔案（僅限）：**
- `modules/translator.py`（`_NAME_RENDERING_RULES` wrong_forms 資料改動）
- `tests/test_translator.py`（新增 Hangul self-form 測試案例）
- `tests/test_config.py`（選用，僅在需要擴充 no-default_slang-pollution 覆蓋 `김봉준` 時）

在 `modules/translator.py` 的 `_NAME_RENDERING_RULES` 中更新四個現有 `_NameRenderingRule` entry，於 `wrong_forms` tuple 末尾追加 Hangul 自身形：

```python
# 챈나 rule
_NameRenderingRule(
    _HADES_PROFILE_ID,
    ("챈나",),
    ("-chan", "-Chan", "－chan", "－Chan", "–chan", "–Chan", "—chan", "—Chan", "챈나"),
    "Chxxnnx",
),
# 봉준/김봉준 rule
_NameRenderingRule(
    _HADES_PROFILE_ID,
    ("김봉준", "봉준"),
    ("Bongjun", "奉俊", "奉主", "봉준", "김봉준"),
    "Kim Bongjun",
),
# 성태 rule
_NameRenderingRule(
    _HADES_PROFILE_ID,
    ("성태",),
    ("Sungtae老師", "Sungtae哥", "Sungtae", "成泰", "狀態哥", "성태"),
    "KimSungtae",
),
# 키마 rule
_NameRenderingRule(
    _HADES_PROFILE_ID,
    ("키마",),
    ("Kima", "基馬", "키마"),
    "Kyma",
),
```

`_replace_wrong_name_forms` 已透過 `re.escape` 處理所有字串為 regex；Hangul 字串合法。最長優先排序不受影響。無其他 code 改動。

**必須保持不動的檔案：**
- `data/default_slang.json`
- `data/streamer_profiles.json`
- `data/translation_profiles.json`
- `modules/translation_policy.py`
- `modules/translation_prompts.py`
- `modules/streamer_profiles.py`
- `config.py`（含已有測試用 `streamer_profile→hades_chxxnnx`，非本 task 一部分，禁 commit）
- 所有 `OPTIMIZATION_*.md` / `ARCHITECTURE_REVIEW*.md`（本地保留，永遠不 stage/push）

### §16.6 測試計畫

**在 `tests/test_translator.py` 新增：**

1. **Hangul self-form → canonical（HADES profile）**
   - source `챈나`，target 含 `챈나` → 修正為 `Chxxnnx`
   - source `봉준`，target 含 `봉준` → 修正為 `Kim Bongjun`
   - source `김봉준`，target 含 `김봉준` → 修正為 `Kim Bongjun`
   - source `성태`，target 含 `성태` → 修正為 `KimSungtae`
   - source `키마`，target 含 `키마` → 修正為 `Kyma`

2. **Source-gate（無 source alias 則不修正）**
   - source 無人名，target 含 Hangul 自身形 → target 不變
   - 例：source=`오늘 방송`，target=`챈나好可愛` → target 不變（챈나 不在 source）

3. **Profile-gate**
   - 在 `stellive_hina`/`isegye_lilpa`/`""` profile 下：HADES Hangul 自身形即便 source 與 target 都含仍不修正
   - `use_profile=False`：同，HADES 名字不修正

4. **Already-canonical target unchanged**（Codex revision #3，與 idempotency 獨立）
   - source 有 챈나，target 已是 `Chxxnnx好可愛`（canonical form, no wrong-form present）→ target 不變
   - source 有 봉준，target 已是 `Kim Bongjun是個人` → target 不變
   - 驗證 early-return guard 在 target 已含 canonical 時不進行任何替換

5. **Idempotency（self-form）**
   - source 有 챈나，target `챈나好可愛` → 修正為 `Chxxnnx好可愛`
   - 再 apply 一次：`Chxxnnx好可愛` → 不變（canonical in result → early return）

6. **Mixed canonical+self-form（documenting known limitation，不修正）**
   - source 有 챈나，target `챈나...Chxxnnx...챈나` → canonical present → early-return → `챈나` 未被修正（預期行為，Task #16 scope）
   - 加 comment：`# Task #16: canonical+wrong coexist; early-return intentionally leaves 챈나 unfixed`

7. **Regression：Task #14 全套測試須 pass**
   - boundary positive/negative、source-gated、profile-gating、idempotency、cache round-trip、prompt-version、no-default_slang-pollution 測試

8. **No default_slang pollution**（Codex revision #5：明確覆蓋 `김봉준`）
   - `data/default_slang.json` 不含 챈나/봉준/**김봉준**/성태/키마/히나 裸形為 key
   - 現有 `test_config.py` test 應已覆蓋 챈나/봉준/성태/키마；若未覆蓋 `김봉준` 則補入（可在 `tests/test_config.py` 擴充現有 assertion list）

### §16.7 與 Task #16 的關係

Task #15 **不改動** `_replace_wrong_name_forms` 的 `if canonical in result: return result` guard。

此 guard 是 Task #16 的問題：當單一 target 同時含 canonical（如 `Chxxnnx`）與 wrong-form（如殘餘 `-chan` 或 `챈나`）時，early-return 使 wrong-form 無法被修正。已在生產環境 seq=11（`-chan...Chxxnnx...-chan`）觀察到。修正方向可能是 per-occurrence 處理（如不重複 match canonical 的 regex lookahead，或分別處理不與 canonical 相鄰的 wrong-form）。這是獨立的精度邊界改動，需要獨立 plan 與 Codex review。

Task #15 與 Task #16 相容：Task #16 修正 early-return guard 後，Task #15 新增的 Hangul 自身形也同樣受益。兩者不衝突。

### §16.8 輸出 / 下一步

**Task #15 plan：完成（本節 §16.1–§16.8）**

**壓縮 4 步流程：可行。** Claude plan → Codex quick cross-review → Codex implement+tests → Claude post-impl。User decides push。

**Codex quick cross-review：必要**（實作前）。驗證：(a) Hangul self-form 無 false positive，(b) idempotency 仍成立，(c) scope 未滲入 default_slang 或 matcher，(d) 前瞻性 성태/키마 safe。Cross-review prompt 由 **user 發出**（非 Claude 代擬）。

**實作前無 user decision 待解：** bare 히나 仍延後（Task #15 out of scope）；고세구 self-form 延後（零 runtime N）。

**實作預期觸動的檔案（§16.5 revised list）：**
- `modules/translator.py`（`_NAME_RENDERING_RULES` 中 4 個 entry 的 `wrong_forms`）
- `tests/test_translator.py`（Hangul self-form 新測試案例，含 already-canonical test）
- `tests/test_config.py`（選用：僅在需補 `김봉준` no-default_slang-pollution 覆蓋時）

**必須保持不動的檔案：** `data/default_slang.json`、`data/streamer_profiles.json`、`data/translation_profiles.json`、`modules/translation_policy.py`、`modules/translation_prompts.py`、`modules/streamer_profiles.py`、`config.py`、所有 `OPTIMIZATION_*.md` / `ARCHITECTURE_REVIEW*.md`（安全限制）。Task #14 commit 不得修改或回退。

下一階段 prompt 由 user 發出（Claude 不擬實作/下一步 prompt，不擴張 scope；發現需改 scope → 標 blocker / user decision）。

---

## 十七、Task #16: `_replace_wrong_name_forms` early-return refinement（canonical+wrong coexist 修正 + idempotency 重新保證）

> 日期：2026-05-21
> 性質：邏輯改動（精度邊界 + idempotency 重新保證），**走完整 7 步流程**
> 狀態：📝 §17.11 revised（Codex low-bias cross-review ⚠️ → 3 項 narrow revisions applied）— 已 cross-review、未 blocker-only re-review、未實作、未 commit、未 push

### §17.1 Problem statement

`modules/translator.py:248-257`：

```python
def _replace_wrong_name_forms(result: str, rule: _NameRenderingRule) -> str:
    if rule.canonical in result:
        return result  # ← 過粗：有 canonical 就完全不修，wrong-form 殘留
    wrong_forms = tuple(sorted(rule.wrong_forms, key=len, reverse=True))
    if not wrong_forms:
        return result
    pattern = re.compile("|".join(re.escape(wrong) for wrong in wrong_forms))
    return pattern.sub(rule.canonical, result)
```

該 early-return 原意是 idempotency 保險，但實際阻擋了 wrong-form + canonical 共存時的修正。Task #14 / Task #15 兩輪 runtime 都觀察到此 bug 殘留。

Task #16 的目標：修這個 guard 過粗的問題，**同時保留 idempotency**（重複套用同一 source/result 不可重複擴張 canonical，例如不可把 `Kim Bongjun` 變成 `Kim Kim Bongjun`）。

### §17.2 Evidence

**Production runtime：**

1. `run 20260520T052405Z-62828, seq=11`
   - source：`챈나야 고맙다. 그래서 나도 채나롱 서버 열심히 홍보했어 챈나야.`
   - target：`-chan，謝謝你。所以我也有好好幫Chxxnnx伺服器宣傳，-chan。`
   - 病徵：target 同時含 `Chxxnnx`（canonical）+ 兩個 `-chan`（wrong-form），early-return 後兩個 `-chan` 殘留。

2. **單元測試已記錄**：`tests/test_translator.py::test_streamer_name_rendering_mixed_canonical_self_form_remains_task16_scope`（Task #15 留下的 documenting test）—— 本 task 完工後該 test 必須改寫為 fix 後的預期行為。

**Idempotency 為何不能直接拿掉 early-return：**

봉준 rule `wrong_forms` 含 `"Bongjun"`，canonical 是 `"Kim Bongjun"`。`Bongjun` 是 `Kim Bongjun` 的**子字串**。若單純拿掉 early-return guard：

- 第一次套用 `봉준來了` → regex match `봉준` → `Kim Bongjun來了` ✓
- 第二次套用 `Kim Bongjun來了` → regex match `Bongjun`（子字串）→ `Kim Kim Bongjun來了` ❌

idempotency 被打破。任何修法都必須同時解決「修 mixed」+「保 idempotent」。

### §17.3 Scope

**In scope：**

- 修改 `modules/translator.py::_replace_wrong_name_forms`，使其
  - 修掉 target 中所有 wrong-form（即便 canonical 已存在）
  - 仍保持 idempotency（重複呼叫不擴張 canonical，含 `Bongjun ⊂ Kim Bongjun` 的子字串情境）
  - 仍保持已 canonical-only 的 target 不變
- 更新 `tests/test_translator.py::test_streamer_name_rendering_mixed_canonical_self_form_remains_task16_scope`（rename + 改 assert 為 fix 後預期）
- 新增 mixed canonical+wrong 修正測試（含 wrong-form 為 canonical 子字串的 idempotency 測試）
- Task #14/#15 既有測試**全部須繼續通過**

**明確不做（per user 指示，逐項固定）：**

- ❌ `wrong_forms` 擴充（Task #15 已完工，本 task 不動）
- ❌ `채나` / `채나롱` / 其他 STT source 變體（source normalization，另一線）
- ❌ `서브주` / `섭주` / `섭쥬` / `섭정` / `服주` / `服主` 相關（source-norm audit #4）
- ❌ `data/default_slang.json`（不動）
- ❌ matcher / source-gate / profile-gate 架構（皆不動）
- ❌ cache schema / cache key / `prompt_version`（皆不動）
- ❌ JSON schema 改動
- ❌ Task #14 / Task #15 commit 不修改、不回退
- ❌ 跨 profile / shared 規則新增（已有的 `_SHARED_NAME_SCOPE` `고세구` 規則行為不變）

### §17.4 Risk classification 與 workflow

**評估：** 比 Task #15 高、比 Task #14 低。

| 維度 | 評估 |
|---|---|
| 精度邊界 | ⚠️ 是（regex 替換在「canonical 為 wrong-form 父字串」情境下需仔細處理） |
| 跨 profile / cross-cutting | ✅ 不涉，shared 規則行為不變 |
| Schema / wiring | ✅ 不涉 |
| Cache idempotency | ⚠️ 是（cache 存 pre-correction text，corrections 在 lookup 與 fresh 兩個 path 都套用；行為改變意味著舊 cache row 重讀時走新邏輯——預期、無 migration、但須測試覆蓋） |
| 可逆性 | ✅ 易回退（單一函式內部改動） |

**流程：完整 7 步（不可壓縮）**

依 [[feedback_codex_prompt_framing_bias]]：精度邊界 + idempotency 重新保證 → 必走完整流程。

1. Claude plan（本節 §17.1–§17.8 + §17.9 預留 Codex 區塊）
2. Codex 低 bias cross-review（填 §17.9）
3. Claude revise（只據 §17.9，新增 §17.11）
4. Codex blocker-only re-review
5. Codex implementation + tests
6. Claude post-impl validation（git/diff/test/scope 獨立重跑）
7. User decides commit/push

Cross-review prompt 由 **user 發出**，不由 Claude 代擬。

### §17.5 Candidate approaches

| # | 方案 | 內容 | 結論 |
|---|---|---|---|
| A | 直接移除 early-return guard | `pattern.sub(canonical, result)` 無條件套用 | ❌ 不採。`Bongjun ⊂ Kim Bongjun`，重套用會產生 `Kim Kim Bongjun`（idempotency 被破）。 |
| B | Placeholder 三段替換 | (1) `canonical → <<PLACEHOLDER>>` (2) `wrong_forms → canonical` (3) `<<PLACEHOLDER>> → canonical` | ⚠️ 可行但較重。3 次掃描；可讀性較差；placeholder 撞 risk。 |
| C | **Canonical 加入 alternation，longest-first** | regex 同時 match `canonical | wrong_forms...`，每 match 一律 `sub` 成 `canonical`。canonical 與自身 match 等同 no-op；wrong-form 替換為 canonical。長度排序使 canonical 不被 wrong-form 子字串先吃掉。 | ✅ **採用**。單次掃描；天然 idempotent；無 placeholder 風險；最小改動。 |
| D | Lookbehind/lookahead 排除 canonical 內部的 wrong-form | regex 加 `(?<!Kim )Bongjun` 之類 | ❌ 不採。逐 rule 客製化；脆且不可一般化。 |
| E | Replace-until-stable 迴圈 | 重複套用 wrong-form 替換直到結果穩定 | ❌ 不採。`Bongjun ⊂ Kim Bongjun` 不收斂；同 A 病根。 |

### §17.6 Preferred design（Candidate C 細節）

**核心：** 將 `rule.canonical` 自身加入 regex alternation，與 `wrong_forms` 一起參與替換；按長度由長至短排序確保 canonical（或最長 wrong-form）優先於可能是其子字串的較短 alternative。每個 match 一律替換為 canonical：canonical → canonical 等同 no-op，wrong-form → canonical 即為修正。

**Proposed code（modules/translator.py）：**

```python
def _replace_wrong_name_forms(result: str, rule: _NameRenderingRule) -> str:
    if not rule.wrong_forms:
        return result
    # Match canonical OR any wrong-form, in a single regex.
    # Sort longest-first so that an alternative which contains a shorter
    # alternative as a substring (e.g. canonical "Kim Bongjun" containing
    # wrong-form "Bongjun") is tried first at each position, preventing
    # the canonical from being re-matched as its own wrong-form on
    # repeated application (idempotency).
    alternatives = sorted({rule.canonical, *rule.wrong_forms}, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(alt) for alt in alternatives))
    return pattern.sub(rule.canonical, result)
```

**逐 rule walk-through：**

| Rule | canonical | wrong_forms 內最長者 | canonical 是否在 wrong_forms 父子集合中？ | longest-first 後 canonical 位置 |
|---|---|---|---|---|
| 챈나 | `Chxxnnx`(7) | `-chan/-Chan/...`(5) | 互不含 | 第 1 |
| 봉준/김봉준 | `Kim Bongjun`(11) | `Bongjun`(7) | **canonical 含 `Bongjun` 為子字串** | 第 1（最長）✓ |
| 성태 | `KimSungtae`(10) | `Sungtae老師`(9) | 互不含 | 第 1 |
| 키마 | `Kyma`(4) | `Kima`(4) | 平手；字元不同，同一位置只一個會 match | 並列第一（無衝突） |
| 고세구 | `Gosegu`(6) | `高世久`(3) | 互不含 | 第 1 |

關鍵在 봉준 rule：sorted longest-first 後，`Kim Bongjun` 在 alternation 最前面。`re.sub` 從左到右掃描，在 `Kim Bongjun來了` 的 position 0，先試 `Kim Bongjun` → 命中，consume 11 chars，no-op replace，continue from end of match。`Bongjun` 沒機會被獨立 match。✓

**Mixed case walk-through（production seq=11 形）：**

target = `-chan，謝謝你。所以我也有好好幫Chxxnnx伺服器宣傳，-chan。`

alternatives sorted longest-first：`Chxxnnx`(7), `-chan/-Chan/...`(5–6), `챈나`(2)

`re.sub` 掃描：

- position 0 `-chan` → 試 `Chxxnnx` 不中、試 `-chan` 命中 → 替換為 `Chxxnnx`，移到 match 結束位置。
- 中段 `Chxxnnx` → 試 `Chxxnnx` 命中 → 替換為 `Chxxnnx`（no-op）。
- 尾段 `-chan` → 命中 → 替換為 `Chxxnnx`。

結果：`Chxxnnx，謝謝你。所以我也有好好幫Chxxnnx伺服器宣傳，Chxxnnx。` ✓

**已 canonical-only 的 target：** target = `Chxxnnx好可愛`，source 含 `챈나가` → position 0 試 `Chxxnnx` 命中 → no-op；後續 `好可愛` 無 match → 結果 `Chxxnnx好可愛`（不變）✓

**重複呼叫：** 第一次 `봉준來了` → `Kim Bongjun來了`；第二次 `Kim Bongjun來了` → alternation 第一個 alt `Kim Bongjun` 命中於 position 0 → no-op → `Kim Bongjun來了`（不變）✓

### §17.7 Implementation approach（非實作，僅描述）

**需改動的檔案（僅 2 個）：**

- `modules/translator.py`
  - 改寫 `_replace_wrong_name_forms`（單一函式，~9 行）
  - 函式簽名不變；上游呼叫點不動

- `tests/test_translator.py`
  - 改寫 `test_streamer_name_rendering_mixed_canonical_self_form_remains_task16_scope`
    - 改 test name 去除 `remains_task16_scope`，改為 `fixes_mixed_canonical_and_wrong_forms` 之類
    - 改 assert：`"챈나...Chxxnnx...챈나"` → `"Chxxnnx...Chxxnnx...Chxxnnx"`
    - 移除註解中「Task #16 owns」字樣
  - 新增測試（詳見 §17.8 test plan）

**必須保持不動：**

- `data/default_slang.json`
- `data/streamer_profiles.json`
- `data/translation_profiles.json`
- `modules/translation_policy.py`
- `modules/translation_prompts.py`
- `modules/streamer_profiles.py`
- `config.py`
- 所有 `OPTIMIZATION_*.md` / `ARCHITECTURE_REVIEW*.md`（本地保留，永不 stage/push）
- Task #14 commit `655f048` 與 Task #15 commit `5f0c0b2` 不修改、不回退

### §17.8 Test plan

**A. 新增/改寫：mixed canonical + wrong-form**

1. `-chan + Chxxnnx + -chan` mixed → 三處皆變 `Chxxnnx`（覆蓋 production seq=11）
2. `챈나 + Chxxnnx + 챈나`（self-form + canonical）→ 三處皆變 `Chxxnnx`
3. `봉준 + Kim Bongjun + 봉준` mixed → 三處皆變 `Kim Bongjun`
4. `Bongjun + Kim Bongjun`（wrong-form-as-substring-of-canonical 共存）→ `Kim Bongjun + Kim Bongjun`
5. **성태 mixed（Codex revision #1）**：source 含 `성태`，target 形如 `Sungtae哥 ... KimSungtae`（含 `Sungtae` 子字串於 canonical 內）→ 預期 `KimSungtae ... KimSungtae`。驗證殘留 wrong-form 在 canonical 已存在時仍被修正，且未引發 `Sungtae` 子字串重套用。

**B. 子字串 idempotency（新增；Candidate A 失敗的就是這條）**

6. 對 `Kim Bongjun來了` 連續呼叫 `_apply_source_aware_corrections` 兩次（source = `봉준이 왔어요`），結果必須等於 `Kim Bongjun來了`（不可變成 `Kim Kim Bongjun來了`）

**B-extra. No-artifact repeated-application checks（Codex revision #2）**

對每個 substring-sensitive rule，在「第一次套用 + 第二次套用 + 已 canonical-only 套用」三條路徑後分別 assert 結果中沒有以下 artifact：

7. **Chxxnnx**：不可出現 `ChxxnnxChxxnnx`、`Chxxnnx Chxxnnx Chxxnnx`（除非原 target 本就有多個 canonical 經正確修正而成的）、或任何 canonical-doubling。具體 case：source 有 `챈나`，target = `Chxxnnx好可愛` → 套用兩次後仍為 `Chxxnnx好可愛`。
8. **Kim Bongjun**：不可出現 `Kim Kim Bongjun`、`Kim BongjunKim Bongjun`、或 canonical 內 `Bongjun` 子字串被再次替換產生的損壞。具體 case：source 有 `봉준`，target = `Kim Bongjun是個人` → 套用兩次後仍為 `Kim Bongjun是個人`；target = `Bongjun + Kim Bongjun + Bongjun` → 套用後 `Kim Bongjun + Kim Bongjun + Kim Bongjun`，再套用一次不變。
9. **KimSungtae**：不可出現 `KimKimSungtae`、`KimSungtaeKimSungtae`、或 canonical 內 `Sungtae` 子字串被再次替換產生的損壞。具體 case：source 有 `성태`，target = `KimSungtae是老師` → 套用兩次後仍為 `KimSungtae是老師`；target = `Sungtae哥 + KimSungtae` → 套用後 `KimSungtae + KimSungtae`，再套用一次不變。

**驗證方式**：每條 assert 採用「`once = correct(source, target)`、`twice = correct(source, once)`、`assert once == expected`、`assert twice == once`」雙套用模式，明確覆蓋兩次套用後無 artifact。

**C. 既有行為迴歸（保留現有 Task #14/#15 assertion）**

10. Wrong-form-only → canonical（`봉준來了` → `Kim Bongjun來了`）
11. Self-form → canonical（`챈나好可愛` → `Chxxnnx好可愛`）
12. Already-canonical-unchanged（`Chxxnnx好可愛` → `Chxxnnx好可愛`）
13. Boundary positive/negative 全套
14. Source-gate（target 含 wrong-form 但 source 無 alias）→ 不變
15. Profile-gate（HADES alternatives 在 stellive/isegye/use_profile=False 下不觸發）
16. Cache round-trip（cache 取回 → 套用 → 不雙重擴張）
17. `Kima` ↔ `Kyma` 平手長度的情境（驗 `Kima` 仍被改成 `Kyma`，`Kyma` 仍保持）

**C-extra. default_slang 與 config test 保護（Codex revision #3）**

明確保證以下不變量，作為 scope 邊界鎖定：

18. **`data/default_slang.json` 必須維持不動**：本 task 不修改該檔；implementation 階段若 diff 中出現對該檔的修改即視為 scope violation。
19. **不得新增 default_slang entries**：包含但不限於 HADES 裸人名（챈나/봉준/김봉준/성태/키마）、히나、고세구。
20. **`tests/test_config.py::test_default_slang_removes_conflicting_bare_person_names` 必須持續通過**：該 test 由 Task #15 擴充至涵蓋 챈나/키마/봉준/김봉준/성태/히나。本 task 不應觸發該 test 失效。
21. **`tests/test_config.py` 預期不需修改**：Task #16 改動侷限在 `_replace_wrong_name_forms`，與 default_slang 路徑無交集。若 implementation 階段意外需要改動 `tests/test_config.py`，視為 scope violation 訊號，應停下檢視（**不**默默改）。

**D. 全套 regression**

22. `pytest tests/test_translator.py -q` 全綠
23. `pytest tests/test_config.py -q` 全綠（baseline 30 passed；Task #16 後預期不變）
24. `pytest -q --basetemp=.pytest-tmp/task16` 全綠（baseline 421 passed / 4 skipped；Task #16 預期增加新測試後保持綠）

### §17.9 Codex low-bias cross-review（待 Codex 填）

> 此區塊由 Codex 於 step 2（low-bias cross-review）填寫。Claude 不得預先填寫或揣測 Codex 觀點。
>
> Cross-review prompt 由 user 發出。

#### §17.9.A Codex 主張驗證
（Codex 對 §17.6 設計、§17.7 file/scope、§17.8 test plan 逐項給 ✅ supported / ⚠️ partially supported / ❌ unsupported，引 code/data/runtime 證據）

#### §17.9.B Codex 目標達成評估
（§17.1 目標是否能被 §17.6 達成；若缺口請指）

#### §17.9.C Codex 不當排除檢查
（§17.3 Out of scope 是否含某項其實是達成目標必要條件）

#### §17.9.D Codex 風險分級
- Blocker：（有 code/data 證據、未修不該動工）
- Non-blocking risk：（合理推論或 runtime 待驗）
- Post-implementation validation：（上線後 runtime 指標）

#### §17.9.E Codex 結論
- ✅ 同意動工 / ⚠️ 修正後再議 / ❌ 不建議

### §17.10 Output / next step

**Plan：本節（§17.1–§17.8 + §17.9 預留 Codex 區塊）= step 1。**

**Workflow：完整 7 步，無壓縮。**

**下一步**：user 發 Codex cross-review prompt（低 bias 模板：goal / proposed changes / non-goals / relevant paths / optional runtime evidence；**不**附 Claude 理由散文與預擬問題）。Codex 填 §17.9 後，Claude 走 step 3（revise → 新增 §17.11 revised plan）。

**禁止項：**

- 不實作 code/data
- 不 stage / commit / push
- 不改 Task #14 / Task #15 任何 commit
- 不擴張 scope
- Claude 不擬 implementation prompt（per [[feedback_codex_prompt_framing_bias]]）
- `OPTIMIZATION_*.md` 不 stage / 不 push

**Verification（plan 通過後實作完成的驗證方式）：**

1. `live-subtitle-env\Scripts\python.exe -m pytest tests/test_translator.py -q`
2. `live-subtitle-env\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp/task16-validation`
3. Runtime post-push validation：下一輪 HADES 直播 log 掃 mixed canonical+wrong 案例（seq=11 同形），應 0 殘留。

### §17.11 Revised plan post-Codex cross-review

> 性質：Claude step 3 修訂。僅據 §17.9 Codex cross-review 的 3 項 narrow revisions 修訂，未擴張 scope。Codex 結論 ⚠️ 修正後再議，**0 blockers**。

**Codex 結論摘要（per session handoff）：**
- Problem framing 正確；Candidate C 為 safest minimal option，supported。
- 0 blockers。3 項 narrow revisions 後即可進 step 4 blocker-only re-review。
- Full 7-step flow 維持，不可壓縮。No user decision needed。
- Implementation 邊界維持 `modules/translator.py` + `tests/test_translator.py`；`tests/test_config.py` 不應需要修改（除非必要）。

**Revisions applied：**

| # | Codex 要求 | 修訂落點 | 應對方式 |
|---|---|---|---|
| 1 | 加入 성태 mixed 測試 | §17.8 A.5 | 新增 case 5：source `성태`、target `Sungtae哥 ... KimSungtae` → 預期 `KimSungtae ... KimSungtae`。明確覆蓋「殘留 wrong-form + canonical 已存在」+「Sungtae 子字串在 canonical 內」雙條件。 |
| 2 | 加入 no-artifact repeated-application checks | §17.8 B-extra（cases 7–9） | 對 Chxxnnx / Kim Bongjun / KimSungtae 三個 substring-sensitive canonical 各新增雙套用 assert：`once = correct(source, target)`、`twice = correct(source, once)`、驗 `twice == once` 且無 `ChxxnnxChxxnnx` / `Kim Kim Bongjun` / `KimKimSungtae` 之類 artifact。 |
| 3 | 明確 default_slang.json 不動 + config test 維持 | §17.8 C-extra（cases 18–21） | 列為 scope 邊界 invariant：(a) `data/default_slang.json` 不修改、(b) 不新增 default_slang entries、(c) `tests/test_config.py::test_default_slang_removes_conflicting_bare_person_names` 持續通過、(d) `tests/test_config.py` 預期不需修改；implementation 若意外需要修則視為 scope violation 訊號。同步在 §17.8 D 加入 `pytest tests/test_config.py -q` baseline 30 passed 的迴歸 check。 |

**Preserved Codex conclusions（不動）：**
- Candidate C 為 preferred design（§17.5 / §17.6 不動）。
- Regex/canonical-in-pattern 對目前 rule set safe。
- Full 7-step flow 維持（§17.4 不動）。
- Implementation 邊界 = `modules/translator.py` + `tests/test_translator.py` + `tests/test_config.py`（only if necessary，本 task 預期不需要）。

**Preserved non-scope（不動）：**
- 不新增 `wrong_forms`、不處理 `채나/채나롱/STT variants`、不處理 source normalization、不處理 `服主/섭주/섭쥬/서브주/섭정`、不擴張 `고세구` 零 N 行為、不動 `히나` 決策、不改 `default_slang.json` / cache / `prompt_version` / source matcher / source boundary、不修改 Task #14 / Task #15 commit。

**Readiness for step 4：**
- 是否仍有 blocker：**無**。
- 是否仍有 user decision needed：**無**。
- 是否可進入 Codex blocker-only re-review：**可**。請 Codex 僅查「§17.9 列出的 3 項 narrow revisions 是否被 §17.11 + §17.8 真解（非被合理化）」，不重開全審。
- 是否仍禁止實作直到 re-review 通過：**是**。下一階段 prompt 由 user 發。
