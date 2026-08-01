# 架構升級提案 (2026-06-12)

目的:收斂「現有架構之外」的升級選項。每項標明攻擊的瓶頸、改動範圍、驗證成本。
**規則:新想法只能加進本文件,不開工;開工前先做「最小驗證」欄位寫的事,拿到數據再決定。**
一次最多一個 in-progress。

狀態圖例:`[提案]` / `[驗證中]` / `[採用]` / `[否決]`(否決要寫原因)

---

## 決議更新 (2026-06-13 第二輪,Claude 裁決)

**`ARCHITECTURE_RECOMMENDATION_20260613.md`(Codex 第二版)採納為執行版計畫,取代下方第一輪決議的分期描述。** 理由:
- Incremental evidence promotion(升級既有 `source_utterance_ids`/`evidence_*`/`source_chunk_usages` 欄位)取代 big-bang rewrite,第一輪決議對 Phase 1 重構風險的顧慮被正確消解。
- 新增 speaker/source policy(從標註 tags `wrong_speaker_selected`/`audio_source_mismatch` 長出的洞察)——「翻錯人」是前兩份文件都遺漏的錯誤類別;先 policy+metadata、不上 diarization 的切法正確。
- multi-STT 與 resolver 先 shadow mode 的紀律優於先前所有版本。

**Gate 範圍修正(接受 Codex 的修正)**:gate 擋模型路徑(resolver 進 live、audio fallback 啟用),**不擋** Phase 1 的低風險 schema normalization——欄位補齊無論 gate 結果如何都需要。

**兩個執行注記**:
1. Phase 0 與 Phase 1 無依賴,可並行。
2. Phase 2 shadow 的 SenseVoice 需直播時本地 GPU 常駐,GPU 佔用/功耗納入 shadow report 一併量測。

第一輪決議全文保留如下(歷史脈絡)。D 軌道(LoRA/聊天室/VOD)與 F1 探針的定位不變:獨立軌道,不進第一批。

### Phase -1 決議(2026-06-13,littleM 拍板)

**Speaker policy = `host-only`。** 只翻 streamer 主聲道;clip/他人聲音視為 noise。評測集 ground truth 依此標。同日盤點發現 `logs/audio_dump/` 已被清空(696 個被引用 wav 全部遺失),gate 離線驗證延後至重新 dump 累積足量;既有 156 筆標註保留為文字面資產。詳見 `PHASE0_EVAL_INVENTORY_20260613.md`。

### 風險補遺(2026-06-13,執行紀律,各 Phase 開工前必讀)

方向判定正確(把 source 決策提前做對 = 提高品質上限的主路徑),但 RECOMMENDATION 是大方向文件,以下執行細節未補全,是 bug 與失敗的主要入口:

1. **「做到一半」是最大風險(solo 項目)**:每個 PR 結束時系統必須維持「今晚就能開台用」;所有新路徑掛 config flag 可即時關閉。遷移到 60% 的狀態比新舊兩個端點都糟。
2. **Schema normalization 風險被低估**:metadata 隱性消費者多(JSONL → sampler → labeling → analysis scripts),欄位語義漂移不炸測試但污染 labeling 地基。Phase 1 必須加 golden-file 重播測試:改動前後同段 log 的 event diff 只允許「新增欄位」。
3. **Shadow mode 觀察者效應**:SenseVoice 與直播/STT/遊戲搶 GPU,實驗本身會污染 baseline。Shadow 第一輪跑錄音重播,確認 GPU 餘裕後才掛真 live。
4. **Resolver 錯誤不對稱**:「錯誤救援」(流暢但錯)比「沒救到」(亂碼)毒——觀眾對後者有警覺。Resolver 進 live 門檻:rescue : false-correction ≥ 10:1,不是只看淨改善。
5. **延遲預算硬數字**:先量現行 end-to-end p50/p95,定紅線(建議 p95 ≤ 現值 +1s),每 Phase 驗收必附。三個「中等延遲」疊加 = 不能用。
6. **評測集循環偏差**:從舊 pipeline 失敗樣本建的評測集會讓 resolver 過擬合歷史 Whisper 怪癖;50-100 句只能偵測 ~15% 以上的效應。混入隨機抽樣、留 hold-out、標注評測解析度。
7. **Speaker policy 先於 Phase 0**:host-only / as-heard 決定「什麼叫翻對」,policy 未定就標註 → ground truth 混雜 → 重標。順序:先定 policy(產品決策)→ 再建評測集。RECOMMENDATION 把它放 Phase 1 metadata 是順序錯誤。
8. **每 Phase 需要 kill criteria**:只有成功條件沒有放棄條件 = 沉沒成本陷阱。開工前寫死(例:shadow 兩週 rescue ceiling < 15% → resolver/fallback 線全停,資源轉 D 軌道)。

