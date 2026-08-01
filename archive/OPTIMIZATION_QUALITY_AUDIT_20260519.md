# 翻譯品質資料審計 — 2026-05-19

> 性質：runtime 資料審計（**本地 review 文件，永不 push、永不 stage**，與 `OPTIMIZATION_*.md` 同類）
> 資料源：`logs/runtime_events_20260519.jsonl`（`translations_20260519.txt` 同源，runtime_events 資訊更全）
> 重點 run：`20260519T123011Z-77304`；交叉佐證 run：`20260519T073837Z-57628` / `100906Z-56232` / `112530Z-82668`（4 場真實 run 共 363 success）
> 結論一句話：filtered/failed 不是問題，**success 裡的專有名詞翻譯**才是肉眼損害主體。

---

## 1. Run 摘要

| run_id | n | success/filtered/failed | engine | retry | template filt | quality_flags |
|---|--:|---|---|--:|--:|---|
| **20260519T123011Z-77304**（重點） | 73 | 68 / 5 / 0 | nvidia | 9 | 5（stt_template×2, stt_garbage×3） | empty_target×5, very_short×5, low_target_cjk×2, low_source_hangul×1 |
| 20260519T073837Z-57628 | 96 | 93 / 3 / 0 | nvidia | 3 | 2 | empty×3, very_short×3, low_target_cjk×2 |
| 20260519T100906Z-56232 | 96 | 90 / 1 / 5 | nvidia | 7 | 1 | empty×6, very_short×6, low_target_cjk×2, low_src_hangul×1 |
| 20260519T112530Z-82668 | 116 | 112 / 2 / 2 | nvidia | 14 | 2 | empty×4, very_short×4, low_target_cjk×3, low_src_hangul×1 |

filtered/failed 佔比低 → 品質問題**不在過濾層**，而在 success 但翻錯/翻不一致，集中於專有名詞。

---

## 2. 明顯問題樣本表（重點 run 77304）

| seq | source_text（節錄） | target_text（節錄） | problem_type | suspected_root_cause | suggested_fix | conf |
|--:|---|---|---|---|---|---|
| 38/40/51 | 마크팬 / 마크 영상 / 마크 서버 | Mark粉 / Mark影片 / Mark伺服器 | model mistranslation | `마크`=마인크래프트(Minecraft)，模型當成人名 Mark | glossary：마크→Minecraft（live 語境） | High |
| 14/16/23 | 단위님 / 단인님 | 單位姐 | 專有名詞 | 人名「단위님」被當普通名詞「單位」+STT 變體 단인 | glossary + source 正規化 단인→단위 | High |
| 68/72/28/30 | 챗나룡 / 챗나룸 / 챗마 / 찬나 | -chan龍 / ChanRoom / chatma / -chan | 專有名詞 + STT split | 同一伺服器名「챗나」家族被 STT 拆 5+ 形、零 dictionary | source 正規化→canonical + glossary | High |
| 60 | 봉준 김봉준 / 고세구 / 주루룰 / 일파 | 봉준、金Bongjun / 高世久 / 朱魯魯爾 / 一帕 | 專有名詞（VTuber） | Isegye Idol／實存主播名，音譯亂碼、無既有譯名 | glossary（既有 zh-TW 圈譯名） | High |
| 61/62 | 성태 님 / 성태님 | 성태老師 / Sungtae哥 | 專有名詞 | 人名，渲染不一致（時中時羅馬時保留） | glossary 固定一式 | Med |
| 10/11 | 나락할까 봐 / 나락하는 달 | 掉進地獄 / 墜入地獄 | slang/idiom | 「나락」=直播圈「翻車/塌房」，被直譯 | prompt idiom rule 或 glossary | High |
| 7 | 그래도 중박은 친다 | 但還是會中獎啊 | slang/idiom | 「중박 치다」=中等成績，非「中獎」 | prompt idiom rule | High |
| 32 | 썹종 시간이라고? | 下播時間？ | slang/idiom | 「섭종/썹종」=伺服器關閉，非「下播」 | glossary/idiom | Med |
| 20/21/22 | 시청자 여러분의 응원과 사랑은 저에게 아주 큰 힘이 됩니다 | 觀眾們的應援與愛…巨大的力量 | template leak | 新罐頭變體，不在 STT_TEMPLATE_CONDITIONAL_PHRASES | 加入 conditional phrases（Task #8 機制） | High |
| 22 | …큰 힘이 됩니다. 아니님을 던져. 인정? 10인분이요? | …巨大的力量。扔個安寧吧？認可？十人份？ | template leak + 混真內容 | 同上，且尾接真內容→Task #9 sanitizer 未涵蓋此 phrase | conditional phrase + sanitizer 清單同步 | High |
| 25/44 | 글씨는? 글씨는?（×2） / 글씨는?（×1） | 字體是？字體是？ / 字體呢？ | STT garbage leak | is_stt_garbage 重複偵測門檻只擋 ≥3 次（×4 被擋，×2/×1 漏） | 重複門檻微調（謹慎，易誤殺短句） | Med |
| 12 | 나있자, 이이자, 이이자, 나있자 | 來吧，這這，這這，來吧 | STT garbage（純亂音節） | STT 誤聽出非詞彙音節串，過了所有過濾 | 難規則化，低優先（或音節熵啟發式） | Low |
| 3 | 트레일러 나옵니다… 타로… 저번에 타로 봤던 거 한 번… | …塔羅……上次看的那一次…… | incomplete/splitting | 尾端碎句無句末標點，Task #10 B1（純標點）不處理 | 留待 B1 後續/morpheme 切分 | Low |

