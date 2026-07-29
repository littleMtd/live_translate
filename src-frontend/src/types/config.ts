export interface ConfigDto {
  audio: AudioConfig
  stt: SttConfig
  splitter: SplitterConfig
  translation: TranslationConfig
  scene: SceneConfig
  subtitle: SubtitleConfig
  database: DatabaseConfig
  live_engine: BackendEngine
  clip_engine: BackendEngine
  ollama: OllamaConfig
  nvidia: NvidiaConfig
}

export type BackendEngine = 'anthropic' | 'ollama' | 'nvidia'
export type TranslationEngine = 'claude' | 'google_translate' | 'ollama' | 'nvidia' | 'openrouter' | 'groq'

export interface AudioConfig {
  sample_rate: number
  channels: number
  chunk_seconds: number
  device_name: string
  volume_threshold: number
  vad_enabled: boolean
  vad_silence_sec: number
  vad_min_speech_sec: number
  vad_max_speech_sec: number
  vad_silero_threshold: number
  queue_maxsize: number
}

export interface SttConfig {
  primary_engine: 'sensevoice' | 'groq'
  sensevoice_model: string
  sensevoice_device: string
  groq_model: string
  language: string
  groq_prompt: string
  batch_size_s: number
  queue_maxsize: number
  no_speech_threshold: number
  avg_logprob_threshold: number
  max_japanese_chars: number
  max_repeat_ratio: number
}

export interface SplitterConfig {
  min_wait_seconds: number
  force_cut_seconds: number
}

export interface TranslationConfig {
  engine_chain: TranslationEngine[]
  model: string
  google_translate_lang: string
  target_lang: string
  max_tokens: number
  temperature: number
  queue_maxsize: number
  context_window: number
  translation_mode: 'live' | 'clip'
  streamer_profile: string
  use_profile: boolean
  current_activity: string
  slang: Record<string, string>
}

export interface SceneConfig {
  publish_open_set_activity: boolean
}

export interface SubtitleConfig {
  idle_hide_ms: number
  font_family: string
  font_size: number
  font_style: string
  bg: string
  ctrl_bg: string
  fg: string
  outline_color: string
  outline_width: number
  alpha: number
  max_width_chars: number
  wraplength: number
  padx: number
  pady: number
  init_offset_x: number
  init_offset_y: number
  poll_interval_ms: number
  min_display_ms: number
  ms_per_char: number
  queue_maxsize: number
}

export interface DatabaseConfig {
  db_path: string
  db_cache_max_rows: number
}

export interface OllamaConfig {
  base_url: string
  model: string
  timeout: number
}

export interface NvidiaConfig {
  model: string
  timeout: number
}

export interface CacheStats {
  total_entries: number
  hit_count_sum: number
  last_used: string
  db_size_mb: number
}

export interface SystemStats {
  unix_timestamp_seconds: number
  platform: string
  arch: string
}
