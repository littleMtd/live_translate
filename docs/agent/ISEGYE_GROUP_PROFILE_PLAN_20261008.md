# 이세계아이돌：把 isegye_lilpa 改為全團共用設定檔（任務計畫）

- 起草：Claude Code，2026-10-08（依 `CROSS_REVIEW_WORKFLOW.md`，本文件為提案，
  待 Codex 交叉審查後實作）
- 任務類型：翻譯／設定檔資料行為變更，跨 `translation_profiles`、`streamer_profiles`、
  `translation_corrections`、`fan_terms` 等多個資料擁有者
- 不呼叫付費 API；不提交

## 1. 使用者決定（evidence: user decision, 2026-10-08）

| ID | 決定 |
|---|---|
| U1 | 把目前偏重 릴파 的 `isegye_lilpa` 改成 이세계아이돌 全團共用，並補充更多團體資訊 |
| U2 | 設定檔 ID 維持 `isegye_lilpa`，只改顯示標籤與內容；改名另案處理 |
| U3 | 成員暱稱（세구땅、르르땅、버거땅／부가땅、이네땅、챠니、릴파넴）在譯文中**保留韓文原樣**，不轉成正式譯名（使用者自己看得懂韓文） |
| U4 | 成員正式名字的譯法不變：고세구→Gosegu、주르르→Jururu、릴파→Lilpa；아이네、징버거、비챤 保留韓文 |

## 2. 現況觀察

| Claim | 內容 | 證據類型與位置 |
|---|---|---|
| C1 | 語音辨識與實體層已經是全團：`streamer_profiles.json` 的 `isegye_lilpa` 已引用 6 位成員與 `isegye_group`，`stt_sequence` 也含 6 人與團名 | code/data：`data/streamer_profiles.json`（profile `isegye_lilpa`） |
| C2 | 偏重 릴파 的部分：標籤 `"Isegye Idol Lilpa"`；`translation_profiles.json` 的 standard 與 qwen 文字（例 62–83 多為 릴파 的 박쥐단、에블바리 세이、나비다、Valorant、Minecraft）；`fan_terms.json` 只有 박쥐단 與 이파리 | data：`data/translation_profiles.json`、`data/fan_terms.json` |
| C3 | 「늘파」不是暱稱，而是 STT 誤聽的校正：`translation_corrections.json` → `source_norm.profiles.isegye_lilpa["늘파"] = "릴파"`；나무위키 릴파 的「별명」段落沒有 늘파 | data：`data/translation_corrections.json:106`；audit：나무위키 미러（2026-08-09 版）릴파 문서 4.5 별명 |
| C4 | 既有測試要求兩種版本的**文字**都含 `Gosegu`、`Jururu`、`Lilpa`、`히게단`、`Official髭男dism`；另要求 standard 版的 output terms ⊇ {Gosegu, Jururu, Lilpa, Official髭男dism} 且不含 `고세구`、`Everybody say`（`히게단` 本身不要求屬於 output terms） | code：`tests/test_translation_prompts.py:84-90, 152-159` |
| C5 | `get_translation_profile_output_terms` 只讀第一個空行之前的 `- X -> Y` 行，並略過右側以 `keep ` 開頭的行；`get_translation_profile_preserve_terms` **掃描整份 profile**，接受自我對應與 `keep` 指令，但 `keep the official title` 類的行會濾掉單字英文詞（例如 `KIDDING`、`OVER`、`White` 不會進入 preserve terms）。兩個 parser 都只讀 standard 版 | code：`modules/translation_prompts.py:38-140`；Codex round 1 記憶體模擬 |
| C6 | 譯文中的韓文會被當成文字殘留，除非屬於「出版核准詞」：profile preserve terms 中**含韓文的詞不會被核准**；韓文核准只來自本句的 obligation、`source_proven_quality_terms(source)`、`_source_honorific_name_terms`、`_source_activated_name_canonicals`、`request_protection.approved_hangul_terms` | code：`modules/translator.py:1040-1085, 1199-1206` |
| C7 | 이파리 這種保留韓文的詞，可藉 `name_rendering_rules` 中 `scope: isegye_lilpa`、`canonical: 이파리`、`source_aliases: [이파리, 이파리들]` 的規則，在**原文命中時**取得句內核准（另有 C6 所列其他核准來源） | data：`data/translation_corrections.json` → `name_rendering_rules`；code：`modules/translator.py:696` |
| C8 | `korean_name_suffixes` 不含 `땅`，所以 `세구땅` 不會被切成「세구＋接尾詞」 | data：`translation_corrections.json` → `korean_name_suffixes` |
| C9 | `fan_terms.json` 在執行期沒有消費者，只有離線工具 `scripts/llm_quality_reviewer.py`、`scripts/suggest_corrections.py` 使用 | code：repo 搜尋（`modules/` 只剩一個已刪模組的 `.pyc`） |
| C10 | 標準版與 qwen 版必須有相同的 profile ID 集合 | code：`modules/translation_prompts.py:28-30` |

