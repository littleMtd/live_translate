use std::collections::HashMap;
use std::process::Child;
use std::sync::Mutex;

pub struct AppState {
    pub python_process: Mutex<Option<Child>>,
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

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct ConfigDto {
    pub audio: AudioConfig,
    pub stt: SttConfig,
    pub splitter: SplitterConfig,
    pub translation: TranslationConfig,
    pub subtitle: SubtitleConfig,
    pub database: DatabaseConfig,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct AudioConfig {
    pub sample_rate: u32,
    pub channels: u32,
    pub volume_threshold: f32,
    pub vad_enabled: bool,
    pub vad_silence_sec: f32,
    pub vad_min_speech_sec: f32,
    pub vad_max_speech_sec: f32,
    pub queue_maxsize: u32,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct SttConfig {
    pub primary_engine: String,
    pub language: String,
    pub queue_maxsize: u32,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct SplitterConfig {
    pub min_wait_seconds: u32,
    pub force_cut_seconds: u32,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct TranslationConfig {
    pub primary_engine: String,
    pub target_lang: String,
    pub max_tokens: u32,
    pub temperature: f32,
    pub queue_maxsize: u32,
    pub slang: HashMap<String, String>,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct SubtitleConfig {
    pub font_family: String,
    pub font_size: u32,
    pub font_style: String,
    pub idle_hide_ms: u32,
    pub alpha: f32,
    pub queue_maxsize: u32,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct DatabaseConfig {
    pub db_path: String,
    pub db_cache_max_rows: u32,
}

#[cfg(test)]
mod tests {
    use super::*;

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
                primary_engine: "sensevoice".into(),
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
                slang: HashMap::from([("ㅋㅋ".into(), "哈哈".into())]),
            },
            subtitle: SubtitleConfig {
                font_family: "Microsoft JhengHei".into(),
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
    fn appstate_initializes_empty() {
        let state = AppState::new();
        assert!(state.python_process.lock().unwrap().is_none());
        assert!(state.config_cache.lock().unwrap().is_none());
    }

    #[test]
    fn config_dto_serializes_to_json() {
        let cfg = sample_config();
        let json = serde_json::to_string(&cfg).unwrap();
        assert!(json.contains("zh-TW"));
        assert!(json.contains("gemini"));
    }

    #[test]
    fn config_dto_deserializes_from_json() {
        let cfg = sample_config();
        let json = serde_json::to_string(&cfg).unwrap();
        let cfg2: ConfigDto = serde_json::from_str(&json).unwrap();
        assert_eq!(cfg.stt.language, cfg2.stt.language);
        assert_eq!(cfg.subtitle.font_size, cfg2.subtitle.font_size);
        assert_eq!(cfg.audio.vad_enabled, cfg2.audio.vad_enabled);
        assert_eq!(cfg.database.db_cache_max_rows, cfg2.database.db_cache_max_rows);
    }

    #[test]
    fn slang_map_survives_json_roundtrip() {
        let cfg = sample_config();
        let json = serde_json::to_string(&cfg).unwrap();
        let cfg2: ConfigDto = serde_json::from_str(&json).unwrap();
        assert_eq!(cfg2.translation.slang.get("ㅋㅋ"), Some(&"哈哈".to_string()));
    }

    #[test]
    fn config_cache_can_be_set_and_read() {
        let state = AppState::new();
        *state.config_cache.lock().unwrap() = Some(sample_config());
        let cached = state.config_cache.lock().unwrap();
        assert!(cached.is_some());
        assert_eq!(cached.as_ref().unwrap().stt.primary_engine, "sensevoice");
    }

    #[test]
    fn config_cache_can_be_invalidated() {
        let state = AppState::new();
        *state.config_cache.lock().unwrap() = Some(sample_config());
        *state.config_cache.lock().unwrap() = None;
        assert!(state.config_cache.lock().unwrap().is_none());
    }
}