---

## 決議 (2026-06-13 第一輪,Claude 裁決)

對照 `archive/roadmaps/ARCHITECTURE_PROPOSAL_CODEX_20260613.md`（Codex
歷史提案）後的最終決定:

**採用 Codex 的 evidence pipeline 作為目標架構與分期順序**(Phase 1 evidence packet → Phase 2 dual-STT resolver → Phase 3 low-confidence audio fallback → Phase 4 draft/final → Phase 5 QA loop),理由:
- 「傳證據而不是傳文字」一次解決可追溯/可量測/可回放,是比本文件離散選項更好的統一抽象。
- A1 降級為「低信心觸發的 audio fallback」且先做韓文轉寫驗證而非直翻,成本可控、可 debug、復用既有 translator/glossary/cache,優於本文件的全量平行軌道版本。

**但加兩條修正**:

1. **驗證閘門前置(gate before build)**:Phase 1 是跨整條 pipeline 的 schema 重構,不得在品質證據之前動工。先完成本文件 A2/A1 的兩個離線最小驗證:
   - 30–50 個已知 STT 錯誤案例離線跑 SenseVoice,量測「雙 STT 不一致且其中一個正確」比例(= dual-STT 救援上限);
   - 20 段 dump 音訊跑 audio model 韓文轉寫驗證,量測品質/延遲/單位成本。
   - **Gate:雙 STT 救援上限 < 30% 且 audio 驗證也救不回 → 整個 evidence 方向重新評估,Phase 1 不開工。**
2. **C2 階段一提前**:純旁路 QA logging(零風險、不依賴任何 Phase)立即可並行,產出的錯誤資料直接餵評測集與 Phase 2 驗收。Codex 把它放 Phase 5 太晚;其「不直接改 glossary、保留 human approval」的約束照採。

**共同前置不變**:固定評測集(50–100 句)仍是第一個交付物,label schema 採 Codex 的五軸拆分(stt_error / segmentation_error / translation_error / profile_term_error / timing_error)。

C1(結構化滾動記憶)維持輔助定位不進主線;Codex 對 C1 的駁回理由針對的是「擴大 context window」,與本文件的結構化狀態版本不同,但結論一致:等錯誤歸因數據顯示譯名/指代類錯誤佔比夠大再評估。

**執行順序**:評測集 → 兩個離線驗證(過 gate)→ C2 階段一並行 → Phase 1 起步,依 Codex 的 Milestone 與驗收指標執行。

**補充原則(2026-06-13):管線薄、資產厚。** 串流多模態 API 若在 6-12 個月內成熟,resolver/仲裁/fallback 這層會被單一 API 取代(bitter lesson)。因此:評測集、累積資料、glossary/corrections 是一級資產(換模型不作廢);resolver 邏輯視為可拋棄層,實作時不過度雕琢。離線驗證實驗順手加測 streaming multimodal session(提案 F),讓同一批數據回答「現在的架構值不值得建」和「下一個典範還有多遠」。

---

## 瓶頸地圖

字幕品質上限 = min(STT 正確率, 斷句合理性, 翻譯品質) − 延遲懲罰。
程式碼可靠性已收斂(見 CODE_REVIEW_20260611/12),以下提案各自攻擊一個瓶頸。

---

## A. 打 STT 錯誤

