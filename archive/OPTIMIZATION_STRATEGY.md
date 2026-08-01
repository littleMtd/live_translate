# 我對這個專案的優化看法

> 日期：2026-05-14
> 性質：立場文（opinion piece），不是 backlog
> 同伴文件：[OPTIMIZATION_NOTES.md](OPTIMIZATION_NOTES.md) 有 16 個可執行項目

---

## TL;DR

**Codex 說「先啟動 runtime，收新的 `runtime_events_*.jsonl`」是對的。但只做一半。**

我的建議：

1. **先收兩週資料，但要先定義什麼叫「好」**（否則收完還是不知道該動哪裡）
2. **停止寫新功能，把既有的觀測層真的用起來**——`runtime_events.jsonl` 沒人在看，等於沒寫
3. **最高槓桿的下一步不是優化 API 成本或延遲，而是一個 daily quality digest**
4. **Phase 2 dashboard 要嘛收尾，要嘛凍結；現在半完工狀態是最差的**

---

## 一、為什麼同意 Codex 但要補強

Codex 的建議聚焦在**收新一輪 `runtime_events_YYYYMMDD.jsonl`**。對的——上一輪 P1 bug 修完後（CJK 範圍、numpy 序列化、daemon 假活著），現在採集的資料才可信。沒有可信資料就沒有可信決策。

但**只說「收資料」不夠**。我看過太多專案：開了 metrics、開了 logs，跑了三個月，沒人去 query。資料變垃圾。

收資料前先回答三個問題：

### 問題 1：什麼指標達到什麼數字算「成功」？

例如不能說「降低翻譯延遲」，要說：
- **p50 翻譯延遲 < 800ms**
- **p95 < 2500ms**
- **API 翻譯（非快取）成功率 > 98%**
- **`empty_target` 旗標每場直播 < 5 次**
- **每小時 token 成本 < $0.30**

寫出來才會發現有些目標其實不重要，可以砍。

### 問題 2：哪些指標是 leading（未來會出事的徵兆），哪些是 lagging（已經出事了）？

- Leading：`translation.fallback.attempt` 突然上升 → 主引擎開始不穩
- Leading：`low_target_cjk` 旗標比例上升 → 翻譯品質開始降
- Lagging：每月帳單暴增 → 已經花完了

優化要對 leading 指標下手。

### 問題 3：誰會在什麼時候看這些指標？

如果回答是「沒人看」，那就先別優化監控。先解決誰看的問題。

---

## 二、最高槓桿的下一步：Daily Quality Digest

不是 Tauri dashboard，不是 cache hit 優化，不是 prompt 微調——是一個**每天自動跑、輸出一頁文字摘要的 cron**。

```
=== live_translate daily report 2026-05-14 ===
Sessions:               3 (total 4h 12m)
Translations completed: 1,847
Cache hit rate:         62%  (memory 41%, db 21%)
Avg latency p50/p95:    720ms / 2.1s
Engine usage:           claude 78%, gemini 19%, google_translate 3%
Token spend (est):      $0.42

⚠️ Anomalies:
- 47 translations flagged `low_target_cjk` (target ratio 0.36)
  → 集中在 18:30-19:00 段，懷疑 Claude rate limit 期間 fallback 品質不佳
- 12 translations had `subtitle_suppressed_reason=duplicate`
  → 正常範圍
- STT consecutive_none warnings: 2 次
  → 比平常多，檢查麥克風

📈 Trends (vs 7-day avg):
- Token spend:  +18%  ⬆️ (新主播啟用，slang 累積中)
- Latency p95:  -8%   ⬇️ (Claude prompt cache 開始發揮作用)
```

**為什麼這個比 Tauri dashboard 重要？**

Tauri dashboard 你要主動打開才看。Daily digest 是被動觀察。**觀察成本接近零的監控才會被持續使用**。

實作不難：

1. `scripts/analyze_runtime_events.py` 已存在 → 擴充輸出格式
2. 加一個簡單的 baseline 對比（每天和過去 7 天平均比）
3. Windows 工作排程器跑一次（早上 9 點）
4. 輸出到 stdout / 寫到 `logs/daily_report_YYYYMMDD.txt`

**這一步開啟了所有後續優化**：之後你才能說「動了 X 之後 cache hit rate 從 62% 升到 78%」——有可驗證的故事。

預估工：3-5 個工作日。投資報酬率最高的一筆。

---

## 三、為什麼我覺得「停止加功能」很重要

過去幾個月這個 repo 加了：

- Phase 1 DB 持久化快取 ✅
- 五個 translator 子模組（engines / runtime / memory / policy / prompts）✅
- streamer profiles JSON ✅
- Silero VAD ✅
- runtime_events.jsonl ✅
- TranslationOutcome dataclass ✅
- Tauri + Vue Phase 2 dashboard 🚧
- prompt evolver ⚠️（預設關閉）
- 多後端模式（nvidia / ollama / anthropic）✅

每一個單看都合理。**累積起來**就是：

- 主分支 ~6000 行新增程式碼
- 13 個 scripts/、28 個 tests/、多份設計文件
- 4 份 ARCHITECTURE_REVIEW.md 看出來的「還有 P1 / P2 / P3 沒修」
- Phase 2 dashboard 一半完成

這已經是一個**「成熟到負擔」**的階段。再加新功能會讓:
- 維護成本爬升（每個新模組要更新文件、寫測試）
- 出 bug 的表面積擴大
- 認知負擔讓你不敢動既有程式碼

**接下來 4-6 週的優化主題應該是「收斂」，不是「擴張」**：

