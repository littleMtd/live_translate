# Frontend Design: Tauri + Rust

## Overview

This document describes the desktop application architecture using Tauri (Rust backend) + Vue.js (frontend).

**Target Audience:** Developers with C/systems programming background (e.g., CTF PWN experience).

**Scope:**
- Tauri app initialization and lifecycle
- Rust backend handlers for config, cache, and statistics
- Python ↔ Rust inter-process communication (IPC)
- Vue.js dashboard components
- Build and development workflow

---

## Architecture

### High-Level Flow

```
User (Vue Dashboard)
    ↓ (Tauri Command invoke)
Tauri Runtime
    ↓ (Rust handler)
Rust Backend (src-tauri)
    ├─ Config Handler → read/write config.json
    ├─ Cache Handler → query SQLite via subprocess
    └─ Python Bridge → spawn Python subprocess
    ↓ (stdout/stderr)
Python Main Process (main.py + modules/)
    ├─ Audio Capture
    ├─ STT
    ├─ Sentence Splitter
    ├─ Translator (writes SQLite cache)
    └─ Subtitle Display (tkinter overlay)
```

### Python Config Bridge

Tauri 無法直接讀 `config.py`（Python dataclass）。Python 在啟動時寫出 `logs/live_translate_config.json`，Tauri 讀這個檔案。API keys **永遠不寫入 JSON**，只存在 `.env`。

```
Python startup
  → utils/config_export.write()
  → logs/live_translate_config.json  ← Tauri reads/writes this file
  → .env                             ← API keys stay here only
```

Tauri 修改 config 後寫回同一 JSON 檔。Python 透過輪詢（或 `watchdog` 套件）偵測檔案變更並熱重載。

> **Tauri v2 注意**：本文件範例使用 Tauri v1 API（`@tauri-apps/api/tauri`）。
> Tauri v2 的 invoke 路徑改為 `@tauri-apps/api`，請在建立專案時確認版本。

### Directory Structure

```
live_translate/
├── src-tauri/                          # Rust backend (Tauri)
│   ├── Cargo.toml                      # Rust dependencies
│   ├── tauri.conf.json                 # Tauri config
│   ├── src/
│   │   ├── main.rs                     # Tauri app entry + command setup
│   │   ├── handlers/
│   │   │   ├── mod.rs                  # module exports
│   │   │   ├── config.rs               # Config API
│   │   │   ├── cache.rs                # Cache query API
│   │   │   ├── stats.rs                # Statistics API
│   │   │   └── python.rs               # Python subprocess bridge
│   │   ├── state.rs                    # Tauri State management
│   │   └── errors.rs                   # Error types
│   └── icons/
│
├── src-frontend/                       # Vue.js frontend
│   ├── public/
│   ├── src/
│   │   ├── App.vue                     # Root component
│   │   ├── main.ts                     # Vue entry
│   │   ├── components/
│   │   │   ├── ConfigPanel.vue         # Settings editor
│   │   │   ├── CacheStats.vue          # Cache hit/miss display
│   │   │   ├── SystemStats.vue         # CPU/memory/uptime
│   │   │   └── Dashboard.vue           # Main layout
│   │   ├── api/
│   │   │   └── client.ts               # Tauri command caller
│   │   └── types/
│   │       └── config.ts               # TypeScript interfaces
│   ├── package.json
│   └── vite.config.ts
│
├── main.py                             # (existing) Python entry
├── config.py                           # (existing, extended for DB path)
├── modules/                            # (existing) STT/translator/etc
├── utils/                              # (existing) logger/etc
├── logs/                               # SQLite cache location
└── frontend-design.md                  # This file
```

---

## Rust Backend Architecture

### Core Concept: Command Handlers

Tauri uses **Commands** to expose Rust functions to the frontend.

```rust
// When frontend calls: invoke('get_config', {})
// This Rust function runs:
#[tauri::command]
fn get_config() -> Result<ConfigDto, String> {
    // Read config.json
    // Return JSON to frontend
}
```

### 1. Main Entry Point (`src-tauri/src/main.rs`)