## 3. 提案

### P1　翻譯設定檔文字（standard 與 qwen）

把 `translation_profiles.json` 中 `isegye_lilpa` 的兩份文字改成全團版本。結構沿用現有格式：
第一段是 `[Fixed proper-noun glossary]`（C5 解析範圍），空行之後才是背景與例句。
提案的 standard 版草稿見附錄 A；qwen 版依附錄 A 精簡，保留 glossary 與 4–6 句例句。

設計重點：
- **暱稱保留韓文（U3）**：用一行 `keep` 指令列出 `세구땅 / 르르땅 / 이네땅 / 버거땅 / 부가땅 / 챠니 / 릴파넴`，
  讓它們進入 preserve terms（C5），並在括號註明各自指誰，讓模型懂上下文。
- **刻意不放進 glossary 的詞**：`라니`（비챤 粉絲名）同時是極常見的語尾「-라니」（例：이게 진짜라니）；
  `똥강아지` 也是一般的親暱稱呼；單字 `챤` 是 `비챤` 的子字串。這三個只在背景說明中出現，
  並註明「只有明確指粉絲／成員時」才保留。
- **「이세돌」歧義**：也可能指圍棋棋士 李世乭（例：SOOPER MATCH「이세돌 vs 이세돌」），
  背景說明要求依上下文判斷；圍棋棋士譯為「李世乭」。
- **時效性**：不寫「우왁굳 是製作人」這類現況描述（2026-09-13 起 우왁굳 已卸下製作人，
  evidence: audit，使用者貼上的 나무위키 本站 2026-10-07 版）。
- 保留現有表現好的例句（이파리 問候、全員到齊、아이네 158）；릴파 專屬例句縮減到 2 句，
  其餘成員各 2 句。

### P2　暱稱在出版檢查中的核准

依 C6／C7，保留韓文的暱稱若沒有核准來源，可能被判為文字殘留。提案在
`translation_corrections.json` → `name_rendering_rules` 為每個暱稱加一條自我對應規則，
格式比照 이파리：

```json
{"scope": "isegye_lilpa", "source_aliases": ["세구땅"], "wrong_forms": [], "canonical": "세구땅"}
```

對 `세구땅`、`르르땅`、`이네땅`、`버거땅`、`부가땅`、`챠니`、`릴파넴` 各一條；
`wrong_forms` 先留空，有實際錯誤樣本再補。

**選定 P2-a**（round 2 修訂）。原列的替代方案 P2-b（依賴 `source_proven_quality_terms`）
已被 Codex round 1 以程式否證：該函式只核准指定的拉丁縮寫，`source_proven_quality_terms("세구땅 왔어")`
回傳空集合（`utils/runtime_events.py:283`）。

