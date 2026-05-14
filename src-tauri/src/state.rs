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
    pub live_engine: String,
    pub clip_engine: String,
    pub ollama: OllamaConfig,
    pub nvidia: NvidiaConfig,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct AudioConfig {
    pub sample_rate: u32,
    pub channels: u32,
    pub chunk_seconds: u32,
    pub device_name: String,
    pub volume_threshold: f32,
    pub vad_enabled: bool,
    pub vad_silence_sec: f32,
    pub vad_min_speech_sec: f32,
    pub vad_max_speech_sec: f32,
    pub vad_silero_threshold: f32,
    pub queue_maxsize: u32,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct SttConfig {
    pub primary_engine: String,
    pub sensevoice_model: String,
    pub sensevoice_device: String,
    pub groq_model: String,
    pub language: String,
    pub groq_prompt: String,
    pub batch_size_s: u32,
    pub queue_maxsize: u32,
    pub no_speech_threshold: f32,
    pub avg_logprob_threshold: f32,
    pub max_japanese_chars: u32,
    pub max_repeat_ratio: f32,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct SplitterConfig {
    pub min_wait_seconds: u32,
    pub force_cut_seconds: u32,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct TranslationConfig {
    pub engine_chain: Vec<String>,
    pub model: String,
    pub gemini_model: String,
    pub google_translate_lang: String,
    pub target_lang: String,
    pub max_tokens: u32,
    pub temperature: f32,
    pub queue_maxsize: u32,
    pub context_window: u32,
    pub translation_mode: String,
    pub streamer_profile: String,
    pub use_profile: bool,
    pub evolve_enabled: bool,
    pub evolve_every: u32,
    pub slang: HashMap<String, String>,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct SubtitleConfig {
    pub idle_hide_ms: u32,
    pub font_family: String,
    pub font_size: u32,
    pub font_style: String,
    pub bg: String,
    pub ctrl_bg: String,
    pub fg: String,
    pub outline_color: String,
    pub outline_width: u32,
    pub alpha: f32,
    pub max_width_chars: u32,
    pub wraplength: u32,
    pub padx: u32,
    pub pady: u32,
    pub init_offset_x: i32,
    pub init_offset_y: i32,
    pub poll_interval_ms: u32,
    pub min_display_ms: u32,
    pub ms_per_char: u32,
    pub queue_maxsize: u32,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct DatabaseConfig {
    pub db_path: String,
    pub db_cache_max_rows: u32,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct OllamaConfig {
    pub base_url: String,
    pub model: String,
    pub timeout: u32,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct NvidiaConfig {
    pub model: String,
    pub timeout: u32,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_config() -> ConfigDto {
        ConfigDto {
            audio: AudioConfig {
                sample_rate: 16000,
                channels: 1,
                chunk_seconds: 3,
                device_name: "CABLE Output".into(),
                volume_threshold: 0.01,
                vad_enabled: true,
                vad_silence_sec: 0.6,
                vad_min_speech_sec: 0.4,
                vad_max_speech_sec: 8.0,
                vad_silero_threshold: 0.5,
                queue_maxsize: 10,
            },
            stt: SttConfig {
                primary_engine: "sensevoice".into(),
                sensevoice_model: "iic/SenseVoiceSmall".into(),
                sensevoice_device: "cuda".into(),
                groq_model: "whisper-large-v3".into(),
                language: "ko".into(),
                groq_prompt: String::new(),
                batch_size_s: 60,
                queue_maxsize: 20,
                no_speech_threshold: 0.6,
                avg_logprob_threshold: -1.0,
                max_japanese_chars: 2,
                max_repeat_ratio: 0.7,
            },
            splitter: SplitterConfig {
                min_wait_seconds: 3,
                force_cut_seconds: 8,
            },
            translation: TranslationConfig {
                engine_chain: vec!["claude".into(), "gemini".into(), "google_translate".into()],
                model: "claude-sonnet-4-6".into(),
                gemini_model: "gemini-2.5-flash".into(),
                google_translate_lang: "zh-TW".into(),
                target_lang: "zh-TW".into(),
                max_tokens: 80,
                temperature: 0.0,
                queue_maxsize: 2,
                context_window: 10,
                translation_mode: "live".into(),
                streamer_profile: "hades_chxxnnx".into(),
                use_profile: true,
                evolve_enabled: false,
                evolve_every: 20,
                slang: HashMap::from([("ㅋㅋ".into(), "哈哈".into())]),
            },
            subtitle: SubtitleConfig {
                idle_hide_ms: 30000,
                font_family: "Microsoft JhengHei".into(),
                font_size: 22,
                font_style: "bold".into(),
                bg: "#010101".into(),
                ctrl_bg: "#1a1a1a".into(),
                fg: "#FFFFFF".into(),
                outline_color: "#000000".into(),
                outline_width: 2,
                alpha: 0.82,
                max_width_chars: 36,
                wraplength: 700,
                padx: 16,
                pady: 8,
                init_offset_x: 400,
                init_offset_y: 160,
                poll_interval_ms: 100,
                min_display_ms: 1500,
                ms_per_char: 80,
                queue_maxsize: 10,
            },
            database: DatabaseConfig {
                db_path: "logs/live_translate.db".into(),
                db_cache_max_rows: 50000,
            },
            live_engine: "nvidia".into(),
            clip_engine: "nvidia".into(),
            ollama: OllamaConfig {
                base_url: "http://localhost:11434".into(),
                model: "qwen2.5:3b".into(),
                timeout: 60,
            },
            nvidia: NvidiaConfig {
                model: "qwen/qwen3.5-122b-a10b".into(),
                timeout: 60,
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
        assert_eq!(
            cfg.database.db_cache_max_rows,
            cfg2.database.db_cache_max_rows
        );
    }

    #[test]
    fn slang_map_survives_json_roundtrip() {
        let cfg = sample_config();
        let json = serde_json::to_string(&cfg).unwrap();
        let cfg2: ConfigDto = serde_json::from_str(&json).unwrap();
        assert_eq!(
            cfg2.translation.slang.get("ㅋㅋ"),
            Some(&"哈哈".to_string())
        );
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