```rust
#![cfg_attr(all(not(debug_assertions), target_os = "windows"), windows_subsystem = "windows")]

mod handlers;
mod state;
mod errors;

use tauri::Manager;

fn main() {
    tauri::Builder::default()
        // Initialize shared state
        .manage(state::AppState::new())
        
        // Register all command handlers
        .invoke_handler(tauri::generate_handler![
            handlers::config::get_config,
            handlers::config::update_config,
            handlers::cache::get_cache_stats,
            handlers::cache::clear_cache,
            handlers::stats::get_system_stats,
            handlers::python::start_python,
            handlers::python::stop_python,
        ])
        
        // Setup
        .setup(|app| {
            // Spawn Python subprocess here or on-demand
            Ok(())
        })
        
        // Window closed handler
        .on_window_event(|event| {
            match event.event() {
                tauri::WindowEvent::CloseRequested { .. } => {
                    // Cleanup: stop Python, close DB, etc
                }
                _ => {}
            }
        })
        
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
```

### 2. State Management (`src-tauri/src/state.rs`)

```rust
use std::sync::Mutex;
use std::process::Child;

/// Shared mutable state accessible from all handlers
pub struct AppState {
    /// Reference to Python subprocess (if running)
    pub python_process: Mutex<Option<Child>>,
    /// Cache for config (avoid repeated file I/O)
    pub config_cache: Mutex<Option<ConfigDto>>,
}

impl AppState {
    pub fn new() -> Self {
        AppState {
            python_process: Mutex::new(None),
            config_cache: Mutex::new(None),
        }
    }
}

/// Full non-secret config mirroring Python's config.py.
/// API keys are never included — they stay in .env.
/// font tuple is split into font_family/font_size/font_style for JSON compat.
#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct ConfigDto {
    pub audio:       AudioConfig,
    pub stt:         SttConfig,
    pub splitter:    SplitterConfig,
    pub translation: TranslationConfig,
    pub subtitle:    SubtitleConfig,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct AudioConfig {
    pub sample_rate:        u32,
    pub channels:           u32,
    pub volume_threshold:   f32,
    pub vad_enabled:        bool,
    pub vad_silence_sec:    f32,
    pub vad_min_speech_sec: f32,
    pub vad_max_speech_sec: f32,
    pub queue_maxsize:      u32,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct SttConfig {
    pub primary_engine: String,   // "sensevoice" or "groq"
    pub language:       String,   // "ko"
    pub queue_maxsize:  u32,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct SplitterConfig {
    pub min_wait_seconds:  u32,
    pub force_cut_seconds: u32,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct TranslationConfig {
    pub engine_chain:   Vec<String>,  // e.g. ["google_translate", "gemini", "claude"]
    pub target_lang:    String,       // "zh-TW"
    pub max_tokens:     u32,
    pub temperature:    f32,
    pub queue_maxsize:  u32,
    pub slang:          std::collections::HashMap<String, String>,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct SubtitleConfig {
    pub font_family:  String,
    pub font_size:    u32,
    pub font_style:   String,
    pub idle_hide_ms: u32,
    pub alpha:        f32,
    pub queue_maxsize: u32,
}
```

### 3. Config Handler (`src-tauri/src/handlers/config.rs`)

```rust
use crate::state::{AppState, ConfigDto};
use tauri::State;
use std::fs;
use std::path::PathBuf;

/// Config JSON written by Python's `utils/config_export.py` on startup.
/// API keys are never included — they stay in .env.
fn config_path() -> PathBuf {
    // Resolve relative to the binary so it works regardless of CWD.
    let base = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."));
    base.join("logs").join("live_translate_config.json")
}

/// Read config. Python must call `utils/config_export.write()` before Tauri starts.
#[tauri::command]
pub fn get_config(state: State<AppState>) -> Result<ConfigDto, String> {
    let cache = state.config_cache.lock().unwrap();
    if let Some(config) = cache.as_ref() {
        return Ok(config.clone());
    }
    drop(cache);

    let path = config_path();
    let content = fs::read_to_string(&path)
        .map_err(|e| format!("Config not found at {:?} — run Python first: {}", path, e))?;

    let config: ConfigDto = serde_json::from_str(&content)
        .map_err(|e| format!("Invalid config JSON: {}", e))?;

    *state.config_cache.lock().unwrap() = Some(config.clone());
    Ok(config)
}

/// Write updated config back to JSON. Python polls the file and hot-reloads.
#[tauri::command]
pub fn update_config(new_config: ConfigDto, state: State<AppState>) -> Result<(), String> {
    if new_config.subtitle.font_size < 8 || new_config.subtitle.font_size > 48 {
        return Err("font_size must be 8–48".to_string());
    }
    if new_config.subtitle.alpha < 0.1 || new_config.subtitle.alpha > 1.0 {
        return Err("alpha must be 0.1–1.0".to_string());
    }
    if new_config.translation.max_tokens < 10 || new_config.translation.max_tokens > 500 {
        return Err("max_tokens must be 10–500".to_string());
    }

    let json = serde_json::to_string_pretty(&new_config)
        .map_err(|e| format!("Serialization error: {}", e))?;
    fs::write(config_path(), json)
        .map_err(|e| format!("Failed to write config: {}", e))?;

    *state.config_cache.lock().unwrap() = Some(new_config);
    Ok(())
}
```