**涵蓋範圍與邊界**：
- 規則只在原文出現該暱稱時啟用（句內、來源證明）。原文沒有暱稱、譯文卻出現暱稱時，不會被核准。
- `고세구땅`（正式名＋땅 連用）**不在涵蓋範圍**：它既不命中 `고세구` 也不命中 `세구땅`
  （Codex round 1 記憶體比對，`modules/translation_corrections.py:241`）。預期行為維持現狀，
  以測試記錄；若之後在實際字幕中常見，再另案加入。
- **延伸（取決於 A7）**：保留韓文的粉絲名（`둘기`、`박쥐단`、`주폭도`、`세균단`）與成員正式名
  （`아이네`、`징버거`、`비챤`）目前也可能沒有核准來源：以
  `_source_activated_name_canonicals("아이네 언니랑 징버거랑 비챤이 왔어. 박쥐단 둘기 주폭도 세균단 이파리들 안녕", profile_id="isegye_lilpa")`
  實測只回傳 `{"이파리"}`，`_source_honorific_name_terms` 回傳空集合（evidence: runtime，Claude round 2）。
  若 A7 證實其他來源（例如 `request_protection.approved_hangul_terms`）也不核准它們，
  則比照 이파리 為這 7 個詞加 self-canonical 規則；成員正式名的規則必須確認不會和既有實體規則衝突。

### P3　語音辨識用語與顯示標籤

- `streamer_profiles.json`：
  - `label`：`"Isegye Idol Lilpa"` → `"Isegye Idol"`。
  - `stt_sequence` 追加純字串（不綁實體，避免被正規化成正式名）：
    `세구땅`、`르르땅`、`버거땅`、`부가땅`、`이네땅`、`챠니`、`이파리`、`맆스틱`、`왁물원`、
    `KIDDING`、`SYZYGY`、`Misty Rainbow`、`Stargazers`、`Be My Light`、`Smile For You`。
  - 數量與總長度是否受 STT prompt 上限影響，見 A3。
- `entity_registry.json`：**不**為暱稱新增 `translation_source` scope 的 alias。
  加了的話，`name_rendering_rules` 的實體規則會把暱稱改寫成正式譯名（Gosegu 等），違反 U3。

### P4　離線 QA 資料

`fan_terms.json`（C9，僅離線工具使用）：補上 6 位成員的個人粉絲名，並修正既有兩筆的
`streamer`／`fandom_of` 欄位。資料見附錄 B。

### P5　文件

`config.py:262` 的選項註解與 `donation_ocr/README.md:32` 若描述成「릴파」，改為「이세계아이돌（全團）」；
ID 不變。

## 4. 非目標

- 不改設定檔 ID `isegye_lilpa`，不改任何歷史評估資料（replay snapshot、semantic_quality、
  manual_quality、t25 manifests、benchmark）。
- 不改成員正式名字的譯法（U4），不改 `늘파 → 릴파` 校正（C3）。
- 不改翻譯管線程式碼；若 A1／A2 顯示需要改程式碼，停下來回報，不在本任務內擴大範圍。
- 不呼叫付費 API，不提交。

## 5. 假設（需驗證或否證）

