use crate::state::{AppState, ConfigDto};
use std::fs;
use std::path::PathBuf;
use tauri::State;

fn app_root() -> PathBuf {
    let base = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."));

    // Dev mode: .../live_translate/src-tauri/target/debug/<app>.exe
    // We need the project root: .../live_translate
    if let Some(project_root) = base.ancestors().nth(3) {
        let root = project_root.to_path_buf();
        if root.join("main.py").exists() {
            return root;
        }
    }

    // Packaged mode: binary sits next to app files.
    if base.join("main.py").exists() || base.join("logs").exists() {
        return base;
    }

    base
}

fn config_path() -> PathBuf {
    app_root()
        .join("logs")
        .join("live_translate_config.json")
}

pub(crate) fn validate_config(cfg: &ConfigDto) -> Result<(), String> {
    if cfg.subtitle.font_size < 8 || cfg.subtitle.font_size > 48 {
        return Err("font_size must be 8–48".to_string());
    }
    if cfg.subtitle.alpha < 0.1 || cfg.subtitle.alpha > 1.0 {
        return Err("alpha must be 0.1–1.0".to_string());
    }
    if cfg.translation.max_tokens < 10 || cfg.translation.max_tokens > 500 {
        return Err("max_tokens must be 10–500".to_string());
    }
    Ok(())
}

pub(crate) fn parse_config(json: &str) -> Result<ConfigDto, String> {
    serde_json::from_str(json).map_err(|e| format!("Invalid config JSON: {}", e))
}

#[tauri::command]
pub fn get_config(state: State<AppState>) -> Result<ConfigDto, String> {
    {
        let cache = state.config_cache.lock().unwrap();
        if let Some(config) = cache.as_ref() {
            return Ok(config.clone());
        }
    }

    let path = config_path();
    let content = fs::read_to_string(&path)
        .map_err(|e| format!("Config not found at {:?} — run Python first: {}", path, e))?;

    let config = parse_config(&content)?;
    *state.config_cache.lock().unwrap() = Some(config.clone());
    Ok(config)
}

#[tauri::command]
pub fn update_config(new_config: ConfigDto, state: State<AppState>) -> Result<(), String> {
    validate_config(&new_config)?;

    let json = serde_json::to_string_pretty(&new_config)
        .map_err(|e| format!("Serialization error: {}", e))?;
    fs::write(config_path(), json).map_err(|e| format!("Failed to write config: {}", e))?;

    *state.config_cache.lock().unwrap() = Some(new_config);
    Ok(())
}

#[tauri::command]
pub fn reload_config(state: State<AppState>) -> Result<ConfigDto, String> {
    *state.config_cache.lock().unwrap() = None;
    get_config(state)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::*;
    use std::collections::HashMap;

    fn sample_config() -> ConfigDto {
        ConfigDto {
            audio: AudioConfig {
                sample_rate: 16000,
                channels: 1,
                volume_threshold: 0.01,
                vad_enabled: true,
                vad_silence_sec: 0.6,
                vad_min_speech_sec: 0.4,
                vad_max_speech_sec: 8.0,
                queue_maxsize: 10,
            },
            stt: SttConfig {
                primary_engine: "groq".into(),
                language: "ko".into(),
                queue_maxsize: 20,
            },
            splitter: SplitterConfig {
                min_wait_seconds: 3,
                force_cut_seconds: 8,
            },
            translation: TranslationConfig {
                primary_engine: "gemini".into(),
                target_lang: "zh-TW".into(),
                max_tokens: 80,
                temperature: 0.0,
                queue_maxsize: 2,
                slang: HashMap::new(),
            },
            subtitle: SubtitleConfig {
                font_family: "Arial".into(),
                font_size: 22,
                font_style: "bold".into(),
                idle_hide_ms: 30000,
                alpha: 0.82,
                queue_maxsize: 10,
            },
            database: DatabaseConfig {
                db_path: "logs/live_translate.db".into(),
                db_cache_max_rows: 50000,
            },
        }
    }

    #[test]
    fn valid_config_passes_validation() {
        assert!(validate_config(&sample_config()).is_ok());
    }

    #[test]
    fn font_size_below_minimum_fails() {
        let mut cfg = sample_config();
        cfg.subtitle.font_size = 7;
        assert!(validate_config(&cfg).is_err());
    }

    #[test]
    fn font_size_above_maximum_fails() {
        let mut cfg = sample_config();
        cfg.subtitle.font_size = 49;
        let err = validate_config(&cfg).unwrap_err();
        assert!(err.contains("font_size"));
    }

    #[test]
    fn font_size_at_boundaries_passes() {
        let mut cfg = sample_config();
        cfg.subtitle.font_size = 8;
        assert!(validate_config(&cfg).is_ok());
        cfg.subtitle.font_size = 48;
        assert!(validate_config(&cfg).is_ok());
    }

    #[test]
    fn alpha_below_minimum_fails() {
        let mut cfg = sample_config();
        cfg.subtitle.alpha = 0.05;
        let err = validate_config(&cfg).unwrap_err();
        assert!(err.contains("alpha"));
    }

    #[test]
    fn alpha_above_maximum_fails() {
        let mut cfg = sample_config();
        cfg.subtitle.alpha = 1.1;
        assert!(validate_config(&cfg).is_err());
    }

    #[test]
    fn max_tokens_below_minimum_fails() {
        let mut cfg = sample_config();
        cfg.translation.max_tokens = 5;
        let err = validate_config(&cfg).unwrap_err();
        assert!(err.contains("max_tokens"));
    }

    #[test]
    fn max_tokens_above_maximum_fails() {
        let mut cfg = sample_config();
        cfg.translation.max_tokens = 501;
        assert!(validate_config(&cfg).is_err());
    }

    #[test]
    fn max_tokens_at_boundaries_passes() {
        let mut cfg = sample_config();
        cfg.translation.max_tokens = 10;
        assert!(validate_config(&cfg).is_ok());
        cfg.translation.max_tokens = 500;
        assert!(validate_config(&cfg).is_ok());
    }

    #[test]
    fn parse_config_invalid_json_returns_error() {
        let result = parse_config("not valid json");
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("Invalid config JSON"));
    }

    #[test]
    fn parse_config_empty_string_returns_error() {
        assert!(parse_config("").is_err());
    }

    #[test]
    fn config_roundtrips_through_json() {
        let cfg = sample_config();
        let json = serde_json::to_string(&cfg).unwrap();
        let cfg2 = parse_config(&json).unwrap();
        assert_eq!(cfg.subtitle.font_size, cfg2.subtitle.font_size);
        assert_eq!(cfg.translation.primary_engine, cfg2.translation.primary_engine);
        assert_eq!(cfg.stt.language, cfg2.stt.language);
    }

    #[test]
    fn slang_map_preserved_in_roundtrip() {
        let mut cfg = sample_config();
        cfg.translation.slang.insert("ㅋㅋ".into(), "哈哈".into());
        let json = serde_json::to_string(&cfg).unwrap();
        let cfg2 = parse_config(&json).unwrap();
        assert_eq!(cfg2.translation.slang.get("ㅋㅋ"), Some(&"哈哈".to_string()));
    }
}