**Key Rust Concepts:**
- `Mutex<T>` = thread-safe mutable state (like a lock)
- `State<T>` = Tauri injection of managed state
- `Result<T, E>` = Rust's error handling (like exception handling in Python)
- `.map_err()` = convert error types

### 4. Cache Handler (`src-tauri/src/handlers/cache.rs`)

Add `rusqlite` to `Cargo.toml` — no Python subprocess needed:

```toml
[dependencies]
rusqlite = { version = "0.31", features = ["bundled"] }
```

```rust
use std::path::{Path, PathBuf};
use rusqlite::Connection;

#[derive(serde::Serialize, serde::Deserialize)]
pub struct CacheStats {
    pub total_entries: u32,
    pub hit_count_sum: u32,
    pub last_used: String,
    pub db_size_mb: f32,
}

fn db_path() -> PathBuf {
    let base = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."));
    base.join("logs").join("live_translate.db")
}

/// Query SQLite cache stats directly via rusqlite — no Python subprocess.
#[tauri::command]
pub fn get_cache_stats() -> Result<CacheStats, String> {
    let path = db_path();

    if !path.exists() {
        return Ok(CacheStats {
            total_entries: 0,
            hit_count_sum: 0,
            last_used: "Never".to_string(),
            db_size_mb: 0.0,
        });
    }

    let conn = Connection::open(&path)
        .map_err(|e| format!("DB open error: {}", e))?;

    let (total, hit_sum, last_used): (u32, u32, String) = conn
        .query_row(
            "SELECT COUNT(*), COALESCE(SUM(hit_count), 0), \
             COALESCE(MAX(last_used_at), 'Never') FROM translations",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .map_err(|e| format!("Query error: {}", e))?;

    let db_size_mb = path
        .metadata()
        .map(|m| m.len() as f32 / 1024.0 / 1024.0)
        .unwrap_or(0.0);

    Ok(CacheStats { total_entries: total, hit_count_sum: hit_sum, last_used, db_size_mb })
}

/// Clear all persisted cache entries.
#[tauri::command]
pub fn clear_cache() -> Result<String, String> {
    let conn = Connection::open(db_path())
        .map_err(|e| format!("DB open error: {}", e))?;

    conn.execute("DELETE FROM translations", [])
        .map_err(|e| format!("Clear failed: {}", e))?;

    Ok("Cache cleared successfully".to_string())
}
```

### 5. Python Bridge (`src-tauri/src/handlers/python.rs`)

```rust
use crate::state::AppState;
use tauri::State;
use std::process::{Command, Stdio};
use std::io::{BufRead, BufReader};
use std::thread;

/// Resolve venv Python path relative to the binary.
/// Falls back to "python" if venv is not found (dev mode).
fn python_exe() -> PathBuf {
    let base = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."));
    let venv = base.join("live-subtitle-env").join("Scripts").join("python.exe");
    if venv.exists() { venv } else { PathBuf::from("python") }
}

/// Start Python main process in background
#[tauri::command]
pub fn start_python(state: State<AppState>) -> Result<String, String> {
    let mut proc = Command::new(python_exe())
        .arg("-u")  // ← CRITICAL: Unbuffered stdout/stderr
        .arg("main.py")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Failed to spawn Python: {}", e))?;
    
    let pid = proc.id();
    
    // Capture stdout in background thread
    if let Some(stdout) = proc.stdout.take() {
        thread::spawn(|| {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                if let Ok(line) = line {
                    println!("[Python] {}", line);  // Log to console
                }
            }
        });
    }
    
    *state.python_process.lock().unwrap() = Some(proc);
    
    Ok(format!("Python started (PID: {})", pid))
}

/// Stop Python process
#[tauri::command]
pub fn stop_python(state: State<AppState>) -> Result<String, String> {
    let mut proc_opt = state.python_process.lock().unwrap();
    
    if let Some(mut proc) = proc_opt.take() {
        proc.kill()
            .map_err(|e| format!("Failed to stop Python: {}", e))?;
        
        proc.wait()
            .map_err(|e| format!("Wait error: {}", e))?;
        
        Ok("Python process stopped".to_string())
    } else {
        Err("No Python process running".to_string())
    }
}

/// Helper: Call Python with unbuffered output
pub fn call_python_subprocess(script_path: &str, args: Vec<&str>) -> Result<String, String> {
    let mut cmd = Command::new(python_exe());
    cmd.arg("-u")  // ← Unbuffered
        .arg(script_path);
    
    for arg in args {
        cmd.arg(arg);
    }
    
    let output = cmd
        .output()
        .map_err(|e| format!("Python call failed: {}", e))?;
    
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).to_string());
    }
    
    String::from_utf8(output.stdout)
        .map_err(|e| format!("UTF-8 error: {}", e))
}
```