| ID | 假設 | 如何驗證 |
|---|---|---|
| A1 | P2-a 的 self-canonical 規則不會讓「고세구」等正式名被改寫，也不會和既有實體規則衝突 | 讀 `name_rendering_rules` 的套用邏輯；新增測試：原文含 `고세구` 與 `세구땅` 時，譯文分別得到 `Gosegu` 與 `세구땅` |
| A2 | `source_proven_quality_terms(source)` 不會自動核准任意照抄的韓文詞（所以需要 P2-a） | 讀該函式；新增測試：原文 `세구땅 왔어`、譯文 `세구땅來了` 時的 quality flags |
| A3 | 新增約 15 個 STT 用語不會超過 STT prompt 的長度或數量限制 | 搜尋 `build_stt_glossary`／STT prompt 組裝的上限；對 `isegye_lilpa` 產生 glossary 並檢查長度 |
| A4 | ~~暱稱 preserve 行不會讓 `고세구땅` 被誤處理~~ → Codex round 1 已判定：`고세구땅` 兩條規則都不命中；改列為 P2 的已知邊界，以測試記錄現狀 | 測試：原文 `고세구땅 왔어` 時，不產生 `세구땅` 或 `고세구` 的啟用 |
| A5 | 標籤變更只影響顯示（控制台下拉選單、狀態），不影響 ID 解析 | 搜尋 `label` 的使用處（Python 與 `src-frontend`） |
| A6 | 「노캣노라」是 고세구 1st Single《NO CAT, NO LIFE!》的韓文縮寫（evidence: inference，來自 왁물원 標題）。**round 2 處理**：未證實，已從附錄 A 移除；只保留 `NO CAT, NO LIFE!` 與歌名 | 使用者確認後另案加入 |
| A7 | 保留韓文的粉絲名與成員正式名（`둘기`、`박쥐단`、`주폭도`、`세균단`、`아이네`、`징버거`、`비챤`）在譯文中目前沒有出版核准來源 | 讀 `request_protection.approved_hangul_terms` 與 `_final_script_rejection_reason` 的來源；用現有測試工具對含這些詞的原文／譯文跑 `translation_quality`，看是否產生 script violation。成立則依 P2 延伸處理 |

近期直播話題（노캣노라 以外，例如 신스멀게동、원펀맨 같이보기、포인트 컬링 대회）不放進本設定檔：
屬於短期熱門用語，適合另外的熱門用語區塊或 cafe_fansite 的名詞建議流程，不在本任務範圍。

## 6. 驗證計畫

1. 窄測：`python -m pytest tests/test_translation_prompts.py tests/test_streamer_profiles.py tests/test_translation_corrections.py tests/test_entity_registry.py -q`
2. 新增／更新的測試：
   - 兩種版本（standard 與 qwen 分別檢查）仍含 C4 的必要文字；standard 的 output terms 仍為 C4 所述。
   - standard 的 preserve terms 含所有暱稱與 `이파리`、`둘기`、`박쥐단`、`주폭도`、`세균단`；
     **不含** `라니`、`똥강아지`、`챤`、`이세돌`、`노캣노라`。qwen 版另以文字斷言檢查相同的詞有／沒有出現在 glossary 段。
   - `이세돌`：glossary 段不含 `이세돌` 的自我對應；背景說明含棋士歧義與「李世乭」譯法。
     （LLM 實際依上下文的判斷無法以離線測試證明，列入 §6.4 的重播觀察。）
   - 出版核准（P2）：
     - 正式名與暱稱同句：原文 `고세구랑 세구땅` → 啟用 `Gosegu` 的實體規則與 `세구땅` 的 self-canonical 規則，譯文 `Gosegu和세구땅` 不產生 script violation。
     - `고세구땅`：不啟用任何一條（記錄 P2 邊界）。
     - 原文沒有暱稱、譯文出現 `세구땅`：不被核准。
     - A7 成立時：粉絲名與保留韓文的成員正式名同樣各一個測試。
   - STT（A3）：
     - `build_stt_glossary("isegye_lilpa")` 含新增用語，順序穩定。
     - Groq 最壞情境：同時有場景用語與近期逐字稿時，組裝結果不超過上限，且截斷順序符合 `stt_policy` 的既有策略。
     - ElevenLabs keyterm manifest：總數 ≤ 100，新增詞通過既有的長度、詞數與字元過濾。
3. 翻譯相關：`python -m pytest tests/test_translator.py -q`
4. 離線重播（不呼叫 API）：`python scripts/replay_eval.py run --snapshot data/replay_eval_snapshot.jsonl`，
   回報有幾筆結果改變、改變是否都來自本次資料變更。必要的預期更新另外列出，不自動 `--update`。
