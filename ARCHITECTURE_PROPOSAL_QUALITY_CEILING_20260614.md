# 架構提案：提高翻譯品質上限的候選方向（Host Voiceprint / Rolling Memory / Draft-Final Subtitle 及其他）

日期：2026-06-14（v3）
作者：Claude (Fable 5)
狀態：提案，未實作，不影響 Phase 0 標註與 live path

## v3 變更說明

- 補上 0.1 節「執行環境」：列出實際可用硬體（筆電 RTX 3050 4GB VRAM + R7 7535；桌機 RTX 3050 8GB VRAM + i7-12400F），供各方向評估計算成本時引用。
- **Domain Fine-tune（LoRA）整體移除**（第 4 節改寫為「v3 更新：現階段不可行」、第 6 節新增 6e「已移除」，原「排序靠後」順移為 6f）：v2 把 LoRA 收斂為「現階段僅做資料盤點」，但使用者指出兩個與資料量無關的根本問題——(1) 本機只能跑 8B/16B 量級模型，跟主力翻譯模型（80B 量級）的整體能力落差，LoRA 補不了；(2) 消費級 4-8GB VRAM 連 80B 推論都做不到，更不用說訓練，所以「對等模型 LoRA」這條路只能靠 hosted fine-tuning API，目前沒有資訊顯示存在。結論：LoRA 不是「延後」而是「移除」，除非第 4.4 節列出的前提改變。第 0 節原本列為「方向四」的 Domain Fine-tune 也已標註為移除，方向五（其他候選）順移為方向四。資料整理動作的價值轉移到 6b（術語資產）與 6d（regression set），不再以 LoRA 為理由。

## v2 變更說明（整合 Codex review）

v1 完成後請 Codex 依 `CODEX_REVIEW_PROMPT_QUALITY_CEILING_20260614.md` 做了一次獨立 review。v2 整合該 review 的主要變更：

- 第 0 節的乘法公式改為 Codex 提出的 pipeline chain（`source selection fidelity → transcript fidelity → segmentation policy → context/terminology fidelity → rendering UX stability`），latency/cost/debug 從「扣分項」改為「每一步的約束」。
- Host Voiceprint（第 1 節）第一版範圍收斂為 `host_presence_score` + abstain/`speaker_unclear`，不做直接二元 routing。
- Rolling Memory（第 2 節）補上對 confidence calibration 的前提依賴，並另定獨立的 memory write abstain policy（與 live output abstain policy 區分，見第 5/6a 節）。
- Draft/Final Subtitle（第 3 節）標註為「對舊文件的概念修正」，不代表排程提前。
- Domain Fine-tune（第 4 節）收斂為「現階段僅做資料盤點」，並補上 hosted model 無法直接 LoRA 的前提問題。
- 第 5 節重排優先序（低價值片段跳過、active learning 移到近期可評估），新增 6 個 Codex 指出的遺漏方向。
- 第 6 節改為按成熟度分層的表格，取代原本「全部 Phase 2+」的單一分類，並標註每項是「補充說明」或「修正」舊文件評價。
- 關於原 v1 引用的「23/70 ≈ 33%」統計：Codex 確認算術正確，但指出該數字只能支撐「STT 對不代表翻譯一定對」這個弱結論，不能支撐「translation correctness 已是主要瓶頸」或「LoRA/rolling memory 應提前」——v1 的措辭確實朝後者過度推論，v2 在相關章節已避免重複這個推論。

## 0. 與現有文件的關係

`ARCHITECTURE_RECOMMENDATION_20260613.md` 第 10 節的影響評估表已經列出 `structured rolling memory` 與 `draft/final subtitle`，但兩者都標「延後」且沒有展開設計；`host voiceprint / 單軌語者辨識` 完全沒有出現——該文件在討論 source separation 時，預設需要 OBS 多軌（多軌在這個專案不可行，因為使用者不是直播主，只能拿到 loopback 混音）。

這份文件不是要插隊 Phase 0/1。原則是：**不進 live、不搶 Phase 0/1 主線；但 read-only/offline 的 evidence 工作（離線重播、計算新分數、人工複核既有標註）可以與 Phase 0/1 平行進行**，不需要等 Phase 0/1 全部完成才開始。「Phase 2 之後排優先序」指的是「config flag 啟用、注入 prompt、改動 routing/UX」這類會碰 live path 的步驟，不是指離線驗證本身要排隊等待。每項都可以先用**離線重播 + 現有/未來標註資料**驗證可行性，符合風險補遺「先驗證、再上 live、config flag 控制」的原則。

v1 用 `source correctness × segmentation correctness × translation correctness − latency/cost/debug penalties` 這個乘法公式當框架。Codex review 指出這只是直覺模型：三者並不獨立（上游錯了下游難救，這點公式抓到了），但 latency 不是單純的扣分項——latency 預算會反過來逼 segmentation 提早切，進而拉低 translation correctness，這是一個約束鏈，不是三個獨立分數相乘再扣分。