**Key Changes:**

1. **`python -u` flag** — Disables stdout/stderr buffering
   - Without: Python buffers output until line break or program exits
   - With: Output is sent immediately

2. **Applied to `start_python()`** — Main process runs unbuffered

3. **Applied to `call_python_subprocess()`** — Helper for executing Python scripts

**Python Side (Fallback if Rust side doesn't set `-u`):**

If you want Python to guarantee unbuffered output:

```python
# main.py
import sys
import io

# Force unbuffered stdout (fallback)
sys.stdout = io.TextIOWrapper(
    sys.stdout.buffer,
    encoding='utf-8',
    line_buffering=True
)

# Now all print() calls flush immediately
print("Hello", flush=True)
```

**Why This Matters:**

```
Without -u:
Rust spawns Python → waits for output → Python keeps buffer → Rust waits forever (or times out)

With -u:
Rust spawns Python → Python line prints → Rust sees it immediately
```

**Performance Note:**

`-u` adds ~1-2% CPU overhead but is negligible for this use case (not millions of I/O operations).

```

### 6. Error Handling (`src-tauri/src/errors.rs`)

```rust
use serde::Serialize;

#[derive(Serialize, Debug)]
pub enum AppError {
    ConfigError(String),
    DatabaseError(String),
    PythonError(String),
    IoError(String),
}

impl AppError {
    pub fn message(&self) -> String {
        match self {
            AppError::ConfigError(msg) => format!("Config error: {}", msg),
            AppError::DatabaseError(msg) => format!("Database error: {}", msg),
            AppError::PythonError(msg) => format!("Python error: {}", msg),
            AppError::IoError(msg) => format!("I/O error: {}", msg),
        }
    }
}
```

---

## Vue.js Frontend Architecture

### Project Setup

**Generate Tauri + Vue.js project:**
```bash
npm create tauri-app@latest live-translate -- --template vue-ts
cd live-translate/src-frontend
npm install
```

**Key Dependencies:**
```json
{
  "dependencies": {
    "@tauri-apps/api": "^1.5",
    "vue": "^3.3",
    "chart.js": "^4.4"
  },
  "devDependencies": {
    "typescript": "^5.0",
    "vite": "^4.0"
  }
}
```

### 1. API Client (`src-frontend/src/api/client.ts`)

```typescript
import { invoke } from '@tauri-apps/api/tauri';
import type { ConfigDto, CacheStats } from '../types/config';

export class TauriClient {
    async getConfig(): Promise<ConfigDto> {
        try {
            return await invoke('get_config');
        } catch (error) {
            throw new Error(`Config fetch failed: ${error}`);
        }
    }
    
    async updateConfig(config: ConfigDto): Promise<void> {
        try {
            await invoke('update_config', { newConfig: config });
        } catch (error) {
            throw new Error(`Config update failed: ${error}`);
        }
    }
    
    async getCacheStats(): Promise<CacheStats> {
        try {
            return await invoke('get_cache_stats');
        } catch (error) {
            throw new Error(`Cache stats fetch failed: ${error}`);
        }
    }
    
    async clearCache(): Promise<string> {
        try {
            return await invoke('clear_cache');
        } catch (error) {
            throw new Error(`Cache clear failed: ${error}`);
        }
    }
    
    async startPython(): Promise<string> {
        try {
            return await invoke('start_python');
        } catch (error) {
            throw new Error(`Python start failed: ${error}`);
        }
    }
    
    async stopPython(): Promise<string> {
        try {
            return await invoke('stop_python');
        } catch (error) {
            throw new Error(`Python stop failed: ${error}`);
        }
    }
}

export const client = new TauriClient();
```

### 2. Types (`src-frontend/src/types/config.ts`)

```typescript
export interface ConfigDto {
    audio: AudioConfig;
    stt: SttConfig;
    translation: TranslationConfig;
    subtitle: SubtitleConfig;
}

export interface AudioConfig {
    sample_rate: number;
    channels: number;
}

export interface SttConfig {
    primary_engine: 'sensevoice' | 'groq';
}

export interface TranslationConfig {
    engine_chain: string[];   // e.g. ["google_translate", "gemini", "claude"]
    max_tokens: number;
}

export interface SubtitleConfig {
    font_size: number;
    opacity: number;
    auto_hide_ms: number;
    position: 'top' | 'center' | 'bottom';
}

export interface CacheStats {
    total_entries: number;
    hit_count_sum: number;
    last_used: string;
    db_size_mb: number;
}
```

### 3. Dashboard Layout (`src-frontend/src/components/Dashboard.vue`)

```vue
<template>
  <div class="dashboard">
    <header>
      <h1>Live Translate Dashboard</h1>
      <div class="status">
        <span :class="{ online: pythonRunning }">{{ pythonRunning ? '● Online' : '● Offline' }}</span>
      </div>
    </header>
    
    <nav class="tabs">
      <button
        v-for="tab in tabs"
        :key="tab"
        :class="{ active: activeTab === tab }"
        @click="activeTab = tab"
      >
        {{ tab }}
      </button>
    </nav>
    
    <main>
      <ConfigPanel v-if="activeTab === 'Settings'" :config="config" @save="saveConfig" />
      <CacheStats v-if="activeTab === 'Cache'" :stats="cacheStats" @refresh="refreshCacheStats" />
      <SystemStats v-if="activeTab === 'Stats'" />
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue';
import { client } from '../api/client';
import ConfigPanel from './ConfigPanel.vue';
import CacheStats from './CacheStats.vue';
import SystemStats from './SystemStats.vue';
import type { ConfigDto, CacheStats as CacheStatsType } from '../types/config';

const activeTab = ref('Settings');
const tabs = ['Settings', 'Cache', 'Stats'];
const config = ref<ConfigDto | null>(null);
const cacheStats = ref<CacheStatsType | null>(null);
const pythonRunning = ref(false);
let refreshInterval: number;

onMounted(async () => {
  await loadConfig();
  await refreshCacheStats();
  await checkPythonStatus();
  
  // Refresh cache stats every 5 seconds
  refreshInterval = setInterval(refreshCacheStats, 5000);
});

onUnmounted(() => {
  clearInterval(refreshInterval);
});

const loadConfig = async () => {
  try {
    config.value = await client.getConfig();
  } catch (error) {
    console.error('Failed to load config:', error);
  }
};

const saveConfig = async (newConfig: ConfigDto) => {
  try {
    await client.updateConfig(newConfig);
    config.value = newConfig;
    alert('Config saved');
  } catch (error) {
    console.error('Failed to save config:', error);
  }
};

const refreshCacheStats = async () => {
  try {
    cacheStats.value = await client.getCacheStats();
  } catch (error) {
    console.error('Failed to refresh cache stats:', error);
  }
};

const checkPythonStatus = async () => {
  try {
    // If Python is running, it will have written the config JSON recently.
    // A successful get_config call is treated as "alive".
    await client.getConfig();
    pythonRunning.value = true;
  } catch {
    pythonRunning.value = false;
  }
};
</script>

<style scoped>
.dashboard {
  display: flex;
  flex-direction: column;
  height: 100vh;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
}

header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 20px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
}

