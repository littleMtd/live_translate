# 斗內 OCR 翻譯 — 最小原型 spec（給 Codex 實作）

- 日期：2026-06-30
- 角色：Claude=spec/發想；Codex=實作；User=決定並發 prompt。
- 狀態：**價值測試原型**，不是 production。目標：用最小代價，回答 User 的單一需求。
- 版控：本地保留，`*.md` 已被 git 排除，不可 push。
- 上游：`SCREEN_OCR_BRAINSTORM_20260630.md`、`SCREEN_OCR_GATE_SCOUT_SPEC_20260630.md`（閘1 已證 PaddleOCR 可讀，winner=paddle 門檻70）。

## 0. 這原型在測什麼（唯一目的）

User 的初衷：**直播主常常不會念出斗內、但會對它「有反應」，而 User 看不懂在反應什麼。** 這是音訊管線結構性補不了的缺口（資訊不在音軌上）。

原型成功的判準（主觀、live）：當主播對一則「沒念出來的斗內」有反應、User 原本看不懂時，面板是否已把那則斗內翻出來、讓反應變得合理。

**反例提醒**：수금타임（主播逐個念斗內）是這需求**不存在**的場景；測試請用**一般遊戲/聊天場**，不要用道謝場。

## 1. Scope（已收斂的設計決定）

- **輸入**：即時擷取**單一固定 ROI**（左側斗內區）。ROI 用 config 給 `(x,y,w,h)` 螢幕像素，可調；啟動時存一張 ROI 裁切 debug 圖讓 User 確認框對了。
- **OCR**：PaddleOCR（korean），confidence ≥ 70。只這顆引擎（閘1 winner）。
- **變化閘**：ROI 幀間做便宜 pixel diff/hash，沒變就不 OCR（省算）。
- **去重**：近時間窗 recency cache——同一則（normalized text 近似）在 N 秒內已處理過就不重譯（左側是會停留/老化的清單，避免一直重翻同一筆）。
- **翻譯**：呼叫 live_translate **既有 translator**（import，不 fork）。單行程、`cfg.active_streamer_profile` 設成當前主播（預設 `isegye_lilpa`，릴파）、`use_profile=True`。譯成 zh-TW，套既有 glossary/名稱正規化。
- **顯示**：**錨定面板**（anchored panel）——一個 always-on-top、borderless 的小視窗，放在 ROI 旁，顯示最近 N 則斗內的「原文 + 譯文」，新的往上疊。tkinter/PyQt 之類最輕的即可。
  - **就地覆蓋（Google Lens 式蓋在斗內上）是 User 的目標顯示形態，但本原型先不做**——左側是會捲動老化的清單，就地追蹤較重；先用錨定面板證明價值，確認有料再做就地覆蓋。
- **執行**：獨立 script，跟 User 平常看直播並行跑（不需開 live_translate 音訊管線）。

## 2. 非目標（這次明確不做）

- 不接 Tauri、不做就地座標追蹤、不建 service 邊界、不寫 runtime_events、不做 ROI 編輯 UI（數字 config 即可）。
- 不碰 MID（靜態 logo/賽程）與 RIGHT（聊天 firehose）兩區。
- 不改 `modules/` production code（**只 import**）。OCR/GUI 依賴裝在原型環境，**不寫進 production requirements.txt**。不 push。

## 3. 處理流程

```
loop（每 ~2-3 秒）:
  grab ROI 截圖
  if 幀無變化: continue
  boxes = paddle_ocr(roi), 過濾 conf<70
  for 每個 box 的 text:
     if text 近似命中 recency cache: continue        # 去重
     zh = translator.translate(text)                  # 共享後端
     panel.append(source=text, target=zh)             # 錨定面板
     recency_cache.add(text)
```

## 4. Codex 交付物

- `scripts/`（或 `scratch/`）下一支可獨立跑的原型 script + 簡短 README（怎麼設 ROI、怎麼跑）。
- 啟動時的 ROI debug 裁切圖，讓 User 確認框對。
- ROI 預設值給一個左側斗內區的合理矩形（可從 gate1 box 的左側 bbox 分佈估，User 再微調）。
- 不替 User 下「值得做」結論——原型是讓 User 自己在 live 判斷。

## 5. 給 User 的最小輸入

- 跑起來後微調 ROI 矩形對準斗內區（看 debug 圖）。
- 開著看一場**一般場**（非 수금타임），遇到「主播有反應、你本來沒看懂」時看面板有沒有補上。

---
*下一步 prompt 由 User 發；本檔為 spec，非執行指令。*