---

## 3. 問題分類統計（重點 run 73 筆 + 跨 run 佐證）

| 分類 | 估計影響 | 說明 |
|---|--:|---|
| **專有名詞 / VTuber dictionary** | **最大宗，~15–20%** | 챗나家族（跨4run ~21次）、마크=Minecraft(4)、단위님(3)、하데스(10,尚一致)、대표님(11)、성태(3)、Isegye名、렌트님 |
| slang / livestream idiom | ~6–10% | 나락、중박、섭종/썹주、개극포、리모컨 파이팅 |
| STT garbage / template leak | 過濾層擋掉多數；漏網 ~4–6 筆 | 新 template 變體（시청자…응원과 사랑）、글씨는?×2、純亂音節 |
| source normalization needed | ~8–12%（與專有名詞重疊） | 챗나룡/룸/찬나/츤나、단인↔단위、SUBJU/섭쥬/썹주 |
| sentence splitting / incomplete | ~10–15%（多數可讀，少數斷裂） | 無句末標點碎句，B1 純標點未涵蓋 |
| post-processing needed | 低 | 主要靠 glossary，非後處理 |
| model mistranslation | ~3–5% | 마크、중박 這類「詞認得但語境錯」 |
| acceptable / no action | 多數（~60–70%） | 一般對話翻譯品質可接受 |

---

## 4. 高頻專有名詞候選（已 web 查證 — 2026-05-19）

**關鍵發現**：config 的 `streamer_profile = "hades_chxxnnx"` → 本場主播是 **챈나 (Chxxnnx)**，韓國 5 人 VTuber 團 **HADES（하데스）** 成員（김봉준企劃、무엔터테인먼트，2025 出道）。審計裡的 `챗나 / 챗나룡 / 챗나룸 / 찬나 / 츤나 / 챗마` 全是 STT 把 **챈나** 及其伺服器名聽歪的變體。三個 config profile 全對上：`hades_chxxnnx`=챈나、`stellive_hina`=Shirayuki Hina、`isegye_lilpa`=Lilpa。

### 4A. 已查證、可直接入 glossary（高信心）

| canonical(韓) | 目標形 | 類別 | STT 變體 | profile | 依據 |
|---|---|---|---|---|---|
| 챈나 | **Chxxnnx** | 人名(本場主播) | 챗나, 챗나룡, 챗나룸, 챗나방, 챗나노, 찬나, 츤나, 챗마 | hades_chxxnnx | HADES 官方成員，= config profile |
| 솜주먹 | Sompunch | 人名(HADES) | - | hades | HADES 成員 |
| 연초록 | Yeon Chorok | 人名(HADES) | - | hades | HADES 成員 |
| 띵귤 | Singgyul | 人名(HADES) | - | hades | HADES 成員 |
| 키마 | Kyma | 人名(HADES) | - | hades | HADES 成員 |
| 봉준 / 김봉준 | Kim Bongjun | 人名(HADES 企劃者) | - | hades | SOOP 主播、HADES 製作人 |
| 성태 | KimSungtae(킴성태) | 人名 | - | 全域 | 前 Sudden Attack 職業選手、SOOP 主播 |
| 고세구 | Gosegu | 人名(Isegye) | - | isegye | 官方羅馬名 |
| 주르르 | Jururu | 人名(Isegye) | 주루룰 | isegye | 官方羅馬名 |
| 릴파 | Lilpa | 人名(Isegye) | 일파 | isegye | 官方羅馬名 |
| 시라유키 히나 | Shirayuki Hina | 人名(StelLive) | - | stellive_hina | 官方羅馬名 |
| 마크 | Minecraft | 遊戲 | - | 全域（語境守門：後接 서버/영상/팬/맵 才換） | 韓圈 마크=마인크래프트 標準縮寫（自有知識，非 web） |
| 하데스 | HADES | 團體/伺服器 | - | hades | 維持 `HADES`，現況已較一致 ✓ |