.status {
  font-size: 14px;
}

.status.online { color: #4ade80; }

.tabs {
  display: flex;
  border-bottom: 1px solid #e5e7eb;
}

.tabs button {
  flex: 1;
  padding: 12px;
  background: none;
  border: none;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  border-bottom: 3px solid transparent;
}

.tabs button.active {
  border-bottom-color: #667eea;
  color: #667eea;
}

main {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}
</style>
```

### 4. Config Panel (`src-frontend/src/components/ConfigPanel.vue`)

```vue
<template>
  <div class="config-panel">
    <h2>Settings</h2>
    
    <div class="section">
      <h3>Subtitle Display</h3>
      <label>
        Font Size:
        <input v-model.number="localConfig.subtitle.font_size" type="range" min="8" max="48" />
        {{ localConfig.subtitle.font_size }}px
      </label>
      <label>
        Opacity:
        <input v-model.number="localConfig.subtitle.opacity" type="range" min="0.1" max="1" step="0.1" />
        {{ Math.round(localConfig.subtitle.opacity * 100) }}%
      </label>
      <label>
        Position:
        <select v-model="localConfig.subtitle.position">
          <option value="top">Top</option>
          <option value="center">Center</option>
          <option value="bottom">Bottom</option>
        </select>
      </label>
    </div>
    
    <div class="section">
      <h3>Translation Engine</h3>
      <label>
        Engine Chain (comma-separated, first = primary):
        <input v-model="engineChainInput" placeholder="google_translate,gemini,claude" />
      </label>
      <label>
        Max Tokens:
        <input v-model.number="localConfig.translation.max_tokens" type="number" min="10" max="200" />
      </label>
    </div>
    
    <div class="actions">
      <button @click="saveSettings" class="primary">Save</button>
      <button @click="resetSettings">Cancel</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import type { ConfigDto } from '../types/config';

interface Props {
  config: ConfigDto | null;
}

interface Emits {
  save: [ConfigDto];
}

const props = defineProps<Props>();
const emit = defineEmits<Emits>();

// Deep-clone helper; returns a safe default when config hasn't loaded yet.
const cloneOrDefault = (c: ConfigDto | null): ConfigDto =>
  c ? JSON.parse(JSON.stringify(c)) : {
    audio: { sample_rate: 16000, channels: 1, volume_threshold: 0.01,
             vad_enabled: true, vad_silence_sec: 0.6,
             vad_min_speech_sec: 0.4, vad_max_speech_sec: 8.0, queue_maxsize: 10 },
    stt: { primary_engine: 'groq', language: 'ko', queue_maxsize: 20 },
    splitter: { min_wait_seconds: 3, force_cut_seconds: 8 },
    translation: { engine_chain: ['google_translate', 'gemini', 'claude'], target_lang: 'zh-TW',
                   max_tokens: 150, temperature: 0.0, queue_maxsize: 2, slang: {} },
    subtitle: { font_family: 'Microsoft JhengHei', font_size: 22, font_style: 'bold',
                idle_hide_ms: 30000, alpha: 0.82, queue_maxsize: 10 },
  };

const localConfig = ref<ConfigDto>(cloneOrDefault(props.config));

watch(() => props.config, (newConfig) => {
  localConfig.value = cloneOrDefault(newConfig);
}, { deep: true });

const saveSettings = () => {
  emit('save', localConfig.value);
};

const resetSettings = () => {
  localConfig.value = JSON.parse(JSON.stringify(props.config));
};
</script>

<style scoped>
.config-panel {
  max-width: 600px;
}

.section {
  margin: 20px 0;
  padding: 15px;
  background: #f3f4f6;
  border-radius: 8px;
}

h3 {
  margin-top: 0;
}

label {
  display: block;
  margin: 10px 0;
  font-size: 14px;
}

input, select {
  margin-left: 10px;
  padding: 6px;
  border: 1px solid #d1d5db;
  border-radius: 4px;
}

.actions {
  margin-top: 20px;
  display: flex;
  gap: 10px;
}

button {
  padding: 10px 20px;
  font-size: 14px;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}

.primary {
  background: #667eea;
  color: white;
}

.primary:hover {
  background: #5568d3;
}
</style>
```

### 5. Cache Stats Component (`src-frontend/src/components/CacheStats.vue`)

```vue
<template>
  <div class="cache-stats">
    <h2>Translation Cache</h2>
    
    <div v-if="stats" class="stats-grid">
      <div class="stat-card">
        <div class="stat-value">{{ stats.total_entries }}</div>
        <div class="stat-label">Total Entries</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.hit_count_sum }}</div>
        <div class="stat-label">Total Hits</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.db_size_mb.toFixed(2) }}</div>
        <div class="stat-label">DB Size (MB)</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.last_used }}</div>
        <div class="stat-label">Last Used</div>
      </div>
    </div>
    
    <div class="actions">
      <button @click="$emit('refresh')" class="secondary">Refresh</button>
      <button @click="clearCache" class="danger">Clear Cache</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { CacheStats } from '../types/config';