### [採用-改形] A1. 端到端語音直翻(平行軌道)→ 依決議降級為低信心 audio fallback(Codex Phase 3 形式),最小驗證保留為 gate 條件
- 做法:音訊 chunk 直接餵多模態模型(候選:Gemini Flash audio / GPT-4o audio / Qwen2-Audio)輸出 zh-TW,跳過「轉寫韓文 → 翻譯」兩段式。
- 攻擊:STT 聽錯這整類錯誤(聽的是原始語音,有語調與語境)。
- 改動範圍:新增一個 engine 類型 + 音訊路由;現有 pipeline 不動,平行跑。
- 風險:延遲未知、成本未知、人名/梗的可控性(沒有 glossary 注入點的中間表示)。
- **最小驗證:挑 20 段已 dump 的音訊(含已知 STT 錯誤案例),離線餵 Gemini Flash,人工比對現有 pipeline 輸出。半天工作量,直接知道品質差距與單位成本。**

### [採用] A2. 雙 STT N-best 仲裁 → 主線(Codex Phase 2 resolver 形式),最小驗證先行為 gate 條件
- 做法:同一 chunk 跑 Groq Whisper + 本地 SenseVoice(接口已存在),結果不一致時把兩個候選都進翻譯 prompt:「STT 可能聽錯,候選 A/B,擇合理者翻譯」。
- 攻擊:STT 聽錯,用 LLM 的語境消歧能力仲裁。
- 改動範圍:stt.py 增加並行轉寫 + 一致性比較;translator prompt 增加候選格式。中等。
- 風險:延遲取決於較慢的 STT;SenseVoice 需要本地 GPU 常駐。
- **最小驗證:從 labeling 資料挑 30 個 STT 錯誤案例,離線跑 SenseVoice 看「兩者不一致且其中一個對」的比例——這個比例就是此方案的理論收益上限。**

## B. 打斷句問題

### [採用-延後] B1. 串流式增量翻譯(draft → refine)→ Codex Phase 4,evidence pipeline 穩定後再做
- 做法:partial 句先出草稿字幕,句子完整後原地更新(同傳字幕模式)。subtitle 視窗從 append-only 改為可更新;translator 增加 partial 翻譯路徑(prefix 約束或直接重翻替換)。
- 攻擊:斷句錯誤的代價(從「翻錯定格」降為「草稿短暫不準」)+ 整體延遲(從 句長+翻譯 降為 接近翻譯)。
- 改動範圍:大——splitter 語義、translator 去重邏輯、subtitle UI、Tauri 前端同步受影響。
- 風險:字幕閃爍/跳動的閱讀體驗;dedup 與 cache 鍵需要 rethink。
- **最小驗證:不寫码,先拿 10 段 log 重播,模擬「incomplete 先出 + 完整後更新」的時間線,人工評估閱讀體驗是否可接受。**

## C. 打翻譯品質

### [擱置] C1. 結構化滾動記憶(取代 recent pairs context)→ 輔助定位,待錯誤歸因數據再評估
- 做法:維護一個狀態物件(當前話題、出場人名與既定譯名、術語、語氣),由便宜模型每 N 句更新,注入翻譯 prompt。手工 corrections/profile 表的自動成長版。
- 攻擊:譯名不一致、跨句指代錯誤、profile 維護成本。
- 改動範圍:中——TranslationMemory 旁加一個 state updater(可獨立 thread);prompt 組裝改造。
- 風險:狀態被污染會持續放大錯誤(需要 quality gate,現有 context gating 經驗可複用)。
- **最小驗證:離線拿一場直播的 log,用腳本批次生成滾動狀態,人工檢查 20 個狀態快照的正確率;再挑 10 句已知譯名錯誤案例,帶狀態重翻看是否改善。**

