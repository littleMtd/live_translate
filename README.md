# live_translate

即時韓語 → 繁體中文字幕疊加，適用於直播。

擷取系統音訊、轉錄韓語語音並顯示浮動字幕視窗 — 不需 OBS 插件，不需瀏覽器擴充功能。

---

## 功能

- **浮動字幕疊加** — 透明 tkinter 視窗，始終置頂，可自由拖移
- **多個 STT 引擎** — SenseVoice-Small（本機 GPU）或 Groq Whisper（雲端備用）
- **多個翻譯引擎** — Gemini、Claude、Google Translate、Ollama（本機）、NVIDIA NIM
- **依模式選擇引擎** — 直播模式與剪輯模式可各自設定不同後端
- **主播 Profile** — 針對特定 VTuber 的內建 Few-shot 提示組（스텔라이브 히나、릴파、챈나、MW:MEU）
- **持久化快取** — SQLite 搭配 LRU 淘汰；重複句子不消耗多餘 API Token
- **Prompt Cache** — 直播模式啟用 Anthropic Prompt Cache（可降低約 90% Token 費用）
- **Tauri Dashboard** — 選用的桌面 UI，可即時編輯設定並查看快取統計(還沒做好，等更新)

---

## 系統需求

| 需求 | 說明 |
|------|------|
| Windows 10/11 | WASAPI 迴環擷取；不支援 macOS / Linux |
| Python 3.11+ | 使用 `str \| None` 聯合型別語法 |
| 虛擬音訊線 | [VB-Cable](https://vb-audio.com/Cable/)（免費）或 NVIDIA RTX Voice |
| GPU（選用） | SenseVoice 本機 STT 需要；最少約 2 GB 顯示記憶體 |
| Rust + Node.js | 僅打包 Tauri Dashboard 時需要 |

---

## 安裝步驟

### 1. Clone 專案並建立虛擬環境

```bash
git clone https://github.com/littleMtd/live_translate.git
cd live_translate
python -m venv live-subtitle-env
live-subtitle-env\Scripts\activate
```

### 2. 安裝依賴套件

```bash
pip install -r requirements.txt
```

> 若要使用 SenseVoice 本機 STT，需另行安裝支援 CUDA 的 PyTorch：
> ```bash
> pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
> ```

### 3. 設定 API 金鑰

將 `.env.example` 複製為 `.env` 並填入要使用的金鑰：

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

只需填入實際使用的引擎金鑰。至少需要一個翻譯引擎的金鑰。

### 4. 設定音訊輸入

開啟 `config.py`，將 `device_name` 設定為虛擬音訊線的輸出裝置名稱：

```python
@dataclass(frozen=True)
class _Audio:
    device_name: str = "CABLE Output"   # 與裝置名稱完全一致
```

列出可用裝置，執行：

```bash
python -c "import sounddevice; print(sounddevice.query_devices())"
```

---

## 使用方法

```bash
# 完整 Pipeline — STT + 翻譯 + 字幕疊加
live-subtitle-env\Scripts\python.exe main.py

# 僅 STT 模式 — 印出識別到的句子，不進行翻譯（適合調整 STT 參數）
live-subtitle-env\Scripts\python.exe main.py --stt-only
```

### 字幕視窗操作

| 操作 | 效果 |
|------|------|
| `空白鍵` 或切換按鈕 | 暫停 / 繼續 Pipeline |
| 拖移 | 在螢幕上移動視窗位置 |
| `Esc` 或雙擊 | 離開 |

---

## Tauri Dashboard（選用）

Tauri Dashboard 是獨立的桌面應用程式，讓你不需開啟終端機就能控制 Pipeline 並查看快取狀態。

### 可用功能

| 功能 | 說明 |
|------|------|
| 啟動 / 停止 Pipeline | 以子程序方式啟動或終止 `main.py` |
| 設定編輯器 | 調整字幕外觀、翻譯設定與 STT 選項；變更會寫入 `logs/live_translate_config.json` |
| 快取統計 | 顯示總筆數、總命中次數、最後使用時間與 DB 檔案大小 |
| 清空快取 | 刪除翻譯資料庫中的所有資料 |

> **注意：** 在 Dashboard 中修改的設定會在**下次重新啟動 Python** 後生效 — 變更寫入的是 JSON 匯出檔，不會直接修改 `config.py`。

> **注意：** Dashboard 讀取 `logs/live_translate_config.json`，這個檔案由 Python 啟動時自動產生。請先執行一次 `main.py` 再開啟 Dashboard，否則設定面板會顯示「找不到設定檔 — 請先執行 Python」的錯誤。

### 前置需求

| 工具 | 說明 |
|------|------|
| [Rust](https://rustup.rs/) | 穩定版工具鏈；透過 `rustup` 安裝 |
| [Node.js](https://nodejs.org/) | 建議 v18 以上 |

### 以開發模式執行

```bash
# 1. 安裝前端依賴（僅首次需要）
cd src-frontend
npm install
cd ..

# 2. 啟動 Dashboard（同時啟動 Vite Dev Server 與 Tauri 視窗）
cd src-tauri
cargo tauri dev
```

Tauri 視窗會在 `http://localhost:5173` 開啟。修改 Vue 元件後會自動熱重載。

### 打包發行版本

```bash
cd src-tauri
cargo tauri build
```

安裝程式與獨立的 `.exe` 檔案會產生在 `src-tauri/target/release/bundle/`。

---

## 設定說明

所有設定都在 [`config.py`](config.py) 中。最常調整的選項：

### 選擇翻譯引擎

```python
# 依模式選擇引擎："anthropic"（Gemini/Claude 鏈）| "ollama" | "nvidia"
live_engine: str = "anthropic"   # translation_mode = "live" 時使用
clip_engine: str = "nvidia"      # translation_mode = "clip" 時使用
```

### 選擇翻譯模式

```python
translation_mode: str = "live"   # "live"（即時，較靈活）
                                 # "clip"（保持結構，適合製作字幕剪輯）
```

### 選擇 STT 引擎

```python
primary_engine: str = "groq"          # "groq"（雲端）| "sensevoice"（本機 GPU）
groq_model:     str = "whisper-large-v3"
```

### 啟用主播 Profile

載入特定主播的 Few-shot 範例，可提升粉絲用語的翻譯準確度：

```python
streamer_profile: str = "stellive_hina"   # 見下表
use_profile:      bool = True
```

| Profile 代碼 | 主播 |
|-------------|------|
| `"stellive_hina"` | 스텔라이브 시라유키 히나 |
| `"isegye_lilpa"` | 이세계아이돌 / 릴파 |
| `"hades_chxxnnx"` | HADES / 챈나 |
| `"mwmeu"` | MW:MEU |
| `""` | 通用（不套用 Profile） |

### 使用本機模型（Ollama）

```bash
ollama pull qwen2.5:3b   # 或 ollama.com/library 中的其他模型
ollama serve
```

```python
live_engine: str = "ollama"
# 在 _Ollama 中：
model: str = "qwen2.5:3b"
```

---

## 翻譯引擎一覽

| 引擎 | 需要金鑰 | 說明 |
|------|---------|------|
| Claude (Haiku / Sonnet) | `ANTHROPIC_API_KEY` | 直播模式支援 Prompt Cache |
| Gemini Flash | `GEMINI_API_KEY` | 速度快、費用低 |
| Google Translate v2 | `GOOGLE_TRANSLATE_API_KEY` | 無 LLM 語境；最快的備用方案 |
| Ollama | — | 完全本機執行；需先啟動 `ollama serve` |
| NVIDIA NIM | `NVIDIA_API_KEY` | 雲端托管開源模型；有免費方案 |

`config.py` 中的 `engine_chain` 控制 `live_engine = "anthropic"` 時的備用順序。

---

## 專案結構

```
live_translate/
├── main.py                  # 程式進入點
├── config.py                # 所有執行期設定
├── .env                     # API 金鑰（不提交至版本控制）
├── .env.example             # 金鑰範本
├── modules/
│   ├── audio_capture.py     # WASAPI 迴環擷取 + VAD
│   ├── stt.py               # 語音轉文字（SenseVoice / Groq）
│   ├── sentence_splitter.py # 韓語句子切割
│   ├── translator.py        # 翻譯引擎 + 快取
│   ├── db.py                # SQLite 持久化快取
│   ├── subtitle_display.py  # 浮動 tkinter 疊加視窗
│   └── prompt_evolver.py    # 動態提示增強（選用）
├── utils/
│   ├── logger.py            # Windows UTF-8 日誌
│   ├── queue_utils.py       # drain_put 工具函數
│   ├── api_retry.py         # 錯誤分類 + 退避重試
│   └── config_export.py     # 將設定匯出為 JSON 供 Tauri 使用
├── src-tauri/               # Tauri Rust 後端（選用 Dashboard）
├── src-frontend/            # Vue.js Dashboard 前端（選用）
├── tests/                   # 單元測試 + 整合測試
└── logs/                    # 翻譯紀錄 + DB（自動建立）
```

---

## 執行測試

```bash
live-subtitle-env\Scripts\python.exe -m pytest tests/ -q
```

---

## 授權條款

MIT