5. 全套：`python -m pytest -q`
6. `git status --short`、`git diff --stat`，確認只動到 §3 列出的檔案。
7. 依 `AGENTS.md` 啟動唯讀的實作後審查。

以上指令用專案的 `live-subtitle-env` 環境執行。

## 7. 審查者檢核清單（逐項驗證，不是確認問題）

- C1–C10 每一項是否與程式碼／資料相符。
- U3 的實作方式（P1 keep 行＋P2）是否確實讓暱稱保留韓文，且不改變 U4。
- 附錄 A 的 glossary 段落經 C5 解析後，output terms 與 preserve terms 是否符合 §6 的預期。
- `라니`、`똥강아지`、`챤` 不在 preserve terms 中的理由是否成立；是否還有其他高歧義詞被放進 glossary。
- A1–A5 的驗證方式是否足以否證對應假設。
- 附錄 A 的事實是否與附錄 C 的來源一致；是否有寫成現況、但可能過時的描述。

## 8. 審查紀錄

### Codex review（round 1，2026-10-08）

審查者：Codex（`codex exec -s read-only`，session `01a11b6f-9db3-7863-bdc6-6ff338dbab39`）。結論：**REVISE**。

- Claim 判定：C1、C2、C6、C8、C9、C10 成立；C3、C4、C5、C7 部分成立。
- 假設：A1 條件成立；A2 成立（P2-b 不足）；A3 在無近期逐字稿與場景用語時成立；A4 不成立；
  A5 大致成立；A6 未證實。
- glossary 模擬解析：output terms 10 個、preserve terms 36 個，符合 §6 的預期；
  單字英文歌名不會進入 preserve terms。
- Blockers：
  1. `이세돌` 同時被要求依上下文判斷，又被固定自我對應列入 output／preserve terms。
  2. 暱稱核准應明確選定 P2-a，並說明 `고세구땅` 不命中的處理。
  3. `노캣노라` 未證實卻放入固定 glossary。
- Non-blocking：修正 C4、C5 的措辭；A3 測試加入 Groq 場景用語與近期逐字稿、ElevenLabs keyterm manifest；
  高歧義詞（`고멤`、`왁굳` 等）逐詞審核。

### Claude Code response（round 2，2026-10-08）

先逐項驗證 Codex 的主張，再修訂：

| Codex 項目 | 驗證 | 修訂 |
|---|---|---|
| Blocker 1 | 同意：附錄 A 原有 `- 이세돌 -> 이세돌` 一行，與背景說明的上下文判斷互相矛盾 | 從 glossary 移除該行；`이세돌` 只在背景說明中處理（團體／棋士判斷、李世乭 譯法）。現行 profile 本來也沒有 `이세돌` 的自我對應，所以這等於維持現狀 |
| Blocker 2 | 同意：Codex 以 `source_proven_quality_terms` 的實際回傳值否證了 P2-b | §3 P2 改為選定 P2-a，寫明「只在原文出現時核准」與 `고세구땅` 的邊界；§6 加入對應測試 |
| Blocker 3 | 同意 | 從附錄 A 移除 `노캣노라`；A6 改為「未確認前不收」 |
| C4、C5 措辭 | 同意 | 已修正 |
| A3 測試範圍 | 同意 | §6 加入 Groq 最壞情境與 ElevenLabs manifest 檢查 |
| 高歧義詞 `고멤`、`왁굳` | 部分同意：兩者在本 profile 的語境下都是專有名詞（고정 멤버、우왁굳），沒有找到一般語境的高頻同形詞；且依 C6，含韓文的 preserve terms 不會因此取得出版核准，影響僅限提示層 | 保留在 glossary；若重播觀察到誤用再移除 |