v2 改用 Codex 提出的鏈狀框架：

```text
source selection fidelity
  -> transcript fidelity
  -> segmentation policy
  -> context/terminology fidelity
  -> rendering UX stability
```

latency/cost/debug 在這個框架裡是**每一步的約束與可觀測性成本**，不是最後才扣的分。各方向對應的環節：

- 方向一（Host Voiceprint）→ `source selection fidelity`：補 host-primary policy 缺的 runtime presence signal
- 方向二（Rolling Memory）→ `context/terminology fidelity`：補跨句/跨節目 context 的不足，但前提依賴 confidence calibration（見第 2 節）
- 方向三（Draft/Final Subtitle）→ `segmentation policy` 與 `rendering UX stability` 之間的取捨：在 latency 約束下提升 segmentation 的上限，代價是 UX 穩定性
- 方向四（其他候選）→ 大多落在 `context/terminology fidelity` 或 `source selection fidelity`，少數是流程/可觀測性優化，未深入設計，列為未來評估清單

> Domain Fine-tune（LoRA）原列為方向四，v3 已評估後從候選清單移除（見第 4 節、6e）：問題不是 prompt vs 權重的資產形式選擇，而是模型量級落差與硬體前提，詳見第 4.2/4.4 節。

### 0.1 執行環境

評估各方向的計算成本時，可用硬體如下：

- 筆電：RTX 3050（4GB VRAM）+ R7 7535
- 桌機：RTX 3050（8GB VRAM）+ i7-12400F

兩台都是消費級入門 GPU。對 §1 host voiceprint 的 embedding 推論（resemblyzer/ECAPA-TDNN 量級），4-8GB VRAM 或純 CPU 都足夠，預期不是瓶頸。這個硬體規格也是 §4 評估後移除 Domain Fine-tune（LoRA）的原因之一：4-8GB VRAM 連 80B 量級的主力模型推論都無法支撐，更不用說訓練（詳見第 4.2 節）。

---

## 1. Host Voiceprint：單軌語者辨識作為 host-primary 的 runtime signal

### 1.1 問題

`PHASE0_EVAL_INVENTORY_20260613.md` 已記錄：`speaker_source_policy=host-primary` 目前只能是離線標註規則，因為 runtime 拿到的是 loopback 混音，沒有 host-silence signal。沒有這個 signal，host-primary 永遠停留在「評測集怎麼標」的層次，無法變成翻譯時的路由依據。

OBS 多軌分離不可行（使用者非直播主）。但 host-primary 的判準本質上是「這段音訊裡，host 的聲音在不在」——這是語者辨識問題，不是音軌分離問題，單軌音訊也能做。

### 1.2 設計

**Enrollment（每個 streamer 一次性）**

- 從 `logs/audio_dump` 裡挑 5-10 分鐘「確定只有 host 一人說話」的片段。既有標註裡 `host_only` 樣本太少（round2 annotations 33 筆只有 2 筆），不適合直接當 enrollment set，需要人工從 audio_dump 另外挑一批乾淨片段。
- 用既有 `torchaudio`（已在 `requirements.txt`）載入音訊，送進語者 embedding 模型（候選：`resemblyzer`、SpeechBrain ECAPA-TDNN——皆 CPU 可跑、與語言無關，韓文/中文/日文都適用）。
- 對多段乾淨片段取 embedding 平均或保留多個 centroid，輸出 `host_voiceprint_<streamer_id>.npy` 之類的資產，存在 repo 外或 `data/` 下（依現有 profile 資產慣例）。

**離線可行性驗證（不動 live path）**

- 寫一支跟 `build_phase0_eval_candidates.py` 同類型的 read-only 腳本：對候選樣本的每個 `source_chunks[].audio_path`，算出與 host voiceprint 的 cosine similarity，輸出 `host_presence_score`。
- 對照既有 round2 標註（`host_only` / `host_over_clip` / `clip_or_other_speaker` / `speaker_unclear`）與 Phase 0 pilot 標完後的 `host_speech` / `clip_when_host_silent` / `speaker_unclear`，看 score 分布能不能把這幾類分開、threshold 大致落在哪。
- 這一步給出量化答案：這條路到底可不可行，值多少 rescue。

**Shadow mode → 第一版 live signal（範圍收斂）**

Codex review 指出：`host_presence_score` 不能二分解決 `host_over_clip`，也不能直接判「該不該翻 clip」——重疊講話的分數本來就會落在中間值。第一版的目標**不是** routing（score 低於門檻 → clip-only，高於門檻 → host 優先這種二元邏輯），而是：

