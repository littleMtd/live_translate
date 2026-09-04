# live_translate

Windows 上的韓語直播即時繁體中文字幕工具。程式擷取指定的系統音訊，完成韓語 STT、斷句、翻譯與有序發布，並由 tkinter 浮動視窗顯示字幕；另有可選的 Tauri/Vue 設定與監控介面。

## 現行 production pipeline

```text
VB-CABLE / sounddevice input
  → ElevenLabs Scribe v2 batch STT
  → 同一音訊 chunk 的 Groq STT fallback（SenseVoice 仍可明確選用）
  → sentence assembly + optional one-shot provisional translation
  → source normalization → canonical obligations
  → unknown-name escrow → semantic terminology escrow
  → DeepSeek V4 Flash → OpenRouter Qwen → DeepL → Groq
  → deterministic restore/corrections + publication guards
  → final fail-closed invariants → ordered subtitle publication
```

`LIVE_TRANSLATE_DEEPSEEK_ROUTE=off` 是現行緊急 provider rollback，會把 ordinary live translation route 切回 `OpenRouter Qwen → DeepL → Groq`。Dashboard 的 `engine_chain` 編輯不能重排這條受保護的 route。

已退休的 translation quality retry、Japanese translation shadow/active、DeepSeek record-only shadow、`source_fuzzy_shadow` 與舊 prompt/model comparison 不會在 production 執行。Kana/Hangul guards、歷史 analyzer 相容性及 frozen evidence 仍保留。

## 安裝與執行

需求：Windows 10/11、Python 3.11+，以及可提供直播/遊戲音訊的輸入端點（通常為 VB-CABLE）。Rust 與 Node.js 只在開發 Tauri Dashboard 時需要。

```powershell
python -m venv live-subtitle-env
.\live-subtitle-env\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env

# 完整 pipeline
.\live-subtitle-env\Scripts\python.exe main.py

# 只執行到 STT/斷句
.\live-subtitle-env\Scripts\python.exe main.py --stt-only

# 歌曲/音樂用的較寬鬆 STT-only 模式
.\live-subtitle-env\Scripts\python.exe main.py --listen
```

依實際 route 在 `.env` 設定 API keys。不要提交或輸出 `.env`。ordinary live 預設使用 ElevenLabs、DeepSeek、OpenRouter、DeepL，並可能使用 Groq STT/translation fallback；缺少後段 fallback key 時，未設定的 provider 會被跳過。

主要設定 owner 是 [`config.py`](config.py)。Dashboard 只可透過白名單欄位把 override 寫入 `logs/live_translate_config.json`，並於下次 Python 啟動套用；它不是第二套無限制設定來源。

## Correctness ownership

- `modules/translation_policy.py`：翻譯前 sanitize/filter、duplicate/slang policy。
- `modules/translation_corrections.py`：source normalization、deterministic corrections、canonical obligations。
- `modules/unknown_name_escrow.py`：source-grounded unknown Korean names；known canonical spans 優先。
- `modules/semantic_terminology.py`：精準觸發的 production semantic terminology。
- `modules/translator.py`：provider/content failure 分離、fallback、finalization、cache/history、provisional promotion、publication invariants 與 ordered output。
- `modules/provisional_subtitles.py`：one-shot provisional candidate 與 exact fingerprint promotion。
- `utils/runtime_events.py`：schema-v5 runtime telemetry 與 quality diagnostics。

所有 provider candidate 都必須經過相同的 restore/corrections、canonical/name/terminology cardinality、Hangul/Kana/meta guards 與 final fail-closed invariant。被拒絕的 candidate 不得進入 subtitle、cache 或 history。

## Dashboard

```powershell
Push-Location src-frontend
npm install
Pop-Location
Push-Location src-tauri
cargo tauri dev
Pop-Location
```

Dashboard 由 `src-tauri/`（Rust/Tauri v2）與 `src-frontend/`（Vue 3/Vite）組成。Python 輸出不含 secrets 的設定快照；API keys 不進入 JSON bridge。

## 驗證

```powershell
.\live-subtitle-env\Scripts\python.exe -m pytest tests -q
.\live-subtitle-env\Scripts\python.exe scripts\replay_eval.py run `
  --snapshot data\replay_eval_snapshot.jsonl
.\live-subtitle-env\Scripts\python.exe `
  scripts\evaluate_translation_prompt_benchmark.py `
  --production-runtime-baseline
```

75-case baseline scorer 是純離線工具；也可用 `--results <json>` 評完整外部結果集。詳細工具與 mutation boundaries 見 `docs/agent/TOOL_INVENTORY.md` 和 `docs/agent/VALIDATION.md`。

## 文件入口

維護工作依序讀 [`AGENTS.md`](AGENTS.md)、[`docs/agent/AGENT_BRIEF.md`](docs/agent/AGENT_BRIEF.md)、[`docs/agent/TASK_INDEX.md`](docs/agent/TASK_INDEX.md)。現行詳細 runtime ownership 在 [`docs/agent/PROJECT_CONTEXT.md`](docs/agent/PROJECT_CONTEXT.md)，架構 contract 在 [`system.md`](system.md)。提案、舊 roadmap、實驗報告與 frozen artifacts 是歷史證據，不會自行授權 production 變更。