interface Props {
  stats: CacheStats | null;
}

interface Emits {
  refresh: [];
}

defineProps<Props>();
defineEmits<Emits>();

const clearCache = async () => {
  if (!confirm('清除所有快取記錄？此操作無法復原。')) return;
  try {
    const msg = await client.clearCache();
    alert(msg);
    await refreshCacheStats();
  } catch (error) {
    alert(`清除失敗：${error}`);
  }
};
</script>

<style scoped>
.cache-stats {
  max-width: 800px;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 15px;
  margin: 20px 0;
}

.stat-card {
  padding: 20px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  border-radius: 8px;
  text-align: center;
}

.stat-value {
  font-size: 24px;
  font-weight: bold;
}

.stat-label {
  font-size: 12px;
  margin-top: 8px;
  opacity: 0.9;
}

.actions {
  display: flex;
  gap: 10px;
  margin-top: 20px;
}

button {
  padding: 10px 20px;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}

.secondary {
  background: #e5e7eb;
  color: #1f2937;
}

.danger {
  background: #ef4444;
  color: white;
}
</style>
```

---

## Development Workflow

### Build and Run

**Prerequisites:**
```bash
# Install Node.js (LTS)
# Install Rust: https://rustup.rs/

# Verify installations
node --version      # v18+
npm --version       # 9+
rustc --version     # 1.70+
cargo --version     # 1.70+
```

**Initial Setup:**
```bash
cd live_translate
npm install         # Install Node deps
cd src-tauri
cargo build         # Download Rust deps
cd ..
```

**Development (Hot Reload):**
```bash
# Terminal 1: Rust backend (auto-recompile on change)
cargo tauri dev

