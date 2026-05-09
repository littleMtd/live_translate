<template>
  <div class="config-panel">
    <h2>Settings</h2>

    <div class="section">
      <h3>Subtitle Display</h3>
      <label>
        Font Size: <strong>{{ local.subtitle.font_size }}px</strong>
        <input v-model.number="local.subtitle.font_size" type="range" min="8" max="48" />
      </label>
      <label>
        Opacity: <strong>{{ Math.round(local.subtitle.alpha * 100) }}%</strong>
        <input v-model.number="local.subtitle.alpha" type="range" min="0.1" max="1" step="0.05" />
      </label>
      <label>
        Auto-hide after:
        <input v-model.number="local.subtitle.idle_hide_ms" type="number" min="1000" max="120000" step="1000" />
        ms
      </label>
      <label>
        Font family:
        <input v-model="local.subtitle.font_family" type="text" />
      </label>
    </div>

    <div class="section">
      <h3>Translation Engine</h3>
      <label>
        Primary engine:
        <select v-model="local.translation.primary_engine">
          <option value="gemini">Gemini 2.5 Flash</option>
          <option value="claude">Claude Haiku</option>
        </select>
      </label>
      <label>
        Max tokens:
        <input v-model.number="local.translation.max_tokens" type="number" min="10" max="500" />
      </label>
      <label>
        Target language:
        <input v-model="local.translation.target_lang" type="text" />
      </label>
    </div>

    <div class="section">
      <h3>STT Engine</h3>
      <label>
        Primary engine:
        <select v-model="local.stt.primary_engine">
          <option value="sensevoice">SenseVoice (local)</option>
          <option value="groq">Groq Whisper (cloud)</option>
        </select>
      </label>
    </div>

    <div class="section">
      <h3>Audio / VAD</h3>
      <label>
        VAD enabled:
        <input v-model="local.audio.vad_enabled" type="checkbox" />
      </label>
      <label>
        Silence threshold (s):
        <input v-model.number="local.audio.vad_silence_sec" type="number" min="0.1" max="5" step="0.1" />
      </label>
      <label>
        Max speech chunk (s):
        <input v-model.number="local.audio.vad_max_speech_sec" type="number" min="1" max="30" step="0.5" />
      </label>
    </div>

    <div class="actions">
      <button @click="save" class="primary">Save</button>
      <button @click="reset">Cancel</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import type { ConfigDto } from '../types/config'

const props = defineProps<{ config: ConfigDto | null }>()
const emit = defineEmits<{ save: [ConfigDto] }>()

const defaultConfig = (): ConfigDto => ({
  audio: { sample_rate: 16000, channels: 1, volume_threshold: 0.01,
           vad_enabled: true, vad_silence_sec: 0.6,
           vad_min_speech_sec: 0.4, vad_max_speech_sec: 8.0, queue_maxsize: 10 },
  stt: { primary_engine: 'groq', language: 'ko', queue_maxsize: 20 },
  splitter: { min_wait_seconds: 3, force_cut_seconds: 8 },
  translation: { primary_engine: 'gemini', target_lang: 'zh-TW',
                 max_tokens: 80, temperature: 0.0, queue_maxsize: 2, slang: {} },
  subtitle: { font_family: 'Microsoft JhengHei', font_size: 22, font_style: 'bold',
              idle_hide_ms: 30000, alpha: 0.82, queue_maxsize: 10 },
  database: { db_path: 'logs/live_translate.db', db_cache_max_rows: 50000 },
})

const clone = (c: ConfigDto | null): ConfigDto =>
  c ? JSON.parse(JSON.stringify(c)) : defaultConfig()

const local = ref<ConfigDto>(clone(props.config))

watch(() => props.config, (c) => { local.value = clone(c) }, { deep: true })

const save = () => emit('save', local.value)
const reset = () => { local.value = clone(props.config) }
</script>

<style scoped>
.config-panel { max-width: 620px; }

h2 { margin-bottom: 20px; font-size: 20px; }

.section {
  margin-bottom: 20px;
  padding: 16px;
  background: white;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}

h3 { margin-bottom: 12px; font-size: 14px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em; }

label {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 10px 0;
  font-size: 14px;
  color: #374151;
}

label strong { min-width: 48px; text-align: right; color: #111827; }

input[type="range"] { flex: 1; }
input[type="number"], input[type="text"], select {
  padding: 5px 8px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  font-size: 13px;
}
input[type="number"] { width: 90px; }
input[type="text"]   { width: 180px; }

.actions { display: flex; gap: 10px; margin-top: 8px; }

button {
  padding: 9px 20px;
  font-size: 14px;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-weight: 500;
}

.primary { background: #667eea; color: white; }
.primary:hover { background: #5568d3; }
button:not(.primary) { background: #e5e7eb; color: #374151; }
button:not(.primary):hover { background: #d1d5db; }
</style>