**round 2 新增**（不屬於 Codex 的 blocker，是依 Codex 對 C6 的驗證延伸出的檢查）：
A7 ——保留韓文的粉絲名與成員正式名目前可能沒有出版核准來源（實測只有 `이파리` 被啟用）。
已列入 §5 的假設與 §3 P2 的延伸處理，請 round 2 審查一併驗證。

### Codex re-review（round 2，2026-10-08）

審查者：Codex（`codex exec -s read-only`）。結論：**YES**，無 new blocker。

- Blocker 1–3：皆已解決（記憶體模擬確認 `이세돌`、`노캣노라` 不進入 output／preserve terms；P2-a 與 `고세구땅` 邊界有程式依據）。
- **A7 成立**：`request_protection.approved_hangul_terms` 只來自 unknown-name escrow；七個詞逐一以「詞＋왔어」測試，
  `translation_quality` 皆得到 `target_has_unexpected_hangul`，最終 script 檢查回傳 `unexpected_hangul`
  （`modules/request_protection.py:103`、`modules/translator.py:1063, 1458`）。
  → §3 P2 的延伸處理成立，納入實作範圍。成員正式名（아이네、징버거、비챤）的規則須確認與既有實體啟用、
  obligation 及修正流程同句運作，不能只檢查 script flag。
- 高歧義詞：Claude 的理由部分成立；保留在 glossary，以 §6 的離線重播觀察。

**狀態**：計畫通過交叉審查，進入實作（Codex）。

### 實作後發現（Claude Code，2026-10-08）

**C11（計畫遺漏的前提，evidence: code）**：正式路徑不會讀取 standard 版全文。
- DeepSeek：`effective_system_prompt_for_engine` → `_deepseek_system_prompt` → `_deepseek_capsule_prompt`，
  只放 `get_translation_profile_facts(profile_id)`，也就是 **qwen 版第一個空行之前的段落**
  （`modules/translation_engines.py:758-790`、`modules/translation_prompts.py:43-51`）。
- Groq 後備：`_groq_system_prompt` 使用 `_compact_profile_digest`，同樣以該段落為主，並受
  `tests/test_translation_engines.py:285` 的 digest < 600 字元限制。
- standard 版全文只經 `_compose_system_prompt` 組進共用 system prompt，在上述兩個引擎都會被取代。

**影響**：實作版本為了符合 600 字元限制，在 qwen glossary 中插入空行，導致正式提示**失去**
`히게단=Official髭男dism`（原版的 qwen 第一段有這一項）、`패러블`、歌名、作品名，背景說明中的
`이세돌`／李世乭 判斷與 `라니` 提醒也從未進入正式提示。

**修正**：qwen 第一段改寫為精簡格式（483 字元；digest 553 字元，含自動附加的共用規則），
內容涵蓋：正式譯名與 히게단、保留韓文的名字與用語、暱稱、粉絲名（含 똥강아지／라니 的限制）、
主要作品名、이세돌 的棋士判斷。英文歌名在譯文中本就會原樣保留，只列最常提及的幾首。
另加測試，直接檢查 `_deepseek_capsule_prompt("isegye_lilpa")` 含上述關鍵項。

### 實作後獨立審查（Codex，fresh context，read-only）

- 第 1 輪：**REVISE**。中：qwen facts 同時把 `이세돌` 列入「Keep Korean」並要求棋士語境譯為 李世乭，互相衝突；
  低：`test_each_name_rendering_rule_triggers_and_is_gated` 對空 `wrong_forms` 規則直接略過觸發與 profile 隔離檢查；
  低：isegye 的 standard／qwen 詞彙比較放寬後抓不到上述衝突；低：根目錄留有沙箱建立的 pytest cache 目錄。
- 修正：facts 第一段改為「이세돌: the group, keep Korean; with 바둑/9단 it is Go player 李世乭.」（facts 508、digest 578 字元）；
  空 `wrong_forms` 規則改驗證 isegye_lilpa 啟用、url 不啟用；新增 DeepSeek capsule 與 Groq digest 的消歧回歸斷言。