### 4B. 決策結果（user 已拍板 2026-05-19）

| # | 項目 | 決策 | 處置 |
|--:|---|---|---|
| 1 | 단위님 / 단인님 | user 不確定身分（某實況主或 챈나朋友，不清楚），**非重點** | **DEFER**：不進 glossary，維持現狀翻譯。日後查到再增量補 |
| 2 | 렌트님 | 同上，無解、非重點 | **DEFER**：同上 |
| 3 | 房 style | **全官方羅馬拼音** | 已採（§4A 目標形即羅馬） |
| 4 | 챈나 的 룡/룸/방/노 | 챈나最近開 Minecraft server，**那些片段都是伺服器相關**，STT 聽歪 | canonical 仍 `챈나→Chxxnnx`（人名,profile）；伺服器詞硬收斂屬 §6/audit #4，本 task 只用 stt_terms 偏置 |
| 5 | glossary 綁定 | **人名綁 profile、遊戲/通用詞全域** | 已採（§14 Task #13 設計） |
| 6a | 섭쥬 / 썹주 / SUBJU | = `섭주`（서버 주인 / Server Owner 縮寫），**通用角色詞、非伺服器名** | 入 **全域** glossary → 目標 `服主`（zh-TW 直播慣用；Server Owner 為可接受 alt） |
| 6b | 대표님 | **通用「代表/老闆」，非特定人** | **不進 glossary**；維持普通詞翻譯（現譯「老闆」已可接受） |
| 6c | 지효 / 민지 / 지상 | user 未指明、非重點 | **DEFER**：增量補，不阻擋 |

**背景補充（user 提供）**：本批 run 全是 챈나 籌備/運營一台 Minecraft server 的情境 —— 故 §5 的 섭종/연장/룰렛/운영자 API 等多為 Minecraft 伺服器運營術語（context，不擴大 Task #13 範圍）。

§4A 據此新增一筆全域詞：

| canonical(韓) | 目標形 | 類別 | 變體 | profile | 依據 |
|---|---|---|---|---|---|
| 섭주 | **服主**（alt: Server Owner） | 通用角色詞 | 섭쥬, 썹주, SUBJU, 섭쥬방→服主房 | 全域 | user 確認 = 서버 주인 縮寫 |

### 4C. 訊息來源

