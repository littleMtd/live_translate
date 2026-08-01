# Phase 0 評測集:資料盤點與決議 (2026-06-13)

狀態:盤點完成(只讀,未動 live path)。本文件是 Phase 0 的第一份工作紀錄。

> **重跑驗證(2026-06-13,Claude):** 重新從原始檔解析,labeling 數字與本文件先前記載完全一致(見下,156/160、86 筆 STT 錯誤、tag 分布皆相符)。**唯一變動:audio dump 不再為空**——新增一場 `20260612T170509Z-63112` 已落地(見「盤點:audio dump」更新)。此變動解除了 gate 的「等新 dump」阻塞。

## 決議(2026-06-13,littleM 拍板)

1. **Speaker policy = `host-primary`**:host 有可辨識語音時,只翻 streamer 主聲道/主語者;host 完全沉默且 clip/影片/遊戲角色是唯一清楚語音時,可翻該 clip/其他語者內容。
   - 「翻對」定義:host 有講話 → 譯文對應 host 的話;host 沉默且只有 clip/其他語者清楚說話 → 譯文可對應該非 host 語音。
   - `wrong_speaker_selected` 判準:host 有講話但系統選到 clip/他人;或 host 沉默但系統選到不相干來源。
   - `host_over_clip` 重疊情境仍以 host 為正解。
   - **操作型定義**:`host 沉默` 只在全部相關 source chunks 裡完全沒有可辨識 host 語音,且 clip/遊戲/其他語者是唯一可懂語音時成立。host 小聲碎念、短句、明確語助詞、同時講話都算 host 有聲;純笑聲、哼歌、呼吸、不可辨識呢喃不自動算 host 有聲,標 `speaker_unclear` 或 `unclear`。
   - **Phase 1 限制**:此 policy 短期只作為離線評測/標註規則。runtime 目前是 loopback 混音,沒有「host 是否沉默」signal;Phase 1 只能記錄 `speaker_source_policy=host-primary` 與標註回饋,不能把它當可自動 routing 的 live signal。
   - 後續(非今日):config 加 `speaker_policy`、labeling instructions 更新、runtime event 加 `speaker_source_policy`(Phase 1 範圍,掛 flag;不做自動 host-silence routing)。
2. **Audio dump 遺失 → 重新 dump + 增量標註**:不依賴舊 696 個 wav(已被清空,見下)。接下來直播以 `LIVE_TRANSLATE_DUMP_AUDIO=1` 收新音訊,依 host-primary policy 標註新樣本。Gate 的兩個離線驗證(SenseVoice 比對、audio verification)等新 dump 累積後執行。

## 盤點:labeling 資料

來源:`logs/labeling_sample_*.annotations.json`(6 檔)。

- 已標註 **156 / 160** 筆。
- Label 分布:`b_stt_error` 79、`a_translation_error` 23、`both` 7、`ok` 39、`unclear` 8。
  - **STT 錯誤案例共 86 筆**(79+7),數量上滿足 gate 的 30–50 筆需求,但音訊已遺失(見下),只能做文字面分析,不能重跑 SenseVoice。