- 第 2 輪：**APPROVE**，無新問題。

### 最終驗證（2026-10-08）

- 全套 pytest（非沙箱環境，Claude Code 重跑）：1533 passed、1 skipped、451 subtests passed。
- `scripts/replay_eval.py run`：750 筆，0 偏差。
- 變更：11 個檔案（371 insertions、25 deletions），未暫存、未提交。
- 待使用者處理：根目錄 `UsersuserAppDataLocalTemplive_translate_pytest_cache/`（2026-10-08 20:42 由 Codex 沙箱建立，權限鎖定）。

---

## 附錄 A：standard 版草稿

```
[Fixed proper-noun glossary]
- 고세구 -> Gosegu
- 주르르 -> Jururu
- 릴파 -> Lilpa
- 아이네 -> 아이네
- 징버거 -> 징버거
- 비챤 -> 비챤
- 이세계아이돌 / 이세계 아이돌 / ISEGYE IDOL -> 이세계아이돌
- 세구땅 / 르르땅 / 이네땅 / 버거땅 / 부가땅 / 챠니 / 릴파넴 -> keep the nickname exactly (세구땅=Gosegu, 르르땅=Jururu, 이네땅=아이네, 버거땅/부가땅=징버거, 챠니=비챤, 릴파넴=Lilpa)
- 이파리 / 둘기 / 박쥐단 / 주폭도 / 세균단 -> keep the fan name exactly (이파리=이세계아이돌 fandom; 둘기=아이네; 박쥐단=Lilpa; 주폭도=Jururu; 세균단=Gosegu)
- 우왁굳 / 왁굳 / 왁타버스 / 왁물원 / 고멤 / 맆스틱 -> keep Korean
- 패러블 / Parable Entertainment -> Parable Entertainment
- RE : WIND / 겨울봄 / KIDDING / LOCKDOWN / Another World / Superhero / OVER / SYZYGY / Misty Rainbow / Stargazers / ELEVATE / MEMORY / Be My Light / Nameless / White / Smile For You -> keep the official song title
- NO CAT, NO LIFE! / 고양이가 세상을 구한다! -> keep the official title (고세구 1st single and title song)
- 마법소녀 이세계아이돌 / 차원을 넘어 이세계아이돌 / 이세계 페스티벌 -> keep the official title
- 히게단 -> Official髭男dism

input: 고세구 주르르 릴파
output: Gosegu Jururu Lilpa

【이세계아이돌 背景】
이세계아이돌（이세돌，ISEGYE IDOL）是 6 人虛擬偶像團體，2021-12-17 以 RE : WIND 出道，
在 SOOP 直播。成員：아이네、징버거、릴파(Lilpa)、주르르(Jururu)、고세구(Gosegu)、비챤。
團體粉絲名 이파리；個人粉絲名：아이네=둘기、징버거=똥강아지、릴파=박쥐단、주르르=주폭도、
고세구=세균단、비챤=라니。「똥강아지」「라니」也是一般韓語（親暱稱呼、語尾「-라니」），
只有明確指粉絲時才保留韓文。「챤이」「챤님」是 비챤 的暱稱形式，保留韓文。
「이세돌」通常指本團，但也可能是圍棋棋士 李世乭（例：이세돌 9단、바둑）；指棋士時譯為「李世乭」。
開場問候「(둘, 셋) 차원을 넘어! 안녕하세요, 이세계아이돌입니다!」譯為「(二、三) 跨越次元！大家好，我們是이세계아이돌！」。
常見用語：ㄱㅇㅇ=귀여워（可愛）；알잘딱=自己看著辦好、俐落又有眼色；遊戲或企劃結束時說「관」（結束）；
낮뱅=白天開台、휴뱅=休台、뱅온=開台、방종=關台。

例 62（全團問候）
input:둘, 셋! 차원을 넘어! 안녕하세요, 이세계아이돌입니다!
output:二、三！跨越次元！大家好，我們是이세계아이돌！

例 63（暱稱保留：고세구）
input:세구땅 신곡 들어봤어? 진짜 좋더라
output:세구땅的新歌聽了嗎？真的很好聽

例 64（暱稱保留：징버거）
input:버거땅 생일 축하해! 오늘 다 같이 모였어
output:버거땅生日快樂！今天大家都到齊了

例 65（이세계아이돌 팬덤명 이파리）
input:이파리들 오늘 많이 와줬네요!
output:이파리們今天來了好多人！

例 66（주르르 粉絲）
input:르르땅 방송 켰어? 주폭도들 다 모여
output:르르땅開台了嗎？주폭도們都集合

例 67（아이네 身高 158 Meme）
input:158이라고요? 저 그거보다 크거든요!
output:說我158？我比那個高啦！

例 68（비챤 暱稱與白天開台）
input:챠니 오늘 낮뱅 한대
output:聽說챠니今天要白天開台

例 69（릴파 粉絲名 박쥐단）
input:박쥐단들 오늘도 와줘서 고마워요!
output:박쥐단們今天也來了謝謝！

例 70（이세돌 歧義：圍棋棋士）
input:바둑 이세돌 9단이랑 이세돌이 대결했대
output:聽說圍棋的李世乭九段和이세돌對決了

例 71（STT 把歌名念成韓文）
input:스마일 포 유 뮤비 봤어?
output:看過《Smile For You》的MV了嗎？

例 72（ㄱㅇㅇ）
input:아 ㄱㅇㅇ 진짜 너무 귀엽다
output:啊好可愛，真的太可愛了

例 73（ISEDOL 成員合體直播：全員集合）
input:오늘 이세돌 다 모였어요 진짜 오랜만이에요
output:今天이세돌全員到齊了，真的好久不見
```

