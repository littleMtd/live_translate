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

`LIVE_TRANSLATE_DEEPSEEK_ROUTE=off` 會讓 ordinary live translation 使用 Groq-only emergency rollback。Dashboard 的 `engine_chain` 不能重新排列這條受保護的 production route。

OpenRouter 與 DeepL adapter 仍可能存在於程式碼中供明確的非 production/custom 路徑使用，但目前不是 ordinary live fallback。不要把歷史 Qwen、DeepL 或 shadow 文件當成現行 routing。

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
```

自然 production 證據必須由使用者執行 `python main.py`，並在自己的 SOOP/CHZZK 直播觀看流程中產生。分析工具不得自行搜尋、播放或下載外部直播代替 production evidence。

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