- 可行性驗證通過後，作為 Phase 1 evidence schema 的新欄位（`host_presence_score`），非同步計算、記 log、不影響翻譯決策，config flag 控制。
- 第一版輸出是「兩個確定狀態 + 一個 abstain 區間」三段式：`host_presence_score` 明確高/低時，記錄為對應的 evidence（host 明顯在場 / host 明顯不在場）；分數落在中間（重疊、不確定）時，標記為 `speaker_unclear`/abstain，**交還給既有規則或人工判斷**，不強制系統自己做二元決定。
- 只有在 abstain 比例夠低、且分數分布跟標註結果（`host_speech`/`clip_when_host_silent`/`speaker_unclear`）對得上之後，才考慮讓 `speaker_source_policy=host-primary` 在更明確的場景（host 完全沉默 vs. host 明顯在講話）下參與 routing；`host_over_clip` 這類重疊情境仍維持既有人工/規則判斷，不納入第一版 routing 範圍。

### 1.3 邊角案例與風險

- `host_over_clip`（重疊講話）：聲紋分數會落在中間值，不會乾淨二分。第一版直接標記為 `speaker_unclear`/abstain，不強求二元輸出。
- `multi_streamer`（連動直播）：需要為每位參與者建立 voiceprint，是後續擴充，不影響單人直播的第一版。
- Voice drift：enrollment 樣本應涵蓋不同情緒/音量狀態（平靜講話、大笑、激動），否則 score 在激動片段可能偏低。
- 計算成本：embedding 推論的實際耗時需要 benchmark，不應預先假設是 ms 級。但無論結果如何，初期 shadow mode 不在主路徑上、非同步計算，即使較慢也不影響 live latency。以 0.1 節列出的硬體（4-8GB VRAM 或 CPU）來看，resemblyzer/ECAPA-TDNN 量級的 embedding 推論預期不是瓶頸，但仍應實測確認。

---

## 2. Rolling Memory：直播層級的上下文，補句子層級 context window 的不足

### 2.1 問題

現有 translator 的 context window 是句子層級（前後幾句），但這個內容領域（特定直播主、特定遊戲/類型）的翻譯難點常常來自：

- 重複出現的梗、稱呼、角色名（`streamer_profiles.json` 已處理「固定」的部分，但臨時產生的、當場才出現的梗無法預先收錄）
- 當場的遊戲進度/狀態（例如「剛剛打的那隻 boss」這種指代，幾句前才提到）
- 跨段落呼應的笑話/吐槽

句子層級 context 對這類「跨距較遠但當場很重要」的資訊是結構性盲區，再怎麼調 prompt 也補不了。

**前提依賴：confidence calibration（必要）+ memory write abstain policy（另定）**

Codex review 指出一個風險：現有錯誤主因仍偏 source/STT/forced cut；如果某次寫入 rolling memory 的內容本身是錯的（例如 STT 聽錯的人名），後續所有引用這個 memory 的翻譯都會延續同一個錯誤，而且因為「memory 說這是對的」，錯誤反而會被翻譯系統更有信心地重複輸出——等於把單次錯誤放大成持續性錯誤。

這個風險本身不代表 rolling memory 的上限被高估，而是說明 rolling memory 的可行性有兩層前提，且成熟度不同：

- **confidence calibration 是必要前提**（見第 5/6a 節）：沒有可信的信心分數，就無法判斷「這次寫入 memory 的內容夠不夠可靠」，這是 read-only evidence 工作，可與 Phase 0/1 平行驗證。
- 在 calibration 之上，**另需定義一個獨立的「memory write abstain policy」**：信心不足的內容只記錄但不寫入 memory（或標記為低信心、不採用）。這個 policy 只影響 rolling memory 自身的寫入篩選，**不等於**第 5/6a 節討論的、會改變即時翻譯輸出的 live abstain/output policy——兩者依賴同一個 calibration 結果，但是兩個獨立的、範圍不同的設計。

沒有 confidence calibration 之前，rolling memory 應該維持 shadow-only，不注入 prompt；memory write abstain policy 的設計可以在 calibration 驗證通過後同步進行，不必等待 live output abstain policy 上線。

### 2.2 設計

不是要做完整對話記憶系統，而是一個輕量、持續更新的「本場直播狀態摘要」：

- **內容**：最近提到的人名/角色名/地點、目前遊戲進度關鍵字、最近被翻譯系統標記為 `speaker_unclear`/`low_confidence` 但後來澄清的詞、本場新出現且重複 ≥2 次的詞彙。
- **更新機制**：每 N 句或每 M 秒，用一個小而快的步驟（規則式抽取 + 小模型摘要皆可，先用規則式：詞頻 + NER-like pattern，成本最低）更新摘要，摘要本身保持簡短（例如 5-10 行 bullet）。
- **注入方式**：作為 translator prompt 的一個額外 context block，與 `streamer_profiles.json`/`translation_corrections.json` 互補——後者是跨直播的固定資產，rolling memory 是單場直播的動態資產。
- **Reset 機制**：偵測到長時間靜默、廣告/中場休息、或場景切換（如果未來有 OCR signal 可用）時清空或降權，避免舊話題污染新段落。

### 2.3 落地分期

