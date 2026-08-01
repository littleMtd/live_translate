# Review 請求：ARCHITECTURE_PROPOSAL_QUALITY_CEILING_20260614.md

## 背景

今天的主線是 Phase 0 host-primary pilot 標註準備（10 筆 pilot sample 已選定，候選檔已同步）與根目錄文件整理（19 份歷史任務文件搬到 `archive/`，未刪除）。

過程中使用者額外提出一個獨立於目前 roadmap 的問題：「如果不管現有架構限制，純粹討論翻譯品質的理論上限還能往哪裡推」。這部分的產出是 `ARCHITECTURE_PROPOSAL_QUALITY_CEILING_20260614.md`，列了 5 個方向：

1. Host voiceprint（單軌語者辨識，作為 host-primary 的 runtime signal）
2. Rolling memory（直播層級的動態上下文）
3. Latency-adaptive draft/final subtitle
4. Domain fine-tune（LoRA）
5. 其他候選（遊戲 wiki RAG、聊天室 context、低價值片段跳過、開播前預熱、跨 streamer 術語共享、prosody 感知、active learning 標註排序——皆未深入設計）

這份文件是腦力激盪產出，**還沒有經過交叉檢視**，文件裡的排序、風險評估、可行性判斷都只是一個人的初步判斷，不代表已經有共識。

## 請求獨立評估，不要預設文件的結論是對的

1. **逐項可行性與影響**：5 個方向各自的可行性、對品質上限的預期影響、風險評估，你是否同意？有沒有哪個方向被文件高估或低估？有沒有哪個方向你認為前提就有問題？

2. **文件引用的一個統計數字**：現有 156 筆標註中，label 分布為 `b_stt_error` 79、`ok` 39、`a_translation_error` 23、`both` 7、`unclear` 8。文件用「STT 沒被標為錯誤的 70 筆（ok+a_translation_error+unclear）裡，a_translation_error 占 23 筆（~33%）」來支撐「STT 聽對不代表翻譯對」的論點。這個算法、這個比例、以及樣本本身的抽樣偏差，你覺得有沒有問題？是否能支撐文件的結論？

3. **第 6 節的排序**：文件把全部 5 個方向都排在 Phase 0/1 baseline 之後，且都先要求一個離線驗證步驟。這個排序合理嗎？有沒有方向其實值得提前、或者根本不該排進來？

4. **遺漏與框架本身**：有沒有遺漏的方向？文件用的公式 `live quality ceiling = source correctness × segmentation correctness × translation correctness − latency/cost/debug penalties` 本身，你覺得有沒有問題（例如這幾個因子是否真的獨立、是否有更好的拆法）？

5. **跟 `ARCHITECTURE_RECOMMENDATION_20260613.md` 的關係**：這份新文件對 `structured rolling memory`、`draft/final subtitle`、`LoRA` 三項的評價跟原文件（皆評為「延後」）不完全一致，host voiceprint 則是原文件沒提過的新方向。你認為這算是對原文件評估的修正，還是只是補充說明？

## 範圍

純粹是 review/討論，不涉及任何程式碼或設定變更，也不影響今天進行中的 Phase 0 pilot 標註，排在你方便的時候做即可。
