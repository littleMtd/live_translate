export interface ConfigDto {
  audio: AudioConfig
  stt: SttConfig
  splitter: SplitterConfig
  translation: TranslationConfig
  subtitle: SubtitleConfig
  database: DatabaseConfig
}

export interface AudioConfig {
  sample_rate: number
  channels: number
  volume_threshold: number
  vad_enabled: boolean
  vad_silence_sec: number
  vad_min_speech_sec: number
  vad_max_speech_sec: number
  queue_maxsize: number
}

export interface SttConfig {
  primary_engine: 'sensevoice' | 'groq'
  language: string
  queue_maxsize: number
}

export interface SplitterConfig {
  min_wait_seconds: number
  force_cut_seconds: number
}

export interface TranslationConfig {
  primary_engine: 'gemini' | 'claude'
  target_lang: string
  max_tokens: number
  temperature: number
  queue_maxsize: number
  slang: Record<string, string>
}

export interface SubtitleConfig {
  font_family: string
  font_size: number
  font_style: string
  idle_hide_ms: number
  alpha: number
  queue_maxsize: number
}

export interface DatabaseConfig {
  db_path: string
  db_cache_max_rows: number
}

export interface CacheStats {
  total_entries: number
  hit_count_sum: number
  last_used: string
  db_size_mb: number
}

export interface SystemStats {
  uptime_seconds: number
  platform: string
  arch: string
}