1. **離線分析**：重播現有 `runtime_events_*.jsonl`，找出 Phase 0 標註裡因「缺乏跨句 context」而判 `a_translation_error` 的案例比例，量化這個問題到底有多大。Codex review 指出：現有 `context_tags`（`multi_speaker`、`clip_audio`、`over_attributed_chunks`、`bgm_mixed`、`unclear_audio` 等）裡沒有 long-context/anaphora/transient-term 這類標籤，不能直接篩出「缺乏跨句 context」的案例。實際做法需要：(a) 人工 re-review 既有 `a_translation_error` 案例的 `annotation.notes`，逐筆判斷錯誤原因是否屬於「跨句/跨段 context 缺失」；或 (b) 在 Phase 0 pilot 之後的標註流程新增 `requires_long_context`/`missing_ephemeral_term` 這類欄位，供之後的樣本直接標記。在這個分析完成、有實際比例數字之前，「跨句 context 缺失有多嚴重」仍是未知數，不應假設它是顯著問題。
2. **設計 rolling memory schema**：定義摘要的資料結構（topic/character/glossary mentions + timestamp + 來源 utterance id + confidence/abstain 標記），方便之後做 golden-file replay；confidence 欄位是寫入門檻的依據，不是事後才加。
3. **Shadow mode**：計算並記錄 rolling memory context，但不注入 prompt；離線用「有 vs 沒有這段 context」分別跑翻譯，比較人工評分差異（A/B replay，不影響 live）。
4. **若有效，加 config flag 注入 prompt**，並監控 prompt token 增量對 latency/cost 的影響。

### 2.4 風險

- **沒有 confidence calibration 之前不該注入 prompt**：這是第一順位風險，見 2.1 的前提依賴說明。
- Prompt token 增加 → latency/cost 上升，需要控制摘要長度上限。
- Rolling memory 本身若抽取錯誤，可能讓錯誤持續被當作「context」傳遞下去（誤差累積），需要有效的 reset/衰減機制——confidence 門檻只能降低機率，不能完全避免。
- 與 `translation_corrections.json` 的關係需要明確分工，避免兩套機制互相覆蓋或衝突。

---

## 3. Latency-adaptive Draft/Final Subtitle：segmentation correctness 的硬限制

### 3.1 問題

韓文是動詞在後、高度依賴後文的語言。風險補遺已設定 latency budget（p95 ≤ current+1s）。在這個預算內，有些句子在語意完整之前就必須切出去翻譯——這不是 STT 或 translator 的錯，是「在語意完整前被迫輸出」的結構性限制，屬於 segmentation correctness 的天花板，跟 source correctness 無關。

`ARCHITECTURE_RECOMMENDATION_20260613.md` 把這類機制歸為「draft/final subtitle，延後，UX 風險中高」，著眼點是「事後補 correction 的慢路徑」。這份提案把它重新框定為：**這可能是唯一能在不犧牲低延遲的前提下，提升 segmentation correctness 上限的機制**。

Codex review 認同這個重新框定比舊文件只看 UX 更準——這是一個**概念修正**：問題本質從「要不要做一個 UX 功能」變成「segmentation policy 的天花板在哪」。但概念修正不等於排程提前：風險（subtitle id、replace semantics、revision policy）是真實的，仍然只值得先做 3.3 步驟 1 的離線分析，不值得提前動 UI 或事件 schema。

### 3.2 設計（範圍限定、降低 UX 風險）

不做「全面可修改字幕」，只在系統自己偵測到「這次切分可能不完整」時才觸發：

- 觸發條件：`sentence_forced=true`、或 cut reason 屬於 `forced_cut`/`low_confidence` 類別。
- 行為：先以目前的切分輸出一個標記為 draft 的翻譯；當下一句到達、resolver 確認語意完整後，若翻譯有顯著差異，才輸出一次 `final` 修正（事件記錄為 `subtitle_revision`），其餘大多數正常切分的句子完全不受影響、不會有「字幕跳動」的體感。

### 3.3 落地分期

1. **離線分析（最低成本，先做這個）**：在 Phase 0 的 100 筆候選裡，篩出 `forced_cut`/`sentence_forced=true` 的樣本（bucket 設計裡已有 15 筆 `forced_cut`），人工標註後看有多少筆「如果看到下一句的內容，翻譯會明顯不同/變正確」。這個數字直接回答「這條路值不值得做」。
2. **事件 schema 擴充設計**：定義 `subtitle_revision` 事件（draft_id、final_text、reason、revision_latency），確保可被 golden-file replay 涵蓋。
3. **前端原型（不影響 live）**：先做「淡入修正」的 UI 原型，讓使用者測試體感能不能接受。
4. **Shadow mode**：記錄 draft vs final 的差異率與 revision 延遲，不真正改字幕。
5. **若 revision rate 足夠高且使用者體感可接受**，才用 config flag 啟用。

### 3.4 風險

- UI/事件語義變更，工程風險中高（與
  `archive/roadmaps/ARCHITECTURE_PROPOSAL_CODEX_20260613.md` 的歷史判斷一致）。