- Speaker-source 標註(含 round2 專項 33 筆):`host_only` 35、`host_over_clip` 31、`audio_source_mismatch` 22、`clip_or_other_speaker` 21、`wrong_speaker_selected` 15、`speaker_unclear` 13、`multi_streamer` 9。
- Context tags:`multi_speaker` 64、`clip_audio` 35、`over_attributed_chunks` 29、`bgm_mixed` 28、`unclear_audio` 16。混音是最大宗情境 → host-primary policy 影響面大,既有標註需依新 policy 複核(增量標註時一併處理)。
- **分布偏差警告(風險補遺 #6)**:86 筆 STT 錯誤中 62 筆來自單一場 `20260531T115809Z-123124`。評測集不得只用這批;新樣本須混入隨機抽樣 + hold-out,跨多場。

補充盤點(Codex,2026-06-13):

- Labeling sample JSON 共 9 份、493 rows;source chunk audio path refs 共 814。依去重口徑,舊 labeling 樣本引用 696 個 unique wav,目前 0 個在磁碟上。
- 可優先參考的完整標註:`labeling_sample_20260611_153820.annotations.json`(50)、`labeling_sample_20260531_232759.annotations.json`(60)、`labeling_sample_20260531_232759.speaker_source_round2.annotations.json`(33)。
- `20260611_153820` 分布:`ok` 20、`b_stt_error` 19、`a_translation_error` 7、`unclear` 4;speaker/source tags:`host_only` 26、`clip_or_other_speaker` 15、`audio_source_mismatch` 8、`host_over_clip` 7、`wrong_speaker_selected` 3。
- `20260531_232759.speaker_source_round2` 是 speaker/source 密集樣本:`host_over_clip` 24、`wrong_speaker_selected` 12、`speaker_unclear` 11、`audio_source_mismatch` 8、`multi_streamer` 8、`clip_or_other_speaker` 6。

## 盤點:audio dump(2026-06-13 重跑後更新)

- **新 dump 已落地**:`logs/audio_dump/20260612T170509Z-63112/` 有 **3765 個 wav**(16 kHz、mono、16-bit),約 **805 MB**、抽樣估 **~6.95 小時**(平均 6.6 s/檔,utt-1..utt-3902)。先前本文件記「audio_dump 現為空」已過時——這場是 Jun 12 開台、`DUMP_AUDIO` 開啟所收,正是文件先前在等的新音訊。
- **音訊有配對文字**:`logs/runtime_events_20260613.jsonl`(13 MB)全部 14622 筆事件都屬此 session:`audio` 4687、`stt` 3903、`sentence` 3016、`translation` 3016。亦即這場有完整 audio + STT + sentence + translation,可直接做離線重播與 candidate 比對。
- **舊標註音訊仍遺失**:既有 labeling 樣本引用 **696 個 unique wav**(8 個 session;取樣當時 `audio_exists: true`),現 **0 個在磁碟上**。86 筆已標 STT 錯誤仍只能做文字面分析,無法重跑 SenseVoice。
- **此新 session 尚未標註**:`20260612T170509Z-63112` 不在既有 6 個標註檔涵蓋的 8 個 session 內。要拿它跑 gate,需先依 host-primary policy 抽樣標註(找出 STT 錯誤案例)。
- `audio/` 仍僅 2 個 VOD(LGcLBC9_RUk 9.8 min、WgzD8Qiq-ac 16.3 min),與已標註 session 對應不明,不採用對齊重建方案。
- `logs/runtime_events_20260526..0613.jsonl` 完整,文字 replay 可用(golden-file 測試、resolver 離線分析)。

## 盤點:20260613 runtime / audio join

來源:`logs/runtime_events_20260613.jsonl` + `logs/audio_dump/20260612T170509Z-63112/`。

- run id:`20260612T170509Z-63112`
- translation events:3016;success 2951、filtered 64、failed 1
- STT events:3903;sentence events:3016
- source refs:3765;source refs with wav:3765
- evidence refs:277;evidence refs with wav:277
- translations with all source wav:2997
- eligible success translations with all source wav:2932
- profile:`hades_chxxnnx` only

Cut reason 分布:

| cut reason | count |
|---|---:|
| `silence_complete` | 1228 |
| `natural` | 765 |
| `forced_blob` | 522 |
| `forced_prefix` | 317 |
| `merged:forced_blob+natural` | 95 |
| `merged:forced_blob+silence_complete` | 59 |
| `forced_gap_prefix` | 30 |

Quality flags:

| flag | count |
|---|---:|
| `target_high_latin` | 233 |
| `empty_target` | 65 |
| `low_target_cjk` | 45 |
| `very_short_target` | 42 |
| `target_has_hangul` | 42 |
| `low_source_hangul` | 24 |
| `repetitive_target` | 8 |
| `target_has_japanese` | 3 |

STT evidence:

- engine:Groq only
- status:`success` 3797、`filtered` 71、`skipped` 34、`failed` 1
- filtered reasons:`avg_logprob` 60、`no_speech_prob` 9、`hallucinated` 2
- skipped reason:`below_volume_threshold` 34
- STT latency:p50 515 ms、p95 1000 ms、max 7000 ms
- avg_logprob:p05 -0.7421、p50 -0.3016、p95 -0.1103
- no_speech_prob:p05 0.00008、p50 0.00338、p95 0.04251

Translation latency baseline:

| date | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| `20260611` | 1671 | 1750 ms | 12782 ms | 14109 ms | 22875 ms | 120109 ms |
| `20260613` | 3016 | 1078 ms | 2453 ms | 3891 ms | 11437 ms | 16000 ms |

判斷:`20260613` 是目前最乾淨的新 baseline。`20260611` 有 50 筆完整 annotation,但 audio dump 缺失且 profile/run 混雜,不適合作為 audio gate 母體。

## 影響與下一步(2026-06-13 重跑後修訂)

1. **Gate 不再卡在等 dump**:`20260612T170509Z-63112`(~7h、3765 wav、配對 runtime events)已足以啟動 gate 的離線驗證。原先「延後到累積 1–2 場」的前提已被這場滿足。
2. **新樣本標註是現在的瓶頸**:這場音訊未標註。下一步是依 host-primary policy 從 `runtime_events_20260613.jsonl` 抽樣建標(沿用既有 `sample_labeling_cases.py`),目標先湊出 gate 需要的 30–50 筆 STT 錯誤 + 20 段 audio verification 候選。labeling instructions 需先更新為 host-primary(Phase -1 收尾項)。
3. **此後開台維持** `LIVE_TRANSLATE_DUMP_AUDIO=1`(collection mode 既有功能,非新路徑),持續累積跨場樣本以降低單場偏差。
4. 既有 156 筆標註保留為文字面參考資產;speaker 相關 labels 依 host-primary 重新解讀(`host_over_clip` → host 為正解;host 沉默時 `clip_or_other_speaker` 可是有效 source)。86 筆 STT 錯誤可做文字面 resolver 分析,但無音訊不能進 SenseVoice gate。
5. 評測集組成(50–100 筆)目標:STT error / forced cut / speaker mix / profile term / translation error / latency outlier 全覆蓋 + 隨機抽樣 + hold-out。**仍須跨多場**——勿只用新這一場,否則重蹈 #6 單場偏差。
6. 第一版 100 筆候選抽樣建議:40 筆 random hold-out、15 筆 forced_prefix/forced_blob、15 筆 silence_complete、10 筆 multi-source/evidence-source、10 筆 STT low confidence/filtered-nearby、10 筆 quality flags suspicious。
7. Gate audio cases 優先挑:low avg_logprob、high no_speech_prob、forced cuts、multi-source/evidence-source、`low_source_hangul`、repeated source candidates、`target_has_hangul` 或 `low_target_cjk`。
8. 若要自動化候選輸出,新增獨立 script 即可,例如 `scripts/build_phase0_eval_candidates.py`;它只讀 `runtime_events_*.jsonl` 與 `logs/audio_dump`,輸出候選 JSON,不 import 或修改 live modules。
9. **先做 10 筆 host-silence/clip-heavy pilot**:正式標滿 100 筆前,先挑約 10 筆 host 沉默或 host/clip 邊界模糊案例試標,檢查 `host 沉默` 判準是否穩定。如果標註者需要反覆爭論,先收緊 annotation rules,不要繼續擴大標註。

## 實作更新(Codex,2026-06-13)

- 已新增 `scripts/build_phase0_eval_candidates.py`,只讀 `runtime_events_*.jsonl` 與 `logs/audio_dump`,輸出標準 labeling sample JSON;不 import 或修改 live modules。
- 已更新 labeling sample metadata 與 review UI,顯示 `speaker_policy=host-primary`、annotation goal、host-primary rules。
- 已產生第一版候選檔:`logs/labeling_sample_phase0_eval_20260613_host_primary.json`。
  - seed:`20260613`
  - source:`logs/runtime_events_20260613.jsonl` + `logs/audio_dump/20260612T170509Z-63112`
  - eligible population:3018(產生時 runtime log 已較本文件盤點多 2 條 translation)
  - sample size:100
  - bucket counts:`forced_cut` 15、`silence_complete` 15、`multi_or_evidence` 10、`low_confidence` 10、`quality_suspicious` 10、`random_holdout` 40
  - missing audio:0;missing STT:0
- 標註方式:`python scripts/labeling_review_server.py logs/labeling_sample_phase0_eval_20260613_host_primary.json --open`
- 驗證:`python -m pytest tests/test_sample_labeling_cases.py tests/test_labeling_review_server.py tests/test_build_phase0_eval_candidates.py --basetemp .pytest-tmp-phase0` 通過(22 passed)。系統預設 temp 目錄權限不足,所以測試需指定 repo 內 basetemp。

## 標註細則補充(2026-06-13,littleM 拍板)

- **政策修正**:`host-only` 改為 `host-primary`。原因:host 完全沉默在看 clip 時,clip/影片/遊戲角色語音本身就是觀看理解的一部分,全部視為 noise 會讓評測失真。
- **不新增標籤、不動架構**:沿用既有 `speaker_source_options`。
  - host 有可辨識語音 → 以 host 為正解。
  - host 沉默、純 clip/其他清楚人聲 → 該語音可作為正解,標 `clip_or_other_speaker`,但不自動算錯。
  - host 與 clip/其他語者重疊 → 以 host 為正解,標 `host_over_clip`。
  - host 有講話但 runtime 選錯/混入來源 → `wrong_speaker_selected` / `audio_source_mismatch` / `host_over_clip`。
- **沉默邊界**:host 完全沒有可辨識語音才算沉默。host 小聲碎念、短句、明確語助詞或和 clip 同時說話,都算 host 有聲;純笑聲/哼歌/呼吸/不可辨識呢喃不自動算 host 有聲,標 `speaker_unclear` 或 `unclear`。
- **分析規則**:gate 計算 STT rescue rate 必須分層:
  - `host_speech`:host 有聲,主要看 host 語音 STT/translation 是否被救回。
  - `clip_when_host_silent`:host 沉默且 clip/其他語者清楚,單獨統計,不可和 host_speech 混成一個 rescue rate。
  - `overlap_or_wrong_speaker`:host 有聲但混入/選錯來源,單獨看 speaker/source attribution。
  - `speaker_unclear`:排除於 rescue rate 分母。
- **Profile/glossary 注意**:`streamer_profiles.json` 與 `translation_corrections.json` 主要是 host/domain 資產。clip-only 樣本的錯誤率、rescue rate、術語錯誤必須獨立報告,避免把 host 語音改進平均掉。
- **Runtime 限制**:純標註層面的判斷規則,不影響 live path。runtime 目前沒有 host-silence signal,所以 `host-primary` 暫不能直接變成 Phase 1 routing 規則;Phase 1 只能記錄 policy 與標註衍生 metadata。
- **非韓文 clip**:host 沉默、clip/其他語者內容非韓文(遊戲對話、影片旁白等)時,仍是有效 source,照常判斷 STT/翻譯對錯;不單獨排除,不視為 out-of-scope。
- **clip-only 翻譯品質基準**:以忠實度為準——譯文準確反映聽到的 clip 內容即算 `ok`,不要求對齊 `streamer_profiles.json`/`translation_corrections.json`(host-specific 資產,clip 內容不適用)。
- **錯誤標籤採根因歸屬**:音訊中的斗內/提示音/其他語者被混入或選成 SOURCE,導致後續譯文失真時,標 `b_stt_error` 與對應 speaker/source tag,不再把同一因果鏈重複算成 translation error。`a_translation_error` 只用於 SOURCE 正確但翻譯錯誤；`both` 只用於 SOURCE 錯誤之外,譯文在不受該錯誤影響的部分另有可獨立確認的實質翻譯錯誤。

## Pilot(2026-06-13)

- 來源:從現有 100 筆候選中,依「clip 密集度」啟發式排序(bucket 為 `silence_complete`/`forced_cut`、`unique_source_utterance_id_count>1`、`low_source_hangul`/`target_high_latin`/`empty_target`/`target_has_hangul`/`target_has_japanese` quality flags 加權)挑出前 10 筆。
- **Pilot 樣本(10 筆)**:`S027`、`S003`、`S007`、`S008`、`S010`、`S012`、`S018`、`S021`、`S022`、`S026`。
- 目的:驗證 host-primary 操作型定義(host 沉默判準、clip-only 忠實度判準)在實際樣本上是否好用、判斷是否穩定。標完這 10 筆後再決定是否調整規則,才標剩餘 90 筆。
- **完成結果(2026-06-20)**:10/10 已標註；`ok` 4、`b_stt_error` 3、`a_translation_error` 2、`unclear` 1、`both` 0。speaker/source tags 為 `host_only` 7、`host_over_clip` 2、`wrong_speaker_selected` 2、`audio_source_mismatch` 2；context tags 為 `multi_speaker` 2、`over_attributed_chunks` 2。此為刻意分層的 pilot,不得外推全域錯誤率。
- 兩筆 `b_stt_error` 明確出現 host 與斗內/其他聲音重疊後選錯來源；一筆為 audio/source attribution mismatch。`unclear` 為唱歌案例。host-primary 邊界可操作,但 alert/donation contamination 應獨立視為 source-routing 問題,不可和一般 STT 聽錯混成單一機制。
- Review UI 原先依 current/evidence 分組顯示,使 carry-forward evidence 的播放順序可能成為 `3→1→2`。2026-06-20 已改為依 `stt_event_line` 顯示時間順序；只影響離線標註 UI,未動 live path。

## Phase 0 最小評測集凍結(2026-06-24)

- **凍結範圍**:上述 pilot 10 筆加 batch 2 的 20 筆,共 30 筆；annotation 寫入 `logs/labeling_sample_phase0_eval_20260613_host_primary.annotations.json`。30/30 均有 label,一致性檢查無衝突。
- **最終 label 分布**:`b_stt_error` 12、`ok` 11、`a_translation_error` 4、`unclear` 3、`both` 0。
- **B 類根因拆分**:12 筆中,9 筆帶有 `wrong_speaker_selected` / `audio_source_mismatch` / `host_over_clip` / `multi_streamer` / `clip_or_other_speaker` 等 source-routing 或 attribution 證據；2 筆標為 `host_only`,但都缺 exact heard-source transcript(其中 `S063` 只有 corrected-span note)；1 筆 `S062` 無 speaker/source tag、heard text 或 correction,衍生分析降為 `stt_unverified`。這表示目前可重現證據主要支持來源選擇與混音問題,不能用這批資料估計純聲學 STT 錯誤率。
- **可解析的 clean-host 案例**:`S063` 將遊戲停服縮寫 `섭종` 聽成 `섭쥬`,而後文 `크아도 못해?` 足以消歧；此類應作為 context-aware source resolver 的離線案例,但不因翻譯未救回而重複標成 `both`。
- **翻譯/政策案例**:4 筆原始 label 為 `a_translation_error`；衍生根因拆成 3 筆實際 translation 與 1 筆 filter-policy false positive。`S053` 的 `맞아 어 맞아` 被 repetition heuristic 誤判 `stt_garbage`,API 從未呼叫,不得計入翻譯引擎錯誤。規則已於 2026-06-24 收緊為同詞至少重複 3 次才過濾,並以 translation-policy tests 驗證。
- **不可外推**:這 30 筆來自單場、分層與問題導向抽樣,只可作為機制覆蓋、重播與重構前後 regression set；不得用 `12/30` 等比例估計全域 STT 或翻譯錯誤率。
- **下一步 gates 必須拆開**:(A)routing gate 以原始 WAV time span 標 `host` / `content_other` / `alert_tts` / `mixed` / `unrelated`,不可把 buggy runtime chunk boundary 當 ground truth；(B)acoustic/resolver gate 另存 exact `heard_source_text` 或 corrected spans,並區分純聲學與 context-resolvable cases。
- **Replay baseline(2026-06-24)**:已完成 provenance manifest、replay-time WAV size/SHA-256 重驗與本機 SenseVoice shadow；結論與限制見 `archive/experiments/PHASE0_ROOT_CAUSE_REPLAY_20260624.md`。現有 clean-host ground truth 的檢定力不足,dual-STT 目前是「無法檢定、無 measured benefit 因而延後」,不是「已證無效」。routing cases 缺 time-span ground truth,目前不得計算 router precision/recall。

## 原始盤點當日未動事項(風險補遺遵守)

- 2026-06-13 原始盤點與候選建置未改任何 live path 程式碼、config、prompt。後續 2026-06-19 至 2026-06-24 依另行確認的底層 bug 修正了 Groq fallback、garbage false positive 與 diagnostics；不得把後續變更回寫成 Phase 0 候選建置當日行為。
- 未刪未動任何既有 log/標註檔。
- 重跑驗證(Claude)只做唯讀解析與本文件更新;`git status` 確認無 tracked 程式碼變動(僅 `.analysis-tmp/`、`scripts/labeling_review_server.py`、`tests/test_labeling_review_server.py` 等既有 untracked 檔,與本次盤點無關)。
- Codex 後續補充統計已併入本文;`PHASE0_DATA_INVENTORY_20260613.md` 刪除,避免 Phase 0 盤點有兩份來源。