## 附錄 B：fan_terms.json 資料

| term | streamer | fandom_of | notes |
|---|---|---|---|
| 이파리 | （空） | Isegye Idol fans | 團體粉絲名，保留韓文 |
| 둘기 | 아이네 | 아이네 fans | 由來：비둘기 |
| 똥강아지 | 징버거 | 징버거 fans | 一般韓語也是親暱稱呼；只在明確指粉絲時保留 |
| 박쥐단 | Lilpa | Lilpa fans | 既有資料 |
| 주폭도 | Jururu | Jururu fans | |
| 세균단 | Gosegu | Gosegu fans | |
| 라니 | 비챤 | 비챤 fans | 與語尾「-라니」同形；只在明確指粉絲時保留 |

## 附錄 C：來源

- 나무위키 本站「이세계아이돌」，2026-10-07 13:27 版（使用者手動貼上）：團名、出道、所屬、粉絲名 이파리、應援棒 맆스틱、
  問候語、歌曲與年表、2026-09-13 製作人變更、共用用語（알잘딱、관、ㄱㅇㅇ）、SOOPER MATCH。
- 나무위키 미러（namu.moe）：
  - 「틀:이세계아이돌 멤버별 개인 팬덤명」2026-01-04 版：各成員粉絲名與由來。
  - 成員各頁 2026-08-05～08-23 版：暱稱（이네땅、버거땅/부가땅、르르땅、세구땅 系列、챠니、릴파넴）與直播梗。
  - 與本站 2026-10-07 版比對：除 2026-09-13 製作人變更外，抽查的 17 項內容一致。
- 한국어 위키백과「이세계아이돌」：成員、出道日、平台變遷、作品列表。
- cafe_fansite 實際抓取的 왁물원 公告（2026-10）：`[주르르] … (부가땅 생축)`、`[고세구] 버건니 생일 추카해` 等，
  佐證 부가땅＝징버거（징버거 生日 10-08）。