# Frontend auto-refreshes on file change
# Open http://localhost:3000 in browser (auto-opened)
```

**Production Build:**
```bash
cargo tauri build   # Outputs .exe in src-tauri/target/release/
```

### Debugging Tips

**Rust:**
```bash
# View compilation errors
cargo check

# Run tests
cargo test

# Format code
cargo fmt

# Lint
cargo clippy
```

**Vue/TypeScript:**
```bash
# Check types
npm run type-check

# Lint
npm run lint
```

**IPC Communication:**
```javascript
// In Vue devtools, open console:
import { invoke } from '@tauri-apps/api/tauri';

// Test command
await invoke('get_config').then(r => console.log(r));
```

---

## Performance Considerations (for CTF / Systems People)

### Memory Management

**Rust's Ownership Model** (similar to C++ RAII):
```rust
// Python process automatically killed when `proc` goes out of scope
{
    let proc = Command::new("python").spawn()?;
    // do stuff with proc
    // proc is dropped here → SIGTERM sent
}
```

### Thread Safety

**Mutex prevents data races:**
```rust
// Safe even if accessed from multiple threads
let state = Mutex::new(config);
let mut guard = state.lock().unwrap();  // Blocks until lock acquired
*guard = new_config;  // Modify
// lock automatically released when guard is dropped
```

### Subprocess IPC Optimization

**Current:** Python subprocess via stdout (text-based, slow)

**Future:** Binary protocol (gRPC or MessagePack)
```rust
// Example: Use MessagePack instead of JSON for faster serialization
use rmp_serde;

let config = ConfigDto { ... };
let encoded = rmp_serde::to_vec(&config)?;  // ~10x faster than JSON
```

---

## Future Enhancements

1. **Multi-language Support** — i18n for Vue components
2. **Real-time Metrics** — WebSocket for live subtitle stats
3. **Plugin System** — Allow third-party handlers
4. **Custom Themes** — Dark mode, font schemes
5. **CLI Companion** — Tauri command-line tool for headless mode

---

## References

- [Tauri Documentation](https://tauri.app/)
- [Rust Book](https://doc.rust-lang.org/book/)
- [Vue 3 Guide](https://vuejs.org/)
- [serde (Rust serialization)](https://serde.rs/)
- [SQLite3 (Rust binding)](https://github.com/rusqlite/rusqlite)