- 若 revision 頻率過高，反而讓使用者覺得字幕不穩定——這是步驟 1 的離線分析要先回答的問題：值不值得做，取決於 forced_cut 案例裡「看到下一句就會變」的比例有多高。

---

## 4. Domain Fine-tune（領域微調 / LoRA）——v3 更新：現階段不可行

### 4.1 問題（原始構想）

`translation_corrections.json`、`streamer_profiles.json`、`translation_prompts.py` 累積的規則本質上是「把例外塞進 prompt」。這條路有邊際遞減：規則越多，prompt 越長，latency/cost 上升，且模型在長 prompt 裡可能不穩定遵守所有規則；某些東西（語氣、用詞習慣、特定吐槽的固定譯法）本身很難寫成規則，但可以從大量「修正前後對」資料裡用 fine-tune 學到。

`ARCHITECTURE_RECOMMENDATION_20260613.md` 把 LoRA 評為「中，偏成本/風格，延後」。v1/v2 這份提案曾認為：對一個長期只服務少數固定 streamer 的特化型翻譯器，「把固定譯名/語氣風格內化進模型權重」本身就是 translation correctness 的另一層上限。v2（Codex review）把這個構想收斂為「現階段僅做資料盤點」。

### 4.2 v3 修正：為什麼這條路現階段不可行（不只是資料量問題）

v2 仍隱含一個假設：「等資料量夠了，就可以對一個小型開源模型做 LoRA，補上目前 prompt-based 機制做不到的部分」。但這個假設本身有兩個更根本的問題，跟資料量無關：

1. **模型量級落差**：目前主力翻譯模型是 80B 量級的 hosted model（如 `nvidia/qwen3-next-80b`）。LoRA 只能用本機可跑的小模型（8B/16B 等級）。LoRA 能改善的是「特定詞彙/語氣的選擇」這種局部行為，但翻不出來的句子結構理解、長句處理、罕見詞彙——這些是模型底層能力的差距，LoRA 補不了。也就是說，即使 LoRA 把「這個 streamer 的固定譯名/語氣」學得很好，一個 8B/16B 模型的**整體**翻譯品質很可能仍明顯落後 80B + prompt-based 規則，局部增益被底層能力落差蓋掉，整體甚至可能是淨負面。
2. **硬體無法支撐對等模型**：唯一能避開第 1 點的方式是直接對 80B 量級模型做 LoRA/fine-tune——但 0.1 節列出的消費級顯卡（4GB/8GB VRAM）連**推論**80B 模型都做不到，更不用說訓練。這條路只能透過 hosted 服務商提供的 fine-tuning API（如果有的話），跟本機硬體規格是兩個完全不同層級的前提，目前也沒有資訊顯示這類 API 存在或可用。

結論：**LoRA 這條路目前不該排進任何時程**，包括「資料盤點」本身——盤點的價值取決於盤點完之後有沒有地方可以用這份資料，而 4.2 指出的兩個前提問題在硬體/服務層級沒有改變之前都不會消失，資料量再多也無法繞過。

### 4.3 仍有價值的副產品（轉移到第 5 節，不算 LoRA 的鋪路）

把 `label=a_translation_error` 整理成 `(source_text, context, corrected_target_text)` pairs 這個動作本身，不依賴 LoRA 才有意義——這份資料對 §5.2 的術語庫/glossary（靜態版本）跟 §5.4 的 pairwise regression set 都有直接用途。如果之後因為其他原因（例如人工整理術語庫）要做這件事，可以把它當副產品收集，但不應該以「為 LoRA 鋪路」為理由排優先序。

### 4.4 重新評估的條件

只有在以下任一前提改變時，才值得重新討論 LoRA：

- 主力模型供應商提供針對同等級（或可接受差距內）模型的 fine-tuning API，且本機只需負責資料準備而非訓練本身。
- 出現新的、量級更接近目前主力模型、但本機硬體可推論（甚至可訓練）的開源模型。
- 評估的目標改變——例如不是要取代主力翻譯模型，而是用小模型做某個更窄、對模型整體能力要求較低的子任務（例如術語標準化的後處理），此時模型量級落差的影響可能較小，但這已經是不同的設計，需要另外討論，不在本文件範圍內。

在以上任一條件成立之前，LoRA 從本文件的候選清單中移除。

---

## 5. 其他候選方向

v1 把這些全部歸為「天馬行空、未深入設計」一個籃子。Codex review 指出其中幾項其實成本低、現在就能評估，跟真正的天馬行空（prosody 等）不該放在一起。v2 按 Codex 的判斷重新分組，並補上 6 個 review 指出的遺漏方向。

### 5.1 近期可評估（低成本，建議優先於其他候選）