1. 完整跑一輪 [OPTIMIZATION_NOTES.md](OPTIMIZATION_NOTES.md) 的「快速勝利」（第 1-4 項）
2. 開啟 daily digest（上節）
3. 用 2-3 場直播驗證行為符合預期
4. **只有此時**才回頭看 Phase 2 dashboard 要不要繼續

---

## 四、SQLite 快取嚴重沒用滿

這是被低估的優化空間。當前 SQLite 只做兩件事：

1. **去重**：相同 source_text → 返回 cached translation
2. **LRU 淘汰**：滿了刪舊的

**它累積的資料完全沒被分析**。我們有：

- 全部翻譯歷史，含時間戳、引擎、模型、prompt 版本
- 每筆的 hit_count（重複度）
- last_used_at（時序模式）

可以衍生：

| 資料應用 | 帶來什麼 |
|---------|---------|
| **熱詞排行**：跑 `SELECT source_text, hit_count FROM translations ORDER BY hit_count DESC LIMIT 100` | 自動發現該主播口頭禪，回填 streamer profile |
| **Prompt 版本 A/B 比較**：同一句話在不同 prompt_version 下的 target_text 對照 | 換 prompt 後品質變化的客觀證據 |
| **冷熱分層**：90 天沒查過的 entry 移到冷儲存 | DB 變小、查詢變快 |
| **引擎品質回顧**：同一 source_text 不同 engine 的結果差異 | 決定哪個引擎在哪類內容最好 |

這些都是 SQL one-liner 規模的工作，但沒人寫。

**建議**：把 `scripts/analyze_cache.py`（已存在）擴充成有用的工具。或合併到上節的 daily digest 裡。

預估工：每個 query 30 分鐘。整套大概 1 個工作日。

---

## 五、Phase 2 Tauri Dashboard：選一條路

目前狀態：

- `src-tauri/src/`：7 個 Rust 檔，handlers 全在頂層（無 handlers/ 子目錄）
- `src-frontend/src/`：6 個 Vue 元件 + tests
- `utils/config_export.py` 已把 config 寫出來
- **但沒人在用**（基於 git log 和 README 的「還沒做好」標註）

兩條路：

### 路 A：兩週內收尾 ✋
範圍：
- 確認 Tauri 可以正常啟動 / 停止 Python 子行程
- Config panel 能即時改 subtitle 字型 / 位置
- Cache stats 顯示真實 DB 數字
- 不做：config 熱重載（先 dashboard 改完重啟即可）

成本：~2 週
產出：可用的桌面工具

### 路 B：明確凍結 🧊
範圍：
- README 寫清楚 "Tauri dashboard is experimental, may break"
- src-tauri/src-frontend 從 CI 移除（或標 allow_failure）
- 不再為 Tauri 做任何 Python 端 API 變更（如 config_export 結構）

成本：1 小時（文件 + CI）
產出：心智負擔 -1

**我傾向路 B**。理由：

- tkinter 浮動字幕已經夠用（README 自己也說「不需 OBS 插件」）
- Tauri 的價值（即時改 config）需要熱重載才完整，否則就是「重新打開重啟」，那不如直接編輯 config.py
- 即時統計可以用 daily digest（被動觀察）取代，不需要主動打開的 UI
- 維護 Tauri 需要 Rust + Vue + TypeScript 三個技能棧，對單人專案是負擔

如果你個人喜歡寫 Rust，當然走路 A；但「優化專案」的角度看，路 B 更乾淨。

---

## 六、明確的 2 週行動方案

```
Week 1 — 觀測
  Day 1-2  確保 daily digest 可跑（擴 scripts/analyze_runtime_events.py）
  Day 3-5  跑 2-3 場直播，收集真實 runtime_events
  Day 5    第一份 digest 出爐，寫下「我以為會看到 X，實際是 Y」

Week 2 — 行動
  Day 6-7  根據資料挑 1-2 個最痛點實作（從 OPTIMIZATION_NOTES.md 快速勝利區）
  Day 8-10 重新跑直播，digest 對比前後差異
  Day 11+  決策：Tauri 走路 A 還是路 B
```

成功的判準：

- 14 天後，你能用一句話回答「這個專案目前最大的成本/品質問題是什麼」
- 14 天後，至少有一個指標有可驗證的改善
- 14 天後，不會再為「要不要加 X 功能」反覆糾結，因為有了優先級依據

失敗的判準：

- 14 天後仍在新功能 / 重構之間搖擺
- 14 天後 runtime_events 累積了一堆但沒人 query 過
- 14 天後又開了 ARCHITECTURE_REVIEW_5.md

---

## 七、不要做的事（接下來 4 週）

- ❌ 新增第六個翻譯引擎
- ❌ 換 Claude → Sonnet 5（沒理由，現有 sonnet-4-6 已經過剩）
- ❌ 重寫 translator 成 async/await
- ❌ 把 SQLite 換成 PostgreSQL
- ❌ 把 tkinter overlay 換成 Electron / Tauri Vue 元件
- ❌ 為 cross-platform（macOS/Linux）抽象 WASAPI
- ❌ 開新的 review 文件做 4th-pass audit

這些都會被「我先想想優先順序」的衝動驅使。**不要**。除非 daily digest 顯示有具體痛點指向其中之一。

---

## 結語

工程上**最容易做但最沒用**的事是繼續寫程式。
**最難做但最有價值**的事是觀察、決策、放棄錯誤方向。

Codex 點出了「先收資料」這個正確方向。我補上的觀點是：**收資料只是手段，定義成功標準與建立被動觀察機制才是目的**。

接下來兩週只做兩件事：

1. 讓 `runtime_events_YYYYMMDD.jsonl` 從「沒人看的 log」變成「每天 5 分鐘讀完的 digest」
2. 暫停所有新功能，等資料說話

兩週後再回來看這份文件，看看判斷對不對。
