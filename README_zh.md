# live_translate

實時韓語 → 繁體中文字幕覆蓋層，用於直播流。

捕捉系統音頻、轉錄韓語語音並顯示浮動字幕窗口 — 無需OBS插件、無需瀏覽器擴展。

---

## 功能特性

- **浮動字幕覆蓋層** — 透明tkinter窗口，始終置頂，可拖動
- **多個STT引擎** — SenseVoice-Small（本機GPU）或Groq Whisper（雲備用）
- **多個 Translation Engine** — Gemini、Claude、Google Translate、Ollama（本機）、NVIDIA NIM
- **按模式選擇引擎** — 為直播模式和剪輯模式配置不同的後端
- **主播資料庫** — 針對特定虛擬主播的內置少樣本提示集（스텔라이브 히나、릴파、챈나、MW:MEU）
- **持久 Cache** — SQLite with LRU 驅逐；重複句子零 API 代幣成本
- **Prompt Cache** — 直播模式下啟用 Anthropic Prompt Cache（降低代幣成本~90%）
- **暫停/繼續** — 空格鍵或切換按鈕凍結管道，無需關閉窗口
- **Tauri Dashboard** — 可選桌面 UI 用於實時配置編輯和 Cache 統計

---

## 系統要求

| 要求 | 說明 |
|------|------|
| Windows 10/11 | WASAPI迴環捕獲；不支持macOS/Linux |
| Python 3.11+ | 使用 `str \| None` 聯合語法 |
| 虛擬音頻線纜 | [VB-Cable](https://vb-audio.com/Cable/)（免費）或NVIDIA RTX Voice |
| GPU（可選） | SenseVoice本機STT需要；最少~2GB顯存 |
| Rust + Node.js | 僅構建 Tauri Dashboard 時需要 |

---

## 安裝步驟

### 1. 複製倉庫並建立虛擬環境

```bash
git clone https://github.com/your-username/live_translate.git
cd live_translate
python -m venv live-subtitle-env
live-subtitle-env\Scripts\activate
```

### 2. 安裝依賴

```bash
pip install -r requirements.txt
```

> 對於SenseVoice本機STT，需單獨安裝帶CUDA的PyTorch：
> ```bash
> pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
> ```

### 3. 設置API密鑰

將 `.env.example` 複製為 `.env` 並填入計畫使用的密鑰：

```bash
copy .env.example .env
```

```env
ANTHROPIC_API_KEY=...
GROQ_API_KEY=...
GEMINI_API_KEY=...
GOOGLE_TRANSLATE_API_KEY=...
NVIDIA_API_KEY=...
```

只需填入實際使用的引擎密鑰。至少需要一個翻譯引擎密鑰。

### 4. 配置音頻輸入

打開 `config.py` 並將 `device_name` 設定為虛擬音頻線纜輸出：

```python
@dataclass(frozen=True)
class _Audio:
    device_name: str = "CABLE Output"   # 與虛擬線纜名稱完全匹配
```

查看可用設備列表，執行：

```bash
python -c "import sounddevice; print(sounddevice.query_devices())"
```

---

## 使用方法

```bash
# 完整管道 — STT + 翻譯 + 字幕覆蓋層
live-subtitle-env\Scripts\python.exe main.py

# 僅STT模式 — 列印識別的句子，無翻譯（用於調試）
live-subtitle-env\Scripts\python.exe main.py --stt-only
```

### 字幕窗口控制

| 操作 | 效果 |
|------|------|
| `空格` 或切換按鈕 | 暫停/繼續管道 |
| 拖動 | 在螢幕上移動窗口 |
| `Esc` 或雙擊 | 退出 |

---

## Tauri Dashboard（可選）

Tauri Dashboard 是一個獨立桌面應用，可讓您控制 Pipeline 並檢查 Cache，無需接觸終端。

### 可用功能

| 功能 | 詳情 |
|------|------|
| 啟動/停止 Pipeline | 作為 Subprocess 啟動或終止 `main.py` |
| Config Editor | 編輯字幕外觀、翻譯設定和 STT 選項；更改寫入 `logs/live_translate_config.json` |
| Cache 統計 | 顯示總條目數、總 Cache 命中數、最後使用時間戳和 DB 文件大小 |
| 清空 Cache | 刪除翻譯資料庫中的所有行 |

> **注意：** 在 Dashboard 中所做的配置更改在**下次 Python 重啟**時生效 — 它們寫入 JSON 導出文件，不直接修改 `config.py`。

> **注意：** Dashboard 從 `logs/live_translate_config.json` 讀取配置，Python 啟動時會寫入此文件。請在執行 `main.py` 至少一次後打開 Dashboard，否則配置面板將顯示"找不到配置 — 請先執行 Python"錯誤。

### 前置要求

| 工具 | 說明 |
|------|------|
| [Rust](https://rustup.rs/) | 穩定版工具鏈；透過 `rustup` 安裝 |
| [Node.js](https://nodejs.org/) | 建議v18+ |

### 開發模式執行

```bash
# 1. 安裝前端依賴（僅首次）
cd src-frontend
npm install
cd ..

# 2. 啟動 Dashboard（啟動 Vite Dev Server + Tauri 窗口）
cd src-tauri
cargo tauri dev
```

Tauri 窗口在 `http://localhost:5173` 打開。Vue 組件更改時熱重載激活。

### 構建可分發的二進位檔案

```bash
cd src-tauri
cargo tauri build
```

安裝程式和獨立 `.exe` 檔案放在 `src-tauri/target/release/bundle/`。

---

## 配置

所有設定都在 [`config.py`](config.py) 中。最常改動的選項：

### 選擇翻譯引擎

```python
# 按模式選擇引擎："anthropic"（Gemini/Claude鏈）| "ollama" | "nvidia"
live_engine: str = "anthropic"   # 當 translation_mode = "live" 時使用
clip_engine: str = "nvidia"      # 當 translation_mode = "clip" 時使用
```

### 選擇翻譯模式

```python
translation_mode: str = "live"   # "live"（實時，較少保守）
                                 # "clip"（保持結構，更適合字幕剪輯）
```

### 選擇STT引擎

```python
primary_engine: str = "groq"          # "groq"（雲） | "sensevoice"（本機GPU）
groq_model:     str = "whisper-large-v3"
```

### 啟用主播資料庫

特定主播的少樣本示例可改進粉絲詞彙翻譯準確度：

```python
streamer_profile: str = "stellive_hina"   # 見下表
use_profile:      bool = True
```

| 資料庫密鑰 | 主播 |
|-----------|------|
| `"stellive_hina"` | 스텔라이브 시라유키 히나 |
| `"isegye_lilpa"` | 이세계아이돌 / 릴파 |
| `"hades_chxxnnx"` | HADES / 챈나 |
| `"mwmeu"` | MW:MEU |
| `""` | 通用（無資料庫） |

### 使用本機模型（Ollama）

```bash
ollama pull qwen2.5:3b   # 或ollama.com/library中的任何模型
ollama serve
```

```python
live_engine: str = "ollama"
# 在 _Ollama 中：
model: str = "qwen2.5:3b"
```

---

## Translation Engine 參考

| Engine | 需要密鑰 | 說明 |
|------|---------|------|
| Claude (Haiku / Sonnet) | `ANTHROPIC_API_KEY` | 直播模式下支持 Prompt Cache |
| Gemini Flash | `GEMINI_API_KEY` | 快速、經濟高效 |
| Google Translate v2 | `GOOGLE_TRANSLATE_API_KEY` | 無 LLM 上下文；最快的備用方案 |
| Ollama | — | 完全本機；需要 `ollama serve` 執行 |
| NVIDIA NIM | `NVIDIA_API_KEY` | 雲託管開源模型；免費套餐可用 |

`config.py` 中的 `engine_chain` 控制使用 `live_engine = "anthropic"` 時的備用順序。

---

## 專案結構

```
live_translate/
├── main.py                  # 入口點
├── config.py                # 所有執行時設定
├── .env                     # API密鑰（不提交）
├── .env.example             # 範本
├── modules/
│   ├── audio_capture.py     # WASAPI迴環 + VAD
│   ├── stt.py               # 語音識別（SenseVoice / Groq）
│   ├── sentence_splitter.py # 韓語句子分割
│   ├── translator.py        # 翻譯引擎 + 緩存
│   ├── db.py                # SQLite持久緩存
│   ├── subtitle_display.py  # 浮動tkinter覆蓋層
│   └── prompt_evolver.py    # 可選實時提示豐富
├── utils/
│   ├── logger.py            # Windows UTF-8日誌記錄器
│   ├── queue_utils.py       # drain_put助手
│   ├── api_retry.py         # 錯誤分類 + 退避策略
│   └── config_export.py     # 匯出配置到JSON供Tauri使用
├── src-tauri/               # Tauri Rust後端（可選儀表板）
├── src-frontend/            # Vue.js儀表板前端（可選）
├── tests/                   # 單元 + 整合測試
└── logs/                    # 翻譯歷史 + 資料庫（自動建立）
```

---

## 執行測試

```bash
live-subtitle-env\Scripts\python.exe -m pytest tests/ -q
```

---

## 授權條款

MIT