- **低價值片段的信心式跳過**：純語助詞、笑聲、填充詞目前可能仍被嘗試翻譯，產生無意義字幕，且可能污染 rolling memory（方向二）的 context。用信心/內容判斷主動跳過，成本低、可以較快驗證。Codex review 認為這項被 v1 低估了，應該排在比 RAG/rolling memory 更前面評估。
- **Active learning 標註優先序**：Phase 0 目前用分層抽樣；之後可改成主動學習——優先標註 resolver 分歧大或模型信心低的案例，讓有限標註人力對 gate 的改善幅度最大化。這項是標註流程優化，不直接影響翻譯品質上限，但影響「多快能驗證其他方向」，同樣被 v1 低估。
- **Confidence calibration**（新增，Codex review 指出的遺漏）：讓系統對自己的輸出（STT 文字、語者判斷、翻譯）給出信心分數，並驗證這個分數跟人工標註結果的對應程度。這是 read-only evidence 工作——先離線分析現有 quality_flags/quality_score 跟標註結果的對應程度，看現有信心分數是否已經可用、還是需要重新校準，不改變任何 live 行為，可直接排進 Phase 1 evidence schema。
- **Abstain / output policy**（新增，Codex review 指出的遺漏，依賴上一項）：在信心不足時，讓系統輸出 `unclear`/abstain 而不是硬給答案——這會改變 live 輸出，跟上面的 calibration 不是同一成熟度。這是第 1 節 host voiceprint 第一版範圍收斂、第 2 節 rolling memory 寫入門檻的共同前提，但**前提是 confidence calibration 先驗證過**：calibration 分析完成、分數可信之後，才設計 abstain 的門檻與行為，並透過 config flag 啟用，不是跟 calibration 同時上線。

### 5.2 Context Asset 候選

- **遊戲 Wiki/Fandom RAG**：遊戲術語（道具、技能、角色官方譯名）通常有現成的中文/英文 wiki。建立「遊戲層級」術語庫，依目前偵測到的遊戲（若有 OCR 或手動設定）做 retrieval，注入 translator context。Codex review 建議：應先從靜態 glossary/手動 game profile 開始，不必一開始就上 RAG——RAG 是這個資產的「自動化」版本，靜態版本可以先驗證術語庫本身有沒有用。
- **跨 streamer 遊戲術語共享資產**：同一款遊戲被不同 streamer 玩，術語是共享的，但目前 profile/correction 資產是 per-streamer。拆出一層遊戲層級術語庫可加速新 streamer 的冷啟動，跟「遊戲 Wiki RAG」是同一個資產的兩種來源（一個是外部 wiki，一個是內部累積），同樣建議先做靜態版本。
- **開播前內容預熱**：若使用者知道當天遊戲/活動，提前載入對應術語庫（結合上面的候選），避免系統「邊看邊學」的冷啟動期。
- **術語資產治理流程**（新增，Codex review 指出的遺漏）：`streamer_profiles.json`/`translation_corrections.json`/未來的遊戲術語庫會隨時間累積，需要一個流程決定「誰可以加、怎麼加、怎麼避免衝突或重複」。`ARCHITECTURE_RECOMMENDATION_20260613.md` 明確排除「自動 glossary mutation」，但沒有說人工治理流程要怎麼設計——這是純流程問題，不涉及程式碼，可以隨時討論。

### 5.3 Evidence / Uncertainty 候選

- **Span-level uncertainty + word timestamp alignment**（新增，Codex review 指出的遺漏）：目前 evidence 是 chunk 層級的單一信心分數；如果能拿到 word/phrase 層級的 timestamp 與信心（多數 STT engine 本身就能輸出），可以標出「這句話裡哪幾個字最不確定」，比整句信心分數更精細，對 host voiceprint（哪一段重疊）、draft/final（哪個片語可能要修正）都有幫助。屬於 evidence schema normalization（Phase 1）的延伸，不是獨立新工程。
- **Code-switching / 非韓文 clip 的 language routing**（新增，Codex review 指出的遺漏，且跟今天的標註規則直接相關）：今天在 `PHASE0_EVAL_INVENTORY_20260613.md` 已經訂了「host 沉默且 clip/其他語者內容非韓文時，仍是有效 source，照常判斷」的標註規則。Codex review 指出 v2 草稿把現況描述得太空白——`modules/stt.py` 目前是用固定的 `cfg.stt.language`（韓文）送給 Groq STT，並讀取回傳的 `resp.language` 做語言檢查（`stt_policy.should_reject_language`）：偵測為日文時直接 reject（防 hallucination），偵測為韓文以外、非日文的語言時只記一筆 warning log、文字仍照常往下走（`modules/stt.py:489,495,507`，`modules/stt_policy.py:90-102`）。較準確的說法是：**現況已有「韓文固定轉錄 + 日文 hallucination 防護」，但沒有「host 沉默時，針對有效非韓文 source 的多語言 routing」**——非韓文非日文音訊目前是「被動放行、用韓文模型硬轉錄」，而不是「完全未定義」。這個方向要設計的是：偵測到非韓文音訊（且屬於今天標註規則認定的有效 source）時，是否該動態切換 STT 語言、或調整 translator prompt，而不是繼續用韓文模型硬轉錄非韓文內容。