### [採用] C2. 延遲自我修正層(post-hoc QA)→ 階段一(純旁路 logging)立即並行;階段二依 Codex Phase 5 約束(human approval)
- 做法:live 字幕照常出;背景 QA worker 每隔幾秒回看「原文+已出字幕」,評分並記錄(階段一)→ 原地修正字幕(階段二)。錯誤案例自動累積進 corrections 候選。
- 攻擊:翻譯錯誤的長尾 + corrections 表的手工維護成本。
- 改動範圍:小(階段一純旁路,只寫 log)→ 中(階段二需要字幕更新能力,與 B1 共用基礎)。
- 風險:階段二的字幕跳動;QA 模型自身的誤判率。
- **最小驗證:階段一本身就是驗證——旁路跑一場,統計 QA 抓到的錯誤率與誤報率。零風險。**

---

## D. 資料資產方向(與 A/B/C 不衝突,獨立軌道)

### [提案] D1. LoRA 微調本地小模型(領域專翻)
- 做法:用累積的直播翻譯資料 + corrections/slang/profiles 微調 Qwen 7B 級模型做 ko→zh-TW 直播領域專翻,本地推理。
- 攻擊:API 延遲/成本/限流;corrections 表從 runtime 補丁變成訓練資料(一勞永逸);領域內品質可能超過通用 API 模型。
- 改動範圍:訓練 pipeline 全新;推理端只是多一個 engine(現有抽象直接容納)。
- 風險:訓練資料品質(需先過 label 清洗);本地 GPU 常駐;模型更新節奏自己扛。
- 前置依賴:評測集(沒有它無法證明微調有效)。資料每天在累積,啟動越晚沉沒越多。
- **最小驗證:從 labeling 資料抽 2k 清洗過的 pairs,LoRA 一輪,評測集上對比 nvidia 現役模型。一個週末工作量。**

### [提案] D2. 聊天室翻譯(產品面擴張,性價比極高)
- 做法:讀取直播聊天室(韓文彈幕)批次翻譯顯示。純文字、可批次、無 STT/斷句問題,translator 直接複用。
- 攻擊:觀看體驗(看懂彈幕常比聽懂主播更影響理解);順帶產生乾淨的 ko→zh 平行資料餵 D1。
- 風險:聊天室抓取的平台 API/ToS;彈幕梗密度高,反而是 glossary 的好試金石。
- **最小驗證:抓一場 VOD 的聊天 replay 離線翻 200 條,人工看品質。半天。**

### [提案] D3. VOD/clip 批次字幕(SRT 輸出)
- 做法:clip mode 延伸成完整 VOD 批次處理,輸出 SRT。非即時 → 可用最貴最好的模型 + 全文 context 重排。
- 攻擊:「品質上限」最便宜的突破口——不需要任何即時架構投資就能展示天花板品質;也是評測 pipeline 改動的理想沙盒。
- **最小驗證:現有 clip mode 跑一支 10 分鐘 VOD 出 SRT,看離可用差多少。**

## E. 典範轉移觀察(不開工,只量測)

### [提案] F1. 串流多模態長駐 session 探針
- 做法:Gemini Live 式 API 聽一段連續音訊直接出 zh-TW,人工評品質/延遲/成本/穩定性。不integration,純探針。
- 目的:量化「單一 session 取代整條 pipeline」還有多遠;每季重測一次,結果決定 evidence pipeline 的投資深度。
- **最小驗證:併入 A1 的 20 段音訊離線實驗,多跑一個 provider。增量成本接近零。**

---

## 依賴與順序建議

- A1 驗證成本最低、攻擊最大瓶頸 → **建議第一個做**。
- A1 若品質達標但延遲/成本不行 → A2 是退路(共用同一批驗證資料)。
- C2 階段一(純旁路)隨時可做,與其他項不衝突,且產出的錯誤資料反過來餵 A/B/C 所有提案的評測集。
- B1 與 C2 階段二共用「字幕可更新」基礎,先後實作可攤銷成本。
- C1 依賴錯誤歸因數據(先確認譯名/指代類錯誤佔比夠大才值得做)。

## 前置:評測集(所有提案的共同依賴)

從 labeling 資料建固定回歸集(原文音訊 + 人工標準譯文 + 錯誤類型標籤),任何提案的驗證都跑同一套,分數才可比。沒有這個,上面所有「最小驗證」都會退化成憑感覺。
→ 建議在動任何提案之前先建這個(估 50–100 句即可起步)。