- [Isegye Idol — Wikipedia](https://en.wikipedia.org/wiki/Isegye_Idol)：고세구=Gosegu / 주르르=Jururu / 릴파=Lilpa（六人團官方羅馬名）
- [StelLive — Kpopping](https://kpopping.com/profiles/group/StelLive)：시라유키 히나=Shirayuki Hina（成員羅馬名）
- [HADES — Kpopping](https://kpopping.com/profiles/group/HADES)：HADES 5 人團、Chxxnnx 等羅馬名
- [하데스(버츄얼 그룹) — 나무위키](https://namu.wiki/w/%ED%95%98%EB%8D%B0%EC%8A%A4(%EB%B2%84%EC%B8%84%EC%96%BC%20%EA%B7%B8%EB%A3%B9))：HADES 由 김봉준企劃、무엔터테인먼트、最終 5 人(솜주먹/연초록/챈나/띵귤/키마)
- [김봉준 — 나무위키](https://namu.wiki/w/%EA%B9%80%EB%B4%89%EC%A4%80)：김봉준 SOOP 主播、Minecraft 活動、HADES 企劃者
- [킴성태 — 나무위키](https://namu.wiki/w/%ED%82%B4%EC%84%B1%ED%83%9C)：성태=킴성태(김성태)，前 Sudden Attack 職業選手、SOOP 主播

> namuwiki 對 WebFetch 回 403，HADES/김봉준/킴성태 細節取自 WebSearch 摘要，非逐頁全文；단위님/렌트님等本場專屬暱稱通搜不可得，列 4B 待你決策。

---

## 5. Slang / idiom 候選

| Korean | 現況問題 | 建議 zh-TW 直播風 | 歸類 |
|---|---|---|---|
| 나락 (가다/하다) | 直譯「掉進地獄」 | 翻車 / 塌房 / 糊掉 | prompt idiom rule（語境依賴，硬替換有風險） |
| 중박(을) 치다 | 誤譯「中獎」 | 小爆 / 中等成績 / 還算紅 | prompt idiom rule |
| 섭종 / 썹종 | 誤譯「下播」 | 伺服器關閉 / 收伺服器 | glossary |
| 썹주 / 섭주 | 「攝主」音譯 | 本週伺服器 / 該伺服器 | glossary（與 SUBJU 統一） |
| 개극포 | 「開極堡」音譯亂碼 | 需語境確認，疑為遊戲術語 ⚠ | source correction 候選 |
| 리모컨 파이팅 | 「遙控器加油」字面 | 疑為梗/暱稱 ⚠ | 觀察，暫不動 |
| 빡세다 | 「壓迫感」 | 硬核 / 累人 | prompt（低優先，現譯尚可） |

---

## 6. STT source correction 候選（可規則化的誤聽）

| STT 誤聽形 | 正規化目標 | 依據 | 風險 |
|---|---|---|---|
| 챗나룡 / 챗나룸 / 챗나노 / 찬나 / 츤나 / 챗마 | `챗나`（canonical 伺服器名） | 同場高頻、同指涉、STT 碎裂明確 | 中（츤나/찬나 可能偶為他詞，需詞邊界守門） |
| 단인님 | `단위님` | 單場交替出現、同指人 | 低 |
| SUBJU / 섭쥬 / 썹주 | 統一伺服器 token | 同指涉 | 低 |
| 마크 (서버/영상/팬 語境) | 마인크래프트 / Minecraft | 遊戲語境穩定 | 中（裸「마크」偶可能真人名，需語境：後接 서버/영상/팬/맵 時才換） |

> 純亂音節（`나있자/이이자`、`글씨는?` 連發）不適合正規化，屬過濾層問題（見 §3）。

---

## 7. 優先修復建議（ROI 排序 Top 5）

每項各自走 plan → Codex cross-review → sign-off → 實作 → post-implementation review 流程。

### 1. VTuber / 伺服器 專有名詞 glossary（含 마크=Minecraft）
- **why first**：最高頻、肉眼損害最大（每場數十次）、純加法 glossary、零翻譯邏輯風險、ROI 最高。
- **files**：profile/glossary 設定（`config.py` slang/profile 或 glossary 來源）+ glossary 查表測試。
- **risk**：低（加法；`마크→Minecraft` 須語境守門避免真人名誤換）。
- **tests**：glossary 命中 `마크 서버/영상/팬`→Minecraft、`단위님` 不被拆「單位」、Isegye 名固定式；既有翻譯回歸。

### 2. 新 STT template 變體納入 Task #8 conditional phrases
- **why**：`시청자 여러분의 응원과 사랑은 저에게 (아주) 큰 힘이 됩니다` 純罐頭、現漏網並污染字幕（seq 20–22）；Task #8/#9 機制成熟、已驗證、低風險。
- **files**：`utils/text_heuristics.py`（CONDITIONAL_PHRASES + STRIP 同步）、`tests/test_translation_policy.py`。
- **risk**：低（同 #8/#9 模式；須確認 sanitizer strip 清單同步，避免 seq22 混真內容仍漏）。
- **tests**：孤立變體→stt_template_garbage；混真內容→strip 後只翻真內容；既有正例不改名。

### 3. Slang idiom prompt rule（나락 / 중박 / 섭종 / 썹주）
- **why**：高可見度誤譯（意思全錯），數量穩定；語境依賴，glossary 硬替換危險→走 prompt idiom hint。
- **files**：translation prompt / system prompt idiom 段（不碰 policy/engine）。
- **risk**：中（prompt 改動影響全域翻譯，需 A/B 與回歸觀察）。
- **tests**：難單元化，靠 runtime 前後對比 + 既有 prompt 測試不破。

### 4. STT source 正規化：챗나家族 + 단인→단위
- **why**：解 source split 根因，讓 glossary(#1) 真正生效（不正規化則 glossary 要列舉所有 STT 變體，不可持續）。
- **files**：`modules/translation_policy.py` 或 source 前處理層 + 測試。
- **risk**：中（츤나/찬나 詞邊界誤換風險，需守門；**應排在 #1 之後**，用 #1 的 canonical 清單）。
- **tests**：챗나룡/룸/찬나→canonical；非指涉語境不誤換；단인→단위。

### 5. STT 重複門檻微調（글씨는? ×2 漏網）
- **why**：低成本、明確漏網；但 ROI 低於前四且易誤殺正常短重複（如「진짜 진짜」），須謹慎。
- **files**：`modules/translation_policy.py`（is_stt_garbage 重複比率/長度條件）+ policy 測試。
- **risk**：中（過嚴會誤殺正常口語重複）；建議**最後做或暫緩**，先觀察前四項效果。
- **tests**：글씨는?×2 擋下、正常「진짜 진짜 좋아」不誤殺、既有 stt_garbage 正例不變。

---

> **狀態**：Claude 審計（2026-05-19）。本文件為本地分析產出，**未實作、未 push、不可 push**。下一步由 user 決定先取哪項進 plan 流程。