### 5.4 評估方法論候選

- **Pairwise human eval + regression set**（新增，Codex review 指出的遺漏）：目前的標註是單筆打分（`label` + `context_tags`）；pairwise（同一段給兩個版本的翻譯，請人選哪個更好）對於評估「改動是否真的變好」可能更敏感，尤其在差異細微時。可以跟 Phase 0 的 gate 一起設計：gate 本身可以是一組 regression set，pairwise 是比較新舊版本輸出的方法。
- **使用者修正回饋半自動收斂機制**（新增，Codex review 指出的遺漏）：如果使用者在 `labeling_review_server.py` 上修正了翻譯，這個修正除了變成標註資料外，能不能半自動（人工確認後）回寫進 `translation_corrections.json`？`ARCHITECTURE_RECOMMENDATION_20260613.md` 排除的是「自動 mutation」，但「使用者修正 → 人工確認 → 寫入」是有人在中間把關的半自動流程，跟自動 mutation 不是同一件事，值得分開討論。

### 5.5 排序靠後

- **觀眾聊天室作為 context signal**：聊天室常常即時澄清/反應主播說的話，對專有名詞/梗的 disambiguation 可能有價值，但聊天室本身雜訊多，且不是所有來源都能拿到聊天 log，可行性待確認。
- **Prosody/語氣感知翻譯**：音訊語調（驚訝、諷刺、興奮）可能影響翻譯用詞選擇。研究性質較重，ROI 不確定，維持排序最後。

---

## 6. 與既有 Roadmap 的排序關係（按成熟度分層）

v1 把所有方向都塞進「Phase 2+」一個籃子，看不出彼此的依賴關係。Codex review 指出這幾項其實成熟度差異很大：有些是資料/evidence 層的小改動，現在就能離線驗證；有些是要建立新的 context 資產；有些動到 UX/事件語義；有些是長期訓練計畫。v2 按這四層分組，並標註每項相對 `ARCHITECTURE_RECOMMENDATION_20260613.md` 是「補充說明」還是「修正」。

### 6a. Evidence / 資料品質層（成熟度最高，可與 Phase 1 平行推進）

這一層的共同特徵：核心是「在既有 pipeline 裡多算一個分數/多記一筆 log」的 read-only evidence 工作，風險最低，confidence calibration 是其他幾項的基礎。但**這一層裡有兩項（abstain/output policy、低價值片段跳過）一旦真正啟用會改變 live 輸出**，跟其他純 evidence 項目的成熟度不同，下表分別標註。

| 方向 | 對應現有表格項目 | 建議時機 | 前置條件 | 與舊文件關係 |
|---|---|---|---|---|
| Confidence calibration | 文件未列出 | Phase 1 evidence schema 之內，作為基礎欄位；read-only，可與 Phase 0/1 平行 | 離線分析現有 quality_flags/quality_score 與人工標註的對應程度 | 新方向，補既有 evidence schema 的缺口 |
| Abstain / output policy | 文件未列出 | **離線設計可平行；config flag 啟用需等 confidence calibration 驗證通過** | confidence calibration 完成且分數可信 | 新方向，依賴上一項 |
| Host Voiceprint（`host_presence_score`） | 文件未列出（source separation 的替代方案） | Phase 1 evidence schema 之後；離線可行性驗證可平行 | enrollment 樣本 + 離線可行性驗證；第一版範圍收斂為 evidence 欄位，非 routing | 全新方向，填補 `ARCHITECTURE_RECOMMENDATION_20260613.md` 完全沒提到的 source-selection signal 缺口 |
| 低價值片段信心式跳過 | 文件未列出 | **離線回測可與 Phase 0/1 平行；實際跳過行為（改變 live 輸出）需等 Phase 1 evidence schema 與 gate 通過** | 規則/信心判斷的離線回測 | 補充說明 |
| Span-level uncertainty + word timestamp alignment | 文件未列出 | Phase 1 evidence schema normalization 的延伸；read-only | 確認 STT engine 是否已輸出 word-level timestamp/confidence | 補充說明 |
| Active learning 標註優先序 | 文件未列出（標註流程） | Phase 0 pilot 之後可評估 | 需先有 resolver 分歧/信心分布的統計 | 標註流程優化，不涉及舊文件評價 |

### 6b. Context Asset 層（建立跨句/跨節目的上下文資產）

這一層的共同特徵：都是「額外的 context 注入 prompt」，且**依賴 6a 的 confidence calibration**——沒有信心門檻，寫入的內容可能是錯的，會被當作可信 context 持續放大錯誤。

