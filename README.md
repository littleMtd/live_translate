# live_translate

Windows 韓語直播即時翻譯工具。它從指定音訊裝置取得直播聲音，經過 STT、句子組裝、翻譯與發布安全檢查後，在 tkinter overlay 顯示繁體中文字幕。

## 目前 production pipeline

```text
VB-CABLE / sounddevice
  → ElevenLabs Scribe v2 STT
      → 同一 audio chunk 的 Groq STT provider-failure fallback
  → sentence assembly
      → optional one-shot provisional translation
  → source normalization
  → reviewed entity / unresolved referent / semantic terminology protection
  → DeepSeek V4 Flash
      → Groq translation fallback
  → deterministic restore and corrections
  → canonical/entity/script/publication guards
  → ordered subtitle publication
```

`LIVE_TRANSLATE_DEEPSEEK_ROUTE=off` selects the Groq-only emergency rollback. Provider order is a fixed contract and is not editable from the dashboard.

OpenRouter is retained for scene vision only. OpenRouter translation, DeepL, Claude, and Google Translate adapters are retired.

## 啟動

```powershell
python -m venv live-subtitle-env
.\live-subtitle-env\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env

# 自然 production session
.\live-subtitle-env\Scripts\python.exe main.py

# STT diagnostics
.\live-subtitle-env\Scripts\python.exe main.py --stt-only
.\live-subtitle-env\Scripts\python.exe main.py --listen

# 咖啡廳剪輯來源：手動鎖定 reviewed profile，並標記這次 run
$env:LIVE_TRANSLATE_RUN_KIND="cafe_clip"
.\live-subtitle-env\Scripts\python.exe main.py --profile url
Remove-Item Env:LIVE_TRANSLATE_RUN_KIND
```

`--profile <id>` 可與 `--stt-only` 或 `--listen` 合用，不能與 identity ROI 模式合用。`cafe_clip` 事件與直播事件可寫在同一天的 JSONL；analyzer 預設只取 `live`；其他分析、重播與標註工具預設只排除 `cafe_clip`，保留其他 kind 的既有行為，需要剪輯事件時明確指定 `--run-kind cafe_clip`。`cafe_fansite` 的停止方式為強制結束行程；`cafe_clip` 不屬於 production 證據。此模式不會在結束時自動匯出 ChatGPT bundle；需要時可用 `scripts/export_chatgpt_bundle.py --list-runs` 查詢 run，再手動匯出。

自然 production 證據必須由使用者執行 `python main.py`，並在自己的 SOOP/CHZZK 直播觀看流程中產生。分析工具不得自行搜尋、播放或下載外部直播代替 production evidence。

## Profile identity

Auto mode 啟動時是 neutral；畫面上的 `profile=general` 表示尚未取得 reviewed identity evidence，不是辨識到一個名為 general 的主播。`config.py` 的 `streamer_profile` 只保留 configured source metadata，不能在 auto mode 自動取得 ownership。校準方式：

```powershell
.\live-subtitle-env\Scripts\python.exe main.py --calibrate-identity-roi
```

存在有效 calibrated SOOP/CHZZK identity ROI 時，它會取代 whole-scene profile resolution，並只接受 ROI 中 exact reviewed visible/OCR aliases。沒有有效 ROI 時仍使用既有 whole-scene consensus。Blank、unknown、capture failure 或 provider failure 都 fail closed；已確認 profile 會被保留，startup 尚未確認時則維持 general。Groq vision 回傳 HTTP 429 時，identity reader 會依 provider reset duration 進入 backoff，ROI 畫面改變也不能繞過該 fence。

直播前或 fresh run 後可產生 OCR alias 候選報告：

```powershell
.\live-subtitle-env\Scripts\python.exe scripts\suggest_identity_ocr_aliases.py `
  "logs\runtime_events_*.jsonl" `
  --json-output scratch\analysis\identity_ocr_alias_candidates.json `
  --report-output scratch\analysis\identity_ocr_alias_candidates.md