| 方向 | 對應現有表格項目 | 建議時機 | 前置條件 | 與舊文件關係 |
|---|---|---|---|---|
| Rolling Memory | `structured rolling memory`（原評「低到中，延後」） | Phase 2/3 之後，**且需先有 6a 的 confidence calibration 驗證通過**（注入 prompt 是 live 行為，離線分析步驟本身可平行） | 離線分析「跨句 context 缺失」案例比例 + confidence 寫入門檻設計 | 補充說明（範圍比原文件描述更具體，但評價方向一致） |
| 遊戲 Wiki/Fandom RAG（先做靜態 glossary） | 文件未列出 | 先做靜態版本，RAG 是之後的自動化升級 | 手動建立至少一款遊戲的術語表，驗證有沒有用 | 補充說明 |
| 跨 streamer 遊戲術語共享資產 | 文件未列出 | 與上面同步評估，先做靜態版本 | 同上 | 補充說明 |
| 術語資產治理流程 | 文件未列出（流程問題） | 隨時可討論，不涉及程式碼 | 無 | 補既有「排除自動 mutation」決策的人工流程缺口 |
| 開播前內容預熱 | 文件未列出 | 依賴上面術語庫資產先存在 | 同上 | 補充說明 |

### 6c. UX / 事件語義層（動到字幕呈現與事件 schema）

| 方向 | 對應現有表格項目 | 建議時機 | 前置條件 | 與舊文件關係 |
|---|---|---|---|---|
| Draft/Final Subtitle | `draft/final subtitle`（原評「source correctness 低，延後」） | Phase 2/3 之後，且需先過離線分析 | Phase 0 `forced_cut` 案例的人工複核（3.3 步驟 1） | **概念修正**：問題重新框定為 segmentation-policy 的天花板，而非單純 UX 功能；但不代表排程提前，仍排在 Phase 2/3 之後 |
| Code-switching / 非韓文 clip 的 language routing | 文件未列出 | 與 Phase 0 pilot 的標註規則同步討論，runtime 設計待 Phase 1 之後 | 現況是「韓文固定轉錄 + 日文 hallucination 防護」，非韓文非日文音訊被動放行、仍用韓文模型轉錄（`modules/stt.py:489,495,507`）；需設計是否動態切換語言/prompt | 新方向，把今天的標註規則延伸到 runtime |

### 6d. 評估方法論層

| 方向 | 對應現有表格項目 | 建議時機 | 前置條件 | 與舊文件關係 |
|---|---|---|---|---|
| Pairwise human eval + regression set | 文件未列出（評估方法論） | 可與 Phase 0 gate 設計同步 | 無，是評估方法本身 | 新方向，補既有標註方法的缺口 |
| 使用者修正回饋半自動收斂機制 | 文件未列出（流程問題） | 待 `labeling_review_server.py` 穩定使用後評估 | 需先定義「人工確認」的把關步驟 | 補既有「排除自動 mutation」決策的半自動流程缺口 |

### 6e. 已移除：Domain Fine-tune（LoRA）

v1 將 LoRA 列為候選方向；v2（Codex review）收斂為「現階段僅做資料盤點」；**v3 進一步移除**——原因不是資料量，而是模型量級落差（本機只能跑 8B/16B，主力模型是 80B 量級，LoRA 補不了底層能力差距）與硬體無法支撐對等模型訓練（消費級 4-8GB VRAM 連 80B 推論都做不到）兩個前提問題，與資料量無關。詳見第 4 節 4.2/4.4。`ARCHITECTURE_RECOMMENDATION_20260613.md` 原評「中，偏成本/風格，延後」——v3 的結論是**修正**：不是「延後」，是在第 4.4 節列出的前提改變之前都不該排進候選清單。資料整理本身的價值轉移到 6b（術語資產）與 6d（regression set）。

### 6f. 排序靠後（未深入設計）

觀眾聊天室 context signal、Prosody/語氣感知翻譯——維持 v1 的判斷，ROI 不確定或依賴條件未滿足，排在所有上述層級之後。

---

這份提案不改變現有 Phase 0/1 的優先序，只是把「之後要排什麼、彼此依賴關係是什麼」的候選清單寫清楚，並各自附上一個低成本的離線驗證步驟，避免之後又落入「沒資料就猜」的狀況。**會碰 live path 的步驟（config flag 啟用、注入 prompt、改 routing/UX、實際 skip/abstain 行為）** 都排在 Phase 0 baseline 與 Phase 1 evidence schema 完成之後；但**離線/read-only 的驗證步驟（離線回測、shadow logging、計算新分數、人工複核既有標註）可以與 Phase 0/1 平行進行**，不必互相等待。

6a 整層裡，多數項目本質就是 read-only evidence，可以直接平行；但其中「abstain/output policy」與「低價值片段跳過」即使在 6a，也是「離線設計/回測可平行，實際啟用需等 Phase 1 evidence schema 與 gate 通過」——平行的是驗證步驟，不是最終行為本身。6b/6c/6d 各自第一步的離線分析同樣可平行，後續步驟（注入 prompt、改 UX、寫入資產）則排在 Phase 2/3 之後，各自表格已標註。

## 7. 未動事項

- 本文件純屬提案，未修改任何程式碼、設定、prompt 或既有標註/log 檔案。
- 不影響今天進行中的 Phase 0 pilot 標註（10 筆）。