```

此工具只讀取具 calibrated ROI provenance 的 persisted observations，且只輸出人工審核候選；它不修改 registry，也不允許 fuzzy runtime activation。核准的變形只能加入 `identity_ocr` scope。

## Translation correctness ownership

- `modules/entity_registry.py`：reviewed global entity data 與 exact alias activation。
- `modules/request_protection.py`：request-local protected source spans 的統一契約。
- `modules/unknown_name_escrow.py`：未解析但 source-grounded referent 的 exact preservation。
- `modules/semantic_terminology.py`：reviewed semantic terminology 的 deterministic rendering。
- `modules/translation_corrections.py`：source normalization、canonical obligations 與 deterministic target corrections。
- `modules/translator.py`：request construction、fallback、cache/history、provisional promotion、adjudication 與 ordered publication。
- `modules/provisional_subtitles.py`：one-shot provisional candidate 與 exact fingerprint。

所有 provider candidate 都必須經過相同的 restore、canonical/entity/terminology cardinality、Hangul/Kana/script/meta 與 final publication invariants。被拒絕的 candidate 不得進入 subtitle、cache 或 history。

## Runtime evidence

Runtime JSONL 使用 schema v6，位於 `logs/runtime_events_YYYYMMDD.jsonl`。

- `stt_request_contract`：實際 STT prompt/keyterms、來源、profile/activity、provider-bound audio hash。
- `translation_request_contract`：實際 provider messages、history、protected spans、canonical obligations 與 data/policy hashes。
- STT result、translation attempts、provisional 與 final translation 透過 contract ID 串接。
- Candidate adjudication保留 raw、protection-restored、source-corrected 三個階段及所有 failed invariants。

匯出單一執行 bundle：

```powershell
.\live-subtitle-env\Scripts\python.exe scripts\export_chatgpt_bundle.py `
  --run-id <run_id> --include-audio
```

驗證 bundle 並建立 causal chain：

```powershell
.\live-subtitle-env\Scripts\python.exe scripts\analyze_forensics_bundle.py `
  scratch\chatgpt_bundles\chatgpt_bundle_<run_id> `
  --json-output scratch\analysis\<run_id>.forensics.json `
  --report-output scratch\analysis\<run_id>.forensics.md
```

`runtime_events*.jsonl` 是 bundle 的 chronological source of truth；`request_contracts.json`、`manifest.json` 與報表是索引或 derived views。缺少或歧義的 lineage 必須保留為 unresolved。

## Blind Phase 1

第一階段 blind translation forensics 使用 ChatGPT Project 完成，因此必須透過 computer use 操作 Project UI。輸入是上述自然 production session 的 exact runtime bundle，不提供已知 failure labels、預期 root cause 或修正方向。Blind findings 只能作為 triage leads；Codex 必須再對照 raw runtime evidence 與當前程式碼獨立查證後，才能修改 production。

完整流程與停止條件見 [`docs/agent/BLIND_PHASE1_WORKFLOW.md`](docs/agent/BLIND_PHASE1_WORKFLOW.md)。

## 驗證與文件入口

```powershell
.\live-subtitle-env\Scripts\python.exe -m pytest tests -q
.\live-subtitle-env\Scripts\python.exe scripts\replay_eval.py run `
  --snapshot data\replay_eval_snapshot.jsonl
.\live-subtitle-env\Scripts\python.exe scripts\evaluate_translation_prompt_benchmark.py `
  --production-runtime-baseline
```

- Agent onboarding：[`AGENTS.md`](AGENTS.md)、[`docs/agent/AGENT_BRIEF.md`](docs/agent/AGENT_BRIEF.md)、[`docs/agent/TASK_INDEX.md`](docs/agent/TASK_INDEX.md)
- Current architecture：[`system.md`](system.md)
- Detailed runtime map：[`docs/agent/PROJECT_CONTEXT.md`](docs/agent/PROJECT_CONTEXT.md)
- Validation workflows：[`docs/agent/VALIDATION.md`](docs/agent/VALIDATION.md)
- Maintained tool inventory：[`docs/agent/TOOL_INVENTORY.md`](docs/agent/TOOL_INVENTORY.md)

### 離線字幕工具

以獨立程序執行 `live-subtitle-env\Scripts\python.exe scripts\make_subtitles.py translate --input ko.vtt --profile isegye_lilpa --out-dir <目錄>`；音訊／影片則使用 `transcribe --input audio.m4a`。語音辨識預設使用 ElevenLabs Scribe v2（需 `.env` 的 `ELEVENLABS_API_KEY`；`--stt groq` 可改用 Groq Whisper），翻譯預設使用 Gemini 3.8 Flash（需 `.env` 的 `GEMINI_API_KEY`；`--translator deepseek` 可改用 DeepSeek），Gemini 預付餘額用完時會直接提示到 AI Studio 加值。支援 VTT／SRT、YouTube 滾動字幕清理，輸出韓文 VTT、繁中 VTT／SRT 及品質報告；stdout 為含 job ID 的 JSONL，診斷在 stderr。

`--estimate` 僅作本機估算，不呼叫 API。實際 `translate` 會呼叫 Gemini（或指定的 DeepSeek）；`transcribe` 另呼叫 ElevenLabs（或指定的 Groq），均可能收費，請先確認授權。轉錄需 ffmpeg／ffprobe，可用 `--ffmpeg <ffmpeg.exe 路徑>` 指定同目錄的工具。費用為估算，重試不包含在內；未知費率以 null 表示。工作標記為 `cafe_clip`，鎖定指定 profile、空 activity，不使用即時 cache／history／memory／breaker。輸出目錄有同名成品時拒絕覆寫；失敗會撤回本次交付，單句翻譯失敗則標示 `【未翻譯】`。目前不提供續跑 checkpoint。
